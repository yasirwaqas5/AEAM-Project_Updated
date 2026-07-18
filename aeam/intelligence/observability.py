"""
aeam/intelligence/observability.py

Enterprise Observability Engine (Phase D3).

Continuously summarizes how AEAM ITSELF is performing across every completed
investigation -- it never re-investigates, never recomputes a single
incident's root cause/execution plan/explainability/evaluation, and never
writes anything. It reads ONLY the ``findings`` list already persisted on
each row of the ``incidents`` table (the exact same JSON structure
ExecutionPlanningEngine/ExplainabilityEngine/AIEvaluationEngine already
produce and the Investigation Workspace already renders) and reduces it to
cross-incident rates/trends.

Design rationale (Architecture Gate conclusion):
- This is a genuinely different SHAPE of engine than C7/D1/D2: those three
  operate on ONE incident's findings at ``Orchestrator.finalize_incident()``
  time and append one more findings entry to THAT incident. Observability
  is a cross-incident summary with no single incident to attach itself to --
  attaching a constantly-changing, all-incidents-wide summary onto every
  individual incident row would be architecturally wrong (redundant writes,
  a summary that goes stale the moment the NEXT incident completes). So this
  engine is NOT wired into the Orchestrator at all. It is a pure function
  over the SAME incident list ``GET /api/v1/incidents/`` already returns
  (unchanged), invoked by a new, thin, read-only API endpoint
  (``aeam/api/observability.py``) -- exactly the reuse pattern the mission
  requires: no second monitoring pipeline, no duplicate metrics store, no
  Orchestrator/investigation-pipeline change.
- No existing cross-incident aggregation exists anywhere in this codebase
  (confirmed: ``LongTermMemory``/``DatabaseClient`` expose no ``get_stats()``-
  style method; Dashboard/Analytics already aggregate incidents CLIENT-SIDE
  in the browser from the same unmodified ``/api/v1/incidents/`` payload).
  This engine is the backend-side equivalent of that same, already-
  established pattern -- not a new one.
- Prometheus (``aeam/monitoring/metrics.py``) already instruments incident
  lifecycle timing (``investigation_duration``, a *global, unlabeled*
  histogram -- no per-incident value, no persistence, resets on process
  restart) and action outcomes. It has zero visibility into per-incident
  evidence-source data (memory/policy/cross-dataset/adaptive/retrieval hit
  rates, execution-plan/AI-evaluation scores) -- those only exist inside
  ``findings``. This engine therefore reads exclusively from ``findings``,
  never touches Prometheus, and is not a second metrics system: it is the
  same read pattern as C7/D1/D2, applied across many incidents instead of
  one.
- Investigation duration honesty: no per-incident duration is persisted
  ANYWHERE (not in ``incidents``, not in ``findings``, not in
  ``audit_summary``) -- only Prometheus's process-lifetime aggregate mean
  exists, in a completely different data source this engine deliberately
  does not merge in (mixing a point-in-time incident-table snapshot with a
  live, resettable, unlabelled process metric would misrepresent both). This
  metric is honestly reported as unavailable at the per-incident level, with
  the real reason stated -- exactly the same honesty precedent
  Analytics.jsx already set for "Forecast vs Actual."
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_STATUS_RESOLVED = "RESOLVED"

# Health-score weights: which computed rates/averages feed the overall
# score, and how. Fully disclosed here and in the returned
# ``overall_ai_health_formula`` string -- never a hidden coefficient. Each
# term is already a [0, 1] rate/score; unavailable terms are dropped from
# the mean entirely (never defaulted to zero, which would silently punish
# a feature nobody has finished configuring yet).
_HEALTH_SCORE_TERMS: tuple[str, ...] = (
    "memory_hit_rate", "policy_hit_rate", "retrieval_success_rate",
    "cross_dataset_usage_rate", "adaptive_detection_usage_rate",
    "execution_plan_confidence_trend", "ai_evaluation_trend",
    "investigation_success_rate",
)

# Cap on "recent_values" trend payloads -- a display convenience only;
# `average`/`direction` are always computed from the FULL series regardless.
_TREND_WINDOW: int = 20


class ObservabilityEngine:
    """
    Summarizes AEAM's own operating quality across every completed
    investigation. Stateless and dependency-free -- every input is passed
    explicitly to :meth:`summarize`; no database handle, no LLM, no
    retrieval pipeline. The constructor is entirely OPTIONAL -- every
    existing zero-arg ``ObservabilityEngine()`` call site keeps working
    unchanged.

    Args:
        trend_window: Overrides ``_TREND_WINDOW`` (Phase D4 Enterprise
                      Configuration Engine) -- the display cap
                      ``recent_values`` entries. ``None`` (the default)
                      preserves the module default (20).
    """

    def __init__(self, trend_window: int | None = None) -> None:
        self._trend_window = trend_window if trend_window is not None else _TREND_WINDOW

    def summarize(self, incidents: list[dict[str, Any]]) -> dict[str, Any]:
        """
        Build the observability summary across ``incidents``.

        Args:
            incidents: Every row from the ``incidents`` table (the SAME,
                       unmodified list ``GET /api/v1/incidents/`` already
                       returns), each with its ``findings`` field already
                       parsed from JSON text into a ``list[dict]`` (parsing
                       itself is the API layer's job, not this engine's --
                       this engine only ever reads plain Python data).
                       Every row is definitionally a COMPLETED investigation:
                       ``LongTermMemory.record_incident()`` is only ever
                       called from ``Orchestrator.finalize_incident()``, so
                       no partial/in-progress incident is ever persisted.

        Returns:
            A JSON-serialisable dict with one entry per required metric
            (``investigation_duration``, ``memory_hit_rate``,
            ``policy_hit_rate``, ``retrieval_success_rate``,
            ``cross_dataset_usage_rate``, ``adaptive_detection_usage_rate``,
            ``execution_plan_confidence_trend``, ``ai_evaluation_trend``,
            ``investigation_success_rate``, ``overall_ai_health``), plus
            ``total_investigations`` and ``overall_ai_health_formula``.
            Each metric is a dict with ``available`` (bool) and either a
            computed value or an honest ``reason`` string -- never both
            fabricated and hidden.

        Raises:
            Never raises -- caught by the API endpoint caller.
        """
        total = len(incidents)

        memory_hit = _consulted_and_hit_rate(
            incidents, "memory", lambda data: bool(data.get("matches")),
        )
        policy_hit = _consulted_and_hit_rate(
            incidents, "policy", lambda data: bool(data.get("matches")),
        )
        retrieval_success = _consulted_and_hit_rate(
            incidents, "rag", lambda data: bool((data.get("retrieved_count") or 0) > 0),
        )
        cross_dataset_usage = _consulted_and_hit_rate(
            incidents, "cross_dataset", lambda data: not data.get("insufficient_data"),
        )
        adaptive_usage = _consulted_and_hit_rate(
            incidents, "adaptive",
            lambda data: not data.get("adaptive_baseline_insufficient") or not data.get("seasonality_insufficient"),
        )

        plan_confidence_trend = _numeric_trend(
            incidents, "execution_plan", lambda data: data.get("confidence"), self._trend_window,
        )
        ai_eval_trend = _numeric_trend(
            incidents, "ai_evaluation", lambda data: data.get("overall_score"), self._trend_window,
        )

        success_rate = _investigation_success_rate(incidents)

        duration = {
            "available": False,
            "reason": (
                "Per-incident investigation duration is not persisted in the incidents table, "
                "findings, or audit_summary -- only a global, unlabeled, process-lifetime "
                "aggregate exists via the investigation_duration Prometheus histogram (see "
                "/metrics), which this engine deliberately does not merge in as a second, "
                "differently-shaped data source. The Dashboard/Analytics pages already surface "
                "that Prometheus aggregate directly."
            ),
        }

        metrics: dict[str, dict[str, Any]] = {
            "investigation_duration": duration,
            "memory_hit_rate": memory_hit,
            "policy_hit_rate": policy_hit,
            "retrieval_success_rate": retrieval_success,
            "cross_dataset_usage_rate": cross_dataset_usage,
            "adaptive_detection_usage_rate": adaptive_usage,
            "execution_plan_confidence_trend": plan_confidence_trend,
            "ai_evaluation_trend": ai_eval_trend,
            "investigation_success_rate": success_rate,
        }

        overall, formula = _compute_overall_health(metrics)
        metrics["overall_ai_health"] = overall
        metrics["overall_ai_health_formula"] = formula

        return {
            "total_investigations": total,
            **metrics,
        }

    def __repr__(self) -> str:
        return "ObservabilityEngine()"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _latest_finding_data(findings: list[dict[str, Any]], type_name: str) -> dict[str, Any] | None:
    """Same scan pattern as every C/D-phase engine's own helper -- last entry of ``type_name`` wins."""
    latest: dict[str, Any] | None = None
    for entry in findings or []:
        if isinstance(entry, dict) and entry.get("type") == type_name:
            latest = entry.get("data") or {}
    return latest


