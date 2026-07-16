"""
aeam/tests/test_phase_d1_explainability.py

Enterprise Explainability Engine (Phase D1) tests.

Three layers, matching this codebase's established test conventions:

1. ExplainabilityEngine's own synthesis logic -- pure function over plain
   dicts (findings + an already-computed execution plan), no fakes needed
   (it has zero external dependencies by design, exactly like
   ExecutionPlanningEngine).
2. Orchestrator wiring: the explanation runs exactly once per incident
   lifecycle (inside finalize_incident(), guarded), strictly AFTER the
   execution_plan finding, is appended as its own `type: "explainability"`
   findings entry, and NEVER alters the execution plan / root_cause /
   confidence / ActionAgent execution it explains.
3. ReportAgent: the "Enterprise Explainability" section appears honestly in
   every state (never consulted / real explanation with conflicts).
"""

from __future__ import annotations

import pytest

from aeam.agents.orchestrator.decision_engine import DecisionEngine
from aeam.agents.orchestrator.evaluation_engine import EvaluationEngine
from aeam.agents.orchestrator.orchestrator import Orchestrator
from aeam.agents.orchestrator.state_machine import IncidentStateMachine
from aeam.agents.report.report_agent import ReportAgent
from aeam.config.settings import Settings
from aeam.core.event_bus import EventBus
from aeam.core.event_models import Event
from aeam.intelligence.execution_planning import ExecutionPlanningEngine
from aeam.intelligence.explainability import ExplainabilityEngine
from aeam.memory.long_term import LongTermMemory
from aeam.memory.short_term import ShortTermMemory


# ===========================================================================
# 1. ExplainabilityEngine
# ===========================================================================

def _plan_and_explain(findings, root_cause=None, confidence=None, requires_human=False,
                       runbook_recommended_actions=None):
    plan_engine = ExecutionPlanningEngine()
    plan = plan_engine.plan(
        event_type="DB_LATENCY", metric="db_latency_ms", severity="HIGH",
        current_value=950, expected_value=1900,
        findings=findings, root_cause=root_cause, confidence=confidence,
        requires_human=requires_human,
        runbook_recommended_actions=runbook_recommended_actions or ["Optimize indexes"],
    )
    engine = ExplainabilityEngine()
    result = engine.explain(findings=findings, execution_plan=plan, raw_confidence=confidence)
    return plan, result


def test_no_evidence_yields_honest_empty_explanation():
    plan, result = _plan_and_explain(findings=[])
    assert result["insufficient_evidence"] is True
    assert result["missing_evidence"]  # every source reported missing
    assert result["contradictions"] == []
    assert result["assumptions"] == []
    # Only the runbook baseline recommendation exists -- traced honestly.
    assert len(result["decision_graph"]) == 1
    assert result["decision_graph"][0]["source"] == "runbook"
    assert result["decision_graph"][0]["evidence_id"] is None


def test_decision_graph_attributes_policy_recommendation_to_policy_id():
    findings = [{"type": "policy", "data": {"matches": [
        {"policy_id": "p1", "business_rule": "DB Latency Escalation", "condition": "cond",
         "actions": ["Page on-call team"], "approval_required": True, "match_reason": "metric",
         "similarity": 0.8, "source_document": "policy.md"},
    ]}}]
    plan, result = _plan_and_explain(findings, confidence=0.9)
    policy_node = next(n for n in result["decision_graph"] if n["source"] == "policy")
    assert policy_node["evidence_id"] == "p1"
    assert policy_node["confidence_contribution"] == 0.8
    assert policy_node["report_section"] == "Matched Enterprise Policies"


def test_decision_graph_attributes_retrieval_recommendation_to_chunk_id():
    findings = [{"type": "rag", "data": {"possible_causes": [
        {"cause": "Missing indexes", "chunk_id": "c1", "confidence": 0.9},
    ]}}]
    plan, result = _plan_and_explain(findings)
    retrieval_node = next(n for n in result["decision_graph"] if n["source"] == "retrieval")
    assert retrieval_node["evidence_id"] == "c1"
    assert retrieval_node["confidence_contribution"] == 0.9


