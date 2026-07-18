"""
aeam/intelligence/adaptive_detection.py

Adaptive Detection Engine (Phase C5).

Improves anomaly quality DURING INVESTIGATION by computing a longer-horizon,
seasonality-aware perspective on the incident's metric — additional,
clearly-separate evidence, never a replacement for MonitorAgent's live
detection pipeline and never a second anomaly detector.

Reuses, unmodified:
- :class:`~aeam.agents.kpi.statistical_detector.StatisticalDetector` — the
  SAME deterministic rolling-window class MonitorAgent's own ``process_kpi``
  already uses. This module constructs a SECOND *instance* with a longer
  ``window_size`` (a genuinely "adaptive"/longer-horizon baseline, distinct
  from MonitorAgent's tight live-monitoring window) — the identical math,
  called again with different data, not a re-implementation.
- :class:`~aeam.memory.long_term.LongTermMemory` — the SAME
  ``get_metric_history`` :class:`~aeam.agents.forecast.forecast_agent.ForecastAgent`
  itself already depends on for historical data. No new data-access path,
  no new database table.
- The event's OWN already-computed ``metadata["statistical"]`` /
  ``metadata["forecast"]`` dicts (populated once, at detection time, by
  MonitorAgent's real StatisticalDetector/ForecastAgent runs) — this module
  reads and combines them, it never re-invokes ForecastAgent (no duplicate
  Prophet retraining/prediction) or re-runs MonitorAgent's own detector.

The one genuinely NEW computation this module adds — because nothing
existing does it — is a lightweight, honest day-of-week seasonality check:
grouping historical values by weekday and comparing between-weekday spread
to overall spread. This is plain descriptive statistics (mean/stdev over
grouped values), not a new anomaly-detection algorithm, not a duplicate of
StatisticalDetector's rolling z-score/percentile logic, and not a
replacement for Prophet's internal seasonal modelling (which ForecastAgent
already uses for prediction but does not expose as an investigatable
"seasonality detected" signal).

This module makes no I/O of its own beyond the two reused calls above,
never creates or dispatches an Event, never calls RuleEngine.evaluate(),
DecisionEngine, or ActionAgent, and is invoked by the Orchestrator exactly
once per investigation — the same integration shape as
EnterpriseMemoryEngine/PolicyRegistry/CrossDatasetAnalyzer (Phases C1/C3/C4).

Honesty contract: with fewer historical points than the stated minimum for
a given sub-analysis, that sub-analysis is reported as
``insufficient_history`` with the real point count and the real minimum
required — never a fabricated baseline or invented seasonal pattern.
"""

from __future__ import annotations

import logging
import statistics
from datetime import datetime
from typing import Any

from aeam.agents.kpi.statistical_detector import StatisticalDetector

logger = logging.getLogger(__name__)

#: Minimum historical points required before an adaptive (longer-horizon)
#: baseline comparison is attempted at all.
MIN_BASELINE_POINTS: int = 10

#: Minimum historical points, and minimum distinct weekdays represented,
#: required before a seasonality judgement is attempted.
MIN_SEASONALITY_POINTS: int = 14
MIN_SEASONALITY_WEEKDAYS: int = 2

#: Ratio of (spread across weekday means) to (overall stdev) above which a
#: day-of-week pattern is considered genuinely present rather than noise.
SEASONALITY_STRENGTH_THRESHOLD: float = 0.5

#: Default longer-horizon window for the adaptive baseline — deliberately
#: wider than MonitorAgent's own StatisticalDetector(window_size=7), so this
#: is a distinct, complementary perspective rather than a repeat of it.
DEFAULT_ADAPTIVE_WINDOW: int = 30

_WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


