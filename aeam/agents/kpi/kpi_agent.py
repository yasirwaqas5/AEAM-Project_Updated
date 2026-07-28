"""
aeam/agents/kpi/kpi_agent.py

The KPI Agent (Phase F1 — Detection, Statistical & Forecast Intelligence
Uplift).

This module is the long-deferred real implementation of the investigation
pass that ``Orchestrator._run_kpi_investigation_placeholder`` stood in for
from Phase 3 until F1. That placeholder emitted a synthetic
``"Simulated root cause"``, which Phase E1 correctly marked
(``root_cause_source="placeholder"``), quarantined from Enterprise Memory
(ENG-5), and badged in the console. It was an honest placeholder — and F1
**deletes** it rather than bypassing it, because PHIL-1 is satisfied by
removal, not by relabeling.

What this agent actually does
-----------------------------
It characterises the anomaly using numbers that already exist or that it
computes itself from real history:

* **Magnitude and direction** — how far the observed value sits from its
  expected value, in percent and in robust sigmas.
* **Persistence** — whether the deviation is a single spike or a sustained
  shift, measured by counting consecutive recent observations on the same
  side of the baseline.
* **Trend** — the direction and slope of the recent series (ordinary least
  squares over the trailing window; deterministic, no library).
* **Breach attribution** — which detectors fired, read from the event's own
  ``metadata`` (``statistical`` / ``forecast`` / ``changepoint`` /
  ``seasonal_hybrid``), never re-invoked.

What it must never do
---------------------
**It never invents a cause.** A statistical characterisation answers
*what* changed, not *why*. Asserting "sales fell because of checkout
latency" from a z-score would be exactly the fabricated traceability
Article X calls the worst possible defect in this platform (EXPL-1, AI-2).
So the agent's ``root_cause`` is always a literal statement of measured
fact, attributed to the detectors that produced it, and its
``root_cause_source`` is ``"kpi_analysis"`` — a distinct, machine-readable
source that downstream stages (RAG's grounded causes, LLM reasoning) freely
supersede when they have a real causal explanation.

When it cannot ground a characterisation, it says so with a reason and sets
no root cause at all. Three states, always distinguished (EXPL-3): analysed
/ analysed with no signal / insufficient data.

Contract
--------
* **Advisory evidence source** (F-series invariant 1, AGENT-5). It appends
  its own findings entry and writes evidence/hypotheses/confidence to STM.
  It never calls ``RuleEngine``, ``DecisionEngine``, ``ActionAgent``, or an
  LLM, and never overrides or suppresses a deterministic decision.
* **Reads, never re-detects.** Detector output on the event is read as
  given. Re-running detection here would produce a second, divergent
  detection pipeline — the thing ARCH-1 forbids.
* **Never raises.** Declared never-raise boundary: an investigation must
  not fail because characterisation failed. Failures return a structured
  result naming the failure.

Distinct from the Adaptive Detection Engine (C5), which computes a
*longer-horizon adaptive baseline* as a corroborating signal. This agent
answers the investigation's question — what is wrong with this KPI, stated
in grounded terms — and is the pass the EvaluationEngine scores against.
"""

from __future__ import annotations

import logging
import statistics
from typing import Any

logger = logging.getLogger(__name__)

# Engine-owned defaults (ENG-6).
_DEFAULT_HISTORY_LIMIT: int = 90
# Minimum observations before a trend or persistence claim is made. Below
# this the agent reports insufficient data rather than describing noise.
_MIN_TREND_POINTS: int = 4
_MIN_PERSISTENCE_POINTS: int = 3
# Window used for trend/persistence. Deliberately short: the question is
# "what is happening now", not "what happened this quarter" (that is the
# adaptive baseline's job, C5).
_RECENT_WINDOW: int = 14
# Scale factor making MAD a consistent estimator of stdev for normal data.
_MAD_TO_SIGMA: float = 1.4826
# Deviation below which a difference is not worth characterising as a
# departure at all, in percent of expected.
_MATERIAL_DEVIATION_PERCENT: float = 1.0


