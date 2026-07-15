"""
aeam/intelligence/cross_dataset_analyzer.py

Cross-Dataset Intelligence (Phase C4).

Correlates related business signals across multiple registered datasets
during an investigation — additional, clearly-separate evidence, never a
replacement for RAG, Enterprise Memory, or Enterprise Policies, and never a
second MonitorAgent/RuleEngine/ForecastAgent.

Reuses, unmodified:
- :class:`~aeam.intelligence.dataset_activation.DatasetActivation` — the
  SAME activated-dataset set MonitorAgent/CompositeKPISource already watch
  (``list_activated_dataset_ids()``).
- :class:`~aeam.intelligence.dataset_intelligence.DatasetIntelligenceService`
  — the SAME profiler that already tells RuleEngine/ForecastAgent which
  columns are measures/dimensions/the time axis. Never re-derives this.
- :class:`~aeam.intelligence.dataset_kpi_source.DatasetKPISource` — the SAME
  ``KPIRowSource`` adapter MonitorAgent itself uses to read a dataset's
  rows. This module never reads a blob or parses a file directly.
- :class:`~aeam.agents.kpi.statistical_detector.StatisticalDetector` — the
  SAME deterministic z-score/percentile detector MonitorAgent's own
  ``process_kpi`` already uses for the ORIGIN dataset's metric. Applying it
  to a CANDIDATE dataset's measure is not a second detector implementation
  — it is the same class, the same math, called again.

This module makes no I/O of its own beyond calling the above (no blob
reads, no new database tables, no new API calls), performs no rule
evaluation, no forecasting, and never creates or dispatches an Event —
Orchestrator calls into it exactly once per investigation, exactly like it
already calls into RAGAgent/EnterpriseMemoryEngine/PolicyRegistry.

Honesty contract:
- With fewer than two activated datasets, or with fewer than the minimum
  number of data points needed for a given comparison, the result says so
  explicitly (``insufficient_data`` / per-entry ``missing_signals`` with a
  stated reason) — never a fabricated correlation or relationship.
- "Strong correlation" is only ever claimed after ALIGNING both series by
  calendar date (never a bare index-position comparison across
  differently-sized/differently-sampled datasets) and finding at least
  ``MIN_CORRELATION_POINTS`` genuinely overlapping dates.
- A dataset is only called "related" via an inspectable structural fact —
  it shares the incident's own metric name, or it shares a dimension
  column name with the origin dataset — never an invented semantic
  similarity.
"""

from __future__ import annotations

import logging
import math
from typing import Any

from aeam.agents.kpi.statistical_detector import StatisticalDetector
from aeam.intelligence.dataset_activation import DatasetActivation
from aeam.intelligence.dataset_intelligence import DatasetIntelligenceError, DatasetIntelligenceService
from aeam.intelligence.dataset_kpi_source import DatasetKPISource

logger = logging.getLogger(__name__)

#: Minimum data points a single dataset/measure needs before ANY anomaly
#: judgement is attempted for it (below this: reported as a missing signal).
MIN_SERIES_POINTS: int = 3

#: Minimum genuinely-overlapping (same calendar date on both sides) points
#: required before a Pearson correlation is computed at all.
MIN_CORRELATION_POINTS: int = 3

#: Minimum |correlation| to report a pair as a "strong correlation".
DEFAULT_CORRELATION_THRESHOLD: float = 0.7