class AdaptiveDetectionEngine:
    """
    Computes a longer-horizon, seasonality-aware adaptive baseline for the
    current incident's metric, and combines it with the statistical/forecast
    evidence MonitorAgent already recorded on the event at detection time.

    Args:
        long_term_memory:  Existing :class:`~aeam.memory.long_term.LongTermMemory`
                           — the SAME instance the Orchestrator already holds.
        statistical_detector: Optional :class:`StatisticalDetector` override
                           (e.g. for tests). Defaults to a fresh instance
                           with ``window_size=DEFAULT_ADAPTIVE_WINDOW``.
        history_limit:     Max historical rows fetched per analysis.
        min_baseline_points, min_seasonality_points,
        seasonality_strength_threshold, adaptive_window: Override the
                           corresponding module constants (Phase D4
                           Enterprise Configuration Engine). Each ``None``
                           (the default) preserves the module default
                           unchanged.

    Raises:
        ValueError: If ``long_term_memory`` is ``None``.
    """

    def __init__(
        self,
        long_term_memory: Any,
        statistical_detector: StatisticalDetector | None = None,
        history_limit: int = 200,
        min_baseline_points: int | None = None,
        min_seasonality_points: int | None = None,
        seasonality_strength_threshold: float | None = None,
        adaptive_window: int | None = None,
    ) -> None:
        if long_term_memory is None:
            raise ValueError("long_term_memory must not be None.")
        self._ltm = long_term_memory
        window = adaptive_window if adaptive_window is not None else DEFAULT_ADAPTIVE_WINDOW
        self._detector = statistical_detector or StatisticalDetector(window_size=window)
        self._history_limit = max(1, int(history_limit))
        self._min_baseline_points = (
            min_baseline_points if min_baseline_points is not None else MIN_BASELINE_POINTS
        )
        self._min_seasonality_points = (
            min_seasonality_points if min_seasonality_points is not None else MIN_SEASONALITY_POINTS
        )
        self._seasonality_strength_threshold = (
            seasonality_strength_threshold
            if seasonality_strength_threshold is not None
            else SEASONALITY_STRENGTH_THRESHOLD
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def analyze(self, metric: str, current_value: float, event_metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        """
        Compute the adaptive detection finding for ``metric``.

        Args:
            metric:         The incident's ``event.metric``.
            current_value:  The incident's ``event.current_value``.
            event_metadata: The incident's ``event.metadata`` dict, as
                            populated by MonitorAgent.create_event() —
                            read-only, used only to surface the
                            ALREADY-COMPUTED ``"statistical"``/``"forecast"``
                            sub-dicts alongside the new adaptive signal.

        Returns:
            A dict, always with the same shape (never raises)::

                {
                    "history_points_used": int,
                    "adaptive_baseline": {...} | None,
                    "adaptive_baseline_insufficient": str | None,
                    "seasonality": {...} | None,
                    "seasonality_insufficient": str | None,
                    "existing_statistical": dict | None,
                    "existing_forecast": dict | None,
                    "combined_signal": bool,
                    "corroborating_signals": list[str],
                }
        """
        try:
            return self._analyze_unsafe(metric, current_value, event_metadata or {})
        except Exception as exc:  # noqa: BLE001
            logger.error("AdaptiveDetectionEngine.analyze | metric=%s | unexpected failure: %s", metric, exc, exc_info=True)
            return self._empty_result(reason=f"Adaptive analysis failed unexpectedly: {exc}")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _analyze_unsafe(self, metric: str, current_value: float, event_metadata: dict[str, Any]) -> dict[str, Any]:
        rows = self._ltm.get_metric_history(metric, limit=self._history_limit) or []
        values, dated_values = self._parse_rows(rows)

        # --- Adaptive (longer-horizon) baseline, via the SAME StatisticalDetector class ---
        adaptive_baseline: dict[str, Any] | None = None
        baseline_insufficient: str | None = None
        if len(values) < self._min_baseline_points:
            baseline_insufficient = (
                f"Only {len(values)} historical point(s) available for '{metric}'; "
                f"at least {self._min_baseline_points} are required for an adaptive baseline."
            )
        else:
            adaptive_baseline = self._detector.detect(current=current_value, history=values)

        # --- Seasonality (day-of-week grouping) ---
        seasonality: dict[str, Any] | None = None
        seasonality_insufficient: str | None = None
        distinct_weekdays = len({d.weekday() for d, _ in dated_values})
        if len(dated_values) < self._min_seasonality_points or distinct_weekdays < MIN_SEASONALITY_WEEKDAYS:
            seasonality_insufficient = (
                f"Only {len(dated_values)} dated historical point(s) across {distinct_weekdays} "
                f"distinct weekday(s) for '{metric}'; at least {self._min_seasonality_points} points "
                f"across {MIN_SEASONALITY_WEEKDAYS}+ weekdays are required for a seasonality judgement."
            )
        else:
            seasonality = self._detect_seasonality(dated_values)

        existing_statistical = event_metadata.get("statistical") if isinstance(event_metadata.get("statistical"), dict) else None
        existing_forecast = event_metadata.get("forecast") if isinstance(event_metadata.get("forecast"), dict) else None

        corroborating: list[str] = []
        if adaptive_baseline and adaptive_baseline.get("statistical_anomaly"):
            corroborating.append("adaptive_baseline")
        if existing_statistical and existing_statistical.get("statistical_anomaly"):
            corroborating.append("existing_statistical")
        if existing_forecast and existing_forecast.get("is_deviation"):
            corroborating.append("existing_forecast")

        return {
            "history_points_used": len(values),
            "adaptive_baseline": adaptive_baseline,
            "adaptive_baseline_insufficient": baseline_insufficient,
            "seasonality": seasonality,
            "seasonality_insufficient": seasonality_insufficient,
            "existing_statistical": existing_statistical,
            "existing_forecast": existing_forecast,
            "combined_signal": len(corroborating) > 0,
            "corroborating_signals": corroborating,
        }

    def _detect_seasonality(self, dated_values: list[tuple[datetime, float]]) -> dict[str, Any]:
        """
        Honest day-of-week seasonality check: groups values by weekday and
        compares the spread of weekday MEANS to the overall stdev. Never
        invents a pattern below the strength threshold — reports "not
        detected" instead.
        """
        by_weekday: dict[int, list[float]] = {}
        all_values = [v for _, v in dated_values]
        for d, v in dated_values:
            by_weekday.setdefault(d.weekday(), []).append(v)

        weekday_means = {wd: statistics.mean(vs) for wd, vs in by_weekday.items()}
        overall_stdev = statistics.stdev(all_values) if len(all_values) >= 2 else 0.0

        if overall_stdev == 0.0 or len(weekday_means) < 2:
            return {
                "detected": False,
                "reason": "No meaningful variation across weekdays to compare.",
                "weekday_means": {_WEEKDAY_NAMES[wd]: round(m, 4) for wd, m in weekday_means.items()},
            }

        spread_of_means = statistics.stdev(list(weekday_means.values())) if len(weekday_means) >= 2 else 0.0
        strength = spread_of_means / overall_stdev

        result: dict[str, Any] = {
            "detected": strength >= self._seasonality_strength_threshold,
            "strength": round(strength, 4),
            "weekday_means": {_WEEKDAY_NAMES[wd]: round(m, 4) for wd, m in sorted(weekday_means.items())},
        }
        if result["detected"]:
            highest_wd = max(weekday_means, key=weekday_means.get)
            lowest_wd = min(weekday_means, key=weekday_means.get)
            result["highest_weekday"] = _WEEKDAY_NAMES[highest_wd]
            result["lowest_weekday"] = _WEEKDAY_NAMES[lowest_wd]
        else:
            result["reason"] = f"Weekday-mean spread ({strength:.2f}) below the {self._seasonality_strength_threshold} significance threshold."
        return result

    @staticmethod
    def _parse_rows(rows: list[dict[str, Any]]) -> tuple[list[float], list[tuple[datetime, float]]]:
        """
        Split raw metric-history rows into (a) a plain value series in
        original order (for StatisticalDetector, which needs no dates) and
        (b) (parsed_datetime, value) pairs for rows whose timestamp could
        actually be parsed (for seasonality grouping). A row with an
        unparseable/missing timestamp or value is skipped, never guessed.
        """
        values: list[float] = []
        dated: list[tuple[datetime, float]] = []
        for row in rows:
            raw_value = row.get("value")
            if raw_value is None:
                continue
            try:
                value = float(raw_value)
            except (TypeError, ValueError):
                continue
            values.append(value)

            ts = row.get("timestamp")
            parsed = _parse_timestamp(ts)
            if parsed is not None:
                dated.append((parsed, value))
        return values, dated

    @staticmethod
    def _empty_result(reason: str) -> dict[str, Any]:
        return {
            "history_points_used": 0,
            "adaptive_baseline": None,
            "adaptive_baseline_insufficient": reason,
            "seasonality": None,
            "seasonality_insufficient": reason,
            "existing_statistical": None,
            "existing_forecast": None,
            "combined_signal": False,
            "corroborating_signals": [],
        }

    def __repr__(self) -> str:
        return f"AdaptiveDetectionEngine(history_limit={self._history_limit})"


def _parse_timestamp(value: Any) -> datetime | None:
    """Best-effort datetime parse (datetime passthrough, or ISO-8601 string,
    tolerating a trailing 'Z') — never fabricates a date for an unparseable
    value; the caller simply excludes that row from seasonality grouping."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value.strip():
        s = value.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            return datetime.fromisoformat(s)
        except ValueError:
            return None
    return None
