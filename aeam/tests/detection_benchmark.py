"""
aeam/tests/detection_benchmark.py

Labeled synthetic detection benchmark (Phase F1).

Test/eval infrastructure, not runtime code: nothing in ``aeam/`` outside the
test suite imports this module. It exists so "detection got better" is a
measured claim with a reproducible number behind it rather than an
assertion — the F1 acceptance criterion requires precision/recall to beat
the recorded Phase-5 baseline by a stated margin.

What it measures
----------------
The **real** shipped detection path: a real ``MonitorAgent`` composing a
real ``RuleEngine``, a real ``StatisticalDetector``, and (when enabled) the
real F1 detectors, driven observation-by-observation over a generated
series. Only the parts that are not detection are faked — the event bus,
the deduplicator, and the forecast agent — because a benchmark that
measured a reimplementation of detection would prove nothing about the
code that ships.

Deduplication is deliberately disabled for the benchmark. It suppresses
repeat events within a time window, which is correct in production and
would silently destroy the recall measurement for exactly the anomaly class
F1 targets: a sustained level shift *is* a repeated signal.

The dataset
-----------
Deterministic (fixed seed), so the same commit always produces the same
score and a regression is unambiguous. Each series is a weekly-seasonal
metric with mild trend and gaussian noise, into which three anomaly classes
are injected at known indices:

* **spike** — one observation far from its neighbours. The z-score detector
  already catches these; they are present so the benchmark cannot be gamed
  by a change that trades away existing capability.
* **level shift** — a sustained step to a new level. This is the Phase-5
  blind spot: the rolling window absorbs the new level within a couple of
  observations and detection goes quiet while the incident is still
  happening.
* **seasonal anomaly** — a value that is unremarkable in absolute terms but
  wrong for its phase (a weekday-sized number on a weekend). A flat rolling
  mean cannot see these at all.

Labels mark exactly the injected observations. Everything else is normal,
including the naturally-low weekend values that a flat baseline
mis-classifies — those false positives are the honest cost of the Phase-5
configuration and they stay in the baseline score.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

from aeam.agents.kpi.rule_engine import RuleEngine
from aeam.agents.kpi.statistical_detector import StatisticalDetector
from aeam.agents.monitor.monitor_agent import MonitorAgent
from aeam.core.event_bus import EventBus
from aeam.core.priority_queue import EventPriorityQueue
from aeam.pipelines.structured_data_pipeline import StructuredDataPipeline

# Fixed seed: the benchmark must be reproducible, or a score change cannot
# be attributed to a code change.
BENCHMARK_SEED: int = 20260728

# Observations before the first injected anomaly, so every detector has the
# history its contract requires before it is scored. Scoring a detector on
# observations where it has honestly reported "insufficient data" would
# measure the warm-up, not the detector.
WARMUP: int = 35

_PERIOD: int = 7
_WEEKDAY_LEVEL: float = 1000.0
_WEEKEND_LEVEL: float = 400.0
_NOISE: float = 25.0
_TREND_PER_OBSERVATION: float = 0.4


@dataclass
class LabeledSeries:
    """One metric's synthetic history with ground-truth anomaly labels."""

    name: str
    values: list[float]
    labels: list[bool]
    anomaly_kinds: dict[int, str]

    def __len__(self) -> int:
        return len(self.values)


def _seasonal_base(index: int, rng: random.Random) -> float:
    """A weekly-seasonal observation with mild trend and gaussian noise."""
    phase = index % _PERIOD
    level = _WEEKEND_LEVEL if phase in (5, 6) else _WEEKDAY_LEVEL
    return level + _TREND_PER_OBSERVATION * index + rng.gauss(0.0, _NOISE)


def generate_labeled_series(
    name: str,
    length: int = 120,
    seed: int = BENCHMARK_SEED,
) -> LabeledSeries:
    """Generate one labeled series containing all three anomaly classes.

    Args:
        name:   Metric name, also used to vary the seed per series so a
                benchmark of several metrics is not the same data repeated.
        length: Total observations. Must leave room after ``WARMUP`` for
                the injected anomalies.
        seed:   Base seed.

    Returns:
        A :class:`LabeledSeries`.

    Raises:
        ValueError: If ``length`` is too short to hold the injections.
    """
    if length < WARMUP + 40:
        raise ValueError(f"length must be >= {WARMUP + 40} to hold every anomaly class.")

    rng = random.Random(seed + sum(ord(c) for c in name))
    values = [_seasonal_base(i, rng) for i in range(length)]
    labels = [False] * length
    kinds: dict[int, str] = {}

    # --- Spikes: isolated extreme observations on weekdays.
    for offset in (0, 18):
        index = WARMUP + offset
        while index % _PERIOD in (5, 6):
            index += 1
        values[index] = values[index] * 2.6
        labels[index] = True
        kinds[index] = "spike"

    # --- Level shift: a sustained step down that the rolling window absorbs.
    shift_start = WARMUP + 30
    shift_length = 12
    for index in range(shift_start, min(shift_start + shift_length, length)):
        values[index] = values[index] * 0.45
        labels[index] = True
        kinds[index] = "level_shift"

    # --- Seasonal anomaly: a weekend observation at weekday scale. Absolute
    # value is entirely ordinary; only its phase makes it wrong.
    for offset in (8, 24):
        index = WARMUP + offset
        while index % _PERIOD not in (5, 6):
            index += 1
        if labels[index]:
            continue
        values[index] = _WEEKDAY_LEVEL + _TREND_PER_OBSERVATION * index
        labels[index] = True
        kinds[index] = "seasonal"

    return LabeledSeries(name=name, values=values, labels=labels, anomaly_kinds=kinds)


