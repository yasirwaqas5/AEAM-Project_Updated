"""
aeam/agents/kpi/advanced_detectors.py

Additional deterministic statistical detectors for the AEAM KPI pipeline
(Phase F1 — Detection, Statistical & Forecast Intelligence Uplift).

Two detectors live here, both built to the exact shape
:class:`~aeam.agents.kpi.statistical_detector.StatisticalDetector` already
has: a constructor taking only tuning parameters, one ``detect(current,
history)`` method returning a plain dict, and **no I/O, no state mutation,
no external libraries**. MonitorAgent composes them the same way it
composes the z-score detector today.

Why these two, specifically — the z-score/percentile detector has two
blind spots the audit's "unimproved since Phase 5" finding points at:

* **A sustained level shift is invisible once the window absorbs it.**
  A metric that steps from 100 to 60 and stays there produces a large
  z-score for one or two observations, then the rolling window catches up
  and the breach silently stops being reported — the incident is
  *ongoing* but detection has gone quiet. :class:`ChangepointDetector`
  finds the shift itself rather than the outlier, so a persistent
  regression stays detected.
* **Seasonality is read as anomaly.** A metric with a weekly cycle is
  legitimately low every Sunday; a flat rolling mean scores that as a
  breach every week. :class:`SeasonalHybridDetector` compares each point
  against the same phase of previous cycles and scores the *residual*
  robustly (median/MAD, not mean/stdev), so a normal Sunday is normal and
  an abnormal Sunday still fires.

Both detectors are **advisory additions, never replacements** (F-series
invariant 1, AGENT-5). They are flag-gated in MonitorAgent, default off;
with the flags off, nothing about the pipeline's output changes by a
single byte.

Robustness note: both use median and MAD rather than mean and standard
deviation. The existing detector already winsorizes for the same reason —
one extreme point must not define "normal" — and MAD carries that
principle through the whole computation instead of only the tails.
"""

from __future__ import annotations

import statistics
from typing import NamedTuple

# Scale factor making MAD a consistent estimator of the standard deviation
# for normally distributed data (1 / Φ⁻¹(0.75)). Without it, a MAD-based
# "sigma" is ~0.67x the equivalent stdev and every threshold expressed in
# sigmas would silently mean something different from the z-score
# detector's thresholds.
_MAD_TO_SIGMA: float = 1.4826

# Engine-owned defaults (ENG-6). Settings-level overrides in MonitorAgent's
# construction may replace them; the values live here, once.
_DEFAULT_CHANGEPOINT_MIN_SEGMENT: int = 4
_DEFAULT_CHANGEPOINT_THRESHOLD: float = 3.0
_DEFAULT_SEASONAL_PERIOD: int = 7
_DEFAULT_SEASONAL_MIN_CYCLES: int = 3
_DEFAULT_SEASONAL_THRESHOLD: float = 3.0


def _median(values: list[float]) -> float:
    """Median of a non-empty list."""
    return float(statistics.median(values))


def _mad_sigma(values: list[float], center: float) -> float:
    """Return a robust sigma estimate from the median absolute deviation.

    Returns ``0.0`` when the data has no spread at all (every value
    identical, or fewer than two points). Callers must treat a zero sigma
    as "no dispersion to score against" and decline to emit a score —
    dividing by it would manufacture infinite confidence out of a
    degenerate series.
    """
    if len(values) < 2:
        return 0.0
    mad = _median([abs(v - center) for v in values])
    return mad * _MAD_TO_SIGMA


class ChangepointResult(NamedTuple):
    """Outcome of a changepoint scan over a metric's recent history."""

    changepoint_detected: bool
    changepoint_index: int | None
    shift_magnitude: float | None
    shift_score: float | None
    before_level: float | None
    after_level: float | None
    insufficient_data: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "changepoint_detected": self.changepoint_detected,
            "changepoint_index": self.changepoint_index,
            "shift_magnitude": self.shift_magnitude,
            "shift_score": self.shift_score,
            "before_level": self.before_level,
            "after_level": self.after_level,
            "insufficient_data": self.insufficient_data,
        }