def _incident_findings(incident: dict[str, Any]) -> list[dict[str, Any]]:
    findings = incident.get("findings")
    return findings if isinstance(findings, list) else []


def _consulted_and_hit_rate(
    incidents: list[dict[str, Any]],
    finding_type: str,
    is_hit: Any,
) -> dict[str, Any]:
    """
    Two honest denominators: how many incidents consulted this source at
    all, and -- of those -- how many got a real, usable result. Never
    conflates "never asked" with "asked and found nothing."
    """
    consulted = 0
    hits = 0
    for incident in incidents:
        data = _latest_finding_data(_incident_findings(incident), finding_type)
        if data is None:
            continue
        consulted += 1
        try:
            if is_hit(data):
                hits += 1
        except Exception:  # noqa: BLE001
            continue

    if consulted == 0:
        return {
            "available": False,
            "reason": f"{finding_type} was never consulted in any of the {len(incidents)} recorded investigation(s).",
        }
    return {
        "available": True,
        "rate": round(hits / consulted, 4),
        "consulted_count": consulted,
        "hit_count": hits,
        "total_investigations": len(incidents),
    }


def _numeric_trend(
    incidents: list[dict[str, Any]],
    finding_type: str,
    extract: Any,
    trend_window: int = _TREND_WINDOW,
) -> dict[str, Any]:
    """
    Chronological (as persisted -- ``incidents`` already arrives newest-first
    from the API, so this reverses to oldest-first) series of a real numeric
    field, plus a fully-disclosed trend direction: mean of the first half
    versus the second half. Never a fabricated forecast/regression.
    """
    ordered = list(reversed(incidents))  # oldest first
    values: list[float] = []
    for incident in ordered:
        data = _latest_finding_data(_incident_findings(incident), finding_type)
        if data is None:
            continue
        value = extract(data)
        if isinstance(value, (int, float)):
            values.append(float(value))

    if not values:
        return {
            "available": False,
            "reason": f"{finding_type} produced no numeric value in any of the {len(incidents)} recorded investigation(s).",
        }

    average = round(sum(values) / len(values), 4)
    direction = "flat"
    delta = 0.0
    if len(values) >= 2:
        mid = len(values) // 2
        first_half = values[:mid] or values[:1]
        second_half = values[mid:] or values[-1:]
        first_avg = sum(first_half) / len(first_half)
        second_avg = sum(second_half) / len(second_half)
        delta = round(second_avg - first_avg, 4)
        if delta > 0.02:
            direction = "improving"
        elif delta < -0.02:
            direction = "declining"

    return {
        "available": True,
        "average": average,
        "direction": direction,
        "delta": delta,
        "sample_count": len(values),
        # Capped to the most recent `trend_window` points so the payload
        # stays small; this is a display convenience, not a filtering/
        # selection bias -- `average`/`direction` above are computed from
        # the FULL series.
        "recent_values": [round(v, 4) for v in values[-trend_window:]],
    }


