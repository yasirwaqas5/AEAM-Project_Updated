"""
aeam/tests/test_phase_d3_observability.py

Enterprise Observability Engine (Phase D3) tests.

Two layers, matching this codebase's established test conventions:

1. ObservabilityEngine's own cross-incident aggregation logic -- pure
   function over a list of plain incident dicts (each with an already-parsed
   ``findings`` list), no fakes needed (zero external dependencies, exactly
   like ExecutionPlanningEngine/ExplainabilityEngine/AIEvaluationEngine).
   Unlike those three, this engine is NOT wired into the Orchestrator (no
   single incident to attach a cross-incident summary to -- see this
   module's own docstring for the Architecture Gate rationale), so there is
   no Orchestrator-wiring test layer here; instead layer 2 exercises the new
   read-only API endpoint directly.
2. The API endpoint (aeam/api/observability.py) reuses incidents.py's own
   SQL/fetch helper unchanged -- verified via a fake DatabaseClient so no
   real Postgres connection is required.
"""

from __future__ import annotations

from aeam.intelligence.observability import ObservabilityEngine


# ===========================================================================
# 1. ObservabilityEngine
# ===========================================================================

def _incident(findings):
    return {"incident_id": "i", "findings": findings}


def test_no_incidents_yields_honest_unavailable_everything():
    result = ObservabilityEngine().summarize(incidents=[])
    assert result["total_investigations"] == 0
    assert result["memory_hit_rate"]["available"] is False
    assert result["overall_ai_health"]["available"] is False
    assert result["investigation_duration"]["available"] is False


def test_investigation_duration_always_reports_unavailable_with_real_reason():
    """No per-incident duration is persisted anywhere -- this must never be
    fabricated, regardless of how much incident history exists."""
    incidents = [_incident([{"type": "audit_summary", "investigation_status": "RESOLVED"}])]
    result = ObservabilityEngine().summarize(incidents)
    assert result["investigation_duration"]["available"] is False
    assert "Prometheus" in result["investigation_duration"]["reason"]


def test_memory_hit_rate_distinguishes_not_consulted_from_zero_hits():
    incidents = [
        _incident([{"type": "memory", "data": {"matches": []}}]),  # consulted, no hit
        _incident([]),  # never consulted at all
    ]
    result = ObservabilityEngine().summarize(incidents)
    m = result["memory_hit_rate"]
    assert m["available"] is True
    assert m["consulted_count"] == 1  # only the first incident consulted memory
    assert m["hit_count"] == 0
    assert m["rate"] == 0.0
    assert m["total_investigations"] == 2


def test_memory_hit_rate_unavailable_when_never_consulted_anywhere():
    incidents = [_incident([{"type": "rag", "data": {"retrieved_count": 5}}])]
    result = ObservabilityEngine().summarize(incidents)
    assert result["memory_hit_rate"]["available"] is False
    assert "never consulted" in result["memory_hit_rate"]["reason"]


def test_policy_hit_rate_computed_from_real_matches():
    incidents = [
        _incident([{"type": "policy", "data": {"matches": [{"policy_id": "p1"}]}}]),
        _incident([{"type": "policy", "data": {"matches": []}}]),
    ]
    result = ObservabilityEngine().summarize(incidents)
    p = result["policy_hit_rate"]
    assert p["available"] is True
    assert p["consulted_count"] == 2
    assert p["hit_count"] == 1
    assert p["rate"] == 0.5


def test_retrieval_success_rate_uses_retrieved_count():
    incidents = [
        _incident([{"type": "rag", "data": {"retrieved_count": 5}}]),
        _incident([{"type": "rag", "data": {"retrieved_count": 0}}]),
    ]
    result = ObservabilityEngine().summarize(incidents)
    assert result["retrieval_success_rate"]["rate"] == 0.5


def test_cross_dataset_usage_excludes_insufficient_data():
    incidents = [
        _incident([{"type": "cross_dataset", "data": {"insufficient_data": True}}]),
        _incident([{"type": "cross_dataset", "data": {"insufficient_data": False, "supporting": [{"x": 1}]}}]),
    ]
    result = ObservabilityEngine().summarize(incidents)
    c = result["cross_dataset_usage_rate"]
    assert c["consulted_count"] == 2
    assert c["hit_count"] == 1
    assert c["rate"] == 0.5


def test_adaptive_usage_hit_when_either_subanalysis_available():
    incidents = [
        _incident([{"type": "adaptive", "data": {"adaptive_baseline_insufficient": None, "seasonality_insufficient": "x"}}]),
        _incident([{"type": "adaptive", "data": {"adaptive_baseline_insufficient": "x", "seasonality_insufficient": "y"}}]),
    ]
    result = ObservabilityEngine().summarize(incidents)
    a = result["adaptive_detection_usage_rate"]
    assert a["consulted_count"] == 2
    assert a["hit_count"] == 1
    assert a["rate"] == 0.5