class CrossDatasetAnalyzer:
    """
    Correlates simultaneous anomalies across the currently-activated
    datasets for a given incident metric.

    Args:
        dataset_activation: Existing :class:`DatasetActivation` — the SAME
                            activated-dataset set already driving
                            MonitorAgent/CompositeKPISource.
        intelligence:       Existing :class:`DatasetIntelligenceService`.
        kpi_source:         Existing :class:`DatasetKPISource`.
        statistical_detector: Optional :class:`StatisticalDetector`
                            override (e.g. for tests). Defaults to a fresh
                            instance with the same ``window_size=7``
                            MonitorAgent itself uses.
        correlation_threshold: Minimum |Pearson r| to report a "strong
                            correlation". Defaults to 0.7.

    Raises:
        ValueError: If any required dependency is ``None``.
    """

    def __init__(
        self,
        dataset_activation: DatasetActivation,
        intelligence: DatasetIntelligenceService,
        kpi_source: DatasetKPISource,
        statistical_detector: StatisticalDetector | None = None,
        correlation_threshold: float = DEFAULT_CORRELATION_THRESHOLD,
    ) -> None:
        if dataset_activation is None:
            raise ValueError("dataset_activation must not be None.")
        if intelligence is None:
            raise ValueError("intelligence must not be None.")
        if kpi_source is None:
            raise ValueError("kpi_source must not be None.")
        self._activation = dataset_activation
        self._intelligence = intelligence
        self._kpi_source = kpi_source
        self._detector = statistical_detector or StatisticalDetector(window_size=7)
        self._correlation_threshold = correlation_threshold

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def analyze(self, metric: str) -> dict[str, Any]:
        """
        Correlate ``metric`` (the current incident's ``event.metric``)
        against every OTHER currently-activated dataset.

        Args:
            metric: The metric name under investigation.

        Returns:
            A dict, always with the same shape (never raises)::

                {
                    "insufficient_data": bool,
                    "reason": str | None,           # set iff insufficient_data
                    "origin_dataset_id": str | None,
                    "origin_dataset_name": str | None,
                    "candidates_checked": int,
                    "supporting": [...],
                    "contradicting": [...],
                    "strong_correlations": [...],
                    "missing_signals": [...],
                }

            Every list entry carries ``dataset_id``/``dataset_name`` for
            traceability. Never fabricated: an empty list means genuinely
            none were found, not an error.
        """
        try:
            return self._analyze_unsafe(metric)
        except Exception as exc:  # noqa: BLE001
            logger.error("CrossDatasetAnalyzer.analyze | metric=%s | unexpected failure: %s", metric, exc, exc_info=True)
            return self._empty_result(
                insufficient_data=True,
                reason=f"Cross-dataset analysis failed unexpectedly: {exc}",
            )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _analyze_unsafe(self, metric: str) -> dict[str, Any]:
        activated_ids = self._activation.list_activated_dataset_ids()
        if len(activated_ids) < 2:
            return self._empty_result(
                insufficient_data=True,
                reason=(
                    f"Only {len(activated_ids)} dataset(s) currently activated; "
                    "cross-dataset correlation requires at least 2."
                ),
            )

        profiles: dict[str, Any] = {}
        for dataset_id in activated_ids:
            try:
                profiles[dataset_id] = self._intelligence.build_profile(dataset_id)
            except DatasetIntelligenceError as exc:
                logger.debug("CrossDatasetAnalyzer | dataset_id=%s not profilable: %s", dataset_id, exc.detail)
                continue

        metric_norm = (metric or "").strip().lower()
        origin_id = None
        for dataset_id, profile in profiles.items():
            if metric_norm and metric_norm in {m.lower() for m in profile.measures}:
                origin_id = dataset_id
                break

        origin_profile = profiles.get(origin_id) if origin_id else None
        origin_series_by_date: dict[str, float] = {}
        if origin_id and origin_profile and origin_profile.timestamp_column:
            origin_rows = self._kpi_source.fetch_rows(origin_id)
            origin_series_by_date = self._series_by_date(origin_rows, metric, origin_profile.timestamp_column)

        supporting: list[dict[str, Any]] = []
        contradicting: list[dict[str, Any]] = []
        strong_correlations: list[dict[str, Any]] = []
        missing_signals: list[dict[str, Any]] = []

        origin_dims = {d.lower() for d in (origin_profile.dimensions if origin_profile else [])}

        for dataset_id, profile in profiles.items():
            if dataset_id == origin_id:
                continue

            shares_metric_name = metric_norm and metric_norm in {m.lower() for m in profile.measures}
            shared_dims = sorted(origin_dims & {d.lower() for d in profile.dimensions}) if origin_dims else []
            if shares_metric_name:
                relation = "shared_metric_name"
            elif shared_dims:
                relation = f"shared_dimension:{shared_dims[0]}"
            else:
                relation = "activated_dataset"

            if not profile.measures:
                missing_signals.append({
                    "dataset_id": dataset_id, "dataset_name": profile.dataset_name, "relation": relation,
                    "reason": "Dataset has no monitorable measures.",
                })
                continue

            rows = self._kpi_source.fetch_rows(dataset_id)
            best_entry: dict[str, Any] | None = None
            best_abs_z = -1.0
            any_series_too_short = False

            for candidate_measure in profile.measures:
                series = self._series_values(rows, candidate_measure)
                if len(series) < MIN_SERIES_POINTS:
                    any_series_too_short = True
                    continue
                detection = self._detector.detect(current=series[-1], history=series[:-1])
                entry = {
                    "dataset_id": dataset_id, "dataset_name": profile.dataset_name, "relation": relation,
                    "metric": candidate_measure,
                    "current_value": series[-1],
                    "moving_avg": detection["moving_avg"],
                    "z_score": detection["z_score"],
                    "statistical_anomaly": detection["statistical_anomaly"],
                }
                if abs(detection["z_score"]) > best_abs_z:
                    best_abs_z = abs(detection["z_score"])
                    best_entry = entry

            if best_entry is None:
                missing_signals.append({
                    "dataset_id": dataset_id, "dataset_name": profile.dataset_name, "relation": relation,
                    "reason": f"Fewer than {MIN_SERIES_POINTS} data points available for any of its measures.",
                })
                continue

            if best_entry["statistical_anomaly"]:
                supporting.append(best_entry)
            elif relation != "activated_dataset":
                # A structurally-related dataset that stayed normal while the
                # origin metric is anomalous -- worth flagging as evidence
                # AGAINST a systemic cause, but only when a real structural
                # relation exists (never claimed for an unrelated dataset).
                contradicting.append(best_entry)
            # else: no structural relation AND no anomaly -- not evidence
            # either way, so genuinely omitted rather than padded in.

            # Correlation: only ever computed over genuinely overlapping
            # calendar dates on both sides, and only when the origin series
            # itself was available.
            if origin_series_by_date and profile.timestamp_column:
                candidate_series_by_date = self._series_by_date(rows, best_entry["metric"], profile.timestamp_column)
                overlap_dates = sorted(set(origin_series_by_date) & set(candidate_series_by_date))
                if len(overlap_dates) >= MIN_CORRELATION_POINTS:
                    xs = [origin_series_by_date[d] for d in overlap_dates]
                    ys = [candidate_series_by_date[d] for d in overlap_dates]
                    corr = _pearson(xs, ys)
                    if corr is not None and abs(corr) >= self._correlation_threshold:
                        strong_correlations.append({
                            "dataset_id": dataset_id, "dataset_name": profile.dataset_name,
                            "metric": best_entry["metric"], "correlation": round(corr, 4),
                            "overlapping_dates": len(overlap_dates),
                        })

        return {
            "insufficient_data": False,
            "reason": None,
            "origin_dataset_id": origin_id,
            "origin_dataset_name": origin_profile.dataset_name if origin_profile else None,
            "candidates_checked": len(profiles) - (1 if origin_id else 0),
            "supporting": supporting,
            "contradicting": contradicting,
            "strong_correlations": strong_correlations,
            "missing_signals": missing_signals,
        }

    @staticmethod
    def _series_values(rows: list[dict[str, Any]], measure: str) -> list[float]:
        """Numeric values for ``measure``, in the row order already supplied
        (DatasetKPISource sorts chronologically; missing/non-numeric values
        are skipped, never coerced/guessed)."""
        values: list[float] = []
        for row in rows:
            v = row.get(measure)
            if v is None:
                continue
            try:
                values.append(float(v))
            except (TypeError, ValueError):
                continue
        return values

    @staticmethod
    def _series_by_date(rows: list[dict[str, Any]], measure: str, timestamp_column: str) -> dict[str, float]:
        """
        Map calendar date (``YYYY-MM-DD``, derived from ``timestamp_column``)
        to ``measure``'s value, for dates where both are present. Alignment
        is at DATE granularity (not exact timestamp) since two independently
        -collected datasets rarely share identical collection instants —
        this coarser granularity is the honest, disclosed comparison unit.
        Last value wins if a date repeats within one dataset.
        """
        by_date: dict[str, float] = {}
        for row in rows:
            ts = row.get(timestamp_column)
            v = row.get(measure)
            if ts is None or v is None:
                continue
            date_key = _to_date_key(ts)
            if date_key is None:
                continue
            try:
                by_date[date_key] = float(v)
            except (TypeError, ValueError):
                continue
        return by_date

    @staticmethod
    def _empty_result(insufficient_data: bool, reason: str | None) -> dict[str, Any]:
        return {
            "insufficient_data": insufficient_data,
            "reason": reason,
            "origin_dataset_id": None,
            "origin_dataset_name": None,
            "candidates_checked": 0,
            "supporting": [],
            "contradicting": [],
            "strong_correlations": [],
            "missing_signals": [],
        }

    def __repr__(self) -> str:
        return f"CrossDatasetAnalyzer(correlation_threshold={self._correlation_threshold})"


def _to_date_key(value: Any) -> str | None:
    """Best-effort calendar-date string from whatever DatasetKPISource's
    pandas-parsed timestamp value is (Timestamp, datetime, or date-like
    string) — never fabricates a date for an unparseable value."""
    if hasattr(value, "date"):
        try:
            return value.date().isoformat()
        except Exception:  # noqa: BLE001
            pass
    s = str(value).strip()
    if not s:
        return None
    return s[:10] if len(s) >= 10 else None


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    """Plain-Python Pearson correlation coefficient — no new dependency,
    no new retrieval/statistics pipeline. Returns None (never a fabricated
    0.0) when either series has zero variance."""
    n = len(xs)
    if n == 0 or n != len(ys):
        return None
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    if var_x == 0.0 or var_y == 0.0:
        return None
    return cov / math.sqrt(var_x * var_y)
