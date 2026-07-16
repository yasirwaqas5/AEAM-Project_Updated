"""
aeam/tests/test_phase_c7_execution_planning.py

Enterprise Action Planning Engine (Phase C7) tests.

Three layers, matching this codebase's established test conventions:

1. ExecutionPlanningEngine's own synthesis logic -- pure function over plain
   dicts, no fakes needed (it has zero external dependencies by design).
2. Orchestrator wiring: the plan runs exactly once per incident lifecycle
   (inside finalize_incident(), guarded), is appended as its own
   `type: "execution_plan"` findings entry distinct from every other C-phase
   finding, and ActionAgent's own execution is provably unaffected by its
   presence or absence.
3. ReportAgent: the "Enterprise Execution Plan" section appears honestly in
   every state (never consulted / insufficient evidence / real plan with
   conflicts).
"""

from __future__ import annotations

from unittest.mock import MagicMock

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
from aeam.memory.long_term import LongTermMemory
from aeam.memory.short_term import ShortTermMemory


# ===========================================================================
# 1. ExecutionPlanningEngine
# ===========================================================================

def _plan(engine, **overrides):
    kwargs = dict(
        event_type="DB_LATENCY", metric="db_latency_ms", severity="HIGH",
        current_value=950, expected_value=1900,
        findings=[], root_cause=None, confidence=None, requires_human=False,
        runbook_recommended_actions=["Optimize indexes"],
    )
    kwargs.update(overrides)
    return engine.plan(**kwargs)


def test_no_evidence_is_insufficient_and_never_fabricates():
    engine = ExecutionPlanningEngine()
    result = _plan(engine)
    assert result["insufficient_evidence"] is True
    assert result["evidence_quality"] == "insufficient"
    assert result["confidence"] == 0.0
    assert result["human_approval_required"] is True
    # Only the deterministic runbook baseline is present -- nothing invented.
    assert len(result["recommended_actions"]) == 1
    assert result["recommended_actions"][0]["source"] == "runbook"


def test_policy_match_generates_action_with_approval_classification():
    engine = ExecutionPlanningEngine()
    findings = [{"type": "policy", "data": {"query": "q", "matches": [
        {"policy_id": "p1", "business_rule": "DB Latency Escalation", "condition": "latency > 5s",
         "actions": "Page on-call DB team", "approval_required": True, "department": "DB Eng",
         "match_reason": "semantic", "similarity": 0.8, "source_document": "policy.md"},
    ]}}]
    result = _plan(engine, findings=findings, confidence=0.9)
    policy_actions = [a for a in result["recommended_actions"] if a["source"] == "policy"]
    assert len(policy_actions) == 1
    assert policy_actions[0]["action"] == "Page on-call DB team"
    assert policy_actions[0]["classification"] == "requires_human_approval"
    assert policy_actions[0]["order"] == 1  # highest priority, ordered first
    assert any(e["source"] == "policy" for e in result["supporting_evidence"])


def test_policy_actions_list_is_rendered_as_a_single_string():
    """Policy.actions is a list[str] (aeam/registry/models.py) -- the plan's
    recommended_actions[].action must always be a plain string, never a
    raw list leaking through unjoined."""
    engine = ExecutionPlanningEngine()
    findings = [{"type": "policy", "data": {"matches": [
        {"policy_id": "p1", "business_rule": "Escalation", "condition": "cond",
         "actions": ["page the on-call team", "requires manager approval before rollback"],
         "approval_required": True, "match_reason": "metric"},
    ]}}]
    result = _plan(engine, findings=findings)
    action = result["recommended_actions"][0]["action"]
    assert isinstance(action, str)
    assert "page the on-call team" in action
    assert "requires manager approval before rollback" in action


def test_policy_without_approval_required_is_execute_immediately():
    engine = ExecutionPlanningEngine()
    findings = [{"type": "policy", "data": {"matches": [
        {"policy_id": "p1", "business_rule": "Rule", "actions": "Notify team",
         "approval_required": False, "match_reason": "keyword"},
    ]}}]
    result = _plan(engine, findings=findings)
    policy_actions = [a for a in result["recommended_actions"] if a["source"] == "policy"]
    assert policy_actions[0]["classification"] == "execute_immediately"


def test_policy_matches_with_differing_approval_flags_is_a_conflict():
    engine = ExecutionPlanningEngine()
    findings = [{"type": "policy", "data": {"matches": [
        {"policy_id": "p1", "business_rule": "A", "actions": "Do A", "approval_required": True, "match_reason": "m"},
        {"policy_id": "p2", "business_rule": "B", "actions": "Do B", "approval_required": False, "match_reason": "m"},
    ]}}]
    result = _plan(engine, findings=findings)
    assert any("disagree on whether human approval" in c["description"] for c in result["evidence_conflicts"])