def test_decision_graph_attributes_cross_dataset_recommendation_to_dataset_metric():
    findings = [{"type": "cross_dataset", "data": {
        "insufficient_data": False, "supporting": [], "contradicting": [],
        "strong_correlations": [{"dataset_name": "Sales", "metric": "revenue", "correlation": 0.9, "overlapping_dates": 7}],
    }}]
    plan, result = _plan_and_explain(findings)
    cd_node = next(n for n in result["decision_graph"] if n["source"] == "cross_dataset")
    assert cd_node["evidence_id"] == "Sales:revenue"
    assert cd_node["confidence_contribution"] == 0.9


def test_runbook_recommendation_has_no_fabricated_evidence_id():
    plan, result = _plan_and_explain(findings=[])
    runbook_node = next(n for n in result["decision_graph"] if n["source"] == "runbook")
    assert runbook_node["evidence_id"] is None
    assert "No originating evidence item" in runbook_node["evidence_summary"]


def test_recommendation_trace_has_one_entry_per_recommendation():
    findings = [{"type": "rag", "data": {"possible_causes": [{"cause": "X", "chunk_id": "c1", "confidence": 0.9}]}}]
    plan, result = _plan_and_explain(findings)
    assert len(result["recommendation_trace"]) == len(plan["recommended_actions"])
    assert all(isinstance(t, str) and t for t in result["recommendation_trace"])


def test_evidence_graph_includes_every_collected_item_even_unused():
    """Memory never generates its own recommendation (C7 design), but its
    evidence must still appear in the evidence graph."""
    findings = [{"type": "memory", "data": {"matches": [
        {"incident_id": "i1", "similarity": 0.7, "root_cause": "X", "resolution_status": "RESOLVED"},
    ]}}]
    plan, result = _plan_and_explain(findings)
    assert result["evidence_graph"]["memory"] == [
        {"id": "i1", "similarity": 0.7, "root_cause": "X", "resolution_status": "RESOLVED"},
    ]
    # Memory contributed no recommendation -- confirmed absent from decision_graph.
    assert not any(n["source"] == "memory" for n in result["decision_graph"])


def test_confidence_breakdown_reports_real_delta_never_fabricated():
    findings = [{"type": "rag", "data": {"possible_causes": [
        {"cause": "A", "chunk_id": "c1", "confidence": 0.7},
        {"cause": "B", "chunk_id": "c2", "confidence": 0.68},
    ]}}]
    plan, result = _plan_and_explain(findings, confidence=0.95)
    cb = result["confidence_breakdown"]
    assert cb["raw_confidence"] == 0.95
    assert cb["plan_confidence"] == plan["confidence"]
    assert cb["adjustment"] == round(plan["confidence"] - 0.95, 4)
    assert cb["adjustment"] < 0
    assert "evidence conflict" in cb["adjustment_reason"]


def test_confidence_breakdown_no_adjustment_when_no_conflicts():
    findings = [{"type": "policy", "data": {"matches": [
        {"policy_id": "p1", "business_rule": "R", "actions": ["Do R"], "match_reason": "m", "similarity": 0.9},
    ]}}]
    plan, result = _plan_and_explain(findings, confidence=0.8)
    cb = result["confidence_breakdown"]
    assert cb["adjustment"] == 0.0
    assert "No adjustment" in cb["adjustment_reason"]


def test_confidence_breakdown_unavailable_when_raw_confidence_missing():
    plan, result = _plan_and_explain(findings=[], confidence=None)
    cb = result["confidence_breakdown"]
    assert cb["raw_confidence"] is None
    assert cb["adjustment"] is None
    assert "not available" in cb["adjustment_reason"]


def test_confidence_breakdown_per_source_never_invents_a_scalar_for_cross_dataset():
    findings = [{"type": "cross_dataset", "data": {
        "insufficient_data": False, "supporting": [{"dataset_name": "X", "metric": "y", "z_score": 3.1, "relation": "shared"}],
        "strong_correlations": [], "contradicting": [],
    }}]
    plan, result = _plan_and_explain(findings)
    cd_source = next(s for s in result["confidence_breakdown"]["per_source"] if s["source"] == "cross_dataset")
    assert cd_source["raw_value"] is None  # no correlation coefficient present, never fabricated
    assert "not a probability" in cd_source["raw_value_label"]


def test_confidence_breakdown_per_source_uses_real_policy_similarity():
    findings = [{"type": "policy", "data": {"matches": [
        {"policy_id": "p1", "business_rule": "R", "actions": ["x"], "match_reason": "m", "similarity": 0.77},
    ]}}]
    plan, result = _plan_and_explain(findings)
    policy_source = next(s for s in result["confidence_breakdown"]["per_source"] if s["source"] == "policy")
    assert policy_source["raw_value"] == 0.77