class ChangepointDetector:
    """
    Deterministic single-changepoint detector over a recent window.

    Scans every admissible split point of ``history + [current]`` and picks
    the split whose robust level shift is largest, scored in robust sigmas
    of the pre-change segment. A shift is reported only when that score
    exceeds ``threshold``.

    Deliberately finds **at most one** changepoint. Multi-changepoint
    segmentation is a materially harder problem whose output an operator
    cannot act on any better, and this detector's job is to answer one
    question honestly: *did this metric step to a new level, and where?*

    Contains no I/O, no randomness, and no state — the same input always
    produces the same output.

    **Seasonality must be removed first, or this detector is unusable on
    real business metrics.** With a weekly cycle and a segment shorter than
    the cycle, any split straddling a weekend compares a weekday-heavy
    segment against a weekend-heavy one and scores a large, entirely
    spurious "shift" — on the benchmark dataset that produced a false
    positive on roughly every third observation. Passing ``period``
    subtracts the per-phase median before scanning, so what is measured is
    a change in the *deseasonalized* level: a real step, not Saturday.

    Leave ``period`` at ``None`` only for metrics with no cycle.

    Args:
        min_segment_size: Minimum observations either side of the split.
                          Smaller segments make "level" meaningless.
        threshold:        Robust-sigma score above which a shift is
                          reported. Matches the z-score detector's default
                          of 3.0 so the two speak the same units.
        period:           Seasonal cycle length. ``None`` (the default)
                          scans the raw series — correct only when the
                          metric genuinely has no cycle.

    Raises:
        ValueError: If ``min_segment_size`` < 2, ``threshold`` <= 0, or
                    ``period`` is not None and < 2.

    Example::

        detector = ChangepointDetector()
        result = detector.detect(60.0, [100, 99, 101, 100, 61, 59, 60])
        # result["changepoint_detected"] is True
    """

    def __init__(
        self,
        min_segment_size: int = _DEFAULT_CHANGEPOINT_MIN_SEGMENT,
        threshold: float = _DEFAULT_CHANGEPOINT_THRESHOLD,
        period: int | None = None,
    ) -> None:
        if min_segment_size < 2:
            raise ValueError("min_segment_size must be >= 2.")
        if threshold <= 0:
            raise ValueError("threshold must be > 0.")
        if period is not None and period < 2:
            raise ValueError("period must be >= 2 when supplied.")
        self._min_segment = min_segment_size
        self._threshold = threshold
        self._period = period

    def _deseasonalize(self, series: list[float]) -> list[float]:
        """Subtract each observation's per-phase median.

        Returns the series unchanged when no period is configured, or when
        there is not at least one observation per phase — with fewer, a
        "phase median" is a single point and subtracting it would erase the
        very signal being looked for.
        """
        if self._period is None or len(series) < self._period * 2:
            return series

        offsets: dict[int, float] = {}
        for phase in range(self._period):
            phase_values = series[phase::self._period]
            if phase_values:
                offsets[phase] = _median(phase_values)

        overall = _median(series)
        return [
            value - (offsets.get(i % self._period, overall) - overall)
            for i, value in enumerate(series)
        ]

    def detect(self, current: float, history: list[float]) -> dict[str, object]:
        """
        Scan for a level shift in ``history`` extended by ``current``.

        Args:
            current: The latest observed value.
            history: Time-ordered prior observations (oldest first).

        Returns:
            A dict with the keys of :class:`ChangepointResult`. When there
            is not enough data, ``changepoint_detected`` is ``False`` and
            ``insufficient_data`` carries the reason — never a silent
            "no anomaly", which would be indistinguishable from a real
            all-clear (EXPL-3).
        """
        raw = [float(v) for v in history] + [float(current)]
        needed = self._min_segment * 2

        if len(raw) < needed:
            series = raw
        else:
            series = self._deseasonalize(raw)

        if len(series) < needed:
            return ChangepointResult(
                changepoint_detected=False,
                changepoint_index=None,
                shift_magnitude=None,
                shift_score=None,
                before_level=None,
                after_level=None,
                insufficient_data=(
                    f"{len(series)} observations available; "
                    f"{needed} required for a {self._min_segment}-point segment either side."
                ),
            ).to_dict()

        best_score: float = -1.0
        best_index: int | None = None
        best_before: float = 0.0
        best_after: float = 0.0
        # Tracked separately from best_index so "every split scored exactly
        # zero" (a genuine, confident all-clear) is never reported with the
        # same wording as "no split could be scored at all" (an honest
        # inability). Collapsing the two would make a flat, healthy metric
        # look like a measurement failure — EXPL-3's distinction between
        # "consulted with no signal" and "insufficient data".
        scorable_splits = 0

        for split in range(self._min_segment, len(series) - self._min_segment + 1):
            before = series[:split]
            after = series[split:]

            before_level = _median(before)
            after_level = _median(after)
            sigma = _mad_sigma(before, before_level)
            if sigma <= 0.0:
                # A perfectly flat pre-change segment. Any change is
                # infinitely many sigmas, which is not a usable score, so
                # fall back to the after-segment's own dispersion; if that
                # is also flat the split is skipped rather than scored
                # with a fabricated number.
                sigma = _mad_sigma(after, after_level)
                if sigma <= 0.0:
                    continue

            scorable_splits += 1
            score = abs(after_level - before_level) / sigma
            if score > best_score:
                best_score = score
                best_index = split
                best_before = before_level
                best_after = after_level

        if scorable_splits == 0:
            return ChangepointResult(
                changepoint_detected=False,
                changepoint_index=None,
                shift_magnitude=None,
                shift_score=None,
                before_level=None,
                after_level=None,
                insufficient_data=(
                    "Series is perfectly flat on both sides of every candidate "
                    "split; a level shift has no dispersion to be scored against."
                ),
            ).to_dict()

        return ChangepointResult(
            changepoint_detected=best_score > self._threshold,
            changepoint_index=best_index,
            shift_magnitude=round(best_after - best_before, 6),
            shift_score=round(best_score, 6),
            before_level=round(best_before, 6),
            after_level=round(best_after, 6),
            insufficient_data=None,
        ).to_dict()

    def __repr__(self) -> str:
        return (
            f"ChangepointDetector(min_segment_size={self._min_segment}, "
            f"threshold={self._threshold})"
        )