def test_memory_never_generates_its_own_action_only_corroborates():
    engine = ExecutionPlanningEngine()
    findings = [{"type": "memory", "data": {"matches": [
        {"incident_id": "i1", "similarity": 0.8, "root_cause": "X", "resolution_status": "RESOLVED"},
    ]}}]
    result = _plan(engine, findings=findings)
    assert not any(a["source"] == "memory" for a in result["recommended_actions"])
    assert any(e["source"] == "memory" for e in result["supporting_evidence"])
    assert result["insufficient_evidence"] is False


def test_memory_mixed_outcomes_flagged_as_conflict():
    engine = ExecutionPlanningEngine()
    findings = [{"type": "memory", "data": {"matches": [
        {"incident_id": "i1", "root_cause": "X", "resolution_status": "RESOLVED"},
        {"incident_id": "i2", "root_cause": "X", "resolution_status": "ESCALATED"},
    ]}}]
    result = _plan(engine, findings=findings)
    assert any("inconsistent outcomes" in c["description"] for c in result["evidence_conflicts"])


def test_cross_dataset_strong_correlation_generates_informational_action():
    engine = ExecutionPlanningEngine()
    findings = [{"type": "cross_dataset", "data": {
        "insufficient_data": False,
        "supporting": [], "contradicting": [],
        "strong_correlations": [{"dataset_name": "Sales", "metric": "revenue", "correlation": 0.9, "overlapping_dates": 7}],
    }}]
    result = _plan(engine, findings=findings)
    cd_actions = [a for a in result["recommended_actions"] if a["source"] == "cross_dataset"]
    assert len(cd_actions) == 1
    assert cd_actions[0]["classification"] == "informational_only"
    assert "Sales" in cd_actions[0]["action"]


def test_cross_dataset_contradicting_is_a_conflict():
    engine = ExecutionPlanningEngine()
    findings = [{"type": "cross_dataset", "data": {
        "insufficient_data": False, "supporting": [], "strong_correlations": [],
        "contradicting": [{"dataset_name": "Inventory", "metric": "stock", "relation": "shared_dimension"}],
    }}]
    result = _plan(engine, findings=findings)
    assert any("remained statistically normal" in c["description"] for c in result["evidence_conflicts"])


def test_cross_dataset_insufficient_data_contributes_no_signal():
    engine = ExecutionPlanningEngine()
    findings = [{"type": "cross_dataset", "data": {"insufficient_data": True, "reason": "only 1 dataset"}}]
    result = _plan(engine, findings=findings)
    assert result["sources_with_signal"]["cross_dataset"] is False
    assert result["insufficient_evidence"] is True  # no other source either


def test_adaptive_seasonality_generates_informational_action():
    engine = ExecutionPlanningEngine()
    findings = [{"type": "adaptive", "data": {
        "seasonality": {"detected": True, "strength": 0.8, "highest_weekday": "Saturday", "lowest_weekday": "Tuesday"},
        "combined_signal": True, "corroborating_signals": ["adaptive_baseline"],
    }}]
    result = _plan(engine, findings=findings)
    adaptive_actions = [a for a in result["recommended_actions"] if a["source"] == "adaptive"]
    assert len(adaptive_actions) == 1
    assert "Saturday" in adaptive_actions[0]["action"]
    assert adaptive_actions[0]["classification"] == "informational_only"
    assert any(e["source"] == "adaptive" for e in result["supporting_evidence"])


def test_retrieval_top_cause_generates_informational_action():
    engine = ExecutionPlanningEngine()
    findings = [{"type": "rag", "data": {"possible_causes": [
        {"cause": "Missing indexes", "chunk_id": "c1", "confidence": 0.9},
    ]}}]
    result = _plan(engine, findings=findings)
    retrieval_actions = [a for a in result["recommended_actions"] if a["source"] == "retrieval"]
    assert len(retrieval_actions) == 1
    assert "Missing indexes" in retrieval_actions[0]["action"]


def test_retrieval_ambiguous_causes_flagged_as_conflict():
    engine = ExecutionPlanningEngine()
    findings = [{"type": "rag", "data": {"possible_causes": [
        {"cause": "Missing indexes", "chunk_id": "c1", "confidence": 0.7},
        {"cause": "Network partition", "chunk_id": "c2", "confidence": 0.65},
    ]}}]
    result = _plan(engine, findings=findings)
    assert any("not clearly distinguished" in c["description"] for c in result["evidence_conflicts"])


def test_retrieval_clear_winner_is_not_flagged_as_ambiguous():
    engine = ExecutionPlanningEngine()
    findings = [{"type": "rag", "data": {"possible_causes": [
        {"cause": "Missing indexes", "chunk_id": "c1", "confidence": 0.95},
        {"cause": "Network partition", "chunk_id": "c2", "confidence": 0.2},
    ]}}]
    result = _plan(engine, findings=findings)
    assert not any("not clearly distinguished" in c["description"] for c in result["evidence_conflicts"])