def test_evidence_contribution_lists_which_recommendations_each_source_influenced():
    findings = [{"type": "policy", "data": {"matches": [
        {"policy_id": "p1", "business_rule": "R", "actions": ["x"], "match_reason": "m"},
    ]}}]
    plan, result = _plan_and_explain(findings)
    policy_contrib = next(c for c in result["evidence_contribution"] if c["source"] == "policy")
    assert policy_contrib["recommendations_influenced"] == [1]
    assert policy_contrib["evidence_count"] == 1


def test_contradictions_are_passthrough_of_execution_plan_conflicts_never_recomputed():
    findings = [{"type": "memory", "data": {"matches": [
        {"incident_id": "i1", "root_cause": "X", "resolution_status": "RESOLVED"},
        {"incident_id": "i2", "root_cause": "X", "resolution_status": "ESCALATED"},
    ]}}]
    plan, result = _plan_and_explain(findings)
    assert result["contradictions"] == [
        {"between": c["between"], "description": c["description"]} for c in plan["evidence_conflicts"]
    ]


def test_missing_evidence_lists_not_consulted_and_no_signal_sources():
    findings = [{"type": "policy", "data": {"matches": []}}]  # consulted, no signal
    plan, result = _plan_and_explain(findings)
    by_source = {m["source"]: m["reason"] for m in result["missing_evidence"]}
    assert "consulted but produced no usable signal" in by_source["policy"]
    assert "not consulted" in by_source["memory"]


def test_assumption_detected_for_metric_only_policy_match():
    findings = [{"type": "policy", "data": {"matches": [
        {"policy_id": "p1", "business_rule": "R", "actions": ["x"], "match_reason": "metric"},
    ]}}]
    plan, result = _plan_and_explain(findings)
    assert any("metric-name match" in a["assumption"] for a in result["assumptions"])


def test_assumption_detected_for_insufficient_cross_dataset():
    findings = [{"type": "cross_dataset", "data": {"insufficient_data": True, "reason": "only 1 dataset"}}]
    plan, result = _plan_and_explain(findings)
    assert any("too few datasets" in a["assumption"] for a in result["assumptions"])


def test_no_assumptions_when_none_of_the_conditions_hold():
    findings = [{"type": "rag", "data": {"possible_causes": [{"cause": "X", "chunk_id": "c1", "confidence": 0.9}]}}]
    plan, result = _plan_and_explain(findings)
    assert result["assumptions"] == []


def test_lower_priority_justification_true_when_policy_absent():
    findings = [{"type": "rag", "data": {"possible_causes": [{"cause": "X", "chunk_id": "c1", "confidence": 0.9}]}}]
    plan, result = _plan_and_explain(findings)
    lpj = result["lower_priority_justification"]
    assert lpj["lower_priority_used"] is True
    assert lpj["highest_priority_available"] == "retrieval"
    assert "no higher-priority evidence" in lpj["reason"].lower()


def test_lower_priority_justification_false_when_policy_present():
    findings = [{"type": "policy", "data": {"matches": [
        {"policy_id": "p1", "business_rule": "R", "actions": ["x"], "match_reason": "m"},
    ]}}]
    plan, result = _plan_and_explain(findings)
    assert result["lower_priority_justification"]["lower_priority_used"] is False


def test_evidence_quality_passthrough_from_execution_plan():
    findings = [{"type": "policy", "data": {"matches": [
        {"policy_id": "p1", "business_rule": "R", "actions": ["x"], "match_reason": "m"},
    ]}}]
    plan, result = _plan_and_explain(findings)
    assert result["evidence_quality"] == plan["evidence_quality"]


def test_explain_never_raises_on_malformed_execution_plan():
    engine = ExplainabilityEngine()
    result = engine.explain(findings=[], execution_plan={}, raw_confidence=None)
    assert result["insufficient_evidence"] is False  # honestly reflects execution_plan's own (empty) claim
    assert result["decision_graph"] == []


# ===========================================================================
# 2. Orchestrator wiring
# ===========================================================================

class FakeLongTermMemory(LongTermMemory):
    def __init__(self):
        self.recorded = None

    def record_incident(self, payload):
        self.recorded = payload
        return payload.get("incident_id", "fake-id")