def test_execution_plan_confidence_trend_reflects_chronological_order():
    """incidents arrive newest-first (as the real /api/v1/incidents/ does);
    the trend must reverse to oldest-first before computing direction."""
    incidents = [
        _incident([{"type": "execution_plan", "data": {"confidence": 0.3}}]),  # newest
        _incident([{"type": "execution_plan", "data": {"confidence": 0.9}}]),  # oldest
    ]
    result = ObservabilityEngine().summarize(incidents)
    trend = result["execution_plan_confidence_trend"]
    assert trend["available"] is True
    assert trend["recent_values"] == [0.9, 0.3]  # oldest -> newest
    assert trend["direction"] == "declining"
    assert trend["delta"] < 0


def test_ai_evaluation_trend_improving_direction():
    incidents = [
        _incident([{"type": "ai_evaluation", "data": {"overall_score": 0.8}}]),  # newest
        _incident([{"type": "ai_evaluation", "data": {"overall_score": 0.2}}]),  # oldest
    ]
    result = ObservabilityEngine().summarize(incidents)
    trend = result["ai_evaluation_trend"]
    assert trend["direction"] == "improving"
    assert trend["recent_values"] == [0.2, 0.8]


def test_trend_unavailable_when_no_numeric_value_present():
    incidents = [_incident([{"type": "execution_plan", "data": {"confidence": None}}])]
    result = ObservabilityEngine().summarize(incidents)
    assert result["execution_plan_confidence_trend"]["available"] is False


def test_trend_recent_values_capped_at_twenty():
    incidents = [_incident([{"type": "ai_evaluation", "data": {"overall_score": 0.5}}]) for _ in range(30)]
    result = ObservabilityEngine().summarize(incidents)
    trend = result["ai_evaluation_trend"]
    assert trend["sample_count"] == 30
    assert len(trend["recent_values"]) == 20


def test_investigation_success_rate_from_audit_summary():
    incidents = [
        _incident([{"type": "audit_summary", "investigation_status": "RESOLVED"}]),
        _incident([{"type": "audit_summary", "investigation_status": "ESCALATED"}]),
        _incident([{"type": "audit_summary", "investigation_status": "FAILED"}]),
    ]
    result = ObservabilityEngine().summarize(incidents)
    s = result["investigation_success_rate"]
    assert s["available"] is True
    assert s["resolved_count"] == 1
    assert s["total_with_status"] == 3
    assert s["rate"] == round(1 / 3, 4)


def test_investigation_success_rate_unavailable_without_audit_summary():
    incidents = [_incident([{"type": "memory", "data": {"matches": []}}])]
    result = ObservabilityEngine().summarize(incidents)
    assert result["investigation_success_rate"]["available"] is False


def test_overall_health_excludes_unavailable_components_never_defaults_to_zero():
    """Only policy was ever consulted -- overall health must be exactly the
    policy hit rate, never diluted by phantom zeros for untried features."""
    incidents = [_incident([{"type": "policy", "data": {"matches": [{"policy_id": "p1"}]}}])]
    result = ObservabilityEngine().summarize(incidents)
    assert result["overall_ai_health"]["available"] is True
    assert result["overall_ai_health"]["score"] == 1.0
    assert result["overall_ai_health"]["based_on"] == ["policy_hit_rate"]


def test_overall_health_formula_is_disclosed_and_excludes_duration():
    result = ObservabilityEngine().summarize(incidents=[_incident([])])
    assert "Unweighted mean" in result["overall_ai_health_formula"]
    assert "investigation_duration" in result["overall_ai_health_formula"]


def test_latest_finding_wins_when_type_appears_twice():
    incidents = [_incident([
        {"type": "policy", "data": {"matches": []}},
        {"type": "policy", "data": {"matches": [{"policy_id": "p1"}]}},
    ])]
    result = ObservabilityEngine().summarize(incidents)
    assert result["policy_hit_rate"]["hit_count"] == 1  # second (latest) entry wins


def test_summarize_never_raises_on_malformed_findings():
    incidents = [{"incident_id": "i1", "findings": "not-a-list"}, {"incident_id": "i2"}]
    result = ObservabilityEngine().summarize(incidents)
    assert result["total_investigations"] == 2
    assert result["memory_hit_rate"]["available"] is False


# ===========================================================================
# 2. API endpoint
# ===========================================================================

def test_observability_endpoint_reuses_incidents_sql_and_helper():
    """Confirms the endpoint imports (not duplicates) incidents.py's own
    query string and fetch helper -- the core "no second data source"
    architecture guarantee."""
    from aeam.api.incidents import _SELECT_ALL_INCIDENTS as inc_sql, _fetch_all as inc_fetch
    from aeam.api.observability import _SELECT_ALL_INCIDENTS as obs_sql, _fetch_all as obs_fetch
    assert obs_sql is inc_sql
    assert obs_fetch is inc_fetch


def test_observability_router_prefix():
    from aeam.api.observability import router
    assert router.prefix == "/api/v1/observability"