def test_actions_ordered_by_source_priority():
    engine = ExecutionPlanningEngine()
    findings = [
        {"type": "rag", "data": {"possible_causes": [{"cause": "C", "chunk_id": "c1", "confidence": 0.9}]}},
        {"type": "policy", "data": {"matches": [
            {"policy_id": "p1", "business_rule": "R", "actions": "Do R", "approval_required": False, "match_reason": "m"},
        ]}},
        {"type": "adaptive", "data": {"seasonality": {"detected": True, "strength": 0.6, "highest_weekday": "Mon", "lowest_weekday": "Fri"}}},
    ]
    result = _plan(engine, findings=findings)
    sources_in_order = [a["source"] for a in result["recommended_actions"]]
    assert sources_in_order.index("policy") < sources_in_order.index("adaptive") < sources_in_order.index("retrieval")
    assert sources_in_order[-1] == "runbook"
    orders = [a["order"] for a in result["recommended_actions"]]
    assert orders == list(range(1, len(orders) + 1))


def test_confidence_capped_when_conflicts_present():
    engine = ExecutionPlanningEngine()
    findings = [{"type": "rag", "data": {"possible_causes": [
        {"cause": "A", "chunk_id": "c1", "confidence": 0.7},
        {"cause": "B", "chunk_id": "c2", "confidence": 0.68},
    ]}}]
    result = _plan(engine, findings=findings, confidence=0.95)
    assert result["evidence_conflicts"]
    assert result["confidence"] <= 0.5


def test_confidence_reused_honestly_when_no_conflicts():
    engine = ExecutionPlanningEngine()
    findings = [{"type": "policy", "data": {"matches": [
        {"policy_id": "p1", "business_rule": "R", "actions": "Do R", "approval_required": False, "match_reason": "m"},
    ]}}]
    result = _plan(engine, findings=findings, confidence=0.73)
    assert result["evidence_conflicts"] == []
    assert result["confidence"] == 0.73  # exact same value already established -- never reinvented


def test_explanation_states_lower_priority_override_when_no_policy():
    engine = ExecutionPlanningEngine()
    findings = [{"type": "rag", "data": {"possible_causes": [{"cause": "C", "chunk_id": "c1", "confidence": 0.9}]}}]
    result = _plan(engine, findings=findings)
    assert "no enterprise policy matched" in result["explanation"].lower()


def test_deviation_percent_included_in_business_risk():
    engine = ExecutionPlanningEngine()
    result = _plan(engine, current_value=950, expected_value=1900)
    assert "50.0%" in result["business_risk_assessment"]


def test_evidence_quality_scales_with_signal_count():
    engine = ExecutionPlanningEngine()
    one_source = [{"type": "policy", "data": {"matches": [
        {"policy_id": "p1", "business_rule": "R", "actions": "Do R", "match_reason": "m"},
    ]}}]
    two_sources = one_source + [{"type": "rag", "data": {"possible_causes": [{"cause": "C", "chunk_id": "c1", "confidence": 0.9}]}}]
    assert _plan(engine, findings=one_source)["evidence_quality"] == "low"
    assert _plan(engine, findings=two_sources)["evidence_quality"] == "medium"


def test_latest_finding_wins_when_type_appears_twice():
    """Mirrors ReportAgent's own _format_xxx scan convention: last entry of a
    given type wins (a later re-run in the SAME incident, if it ever happened,
    is authoritative)."""
    engine = ExecutionPlanningEngine()
    findings = [
        {"type": "policy", "data": {"matches": []}},
        {"type": "policy", "data": {"matches": [
            {"policy_id": "p1", "business_rule": "R", "actions": "Do R", "match_reason": "m"},
        ]}},
    ]
    result = _plan(engine, findings=findings)
    assert any(a["source"] == "policy" for a in result["recommended_actions"])


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


def _build_orchestrator(execution_planning_engine=None, action_agent=None):
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
    )
    return orchestrator, ltm, stm


def _event():
    return Event(
        event_id="1", event_type="DB_LATENCY", metric="latency_ms", severity="HIGH",
        current_value=900, expected_value=200, detection_methods=["rule"],
        timestamp="2026-01-01T00:00:00Z",
    )


def test_orchestrator_without_execution_planner_unaffected():
    orchestrator, ltm, stm = _build_orchestrator(execution_planning_engine=None)
    orchestrator.handle_event(_event())
    assert ltm.recorded is not None
    assert [f for f in ltm.recorded["findings"] if f.get("type") == "execution_plan"] == []