class FakeActionAgent:
    def __init__(self):
        self.calls: list[dict] = []

    def execute(self, action_type, parameters, incident_id):
        self.calls.append({"action_type": action_type, "parameters": parameters, "incident_id": incident_id})
        return {"status": "SUCCESS", "result": {}}


def _build_orchestrator(execution_planning_engine=None, explainability_engine=None, action_agent=None):
    settings = Settings(
        DATABASE_URL="sqlite:///:memory:", REDIS_URL="redis://localhost:6379/0",
        VECTOR_DB_URL="http://localhost", ENVIRONMENT="development", LLM_ENABLED=False,
    )
    bus = EventBus()
    decision = DecisionEngine(settings=settings)
    evaluation = EvaluationEngine(settings=settings)
    stm = ShortTermMemory()
    ltm = FakeLongTermMemory()
    sm = IncidentStateMachine()

    orchestrator = Orchestrator(
        event_bus=bus, decision_engine=decision, evaluation_engine=evaluation,
        short_term_memory=stm, long_term_memory=ltm, state_machine=sm, settings=settings,
        action_agent=action_agent,
        execution_planning_engine=execution_planning_engine,
        explainability_engine=explainability_engine,
    )
    return orchestrator, ltm, stm


def _event():
    return Event(
        event_id="1", event_type="DB_LATENCY", metric="latency_ms", severity="HIGH",
        current_value=900, expected_value=200, detection_methods=["rule"],
        timestamp="2026-01-01T00:00:00Z",
    )


def test_orchestrator_without_explainability_engine_unaffected():
    orchestrator, ltm, stm = _build_orchestrator(
        execution_planning_engine=ExecutionPlanningEngine(), explainability_engine=None,
    )
    orchestrator.handle_event(_event())
    assert ltm.recorded is not None
    assert [f for f in ltm.recorded["findings"] if f.get("type") == "explainability"] == []
    # Execution plan itself is completely unaffected by explainability's absence.
    assert any(f.get("type") == "execution_plan" for f in ltm.recorded["findings"])


def test_explainability_requires_no_execution_planner_to_avoid_crashing():
    """If explainability is wired but the planner is NOT, finalize_incident()
    must not crash -- explainability just reads an empty execution_plan."""
    orchestrator, ltm, stm = _build_orchestrator(
        execution_planning_engine=None, explainability_engine=ExplainabilityEngine(),
    )
    orchestrator.handle_event(_event())  # must not raise
    assert ltm.recorded is not None


def test_orchestrator_appends_explainability_finding_after_execution_plan():
    orchestrator, ltm, stm = _build_orchestrator(
        execution_planning_engine=ExecutionPlanningEngine(), explainability_engine=ExplainabilityEngine(),
    )
    orchestrator.handle_event(_event())

    findings = ltm.recorded["findings"]
    types_in_order = [f.get("type") for f in findings]
    assert "explainability" in types_in_order
    assert types_in_order.index("execution_plan") < types_in_order.index("explainability")
    assert types_in_order.index("explainability") < types_in_order.index("audit_summary")
    explainability_findings = [f for f in findings if f.get("type") == "explainability"]
    assert len(explainability_findings) == 1  # exactly once per incident lifecycle


def test_explainability_data_references_the_same_execution_plan_persisted():
    orchestrator, ltm, stm = _build_orchestrator(
        execution_planning_engine=ExecutionPlanningEngine(), explainability_engine=ExplainabilityEngine(),
    )
    orchestrator.handle_event(_event())
    findings = ltm.recorded["findings"]
    plan_data = next(f["data"] for f in findings if f.get("type") == "execution_plan")
    explain_data = next(f["data"] for f in findings if f.get("type") == "explainability")
    assert len(explain_data["decision_graph"]) == len(plan_data["recommended_actions"])


def test_execution_plan_content_unchanged_by_explainability_presence():
    """The core reuse guarantee: explainability never mutates the plan it explains."""
    o1, ltm1, _ = _build_orchestrator(execution_planning_engine=ExecutionPlanningEngine(), explainability_engine=None)
    o1.handle_event(_event())
    plan1 = next(f["data"] for f in ltm1.recorded["findings"] if f.get("type") == "execution_plan")

    o2, ltm2, _ = _build_orchestrator(execution_planning_engine=ExecutionPlanningEngine(), explainability_engine=ExplainabilityEngine())
    o2.handle_event(_event())
    plan2 = next(f["data"] for f in ltm2.recorded["findings"] if f.get("type") == "execution_plan")

    assert plan1 == plan2


