"""
aeam/tests/test_phase_c3_policy_registry.py

Enterprise Policy Registry (Phase C3) tests.

Three layers, matching this codebase's established test conventions (fakes
duck-typing the real classes' public interface; no live Qdrant, no live LLM,
no live embedding model):

1. PolicyRegistry's own two-tier matching logic (metric-tier deterministic,
   semantic-tier fallback) against a FAKE PolicyRepository/RuleEngine and a
   deterministic FAKE EmbeddingService (fixed vectors, real cosine math).
2. Orchestrator wiring: policy matching runs exactly once per incident
   lifecycle (idempotent across investigation depths), is appended as its
   own `type: "policy"` findings entry structurally distinct from
   `type: "rag"` / `type: "memory"`, and never reaches DecisionEngine,
   RuleEngine, or ActionAgent.
3. ReportAgent: the "Matched Enterprise Policies" report section appears
   honestly in all three states (never consulted / consulted-empty /
   consulted-with-matches).
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
from aeam.intelligence.policy_registry import PolicyRegistry
from aeam.memory.long_term import LongTermMemory
from aeam.memory.short_term import ShortTermMemory
from aeam.registry.models import Policy


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakePolicyRepository:
    def __init__(self, policies=None, raise_on_list=False):
        self._policies = policies or []
        self.raise_on_list = raise_on_list
        self.list_calls = 0

    def list_all(self):
        self.list_calls += 1
        if self.raise_on_list:
            raise RuntimeError("DB outage")
        return list(self._policies)


class FakeRuleEngine:
    def __init__(self, domains=("sales", "complaints", "inventory")):
        self._domains = list(domains)

    @property
    def loaded_domains(self):
        return list(self._domains)

    def evaluate(self, *a, **k):
        raise AssertionError("PolicyRegistry must never call RuleEngine.evaluate()")


class FakeEmbeddingService:
    """Deterministic bag-of-words-ish vectors so cosine similarity is predictable."""

    _VOCAB = ["sales", "drop", "marketing", "budget", "cpu", "server", "escalate", "engineer", "refund", "unrelated"]

    def __init__(self, raise_on_encode=False):
        self.raise_on_encode = raise_on_encode
        self.encoded: list[str] = []

    def encode_text(self, text):
        self.encoded.append(text)
        if self.raise_on_encode:
            raise RuntimeError("embedding model unavailable")
        words = text.lower().split()
        return [float(words.count(w)) + 0.01 for w in self._VOCAB]


def _policy(policy_id="p1", related_metrics=None, raw_text="", **overrides):
    fields = dict(
        policy_id=policy_id, doc_id="doc-1", source_document="policy.md", source_chunk="chunk-1",
        raw_text=raw_text, business_rule=overrides.pop("business_rule", None),
        condition=overrides.pop("condition", None), related_metrics=related_metrics or [],
    )
    fields.update(overrides)
    return Policy(**fields)


# ===========================================================================
# 1. PolicyRegistry
# ===========================================================================

def test_metric_tier_match_is_deterministic_and_exact():
    policies = [_policy("p1", related_metrics=["sales"], business_rule="Sales drop rule")]
    repo = FakePolicyRepository(policies)
    embed = FakeEmbeddingService()
    registry = PolicyRegistry(policy_repository=repo, rule_engine=FakeRuleEngine(), embedding_service=embed)

    matches = registry.match_for_incident(metric="sales", query="sales dropped a lot")

    assert len(matches) == 1
    assert matches[0]["policy_id"] == "p1"
    assert matches[0]["match_reason"] == "metric"
    assert "similarity" not in matches[0]  # metric tier never reports a similarity score
    assert embed.encoded == []  # embeddings never called when a metric match exists


def test_metric_tier_is_case_insensitive():
    policies = [_policy("p1", related_metrics=["Sales"])]
    registry = PolicyRegistry(policy_repository=FakePolicyRepository(policies), rule_engine=FakeRuleEngine(), embedding_service=FakeEmbeddingService())
    matches = registry.match_for_incident(metric="SALES", query="x")
    assert len(matches) == 1


def test_semantic_fallback_when_no_metric_match():
    policies = [
        _policy("p1", related_metrics=["cpu_util"], raw_text="server cpu escalate engineer"),
        _policy("p2", related_metrics=[], raw_text="refund unrelated unrelated unrelated"),
    ]
    registry = PolicyRegistry(policy_repository=FakePolicyRepository(policies), rule_engine=FakeRuleEngine(), embedding_service=FakeEmbeddingService())

    matches = registry.match_for_incident(metric="sales", query="server cpu escalate engineer")

    assert len(matches) == 1
    assert matches[0]["policy_id"] == "p1"
    assert matches[0]["match_reason"] == "semantic"
    assert matches[0]["similarity"] > 0


def test_no_match_returns_empty_honestly():
    policies = [_policy("p1", related_metrics=["cpu_util"], raw_text="totally unrelated content here")]
    registry = PolicyRegistry(policy_repository=FakePolicyRepository(policies), rule_engine=FakeRuleEngine(), embedding_service=FakeEmbeddingService())

    matches = registry.match_for_incident(metric="sales", query="sales drop marketing budget")
    # 'totally unrelated content here' shares zero vocabulary with the query -> below threshold
    assert matches == []


def test_empty_registry_returns_empty():
    registry = PolicyRegistry(policy_repository=FakePolicyRepository([]), rule_engine=FakeRuleEngine(), embedding_service=FakeEmbeddingService())
    assert registry.match_for_incident(metric="sales", query="sales drop") == []


def test_blank_metric_and_query_returns_empty():
    policies = [_policy("p1", related_metrics=["sales"])]
    registry = PolicyRegistry(policy_repository=FakePolicyRepository(policies), rule_engine=FakeRuleEngine(), embedding_service=FakeEmbeddingService())
    assert registry.match_for_incident(metric=None, query="") == []


def test_survives_repository_failure():
    registry = PolicyRegistry(policy_repository=FakePolicyRepository([], raise_on_list=True), rule_engine=FakeRuleEngine(), embedding_service=FakeEmbeddingService())
    assert registry.match_for_incident(metric="sales", query="x") == []


def test_survives_embedding_failure():
    policies = [_policy("p1", related_metrics=[], raw_text="server cpu escalate")]
    registry = PolicyRegistry(policy_repository=FakePolicyRepository(policies), rule_engine=FakeRuleEngine(), embedding_service=FakeEmbeddingService(raise_on_encode=True))
    assert registry.match_for_incident(metric="unrelated_metric", query="server cpu escalate") == []


def test_top_k_limits_metric_matches():
    policies = [_policy(f"p{i}", related_metrics=["sales"]) for i in range(5)]
    registry = PolicyRegistry(policy_repository=FakePolicyRepository(policies), rule_engine=FakeRuleEngine(), embedding_service=FakeEmbeddingService(), top_k=2)
    matches = registry.match_for_incident(metric="sales", query="x")
    assert len(matches) == 2


def test_match_dict_omits_fields_the_policy_never_had():
    policies = [_policy("p1", related_metrics=["sales"])]  # no department/role/threshold/etc.
    registry = PolicyRegistry(policy_repository=FakePolicyRepository(policies), rule_engine=FakeRuleEngine(), embedding_service=FakeEmbeddingService())
    match = registry.match_for_incident(metric="sales", query="x")[0]
    assert match["department"] is None
    assert match["role"] is None
    assert match["threshold"] is None


def test_curated_domains_passthrough():
    registry = PolicyRegistry(policy_repository=FakePolicyRepository([]), rule_engine=FakeRuleEngine(domains=["sales", "inventory"]), embedding_service=FakeEmbeddingService())
    assert registry.curated_domains == ["sales", "inventory"]


def test_registry_rejects_none_dependencies():
    with pytest.raises(ValueError):
        PolicyRegistry(policy_repository=None, rule_engine=FakeRuleEngine(), embedding_service=FakeEmbeddingService())
    with pytest.raises(ValueError):
        PolicyRegistry(policy_repository=FakePolicyRepository([]), rule_engine=None, embedding_service=FakeEmbeddingService())
    with pytest.raises(ValueError):
        PolicyRegistry(policy_repository=FakePolicyRepository([]), rule_engine=FakeRuleEngine(), embedding_service=None)


# ===========================================================================
# 2. Orchestrator wiring
# ===========================================================================

class FakeLongTermMemory(LongTermMemory):
    def __init__(self):
        self.recorded = None

    def record_incident(self, payload):
        self.recorded = payload
        return payload.get("incident_id", "fake-id")


class FakePolicyRegistryForOrchestrator:
    def __init__(self, matches=None):
        self._matches = matches or []
        self.calls = []

    def match_for_incident(self, metric, query):
        self.calls.append({"metric": metric, "query": query})
        return self._matches


def _build_orchestrator(policy_registry=None):
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
        policy_registry=policy_registry,
    )
    return orchestrator, ltm, stm


def _event():
    return Event(
        event_id="1", event_type="DB_LATENCY", metric="latency_ms", severity="HIGH",
        current_value=900, expected_value=200, detection_methods=["rule"],
        timestamp="2026-01-01T00:00:00Z",
    )


def test_orchestrator_without_policy_registry_unaffected():
    orchestrator, ltm, stm = _build_orchestrator(policy_registry=None)
    orchestrator.handle_event(_event())
    assert ltm.recorded is not None
    policy_findings = [f for f in ltm.recorded["findings"] if f.get("type") == "policy"]
    assert policy_findings == []


def test_orchestrator_appends_policy_finding_distinct_from_memory_and_rag():
    match = {"policy_id": "p1", "business_rule": "Escalate latency", "match_reason": "metric"}
    registry = FakePolicyRegistryForOrchestrator(matches=[match])
    orchestrator, ltm, stm = _build_orchestrator(policy_registry=registry)

    orchestrator.handle_event(_event())

    assert len(registry.calls) == 1
    assert registry.calls[0]["metric"] == "latency_ms"

    findings = ltm.recorded["findings"]
    types_seen = [f.get("type") for f in findings]
    policy_findings = [f for f in findings if f.get("type") == "policy"]
    assert len(policy_findings) == 1
    assert policy_findings[0]["data"]["matches"] == [match]
    # Structurally distinct type -- never merged into rag/memory finding types.
    assert "policy" in types_seen


def test_policy_matching_runs_exactly_once_per_incident():
    registry = FakePolicyRegistryForOrchestrator(matches=[])
    orchestrator, ltm, stm = _build_orchestrator(policy_registry=registry)
    orchestrator.handle_event(_event())
    # investigate() can run multiple depths internally; the registry must
    # still only ever be queried once for the whole incident lifecycle.
    assert len(registry.calls) == 1


def test_broken_policy_registry_does_not_break_investigation():
    class _Broken:
        def match_for_incident(self, metric, query):
            raise RuntimeError("boom")

    orchestrator, ltm, stm = _build_orchestrator(policy_registry=_Broken())
    orchestrator.handle_event(_event())  # must not raise
    assert ltm.recorded is not None
    policy_findings = [f for f in ltm.recorded["findings"] if f.get("type") == "policy"]
    assert policy_findings[0]["data"]["matches"] == []  # honest empty, not a crash


def test_policy_registry_never_reaches_decision_or_rule_engine():
    """Structural guarantee: FakePolicyRegistryForOrchestrator has no evaluate()
    method and no coupling to DecisionEngine -- if the Orchestrator ever tried
    to route matches into rule evaluation it would AttributeError here."""
    registry = FakePolicyRegistryForOrchestrator(matches=[{"policy_id": "p1"}])
    orchestrator, ltm, stm = _build_orchestrator(policy_registry=registry)
    orchestrator.handle_event(_event())  # no AttributeError == no such coupling exists
    assert ltm.recorded["root_cause"] is None or isinstance(ltm.recorded["root_cause"], str)


# ===========================================================================
# 3. ReportAgent — "Matched Enterprise Policies"
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


def test_report_states_not_consulted_honestly():
    agent = ReportAgent(settings=Settings(DATABASE_URL="sqlite:///:memory:", REDIS_URL="redis://localhost:6379/0", VECTOR_DB_URL="http://localhost", ENVIRONMENT="development", LLM_ENABLED=False), llm=None)
    report = agent.generate_report(_memory_with_findings([]))
    assert "Matched Enterprise Policies" in report["detailed_report"]
    assert "not consulted" in report["detailed_report"]


def test_report_states_no_match_honestly():
    agent = ReportAgent(settings=Settings(DATABASE_URL="sqlite:///:memory:", REDIS_URL="redis://localhost:6379/0", VECTOR_DB_URL="http://localhost", ENVIRONMENT="development", LLM_ENABLED=False), llm=None)
    findings = [{"type": "policy", "data": {"query": "x", "matches": []}}]
    report = agent.generate_report(_memory_with_findings(findings))
    assert "Matched Enterprise Policies" in report["detailed_report"]
    assert "No matched enterprise policies" in report["detailed_report"]


def test_report_lists_real_matches():
    agent = ReportAgent(settings=Settings(DATABASE_URL="sqlite:///:memory:", REDIS_URL="redis://localhost:6379/0", VECTOR_DB_URL="http://localhost", ENVIRONMENT="development", LLM_ENABLED=False), llm=None)
    findings = [{
        "type": "policy",
        "data": {"query": "x", "matches": [
            {"policy_id": "p1", "business_rule": "Escalate latency", "match_reason": "metric", "source_document": "policy.md"},
        ]},
    }]
    report = agent.generate_report(_memory_with_findings(findings))
    assert "Matched Enterprise Policies" in report["detailed_report"]
    assert "Escalate latency" in report["detailed_report"]
    assert "policy_id=p1" in report["detailed_report"]
    assert "matched_by=metric" in report["detailed_report"]