class KPIAgent:
    """
    Investigation-time KPI analysis agent.

    Produces a grounded characterisation of an anomalous metric from the
    event's already-computed detector metadata plus historical values
    fetched from Long-Term Memory.

    Args:
        long_term_memory: Source of historical metric values — the SAME
                          ``LongTermMemory`` instance the Orchestrator
                          already holds. May be ``None``, in which case the
                          agent works from event metadata alone and says so.
        history_limit:    Maximum historical observations fetched per
                          analysis. Bounds the query (E6).

    Raises:
        ValueError: If ``history_limit`` < 2.

    Example::

        agent = KPIAgent(long_term_memory=ltm)
        result = agent.analyze(
            metric="sales",
            current_value=41000.0,
            expected_value=80000.0,
            event_metadata=event.metadata,
        )
        result["root_cause"]
        # "sales is 48.75% below its expected value of 80000.0 ..."
    """

    def __init__(
        self,
        long_term_memory: Any | None = None,
        history_limit: int = _DEFAULT_HISTORY_LIMIT,
    ) -> None:
        if history_limit < 2:
            raise ValueError("history_limit must be >= 2.")
        self._ltm = long_term_memory
        self._history_limit = int(history_limit)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def analyze(
        self,
        metric: str,
        current_value: float,
        expected_value: float | None = None,
        event_metadata: dict[str, Any] | None = None,
        depth: int = 1,
    ) -> dict[str, Any]:
        """
        Characterise the anomaly for ``metric``.

        Never raises (declared boundary, CODE-5): any failure is returned
        as a structured result whose ``analysis_failed`` field names what
        went wrong, so an investigation continues with an honest record of
        the failure rather than dying on it.

        Args:
            metric:         The incident's ``event.metric``.
            current_value:  The incident's ``event.current_value``.
            expected_value: The incident's ``event.expected_value``. May be
                            ``None``; the agent then falls back to a robust
                            baseline computed from history, and discloses
                            which of the two it used.
            event_metadata: The incident's ``event.metadata``, as populated
                            by ``MonitorAgent.create_event()``. Read-only —
                            detector output is read, never recomputed.
            depth:          Investigation depth this pass runs at, recorded
                            on the result for the audit trail.

        Returns:
            A dict, always with the same shape::

                {
                    "metric":               str,
                    "depth":                int,
                    "current_value":        float,
                    "expected_value":       float | None,
                    "baseline_source":      "event" | "history" | None,
                    "history_points_used":  int,
                    "deviation":            {...} | None,
                    "persistence":          {...} | None,
                    "persistence_insufficient": str | None,
                    "trend":                {...} | None,
                    "trend_insufficient":   str | None,
                    "detectors_fired":      list[str],
                    "detector_evidence":    {...},
                    "root_cause":           str | None,
                    "root_cause_source":    "kpi_analysis" | None,
                    "confidence":           float,
                    "insufficient_data":    str | None,
                    "analysis_failed":      str | None,
                }

            ``root_cause`` is a statement of measured fact or ``None``. It
            is never a causal claim — see the module docstring.
        """
        try:
            return self._analyze_unsafe(
                metric=metric,
                current_value=current_value,
                expected_value=expected_value,
                event_metadata=event_metadata or {},
                depth=depth,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "KPIAgent.analyze | metric=%s | unexpected failure: %s",
                metric, exc, exc_info=True,
            )
            return self._empty_result(
                metric=metric,
                current_value=current_value,
                expected_value=expected_value,
                depth=depth,
                analysis_failed=f"KPI analysis failed unexpectedly: {exc}",
            )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _analyze_unsafe(
        self,
        metric: str,
        current_value: float,
        expected_value: float | None,
        event_metadata: dict[str, Any],
        depth: int,
    ) -> dict[str, Any]:
        history = self._fetch_history(metric)

        # --- Baseline: the event's own expected value wins; history is the
        # fallback. Which one was used is always disclosed, because a
        # deviation percentage means different things against a detector's
        # moving average than against a robust median of a quarter.
        baseline: float | None = None
        baseline_source: str | None = None
        if expected_value is not None and float(expected_value) != 0.0:
            baseline = float(expected_value)
            baseline_source = "event"
        elif history:
            baseline = float(statistics.median(history))
            baseline_source = "history"

        deviation = self._deviation(current_value, baseline, history)
        persistence, persistence_insufficient = self._persistence(current_value, baseline, history)
        trend, trend_insufficient = self._trend(history)
        detectors_fired, detector_evidence = self._read_detectors(event_metadata)

        result: dict[str, Any] = {
            "metric": metric,
            "depth": depth,
            "current_value": float(current_value),
            "expected_value": baseline,
            "baseline_source": baseline_source,
            "history_points_used": len(history),
            "deviation": deviation,
            "persistence": persistence,
            "persistence_insufficient": persistence_insufficient,
            "trend": trend,
            "trend_insufficient": trend_insufficient,
            "detectors_fired": detectors_fired,
            "detector_evidence": detector_evidence,
            "root_cause": None,
            "root_cause_source": None,
            "confidence": 0.0,
            "insufficient_data": None,
            "analysis_failed": None,
        }

        # --- Grounding gate. A characterisation is emitted ONLY when there
        # is a measured deviation to describe. No baseline and no fired
        # detector means the agent has nothing it can honestly say — and
        # saying nothing, with the reason, is the correct output. This is
        # the gate that makes the placeholder's "Simulated root cause"
        # structurally impossible to reproduce.
        if deviation is None and not detectors_fired:
            result["insufficient_data"] = (
                f"No expected value on the event, no history for {metric!r} "
                f"({len(history)} points), and no detector recorded a breach — "
                "there is nothing measured to characterise."
            )
            return result

        if deviation is None:
            result["insufficient_data"] = (
                f"No baseline available for {metric!r} (event carried no expected "
                f"value and history has {len(history)} points); characterisation is "
                "limited to which detectors fired."
            )

        statement = self._compose_statement(
            metric=metric,
            current_value=current_value,
            deviation=deviation,
            persistence=persistence,
            trend=trend,
            detectors_fired=detectors_fired,
        )
        if statement is not None:
            result["root_cause"] = statement
            result["root_cause_source"] = "kpi_analysis"

        result["confidence"] = self._confidence(
            deviation=deviation,
            persistence=persistence,
            detectors_fired=detectors_fired,
            history_points=len(history),
        )

        return result

    def _fetch_history(self, metric: str) -> list[float]:
        """Fetch numeric history for ``metric``, or an empty list.

        A memory that is absent, empty, or failing is a normal condition,
        not an error: the agent degrades to metadata-only analysis and
        discloses the point count it actually used.
        """
        if self._ltm is None:
            return []
        try:
            rows = self._ltm.get_metric_history(metric, limit=self._history_limit) or []
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "KPIAgent | history fetch failed | metric=%s | %s", metric, exc
            )
            return []

        values: list[float] = []
        for row in rows:
            raw = row.get("value") if isinstance(row, dict) else None
            if raw is None:
                continue
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            if value != value:  # NaN
                continue
            values.append(value)
        return values

    @staticmethod
    def _mad_sigma(values: list[float]) -> float:
        """Robust sigma from the median absolute deviation, or 0.0."""
        if len(values) < 2:
            return 0.0
        center = statistics.median(values)
        mad = statistics.median([abs(v - center) for v in values])
        return float(mad * _MAD_TO_SIGMA)

    def _deviation(
        self,
        current: float,
        baseline: float | None,
        history: list[float],
    ) -> dict[str, Any] | None:
        """Quantify the departure from baseline, in percent and robust sigmas.

        ``sigma`` is ``None`` when history has no dispersion to measure
        against — reported honestly rather than as a fabricated zero, which
        a consumer would read as "no deviation".
        """
        if baseline is None:
            return None

        absolute = float(current) - baseline
        percent = (absolute / baseline) * 100.0 if baseline != 0 else None

        sigma = self._mad_sigma(history)
        sigmas = round(absolute / sigma, 4) if sigma > 0 else None

        return {
            "absolute": round(absolute, 6),
            "percent": round(percent, 4) if percent is not None else None,
            "direction": "below" if absolute < 0 else ("above" if absolute > 0 else "at"),
            "sigmas": sigmas,
            "robust_sigma": round(sigma, 6) if sigma > 0 else None,
            "material": (
                abs(percent) >= _MATERIAL_DEVIATION_PERCENT if percent is not None else None
            ),
        }

    def _persistence(
        self,
        current: float,
        baseline: float | None,
        history: list[float],
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Count how many recent observations sit on the current's side of baseline.

        Distinguishes a one-off spike from a sustained shift — the single
        most operationally useful thing a characterisation can say, because
        it decides whether the anomaly is still happening.
        """
        if baseline is None:
            return None, "No baseline available; persistence cannot be measured."
        if len(history) < _MIN_PERSISTENCE_POINTS:
            return None, (
                f"{len(history)} historical observations; "
                f"{_MIN_PERSISTENCE_POINTS} required to distinguish a spike from a shift."
            )

        current_below = float(current) < baseline
        consecutive = 0
        for value in reversed(history[-_RECENT_WINDOW:]):
            if (value < baseline) == current_below:
                consecutive += 1
            else:
                break

        # +1 counts the current observation itself.
        run_length = consecutive + 1
        return {
            "consecutive_observations": run_length,
            "window": min(len(history), _RECENT_WINDOW) + 1,
            "side": "below" if current_below else "at-or-above",
            "sustained": run_length >= _MIN_PERSISTENCE_POINTS,
        }, None

    @staticmethod
    def _trend(history: list[float]) -> tuple[dict[str, Any] | None, str | None]:
        """Ordinary least-squares slope over the trailing window.

        Deterministic and dependency-free: the slope of the best-fit line
        through the recent points, plus the fraction of the level it
        represents per observation, so "declining" is quantified rather
        than asserted.
        """
        if len(history) < _MIN_TREND_POINTS:
            return None, (
                f"{len(history)} historical observations; "
                f"{_MIN_TREND_POINTS} required to fit a trend."
            )

        window = history[-_RECENT_WINDOW:]
        n = len(window)
        mean_x = (n - 1) / 2.0
        mean_y = statistics.mean(window)

        denominator = sum((i - mean_x) ** 2 for i in range(n))
        if denominator == 0:
            return None, "Trend window has no variation in position; slope is undefined."

        slope = sum((i - mean_x) * (window[i] - mean_y) for i in range(n)) / denominator
        percent_per_observation = (slope / mean_y) * 100.0 if mean_y != 0 else None

        if slope > 0:
            direction = "rising"
        elif slope < 0:
            direction = "falling"
        else:
            direction = "flat"

        return {
            "slope": round(slope, 6),
            "direction": direction,
            "percent_per_observation": (
                round(percent_per_observation, 4) if percent_per_observation is not None else None
            ),
            "window": n,
        }, None

    @staticmethod
    def _read_detectors(event_metadata: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
        """Read which detectors fired from the event's own metadata.

        Reads only; never re-invokes a detector. The returned evidence dict
        carries the detectors' own numbers so the finding is traceable to
        the exact values detection recorded (EXPL-1).
        """
        fired: list[str] = []
        evidence: dict[str, Any] = {}

        rule = event_metadata.get("rule")
        if isinstance(rule, dict) and rule.get("rule_triggered"):
            fired.append("rule")
            evidence["rule"] = {
                "rule_name": rule.get("rule_name"),
                "change_percent": rule.get("change_percent"),
                "threshold": rule.get("threshold"),
            }

        statistical = event_metadata.get("statistical")
        if isinstance(statistical, dict) and statistical.get("statistical_anomaly"):
            fired.append("statistical")
            evidence["statistical"] = {
                "z_score": statistical.get("z_score"),
                "moving_avg": statistical.get("moving_avg"),
                "percentile_low": statistical.get("percentile_low"),
                "percentile_high": statistical.get("percentile_high"),
            }

        forecast = event_metadata.get("forecast")
        if isinstance(forecast, dict) and forecast.get("is_deviation"):
            fired.append("forecast")
            evidence["forecast"] = {
                "predicted": forecast.get("predicted"),
                "lower_bound": forecast.get("lower_bound"),
                "upper_bound": forecast.get("upper_bound"),
                "deviation_percent": forecast.get("deviation_percent"),
            }

        # Phase F1 detectors — present only when they are enabled, absent
        # otherwise, so nothing here changes for a default-posture event.
        changepoint = event_metadata.get("changepoint")
        if isinstance(changepoint, dict) and changepoint.get("changepoint_detected"):
            fired.append("changepoint")
            evidence["changepoint"] = {
                "shift_magnitude": changepoint.get("shift_magnitude"),
                "shift_score": changepoint.get("shift_score"),
                "before_level": changepoint.get("before_level"),
                "after_level": changepoint.get("after_level"),
            }

        seasonal = event_metadata.get("seasonal_hybrid")
        if isinstance(seasonal, dict) and seasonal.get("seasonal_anomaly"):
            fired.append("seasonal_hybrid")
            evidence["seasonal_hybrid"] = {
                "seasonal_expected": seasonal.get("seasonal_expected"),
                "residual": seasonal.get("residual"),
                "residual_score": seasonal.get("residual_score"),
                "period": seasonal.get("period"),
            }

        return fired, evidence

    @staticmethod
    def _compose_statement(
        metric: str,
        current_value: float,
        deviation: dict[str, Any] | None,
        persistence: dict[str, Any] | None,
        trend: dict[str, Any] | None,
        detectors_fired: list[str],
    ) -> str | None:
        """Compose the grounded characterisation sentence.

        Every clause restates a number computed above — this function
        performs no analysis of its own (EXPL-2: explanations restate, they
        never recompute). It describes WHAT is measured and never asserts
        WHY, which is the boundary that keeps the output non-fabricated.

        Returns ``None`` when there is no measured deviation and no fired
        detector to describe.
        """
        clauses: list[str] = []

        if deviation is not None and deviation.get("percent") is not None:
            percent = abs(float(deviation["percent"]))
            direction = deviation["direction"]
            clause = f"{metric} is {percent:.2f}% {direction} its expected value"
            if deviation.get("sigmas") is not None:
                clause += f" ({abs(float(deviation['sigmas'])):.2f} robust sigmas from its historical median)"
            clauses.append(clause)
        elif deviation is not None:
            clauses.append(
                f"{metric} is {abs(float(deviation['absolute'])):.4f} "
                f"{deviation['direction']} its expected value"
            )

        if persistence is not None and persistence.get("sustained"):
            clauses.append(
                f"sustained across {persistence['consecutive_observations']} "
                f"consecutive observations {persistence['side']} baseline"
            )
        elif persistence is not None:
            clauses.append(
                f"observed on {persistence['consecutive_observations']} "
                f"consecutive observation(s) — not yet a sustained shift"
            )

        if trend is not None and trend["direction"] != "flat":
            pct = trend.get("percent_per_observation")
            if pct is not None:
                clauses.append(
                    f"with a {trend['direction']} trend of {abs(float(pct)):.2f}% "
                    f"per observation over the last {trend['window']}"
                )
            else:
                clauses.append(
                    f"with a {trend['direction']} trend over the last {trend['window']} observations"
                )

        if not clauses:
            if not detectors_fired:
                return None
            clauses.append(f"{metric} was flagged at {current_value:g}")

        attribution = (
            f"Detected by: {', '.join(detectors_fired)}."
            if detectors_fired
            else "No detector recorded a breach for this observation."
        )

        return f"{'; '.join(clauses)}. {attribution}"

    @staticmethod
    def _confidence(
        deviation: dict[str, Any] | None,
        persistence: dict[str, Any] | None,
        detectors_fired: list[str],
        history_points: int,
    ) -> float:
        """Confidence that the characterisation describes a real anomaly.

        Deliberately about the *measurement*, not about a cause — this
        agent asserts no cause, so a confidence in one would be meaningless.
        Each component is additive, bounded, and traceable to a number
        computed above; nothing here is tuned to reach a target score.

        Components:
        * up to 0.40 — corroborating detectors (0.20 each; independent
          detectors agreeing is the strongest available signal);
        * up to 0.25 — deviation magnitude in robust sigmas;
        * up to 0.20 — persistence (a sustained shift is more certainly
          real than a single point);
        * up to 0.15 — history depth backing the baseline.
        """
        score = 0.0

        score += min(len(detectors_fired) * 0.20, 0.40)

        if deviation is not None:
            sigmas = deviation.get("sigmas")
            if sigmas is not None:
                score += min(abs(float(sigmas)) / 12.0, 0.25)
            elif deviation.get("material"):
                # No dispersion to score against, but the departure is
                # materially large in percentage terms.
                score += 0.10

        if persistence is not None and persistence.get("sustained"):
            score += 0.20

        if history_points >= _RECENT_WINDOW:
            score += 0.15
        elif history_points >= _MIN_TREND_POINTS:
            score += 0.08

        return round(min(score, 1.0), 2)

    @staticmethod
    def _empty_result(
        metric: str,
        current_value: float,
        expected_value: float | None,
        depth: int,
        analysis_failed: str | None = None,
        insufficient_data: str | None = None,
    ) -> dict[str, Any]:
        """The canonical no-analysis result — same shape, honest fields."""
        return {
            "metric": metric,
            "depth": depth,
            "current_value": float(current_value),
            "expected_value": expected_value,
            "baseline_source": None,
            "history_points_used": 0,
            "deviation": None,
            "persistence": None,
            "persistence_insufficient": analysis_failed or insufficient_data,
            "trend": None,
            "trend_insufficient": analysis_failed or insufficient_data,
            "detectors_fired": [],
            "detector_evidence": {},
            "root_cause": None,
            "root_cause_source": None,
            "confidence": 0.0,
            "insufficient_data": insufficient_data,
            "analysis_failed": analysis_failed,
        }

    def __repr__(self) -> str:
        return (
            f"KPIAgent(history_limit={self._history_limit}, "
            f"long_term_memory={'wired' if self._ltm is not None else 'absent'})"
        )