def test_actionagent_execution_unaffected_by_explainability_presence():
    action_agent_without = FakeActionAgent()
    o1, _, _ = _build_orchestrator(execution_planning_engine=ExecutionPlanningEngine(), explainability_engine=None, action_agent=action_agent_without)
    o1.handle_event(_event())

    action_agent_with = FakeActionAgent()
    o2, _, _ = _build_orchestrator(execution_planning_engine=ExecutionPlanningEngine(), explainability_engine=ExplainabilityEngine(), action_agent=action_agent_with)
    o2.handle_event(_event())

    calls_without = [(c["action_type"], sorted(c["parameters"].keys())) for c in action_agent_without.calls]
    calls_with = [(c["action_type"], sorted(c["parameters"].keys())) for c in action_agent_with.calls]
    assert calls_without == calls_with


def test_broken_explainability_engine_does_not_break_finalization():
    class _Broken:
        def explain(self, **kwargs):
            raise RuntimeError("boom")

    orchestrator, ltm, stm = _build_orchestrator(
        execution_planning_engine=ExecutionPlanningEngine(), explainability_engine=_Broken(),
    )
    orchestrator.handle_event(_event())  # must not raise
    assert ltm.recorded is not None
    explain_findings = [f for f in ltm.recorded["findings"] if f.get("type") == "explainability"]
    assert explain_findings[0]["data"]["insufficient_evidence"] is True  # honest degradation, not a crash


# ===========================================================================
# 3. ReportAgent -- "Enterprise Explainability"
# ===========================================================================

def _memory_with_findings(findings):
    stm = ShortTermMemory()
    stm.initialize(task_type="anomaly_investigation", incident_id="inc-1")
    stm.set("event_type", "DB_LATENCY")
    stm.set("severity", "HIGH")
    stm.set("metric", "latency_ms")
    stm.set("findings", findings)
    stm.set("root_cause", "Inefficient queries")
    stm.set("confidence", 0.8)
    return stm


def _report_agent():
    return ReportAgent(
        settings=Settings(DATABASE_URL="sqlite:///:memory:", REDIS_URL="redis://localhost:6379/0", VECTOR_DB_URL="http://localhost", ENVIRONMENT="development", LLM_ENABLED=False),
        llm=None,
    )


def test_report_states_explainability_not_consulted_honestly():
    report = _report_agent().generate_report(_memory_with_findings([]))
    assert "Enterprise Explainability" in report["detailed_report"]
    assert "not consulted" in report["detailed_report"]


def test_report_renders_real_explainability_sections():
    findings = [{"type": "explainability", "data": {
        "decision_graph": [{"order": 1, "recommendation": "Page on-call team", "source": "policy",
                             "evidence_id": "p1", "evidence_summary": "Policy match", "confidence_contribution": 0.8,
                             "report_section": "Matched Enterprise Policies"}],
        "evidence_graph": {"policy": [{"id": "p1"}]},
        "recommendation_trace": ["Recommendation 1 ('Page on-call team') exists because policy evidence [p1]."],
        "confidence_breakdown": {"raw_confidence": 0.9, "plan_confidence": 0.5, "adjustment": -0.4,
                                  "adjustment_reason": "Confidence was reduced.", "per_source": [
                                      {"source": "policy", "consulted": True, "has_signal": True, "raw_value": 0.8, "raw_value_label": "top policy match similarity"},
                                  ]},
        "evidence_contribution": [],
        "contradictions": [{"between": ["retrieval", "retrieval"], "description": "Ambiguous causes."}],
        "missing_evidence": [{"source": "adaptive", "reason": "not consulted."}],
        "assumptions": [{"assumption": "Metric-name match assumed sufficient.", "based_on": "policy match_reason"}],
        "evidence_quality": "medium",
        "lower_priority_justification": {"lower_priority_used": False, "highest_priority_available": "policy", "reason": "policy used"},
        "insufficient_evidence": False,
    }}]
    report = _report_agent().generate_report(_memory_with_findings(findings))
    detailed = report["detailed_report"]
    assert "Enterprise Explainability" in detailed
    assert "Page on-call team" in detailed
    assert "Ambiguous causes." in detailed
    assert "Metric-name match assumed sufficient." in detailed
    assert "adaptive was not consulted" in detailed.lower() or "not consulted." in detailed