class SeasonalHybridResult(NamedTuple):
    """Outcome of a seasonal-hybrid residual scan."""

    seasonal_anomaly: bool
    seasonal_expected: float | None
    residual: float | None
    residual_score: float | None
    period: int
    cycles_used: int
    insufficient_data: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "seasonal_anomaly": self.seasonal_anomaly,
            "seasonal_expected": self.seasonal_expected,
            "residual": self.residual,
            "residual_score": self.residual_score,
            "period": self.period,
            "cycles_used": self.cycles_used,
            "insufficient_data": self.insufficient_data,
        }


class SeasonalHybridDetector:
    """
    Robust seasonal-hybrid decomposition detector.

    Decomposes the series into a robust trend level, a per-phase seasonal
    offset, and a residual, then scores the current observation's residual
    in robust sigmas of the historical residuals.

    "Hybrid" because both halves are robust: the seasonal offsets are
    medians per phase (so one bad Monday does not redefine Mondays), and
    the residual score uses MAD rather than standard deviation (so the
    anomaly being scored does not inflate the very yardstick measuring it —
    the failure mode that makes a plain z-score conservative exactly when
    an anomaly is large).

    Contains no I/O, no randomness, and no state.

    Args:
        period:     Length of the seasonal cycle in observations
                    (7 = weekly on daily data, the AEAM default).
        min_cycles: Minimum complete cycles required before any score is
                    emitted. Below this the detector reports insufficient
                    data rather than guessing a seasonal shape.
        threshold:  Robust-sigma score above which a residual is an
                    anomaly. Matches the z-score detector's 3.0.

    Raises:
        ValueError: If ``period`` < 2, ``min_cycles`` < 2, or
                    ``threshold`` <= 0.

    Example::

        detector = SeasonalHybridDetector(period=7)
        weekly = [100, 100, 100, 100, 100, 40, 40] * 4
        result = detector.detect(40.0, weekly)
        # A normal Saturday: result["seasonal_anomaly"] is False, where a
        # flat moving average would have called it a breach.
    """

    def __init__(
        self,
        period: int = _DEFAULT_SEASONAL_PERIOD,
        min_cycles: int = _DEFAULT_SEASONAL_MIN_CYCLES,
        threshold: float = _DEFAULT_SEASONAL_THRESHOLD,
    ) -> None:
        if period < 2:
            raise ValueError("period must be >= 2.")
        if min_cycles < 2:
            raise ValueError("min_cycles must be >= 2.")
        if threshold <= 0:
            raise ValueError("threshold must be > 0.")
        self._period = period
        self._min_cycles = min_cycles
        self._threshold = threshold

    def detect(self, current: float, history: list[float]) -> dict[str, object]:
        """
        Score ``current`` against the same seasonal phase in ``history``.

        The phase of ``current`` is the one immediately following the last
        history observation, i.e. ``len(history) % period``.

        Args:
            current: The latest observed value.
            history: Time-ordered prior observations (oldest first). Must
                     be phase-aligned with ``current`` — the caller is
                     responsible for passing a contiguous series, which is
                     what MonitorAgent's cleaned history already is.

        Returns:
            A dict with the keys of :class:`SeasonalHybridResult`.
            ``insufficient_data`` carries the reason whenever no score
            could be produced (EXPL-3).
        """
        values = [float(v) for v in history]
        needed = self._period * self._min_cycles

        if len(values) < needed:
            return SeasonalHybridResult(
                seasonal_anomaly=False,
                seasonal_expected=None,
                residual=None,
                residual_score=None,
                period=self._period,
                cycles_used=len(values) // self._period,
                insufficient_data=(
                    f"{len(values)} observations available; {needed} required "
                    f"({self._min_cycles} complete cycles of {self._period})."
                ),
            ).to_dict()

        # Use only whole cycles so every phase is represented equally — a
        # partial trailing cycle would bias the phases it happens to cover.
        # Trimming happens at the START (keeping the most recent data), so
        # the window's element 0 is NOT necessarily phase 0: its phase is
        # `offset % period`. Every phase lookup below therefore goes through
        # the ORIGINAL series index, never the window index. Getting this
        # wrong silently compares Saturday against Wednesday's baseline and
        # reports a confident anomaly on a perfectly normal day.
        usable = len(values) - (len(values) % self._period)
        offset = len(values) - usable
        window = values[offset:]
        cycles_used = usable // self._period

        def phase_of(window_index: int) -> int:
            return (offset + window_index) % self._period

        level = _median(window)
        seasonal: dict[int, float] = {}
        for phase in range(self._period):
            phase_values = [
                window[i] for i in range(len(window)) if phase_of(i) == phase
            ]
            seasonal[phase] = _median(phase_values) - level if phase_values else 0.0

        residuals = [
            window[i] - level - seasonal[phase_of(i)] for i in range(len(window))
        ]
        residual_center = _median(residuals)
        sigma = _mad_sigma(residuals, residual_center)

        current_phase = len(values) % self._period
        expected = level + seasonal.get(current_phase, 0.0)
        residual = float(current) - expected

        if sigma <= 0.0:
            # A perfectly seasonal series with zero residual spread. Any
            # nonzero residual is a real departure from a pattern the data
            # has held exactly; reporting it as an anomaly is honest, but
            # there is no meaningful sigma score to attach to it.
            return SeasonalHybridResult(
                seasonal_anomaly=abs(residual) > 0.0,
                seasonal_expected=round(expected, 6),
                residual=round(residual, 6),
                residual_score=None,
                period=self._period,
                cycles_used=cycles_used,
                insufficient_data=(
                    None
                    if abs(residual) > 0.0
                    else "Residual dispersion is zero; no score is meaningful."
                ),
            ).to_dict()

        score = abs(residual - residual_center) / sigma

        return SeasonalHybridResult(
            seasonal_anomaly=score > self._threshold,
            seasonal_expected=round(expected, 6),
            residual=round(residual, 6),
            residual_score=round(score, 6),
            period=self._period,
            cycles_used=cycles_used,
            insufficient_data=None,
        ).to_dict()

    def __repr__(self) -> str:
        return (
            f"SeasonalHybridDetector(period={self._period}, "
            f"min_cycles={self._min_cycles}, threshold={self._threshold})"
        )