class _NeverDuplicate:
    """Dedup disabled — see the module docstring."""

    def is_duplicate(self, event: Any) -> bool:
        return False


class _NoForecast:
    """Forecast disabled: it is measured separately by the backtest suite,
    and training Prophet per observation would make this benchmark
    unrunnable in CI."""

    def analyze(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"insufficient_data": True, "is_deviation": False}


class _BenchmarkSettings:
    """A minimal, EXPLICIT settings object.

    Every flag the detection path reads is spelled out with a real boolean,
    rather than mocked — the F1 flags are read strictly (``is True``), and a
    benchmark whose configuration was ambiguous would silently measure the
    wrong thing.
    """

    def __init__(
        self,
        changepoint: bool = False,
        seasonal: bool = False,
    ) -> None:
        self.DETECTION_CHANGEPOINT_ENABLED = changepoint
        self.DETECTION_CHANGEPOINT_THRESHOLD = 3.0
        self.DETECTION_CHANGEPOINT_MIN_SEGMENT = 4
        self.DETECTION_SEASONAL_HYBRID_ENABLED = seasonal
        self.DETECTION_SEASONAL_PERIOD = _PERIOD
        self.DETECTION_SEASONAL_MIN_CYCLES = 3
        self.DETECTION_SEASONAL_THRESHOLD = 3.0
        self.SHEET_RANGE = ""


def build_agent(changepoint: bool = False, seasonal: bool = False) -> MonitorAgent:
    """Assemble a real MonitorAgent in the requested detection configuration."""
    return MonitorAgent(
        event_bus=EventBus(),
        queue=EventPriorityQueue(),
        deduplicator=_NeverDuplicate(),
        rule_engine=RuleEngine(),
        statistical_detector=StatisticalDetector(window_size=7),
        forecast_agent=_NoForecast(),
        pipeline=StructuredDataPipeline(),
        settings=_BenchmarkSettings(changepoint=changepoint, seasonal=seasonal),
    )


@dataclass
class BenchmarkScore:
    """Precision / recall / F1 over a labeled run, with the raw counts."""

    true_positives: int
    false_positives: int
    false_negatives: int
    observations_scored: int

    @property
    def precision(self) -> float:
        denominator = self.true_positives + self.false_positives
        return round(self.true_positives / denominator, 4) if denominator else 0.0

    @property
    def recall(self) -> float:
        denominator = self.true_positives + self.false_negatives
        return round(self.true_positives / denominator, 4) if denominator else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return round(2 * p * r / (p + r), 4) if (p + r) else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "precision": self.precision,
            "recall": self.recall,
            "f1": self.f1,
            "true_positives": self.true_positives,
            "false_positives": self.false_positives,
            "false_negatives": self.false_negatives,
            "observations_scored": self.observations_scored,
        }


def score_configuration(
    series_list: list[LabeledSeries],
    changepoint: bool = False,
    seasonal: bool = False,
) -> BenchmarkScore:
    """Run one detection configuration over the labeled data and score it.

    Each observation from ``WARMUP`` onward is fed to ``process_kpi`` with
    the true history preceding it. An event being produced counts as a
    positive detection for that observation.
    """
    agent = build_agent(changepoint=changepoint, seasonal=seasonal)
    tp = fp = fn = 0
    scored = 0

    for series in series_list:
        for index in range(WARMUP, len(series)):
            history = series.values[:index]
            current = series.values[index]
            previous = series.values[index - 1]

            event = agent.process_kpi(
                metric_name=series.name,
                current=current,
                previous=previous,
                history=history,
            )

            detected = event is not None
            actual = series.labels[index]
            scored += 1

            if detected and actual:
                tp += 1
            elif detected and not actual:
                fp += 1
            elif not detected and actual:
                fn += 1

    return BenchmarkScore(
        true_positives=tp,
        false_positives=fp,
        false_negatives=fn,
        observations_scored=scored,
    )


def default_dataset() -> list[LabeledSeries]:
    """The benchmark's canonical dataset: three independent metrics."""
    return [
        generate_labeled_series("sales", length=120),
        generate_labeled_series("orders", length=120),
        generate_labeled_series("sessions", length=120),
    ]
