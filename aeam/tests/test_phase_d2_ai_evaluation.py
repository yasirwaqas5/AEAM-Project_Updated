"""
aeam/tests/test_phase_d2_ai_evaluation.py

Enterprise AI Evaluation & Quality Engine (Phase D2) tests.

Three layers, matching this codebase's established test conventions:

1. AIEvaluationEngine's own scoring logic -- pure function over plain dicts
   (findings + an already-computed execution plan + optional explainability),
   no fakes needed (zero external dependencies, exactly like
   ExecutionPlanningEngine/ExplainabilityEngine).
2. Orchestrator wiring: the evaluation runs exactly once per incident
   lifecycle (inside finalize_incident(), guarded), strictly AFTER
   explainability, is appended as its own `type: "ai_evaluation"` findings
   entry, and NEVER alters findings / the execution plan / explainability /
   ActionAgent execution it evaluates.
3. ReportAgent: the "Enterprise AI Evaluation" section appears honestly in
   every state (never consulted / real evaluation with components).
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
from aeam.intelligence.ai_evaluation import AIEvaluationEngine
from aeam.intelligence.execution_planning import ExecutionPlanningEngine
from aeam.intelligence.explainability import ExplainabilityEngine
from aeam.memory.long_term import LongTermMemory
from aeam.memory.short_term import ShortTermMemory


# ===========================================================================
# 1. AIEvaluationEngine
# ===========================================================================

def _plan_explain_assess(findings, root_cause=None, confidence=None, requires_human=False,
                          runbook_recommended_actions=None, with_explainability=True):
    plan_engine = ExecutionPlanningEngine()
    plan = plan_engine.plan(
        event_type="DB_LATENCY", metric="db_latency_ms", severity="HIGH",
        current_value=950, expected_value=1900,
        findings=findings, root_cause=root_cause, confidence=confidence,
        requires_human=requires_human,
        runbook_recommended_actions=runbook_recommended_actions or ["Optimize indexes"],
    )
    explain = None
    if with_explainability:
        explain = ExplainabilityEngine().explain(findings=findings, execution_plan=plan, raw_confidence=confidence)
    engine = AIEvaluationEngine()
    result = engine.assess(findings=findings, execution_plan=plan, explainability=explain,
                            root_cause=root_cause, confidence=confidence)
    return plan, explain, result


def test_no_evidence_yields_low_but_computable_scores():
    """findings=[] means every SOURCE was never consulted (None, not 0.0),
    but a real execution plan WAS still computed (runbook baseline only),
    so plan-level metrics (coverage/conflict/recommendation/completeness)
    remain honestly computable."""
    plan, explain, result = _plan_explain_assess(findings=[])
    assert result["overall_score"] is not None
    assert result["component_scores"]["evidence_coverage"]["score"] == 0.0
    assert result["component_scores"]["policy_coverage"]["score"] is None  # never consulted, not zero
    assert "not consulted" in result["component_scores"]["policy_coverage"]["reason"]
    assert result["component_scores"]["memory_quality"]["score"] is None  # not consulted, not zero
    assert "not consulted" in result["component_scores"]["memory_quality"]["reason"]


def test_assess_never_raises_on_empty_execution_plan():
    engine = AIEvaluationEngine()
    result = engine.assess(findings=[], execution_plan={}, explainability=None, root_cause=None, confidence=None)
    assert result["overall_score"] is None
    assert "no execution plan" in result["quality_summary"].lower()


def test_evidence_coverage_reflects_signal_count():
    findings = [
        {"type": "policy", "data": {"matches": [
            {"policy_id": "p1", "business_rule": "R", "actions": ["x"], "match_reason": "m", "similarity": 0.8},
        ]}},
        {"type": "rag", "data": {"retrieved_count": 5, "validation_passed": True, "possible_causes": [
            {"cause": "X", "chunk_id": "c1", "confidence": 0.9},
        ]}},
    ]
    plan, explain, result = _plan_explain_assess(findings)
    assert result["component_scores"]["evidence_coverage"]["score"] == 2 / 5


def test_retrieval_quality_zero_when_nothing_retrieved():
    findings = [{"type": "rag", "data": {"retrieved_count": 0, "possible_causes": []}}]
    plan, explain, result = _plan_explain_assess(findings)
    assert result["component_scores"]["retrieval_quality"]["score"] == 0.0


def test_retrieval_quality_uses_real_confidence_and_validation():
    findings = [{"type": "rag", "data": {"retrieved_count": 5, "validation_passed": True, "possible_causes": [
        {"cause": "X", "chunk_id": "c1", "confidence": 0.8},
    ]}}]
    plan, explain, result = _plan_explain_assess(findings)
    # (1.0 has_evidence + 1.0 validated + 0.8 top_conf) / 3, rounded to 4dp by the engine
    assert result["component_scores"]["retrieval_quality"]["score"] == pytest.approx((1.0 + 1.0 + 0.8) / 3, abs=1e-4)


def test_memory_quality_penalised_for_mixed_outcomes():
    findings = [{"type": "memory", "data": {"matches": [
        {"incident_id": "i1", "similarity": 0.8, "root_cause": "X", "resolution_status": "RESOLVED"},
        {"incident_id": "i2", "similarity": 0.6, "root_cause": "X", "resolution_status": "ESCALATED"},
    ]}}]
    plan, explain, result = _plan_explain_assess(findings)
    base = (0.8 + 0.6) / 2
    assert result["component_scores"]["memory_quality"]["score"] == pytest.approx(base - 0.15)
    assert "Penalised" in result["component_scores"]["memory_quality"]["reason"]


def test_memory_quality_zero_when_no_matches_but_consulted():
    findings = [{"type": "memory", "data": {"matches": []}}]
    plan, explain, result = _plan_explain_assess(findings)
    assert result["component_scores"]["memory_quality"]["score"] == 0.0


def test_policy_coverage_scales_with_match_count():
    findings = [{"type": "policy", "data": {"matches": [
        {"policy_id": "p1", "business_rule": "A", "actions": ["x"], "match_reason": "m"},
        {"policy_id": "p2", "business_rule": "B", "actions": ["y"], "match_reason": "m"},
    ]}}]
    plan, explain, result = _plan_explain_assess(findings)
    assert result["component_scores"]["policy_coverage"]["score"] == 1.0  # min(2/2, 1.0)


def test_cross_dataset_coverage_zero_when_insufficient_data():
    findings = [{"type": "cross_dataset", "data": {"insufficient_data": True, "reason": "only 1 dataset"}}]
    plan, explain, result = _plan_explain_assess(findings)
    comp = result["component_scores"]["cross_dataset_coverage"]
    assert comp["score"] == 0.0
    assert comp["reason"] == "only 1 dataset"


def test_cross_dataset_coverage_reflects_found_ratio():
    findings = [{"type": "cross_dataset", "data": {
        "insufficient_data": False, "candidates_checked": 4,
        "supporting": [{"dataset_name": "A", "metric": "x"}], "strong_correlations": [], "contradicting": [],
    }}]
    plan, explain, result = _plan_explain_assess(findings)
    assert result["component_scores"]["cross_dataset_coverage"]["score"] == pytest.approx(0.25)


def test_adaptive_coverage_half_when_only_one_subanalysis_available():
    findings = [{"type": "adaptive", "data": {
        "adaptive_baseline_insufficient": None, "seasonality_insufficient": "too few points",
    }}]
    plan, explain, result = _plan_explain_assess(findings)
    assert result["component_scores"]["adaptive_detection_coverage"]["score"] == 0.5


def test_conflict_severity_scales_with_conflict_count():
    findings = [{"type": "memory", "data": {"matches": [
        {"incident_id": "i1", "root_cause": "X", "resolution_status": "RESOLVED"},
        {"incident_id": "i2", "root_cause": "X", "resolution_status": "ESCALATED"},
    ]}}]
    plan, explain, result = _plan_explain_assess(findings)
    n_conflicts = len(plan["evidence_conflicts"])
    assert result["component_scores"]["conflict_severity"]["score"] == pytest.approx(min(n_conflicts * 0.25, 1.0))


def test_conflict_severity_reuses_real_explainability_adjustment_when_available():
    findings = [{"type": "memory", "data": {"matches": [
        {"incident_id": "i1", "root_cause": "X", "resolution_status": "RESOLVED"},
        {"incident_id": "i2", "root_cause": "X", "resolution_status": "ESCALATED"},
    ]}}]
    plan, explain, result = _plan_explain_assess(findings, confidence=0.9, with_explainability=True)
    reason = result["component_scores"]["conflict_severity"]["reason"]
    adjustment = explain["confidence_breakdown"]["adjustment"]
    if adjustment is not None and adjustment < 0:
        assert str(abs(adjustment)) in reason


def test_evidence_diversity_accounts_for_distinct_chunk_sources():
    findings = [{"type": "rag", "data": {"retrieved_count": 2, "possible_causes": [
        {"cause": "X", "chunk_id": "c1", "confidence": 0.8},
    ], "retrieved_chunks": [
        {"chunk_id": "c1", "source": "doc_a.md"}, {"chunk_id": "c2", "source": "doc_b.md"},
    ]}}]
    plan, explain, result = _plan_explain_assess(findings)
    # 2 distinct sources / 2 chunks = 1.0 chunk_ratio; 1/5 source_ratio
    comp = result["component_scores"]["evidence_diversity"]
    assert comp["score"] == pytest.approx((0.2 + 1.0) / 2)


def test_recommendation_quality_ratio_of_evidence_backed_actions():
    findings = [{"type": "rag", "data": {"possible_causes": [{"cause": "X", "chunk_id": "c1", "confidence": 0.9}]}}]
    plan, explain, result = _plan_explain_assess(findings, runbook_recommended_actions=["Optimize indexes"])
    # 1 retrieval-backed + 1 runbook = 1/2 evidence-backed
    assert result["component_scores"]["recommendation_quality"]["score"] == pytest.approx(0.5)


def test_investigation_completeness_rewards_full_pipeline():
    findings = [
        {"type": "memory", "data": {"matches": []}},
        {"type": "policy", "data": {"matches": []}},
        {"type": "cross_dataset", "data": {"insufficient_data": True}},
        {"type": "adaptive", "data": {"adaptive_baseline_insufficient": "x", "seasonality_insufficient": "y"}},
        {"type": "rag", "data": {"retrieved_count": 0, "possible_causes": []}},
    ]
    plan, explain, result = _plan_explain_assess(findings, root_cause="X", confidence=0.5)
    comp = result["component_scores"]["investigation_completeness"]
    assert comp["score"] == 1.0  # all 5 consulted + root_cause + confidence + plan + explainability


def test_missing_evidence_reuses_explainability_when_available():
    findings = [{"type": "policy", "data": {"matches": []}}]
    plan, explain, result = _plan_explain_assess(findings, with_explainability=True)
    assert result["missing_evidence"] == explain["missing_evidence"]


def test_missing_evidence_derived_independently_when_explainability_absent():
    findings = [{"type": "policy", "data": {"matches": []}}]
    plan, explain, result = _plan_explain_assess(findings, with_explainability=False)
    assert explain is None
    assert any(m["source"] == "policy" for m in result["missing_evidence"])
    assert any(m["source"] == "memory" for m in result["missing_evidence"])


def test_strengths_and_weaknesses_derived_from_real_thresholds():
    findings = [{"type": "policy", "data": {"matches": [
        {"policy_id": "p1", "business_rule": "R", "actions": ["x"], "match_reason": "semantic", "similarity": 0.9},
    ]}}]
    plan, explain, result = _plan_explain_assess(findings)
    # cross_dataset and adaptive never consulted -> None scores, excluded from strengths/weaknesses
    assert not any("Cross-Dataset" in w for w in result["weaknesses"])


def test_improvement_opportunities_suggest_dataset_activation_when_cross_dataset_zero():
    findings = [{"type": "cross_dataset", "data": {"insufficient_data": True, "reason": "only 1 dataset"}}]
    plan, explain, result = _plan_explain_assess(findings)
    assert any("Activate additional related datasets" in o for o in result["improvement_opportunities"])


def test_overall_score_formula_is_disclosed():
    plan, explain, result = _plan_explain_assess(findings=[])
    assert "mean of" in result["overall_score_formula"]
    assert "conflict_severity" in result["overall_score_formula"]


def test_quality_summary_never_fabricates_when_unscoreable():
    engine = AIEvaluationEngine()
    result = engine.assess(findings=[], execution_plan={}, explainability=None, root_cause=None, confidence=None)
    assert "could not be scored" in result["quality_summary"]


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


def _build_orchestrator(execution_planning_engine=None, explainability_engine=None,
                         ai_evaluation_engine=None, action_agent=None):
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
        ai_evaluation_engine=ai_evaluation_engine,
    )
    return orchestrator, ltm, stm


def _event():
    return Event(
        event_id="1", event_type="DB_LATENCY", metric="latency_ms", severity="HIGH",
        current_value=900, expected_value=200, detection_methods=["rule"],
        timestamp="2026-01-01T00:00:00Z",
    )


def test_orchestrator_without_ai_evaluation_engine_unaffected():
    orchestrator, ltm, stm = _build_orchestrator(
        execution_planning_engine=ExecutionPlanningEngine(),
        explainability_engine=ExplainabilityEngine(),
        ai_evaluation_engine=None,
    )
    orchestrator.handle_event(_event())
    assert ltm.recorded is not None
    assert [f for f in ltm.recorded["findings"] if f.get("type") == "ai_evaluation"] == []
    assert any(f.get("type") == "execution_plan" for f in ltm.recorded["findings"])
    assert any(f.get("type") == "explainability" for f in ltm.recorded["findings"])


def test_ai_evaluation_requires_no_upstream_engines_to_avoid_crashing():
    orchestrator, ltm, stm = _build_orchestrator(
        execution_planning_engine=None, explainability_engine=None,
        ai_evaluation_engine=AIEvaluationEngine(),
    )
    orchestrator.handle_event(_event())  # must not raise
    assert ltm.recorded is not None


def test_orchestrator_appends_ai_evaluation_finding_after_explainability():
    orchestrator, ltm, stm = _build_orchestrator(
        execution_planning_engine=ExecutionPlanningEngine(),
        explainability_engine=ExplainabilityEngine(),
        ai_evaluation_engine=AIEvaluationEngine(),
    )
    orchestrator.handle_event(_event())

    findings = ltm.recorded["findings"]
    types_in_order = [f.get("type") for f in findings]
    assert "ai_evaluation" in types_in_order
    assert types_in_order.index("explainability") < types_in_order.index("ai_evaluation")
    assert types_in_order.index("ai_evaluation") < types_in_order.index("audit_summary")
    ai_eval_findings = [f for f in findings if f.get("type") == "ai_evaluation"]
    assert len(ai_eval_findings) == 1  # exactly once per incident lifecycle


def test_findings_execution_plan_and_explainability_unchanged_by_ai_evaluation_presence():
    """The core reuse guarantee: AI evaluation never mutates what it evaluates."""
    o1, ltm1, _ = _build_orchestrator(
        execution_planning_engine=ExecutionPlanningEngine(), explainability_engine=ExplainabilityEngine(),
        ai_evaluation_engine=None,
    )
    o1.handle_event(_event())
    plan1 = next(f["data"] for f in ltm1.recorded["findings"] if f.get("type") == "execution_plan")
    explain1 = next(f["data"] for f in ltm1.recorded["findings"] if f.get("type") == "explainability")

    o2, ltm2, _ = _build_orchestrator(
        execution_planning_engine=ExecutionPlanningEngine(), explainability_engine=ExplainabilityEngine(),
        ai_evaluation_engine=AIEvaluationEngine(),
    )
    o2.handle_event(_event())
    plan2 = next(f["data"] for f in ltm2.recorded["findings"] if f.get("type") == "execution_plan")
    explain2 = next(f["data"] for f in ltm2.recorded["findings"] if f.get("type") == "explainability")

    assert plan1 == plan2
    assert explain1 == explain2


def test_actionagent_execution_unaffected_by_ai_evaluation_presence():
    action_agent_without = FakeActionAgent()
    o1, _, _ = _build_orchestrator(
        execution_planning_engine=ExecutionPlanningEngine(), explainability_engine=ExplainabilityEngine(),
        ai_evaluation_engine=None, action_agent=action_agent_without,
    )
    o1.handle_event(_event())

    action_agent_with = FakeActionAgent()
    o2, _, _ = _build_orchestrator(
        execution_planning_engine=ExecutionPlanningEngine(), explainability_engine=ExplainabilityEngine(),
        ai_evaluation_engine=AIEvaluationEngine(), action_agent=action_agent_with,
    )
    o2.handle_event(_event())

    calls_without = [(c["action_type"], sorted(c["parameters"].keys())) for c in action_agent_without.calls]
    calls_with = [(c["action_type"], sorted(c["parameters"].keys())) for c in action_agent_with.calls]
    assert calls_without == calls_with


def test_broken_ai_evaluation_engine_does_not_break_finalization():
    class _Broken:
        def assess(self, **kwargs):
            raise RuntimeError("boom")

    orchestrator, ltm, stm = _build_orchestrator(
        execution_planning_engine=ExecutionPlanningEngine(), explainability_engine=ExplainabilityEngine(),
        ai_evaluation_engine=_Broken(),
    )
    orchestrator.handle_event(_event())  # must not raise
    assert ltm.recorded is not None
    ai_eval_findings = [f for f in ltm.recorded["findings"] if f.get("type") == "ai_evaluation"]
    assert ai_eval_findings[0]["data"]["overall_score"] is None  # honest degradation, not a crash


# ===========================================================================
# 3. ReportAgent -- "Enterprise AI Evaluation"
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


def test_report_states_ai_evaluation_not_consulted_honestly():
    report = _report_agent().generate_report(_memory_with_findings([]))
    assert "Enterprise AI Evaluation" in report["detailed_report"]
    assert "not consulted" in report["detailed_report"]


def test_report_renders_real_ai_evaluation_sections():
    findings = [{"type": "ai_evaluation", "data": {
        "overall_score": 0.65, "overall_score_formula": "mean of 9 components minus conflict penalty.",
        "component_scores": {
            "evidence_coverage": {"score": 0.6, "reason": "3/5 evidence sources produced a usable signal."},
            "memory_quality": {"score": None, "reason": "Enterprise Memory was not consulted for this investigation."},
        },
        "strengths": ["Retrieval Quality is strong (0.9)."],
        "weaknesses": ["Cross-Dataset Coverage is weak (0.0) -- insufficient data."],
        "missing_evidence": [{"source": "adaptive", "reason": "not consulted."}],
        "improvement_opportunities": ["Activate additional related datasets."],
        "quality_summary": "Overall investigation quality score: 0.65.",
    }}]
    report = _report_agent().generate_report(_memory_with_findings(findings))
    detailed = report["detailed_report"]
    assert "Enterprise AI Evaluation" in detailed
    assert "evidence_coverage" in detailed
    assert "not computable" in detailed  # memory_quality's None score rendered honestly
    assert "Retrieval Quality is strong (0.9)." in detailed
    assert "Activate additional related datasets." in detailed