def test_orchestrator_appends_execution_plan_finding_distinctly():
    engine = ExecutionPlanningEngine()
    orchestrator, ltm, stm = _build_orchestrator(execution_planning_engine=engine)

    orchestrator.handle_event(_event())

    findings = ltm.recorded["findings"]
    types_seen = {f.get("type") for f in findings}
    assert "execution_plan" in types_seen
    plan_findings = [f for f in findings if f.get("type") == "execution_plan"]
    assert len(plan_findings) == 1  # exactly once per incident lifecycle
    assert "recommended_actions" in plan_findings[0]["data"]


def test_execution_plan_appears_before_audit_summary_in_findings_order():
    """Confirms the plan is computed BEFORE the action loop / audit summary,
    i.e. it is genuinely the final reasoning stage preceding execution."""
    engine = ExecutionPlanningEngine()
    orchestrator, ltm, stm = _build_orchestrator(execution_planning_engine=engine)
    orchestrator.handle_event(_event())
    findings = ltm.recorded["findings"]
    types_in_order = [f.get("type") for f in findings]
    assert types_in_order.index("execution_plan") < types_in_order.index("audit_summary")


def test_actionagent_execution_unaffected_by_execution_planner_presence():
    """The core reuse guarantee: ActionAgent.execute() is called identically
    whether or not the planner is wired -- the plan is advisory-only and
    never feeds into runbook/action selection."""
    action_agent_without = FakeActionAgent()
    orchestrator1, _, _ = _build_orchestrator(execution_planning_engine=None, action_agent=action_agent_without)
    orchestrator1.handle_event(_event())

    action_agent_with = FakeActionAgent()
    orchestrator2, _, _ = _build_orchestrator(execution_planning_engine=ExecutionPlanningEngine(), action_agent=action_agent_with)
    orchestrator2.handle_event(_event())

    calls_without = [(c["action_type"], sorted(c["parameters"].keys())) for c in action_agent_without.calls]
    calls_with = [(c["action_type"], sorted(c["parameters"].keys())) for c in action_agent_with.calls]
    assert calls_without == calls_with


def test_broken_execution_planner_does_not_break_finalization():
    class _Broken:
        def plan(self, **kwargs):
            raise RuntimeError("boom")

    orchestrator, ltm, stm = _build_orchestrator(execution_planning_engine=_Broken())
    orchestrator.handle_event(_event())  # must not raise
    assert ltm.recorded is not None
    plan_findings = [f for f in ltm.recorded["findings"] if f.get("type") == "execution_plan"]
    assert plan_findings[0]["data"]["insufficient_evidence"] is True  # honest degradation, not a crash


# ===========================================================================
# 3. ReportAgent -- "Enterprise Execution Plan"
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


def test_report_states_execution_plan_not_consulted_honestly():
    report = _report_agent().generate_report(_memory_with_findings([]))
    assert "Enterprise Execution Plan" in report["detailed_report"]
    assert "not consulted" in report["detailed_report"]


def test_report_states_insufficient_evidence_honestly():
    findings = [{"type": "execution_plan", "data": {
        "executive_summary": "Insufficient evidence.", "recommended_actions": [],
        "order_rationale": None, "supporting_evidence": [], "business_risk_assessment": None,
        "expected_impact": None, "confidence": 0.0, "evidence_quality": "insufficient",
        "evidence_conflicts": [], "human_approval_required": True,
        "explanation": "No evidence.", "insufficient_evidence": True,
    }}]
    report = _report_agent().generate_report(_memory_with_findings(findings))
    assert "Enterprise Execution Plan" in report["detailed_report"]
    assert "Insufficient evidence" in report["detailed_report"]


def test_report_lists_real_recommendations_and_conflicts():
    findings = [{"type": "execution_plan", "data": {
        "executive_summary": "HIGH DB_LATENCY incident.",
        "recommended_actions": [
            {"order": 1, "action": "Page on-call team", "source": "policy",
             "rationale": "Matched policy X.", "classification": "requires_human_approval"},
        ],
        "order_rationale": "Ordered by priority.",
        "supporting_evidence": [{"source": "policy", "summary": "Policy X matched."}],
        "business_risk_assessment": "HIGH severity.", "expected_impact": "Confined to metric.",
        "confidence": 0.6, "evidence_quality": "medium",
        "evidence_conflicts": [{"between": ["retrieval", "retrieval"], "description": "Ambiguous causes."}],
        "human_approval_required": True, "explanation": "Because policy matched.",
        "insufficient_evidence": False,
    }}]
    report = _report_agent().generate_report(_memory_with_findings(findings))
    detailed = report["detailed_report"]
    assert "Enterprise Execution Plan" in detailed
    assert "Page on-call team" in detailed
    assert "requires_human_approval" in detailed
    assert "Ambiguous causes." in detailed
    assert "Human approval required: True" in detailed
