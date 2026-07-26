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
- Investigation duration honesty (UPDATED in Phase E11): the Orchestrator now
  persists the measured per-incident duration into ``audit_summary`` at
  finalize (``investigation_duration_seconds``), so this engine reports a
  REAL duration for every incident recorded after that phase. It still never
  merges in Prometheus's process-lifetime aggregate — the number reported
  here is only ever the measurement actually persisted on the incident.
  Incidents recorded BEFORE E11 carry no such field, and this engine says so
  explicitly (``incidents_without_duration`` + a stated reason) rather than
  silently averaging over a smaller population or backfilling a value it
  never measured — the same mixed-history honesty (COMPAT-1 / EXPL-3) the
  rest of the platform applies.
- Platform cost (Phase E11): the same read, applied to ``audit_summary.cost``
  — LLM spend/tokens, action-execution counts, and retrieval volume that the
  Orchestrator attributed to each incident. Reported per-window with the
  window fully disclosed by the API layer, and with the identical
  mixed-history disclosure as duration. No second cost store exists; this is
  a reduction over already-persisted findings, exactly like every other
  metric in this module.
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

        # Phase E11: real, measured duration for post-phase incidents; an
        # honest, stated reason for the pre-phase ones (never backfilled).
        duration = _investigation_duration(incidents, self._trend_window)

        # Phase E11: the platform cost surface, reduced from the same
        # already-persisted audit_summary entries.
        cost = _platform_cost(incidents)

        metrics: dict[str, dict[str, Any]] = {
            "investigation_duration": duration,
            "platform_cost": cost,
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


def _audit_summary(incident: dict[str, Any]) -> dict[str, Any] | None:
    """Last ``audit_summary`` findings entry for an incident, or ``None``.

    Note the shape difference from :func:`_latest_finding_data`: the
    Orchestrator writes ``audit_summary`` fields at the TOP level of the
    findings entry (not nested under ``data``), which is why this has its
    own accessor rather than reusing the generic one.
    """
    latest: dict[str, Any] | None = None
    for entry in _incident_findings(incident):
        if isinstance(entry, dict) and entry.get("type") == "audit_summary":
            latest = entry
    return latest


def _investigation_duration(
    incidents: list[dict[str, Any]],
    trend_window: int = _TREND_WINDOW,
) -> dict[str, Any]:
    """
    Real per-incident investigation duration (Phase E11).

    Reads ``audit_summary.investigation_duration_seconds`` — the value the
    Orchestrator MEASURED at finalize, not a Prometheus aggregate. Incidents
    recorded before Phase E11 have no such field; they are counted and
    disclosed separately rather than dropped silently or backfilled
    (COMPAT-1 / EXPL-3).
    """
    values: list[float] = []
    missing = 0
    for incident in reversed(incidents):  # oldest first, matching _numeric_trend
        audit = _audit_summary(incident)
        if audit is None:
            missing += 1
            continue
        value = audit.get("investigation_duration_seconds")
        if isinstance(value, (int, float)):
            values.append(float(value))
        else:
            missing += 1

    if not values:
        return {
            "available": False,
            "reason": (
                f"None of the {len(incidents)} recorded investigation(s) carry a measured "
                "audit_summary.investigation_duration_seconds. Per-incident duration is "
                "persisted only for incidents finalized after Phase E11; this engine reports "
                "what was measured and never merges in the global, process-lifetime "
                "investigation_duration Prometheus histogram as a substitute."
            ),
            "incidents_without_duration": missing,
        }

    ordered = sorted(values)
    mid = len(ordered) // 2
    median = ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2.0

    return {
        "available": True,
        "unit": "seconds",
        "average": round(sum(values) / len(values), 4),
        "median": round(median, 4),
        "min": round(ordered[0], 4),
        "max": round(ordered[-1], 4),
        "sample_count": len(values),
        # Mixed-history disclosure: how many incidents in this window predate
        # duration persistence and therefore contribute nothing above.
        "incidents_without_duration": missing,
        "total_investigations": len(incidents),
        "measurement": (
            "Wall-clock seconds from Orchestrator.handle_event() to "
            "finalize_incident(), measured per incident and persisted into "
            "audit_summary.investigation_duration_seconds."
        ),
        "recent_values": [round(v, 4) for v in values[-trend_window:]],
    }


def _platform_cost(incidents: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Platform cost roll-up across the supplied window (Phase E11).

    Sums the per-incident ``audit_summary.cost`` blocks the Orchestrator
    attributed at finalize. Every component is a real measurement: LLM tokens
    are provider-reported, spend is those tokens at the operator-configured
    rate, and action/retrieval counts are what actually happened. Incidents
    predating cost attribution carry no block and are disclosed as such
    instead of being treated as zero-cost (which would understate the
    average).
    """
    with_cost = 0
    totals = {
        "llm_calls": 0,
        "llm_prompt_tokens": 0,
        "llm_completion_tokens": 0,
        "llm_total_tokens": 0,
        "llm_cost_usd": 0.0,
        "retrieval_chunks": 0,
        "actions_executed": 0,
        "actions_skipped": 0,
        "actions_withheld": 0,
    }

    for incident in incidents:
        audit = _audit_summary(incident)
        cost = (audit or {}).get("cost")
        if not isinstance(cost, dict):
            continue
        with_cost += 1
        for key in totals:
            value = cost.get(key)
            if isinstance(value, (int, float)):
                totals[key] += value

    if with_cost == 0:
        return {
            "available": False,
            "reason": (
                f"None of the {len(incidents)} recorded investigation(s) carry an "
                "audit_summary.cost block. Per-incident cost attribution exists only for "
                "incidents finalized after Phase E11; older incidents are reported as "
                "unavailable rather than counted as zero-cost."
            ),
            "incidents_without_cost": len(incidents),
        }

    totals["llm_cost_usd"] = round(totals["llm_cost_usd"], 6)
    return {
        "available": True,
        "totals": totals,
        "per_incident_average": {
            "llm_calls": round(totals["llm_calls"] / with_cost, 4),
            "llm_total_tokens": round(totals["llm_total_tokens"] / with_cost, 4),
            "llm_cost_usd": round(totals["llm_cost_usd"] / with_cost, 6),
            "retrieval_chunks": round(totals["retrieval_chunks"] / with_cost, 4),
            "actions_executed": round(totals["actions_executed"] / with_cost, 4),
        },
        "incidents_with_cost": with_cost,
        # Mixed-history disclosure, same contract as duration above.
        "incidents_without_cost": len(incidents) - with_cost,
        "total_investigations": len(incidents),
        "cost_basis": (
            "LLM spend is provider-reported token counts priced at the operator-configured "
            "LLM_COST_PER_1K_* rates (0.0 when unconfigured) — informational, never an "
            "invoiced total. Action and retrieval counts are measured, not estimated. "
            "The window these totals cover is disclosed by the API layer's 'retention' block."
        ),
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
        "investigation_duration and platform_cost are intentionally excluded "
        "(neither is a [0,1] rate; a cost is not a quality score)."
    )
    if not terms:
        return {"available": False, "reason": "No component metric was computable across the recorded investigations."}, formula

    overall = max(0.0, min(1.0, sum(terms) / len(terms)))
    return {"available": True, "score": round(overall, 4), "based_on": used}, formula