def _investigation_success_rate(incidents: list[dict[str, Any]]) -> dict[str, Any]:
    statuses: list[str] = []
    for incident in incidents:
        audit = None
        for entry in _incident_findings(incident):
            if isinstance(entry, dict) and entry.get("type") == "audit_summary":
                audit = entry
        if audit is not None and audit.get("investigation_status"):
            statuses.append(audit["investigation_status"])

    if not statuses:
        return {
            "available": False,
            "reason": f"No audit_summary.investigation_status found in any of the {len(incidents)} recorded investigation(s).",
        }
    resolved = sum(1 for s in statuses if s == _STATUS_RESOLVED)
    return {
        "available": True,
        "rate": round(resolved / len(statuses), 4),
        "resolved_count": resolved,
        "total_with_status": len(statuses),
        "total_investigations": len(incidents),
    }


def _compute_overall_health(metrics: dict[str, dict[str, Any]]) -> tuple[dict[str, Any], str]:
    terms: list[float] = []
    used: list[str] = []
    for key in _HEALTH_SCORE_TERMS:
        m = metrics.get(key) or {}
        if not m.get("available"):
            continue
        value = m.get("rate", m.get("average"))
        if isinstance(value, (int, float)):
            terms.append(float(value))
            used.append(key)

    formula = (
        f"Unweighted mean of {len(used)}/{len(_HEALTH_SCORE_TERMS)} computable rate/score components "
        f"({', '.join(used) if used else 'none available'}), clamped to [0, 1]. "
        "investigation_duration is intentionally excluded (not a [0,1] rate)."
    )
    if not terms:
        return {"available": False, "reason": "No component metric was computable across the recorded investigations."}, formula

    overall = max(0.0, min(1.0, sum(terms) / len(terms)))
    return {"available": True, "score": round(overall, 4), "based_on": used}, formula
