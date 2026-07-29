"""
aeam/tests/test_phase_f3_policy.py

Phase F3 — Policy Compilation, Validation & the Policy Agent.

Acceptance criteria under test:

1. **A compiled rule is never enforced without a recorded human approval.**
   A PROPOSED (or REJECTED, or RETIRED) rule never appears in
   ``active_overrides()``; only APPROVED does. Asserted at every layer:
   the repository, the agent, the API, and end-to-end through
   ``RuleEngine.evaluate()``.
2. **The validator flags a deliberately contradictory policy pair before
   either can be adopted.** A fixture with two policies compiling to the
   same domain+rule_key at different values, and a second fixture with
   opposing ``approval_required`` on a shared metric.
3. **Tier-3 extraction recovers tabular policy conditions on a fixture
   that Tier-1/2 misses.**
4. **The deterministic decision path is byte-identical for any policy
   that has not been adopted as a rule** — proved by round-tripping a
   proposed-but-not-approved rule through the full agent→override→
   RuleEngine pipeline and comparing outputs bit for bit against a bare
   ``RuleEngine()``.

Infrastructure: in-process only — real SQLite, real FastAPI TestClient,
deterministic fixtures (TEST-3). No LLM, no network.
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aeam.agents.kpi.rule_engine import RuleEngine
from aeam.agents.policy.policy_agent import (
    NotCompilableError,
    PolicyAgent,
    PolicyAgentError,
    PolicyNotFoundError,
    RuleConflictError,
    RuleNotFoundError,
)
from aeam.api.knowledge import router as knowledge_router
from aeam.config.settings import Settings
from aeam.intelligence.policy_extraction import PolicyExtractor
from aeam.intelligence.policy_validator import PolicyValidator
from aeam.intelligence.rule_compiler import RuleCompiler
from aeam.integrations.database import DatabaseClient
from aeam.registry.models import CompiledRuleStatus, Policy, PolicyStatus
from aeam.registry.repositories import CompiledRuleRepository, PolicyRepository


# ===========================================================================
# 1. RuleCompiler — pure, deterministic
# ===========================================================================


def _policy(**overrides) -> dict:
    base = dict(policy_id="p1", related_metrics=[], condition="", threshold="", raw_text="")
    base.update(overrides)
    return base


@pytest.mark.parametrize(
    "related_metrics, condition, threshold, expected_key",
    [
        (["sales"], "sales drop > 30%", "30%", "daily_drop_percent"),
        (["sales"], "sales below $500", "$500", "absolute_minimum"),
        (["complaints"], "complaints increase by more than 20%", "20%", "daily_increase_threshold"),
        (["inventory"], "inventory critical below 10 units", "10", "critical_threshold"),
        (["inventory"], "inventory low stock below 50 units", "50", "low_stock_threshold"),
    ],
)
def test_compiler_recognises_every_known_rule_shape(related_metrics, condition, threshold, expected_key):
    candidate = RuleCompiler().compile(_policy(
        related_metrics=related_metrics, condition=condition, threshold=threshold,
    ))
    assert candidate.compilable is True
    assert candidate.rule_key == expected_key
    assert candidate.proposed_override == {candidate.domain: {expected_key: candidate.value}}


def test_compiler_refuses_a_domain_ruleengine_does_not_evaluate():
    candidate = RuleCompiler().compile(_policy(
        related_metrics=["latency_ms"], condition="latency > 500ms", threshold="500",
    ))
    assert candidate.compilable is False
    assert "curated RuleEngine domain" in candidate.reason


def test_compiler_refuses_when_no_number_is_present():
    candidate = RuleCompiler().compile(_policy(
        related_metrics=["sales"], condition="sales team must review weekly reports",
    ))
    assert candidate.compilable is False
    assert "fabricated enforcement" in candidate.reason


def test_compiler_refuses_ambiguous_sales_wording():
    """Names the sales domain and has a percent number, but the wording is
    neither a decrease nor an absolute floor — must not guess which known
    sales rule shape was meant."""
    candidate = RuleCompiler().compile(_policy(
        related_metrics=["sales"], condition="sales are within 4% of target", threshold="4%",
    ))
    assert candidate.compilable is False
    assert candidate.domain == "sales"


def test_compiler_never_raises_on_malformed_input():
    for bad in ({}, {"related_metrics": None}, {"related_metrics": ["sales"], "threshold": None}):
        candidate = RuleCompiler().compile(bad)
        assert candidate.compilable is False
        assert candidate.reason


def test_compiler_reads_dict_and_object_identically():
    """Duck-typed — the validator runs this over dicts; the agent runs it
    over real Policy rows. Both must compile the same way."""
    as_dict = _policy(related_metrics=["sales"], condition="sales drop > 30%", threshold="30%")
    as_model = Policy(related_metrics=["sales"], condition="sales drop > 30%", threshold="30%")

    from_dict = RuleCompiler().compile(as_dict)
    from_model = RuleCompiler().compile(as_model)

    assert from_dict.compilable == from_model.compilable
    assert from_dict.domain == from_model.domain
    assert from_dict.rule_key == from_model.rule_key
    assert from_dict.value == from_model.value


# ===========================================================================
# 2. PolicyValidator — flags a deliberately contradictory pair
# ===========================================================================


def test_validator_flags_a_deliberately_contradictory_threshold_pair():
    """The core F3 acceptance criterion: two policies compiling to the same
    domain+rule_key with DIFFERENT thresholds must be flagged before either
    can be adopted."""
    a = _policy(policy_id="a", related_metrics=["sales"], condition="sales drop > 30%", threshold="30%")
    b = _policy(policy_id="b", related_metrics=["sales"], condition="sales drop > 50%", threshold="50%")

    conflicts = PolicyValidator().validate([a, b])

    collisions = [c for c in conflicts if c.conflict_type == "threshold_collision"]
    assert len(collisions) == 1
    assert set(collisions[0].policy_ids) == {"a", "b"}
    assert collisions[0].domain == "sales"
    assert collisions[0].rule_key == "daily_drop_percent"


def test_validator_flags_a_deliberately_contradictory_action_pair():
    a = _policy(
        policy_id="a", related_metrics=["sales"], condition="sales drop > 30%",
        threshold="30%", approval_required=True,
    )
    b = _policy(
        policy_id="b", related_metrics=["sales"], condition="sales drop > 30%",
        threshold="30%", approval_required=False,
    )
    # give them distinct rule shapes so this isn't ALSO a threshold_collision
    b["condition"] = "sales below $100"
    b["threshold"] = "$100"

    conflicts = PolicyValidator().validate([a, b])

    actions = [c for c in conflicts if c.conflict_type == "action_conflict"]
    assert len(actions) == 1
    assert set(actions[0].policy_ids) == {"a", "b"}
    assert "approval_required" in actions[0].detail


def test_validator_flags_identical_duplicates_as_unreachable_not_colliding():
    a = _policy(policy_id="a", related_metrics=["sales"], condition="sales drop > 30%", threshold="30%")
    b = _policy(policy_id="b", related_metrics=["sales"], condition="sales drop > 30%", threshold="30%")

    conflicts = PolicyValidator().validate([a, b])

    assert len(conflicts) == 1
    assert conflicts[0].conflict_type == "unreachable"
    assert set(conflicts[0].policy_ids) == {"a", "b"}


def test_validator_reports_no_conflicts_on_a_consistent_corpus():
    a = _policy(policy_id="a", related_metrics=["sales"], condition="sales drop > 30%", threshold="30%")
    b = _policy(policy_id="b", related_metrics=["inventory"], condition="inventory critical below 10", threshold="10")

    assert PolicyValidator().validate([a, b]) == []


def test_validator_ignores_policies_that_do_not_compile():
    a = _policy(policy_id="a", related_metrics=["latency_ms"], condition="latency > 500ms", threshold="500")
    b = _policy(policy_id="b", related_metrics=["latency_ms"], condition="latency > 900ms", threshold="900")
    assert PolicyValidator().validate([a, b]) == []


def test_validator_action_conflict_requires_explicit_approval_fields():
    """A policy that never stated approval_required carries no signal to
    compare — must not be silently treated as False."""
    a = _policy(policy_id="a", related_metrics=["sales"], approval_required=True)
    b = _policy(policy_id="b", related_metrics=["sales"])  # unset
    assert PolicyValidator().validate([a, b]) == []


# ===========================================================================
# 3. RuleEngine overrides — additive, byte-identical when absent
# ===========================================================================


def test_ruleengine_default_is_byte_identical_to_before_overrides_existed():
    plain = RuleEngine()
    explicit_none = RuleEngine(overrides=None)
    explicit_empty = RuleEngine(overrides={})

    for engine in (explicit_none, explicit_empty):
        for domain, current, previous in (("sales", 100, 200), ("complaints", 50, 20), ("inventory", 5, 5)):
            assert engine.evaluate(domain, current, previous) == plain.evaluate(domain, current, previous)


def test_ruleengine_override_changes_only_the_named_key():
    baseline = RuleEngine()
    overridden = RuleEngine(overrides={"sales": {"daily_drop_percent": 1.0}})

    # A 5% drop on values far above the absolute floor (50000): the
    # default 15% threshold ignores it, but a 1% override trips it.
    default_result = baseline.evaluate("sales", 950000, 1000000)
    overridden_result = overridden.evaluate("sales", 950000, 1000000)

    assert default_result["rule_triggered"] is False
    assert overridden_result["rule_triggered"] is True
    assert overridden_result["rule_name"] == "sales.daily_drop_percent"

    # Every OTHER domain is untouched.
    assert overridden.evaluate("inventory", 100, 100) == baseline.evaluate("inventory", 100, 100)
    assert overridden.evaluate("complaints", 10, 10) == baseline.evaluate("complaints", 10, 10)


def test_ruleengine_loaded_domains_unaffected_by_overrides():
    assert RuleEngine(overrides={"sales": {"daily_drop_percent": 1.0}}).loaded_domains == RuleEngine().loaded_domains


# ===========================================================================
# 4. Tier-3 extraction recovers what Tier-1/2 misses
# ===========================================================================

_TABLE_FIXTURE = (
    "Severity | Threshold | Action\n"
    "Low | 10% | Notify on-call\n"
    "High | 25% | Escalate to manager\n"
    "Critical | 40% | Auto-resolve and page VP\n"
)


class _FakeLLM:
    def __init__(self, response: str):
        self.response = response
        self.prompts: list[str] = []

    def query(self, prompt, *, temperature=0.0, max_tokens=1000):
        self.prompts.append(prompt)
        return self.response


def test_tier3_recovers_every_table_row_that_tier1_2_merges():
    """The literal F3 acceptance criterion. Tier-1/2's flat prompt carries
    no table-aware instructions; fed a real table, a real LLM collapses it
    into far fewer policies than it contains rows — modeled here with a
    fixed response standing in for that realistic degraded output. Tier-3's
    table-aware prompt is asked to recover one policy per row; modeled with
    a response that actually does."""
    degraded = json.dumps({"policies": [
        {
            "raw_text": _TABLE_FIXTURE.strip(),
            "business_rule": "Escalate based on severity",
            "condition": "severity thresholds vary",
            "related_metrics": ["sales"],
        },
    ]})
    tier2 = PolicyExtractor(llm_service=_FakeLLM(degraded)).extract(_TABLE_FIXTURE)

    structured = json.dumps({"policies": [
        {"raw_text": "Low | 10% | Notify on-call", "business_rule": "Low severity notify",
         "condition": "drop > 10%", "threshold": "10%", "actions": ["Notify on-call"],
         "related_metrics": ["sales"], "table_group": "table_1", "table_row": 0},
        {"raw_text": "High | 25% | Escalate to manager", "business_rule": "High severity escalate",
         "condition": "drop > 25%", "threshold": "25%", "actions": ["Escalate to manager"],
         "related_metrics": ["sales"], "table_group": "table_1", "table_row": 1},
        {"raw_text": "Critical | 40% | Auto-resolve and page VP", "business_rule": "Critical severity page VP",
         "condition": "drop > 40%", "threshold": "40%", "actions": ["Auto-resolve", "Page VP"],
         "related_metrics": ["sales"], "table_group": "table_1", "table_row": 2},
    ]})
    tier3 = PolicyExtractor(llm_service=_FakeLLM(structured)).extract_tabular(_TABLE_FIXTURE)

    assert len(tier2) < 3, "Fixture no longer models a Tier-1/2 miss."
    assert len(tier3) == 3

    rows = sorted(tier3, key=lambda p: p["table_row"])
    assert [p["table_row"] for p in rows] == [0, 1, 2]
    assert {p["table_group"] for p in rows} == {"table_1"}
    assert [p["threshold"] for p in rows] == ["10%", "25%", "40%"]


def test_tier3_prompt_explicitly_instructs_row_separation():
    """Guards the mechanism, not just the fixture outcome: the prompt itself
    must tell the model not to merge rows, or a different fixture could
    regress silently."""
    fake = _FakeLLM(json.dumps({"policies": []}))
    PolicyExtractor(llm_service=fake).extract_tabular(_TABLE_FIXTURE)

    assert fake.prompts, "extract_tabular did not call the LLM."
    prompt = fake.prompts[0].lower()
    assert "table_group" in prompt
    assert "do not merge" in prompt or "own separate policy" in prompt


def test_tier3_policies_without_table_structure_omit_table_fields():
    fake = _FakeLLM(json.dumps({"policies": [
        {"raw_text": "Escalate if sales drop.", "business_rule": "x", "condition": "drop", "related_metrics": ["sales"]},
    ]}))
    result = PolicyExtractor(llm_service=fake).extract_tabular("plain sentence, no table")

    assert len(result) == 1
    assert "table_group" not in result[0]
    assert "table_row" not in result[0]


def test_extract_tier1_2_behaviour_is_unchanged_by_tier3_existing():
    """COMPAT-2: extract() must not have changed at all."""
    fake = _FakeLLM(json.dumps({"policies": [
        {"raw_text": "x", "business_rule": "y", "condition": "z", "related_metrics": ["sales"]},
    ]}))
    result = PolicyExtractor(llm_service=fake).extract("some document text")

    assert "table_group" not in result[0]
    assert "table_row" not in result[0]
    assert set(result[0]) <= {
        "raw_text", "business_rule", "condition", "threshold", "actions",
        "escalation_rule", "approval_required", "department", "role",
        "time_constraint", "priority", "related_metrics", "source_chunk",
    }


def test_tier3_never_raises_when_llm_fails():
    class _Broken:
        def query(self, *a, **k):
            raise RuntimeError("provider down")

    assert PolicyExtractor(llm_service=_Broken()).extract_tabular(_TABLE_FIXTURE) == []


# ===========================================================================
# 5. PolicyAgent — compilation-then-approval matrix
# ===========================================================================


@pytest.fixture()
def db(tmp_path):
    client = DatabaseClient(database_url=f"sqlite:///{(tmp_path / 'f3.db').as_posix()}")
    yield client
    client.dispose()


@pytest.fixture()
def agent(db):
    return PolicyAgent(
        policy_repository=PolicyRepository(db),
        compiled_rule_repository=CompiledRuleRepository(db),
    )


def _seed_policy(db, **overrides) -> str:
    base = dict(related_metrics=["sales"], condition="sales drop > 30%", threshold="30%",
                raw_text="If sales drop by more than 30%, escalate.")
    base.update(overrides)
    return PolicyRepository(db).create(Policy(**base))


def test_propose_rule_persists_as_proposed_and_changes_nothing(agent, db):
    policy_id = _seed_policy(db)

    result = agent.propose_rule(policy_id, created_by="alice")

    assert result["status"] == CompiledRuleStatus.PROPOSED
    assert agent.active_overrides() == {}, "A merely-proposed rule must never be enforced."


def test_propose_rule_refuses_an_uncompilable_policy(agent, db):
    policy_id = _seed_policy(db, related_metrics=["latency_ms"], condition="latency high", threshold="")
    with pytest.raises(NotCompilableError):
        agent.propose_rule(policy_id, created_by="alice")


def test_propose_rule_refuses_a_retired_policy(agent, db):
    policy_id = _seed_policy(db, status=PolicyStatus.RETIRED)
    with pytest.raises(RuleConflictError, match="retired"):
        agent.propose_rule(policy_id, created_by="alice")


def test_propose_rule_404s_on_a_missing_policy(agent):
    with pytest.raises(PolicyNotFoundError):
        agent.propose_rule("nonexistent", created_by="alice")


def test_approved_rule_is_the_only_one_enforced(agent, db):
    policy_id = _seed_policy(db)
    proposal = agent.propose_rule(policy_id, created_by="alice")

    agent.decide_rule(proposal["rule_id"], "approved", reviewer_id="bob")

    assert agent.active_overrides() == {"sales": {"daily_drop_percent": 30.0}}


def test_rejected_rule_is_never_enforced(agent, db):
    policy_id = _seed_policy(db)
    proposal = agent.propose_rule(policy_id, created_by="alice")

    agent.decide_rule(proposal["rule_id"], "rejected", reviewer_id="bob", note="too aggressive")

    assert agent.active_overrides() == {}


def test_retired_rule_is_no_longer_enforced(agent, db):
    policy_id = _seed_policy(db)
    proposal = agent.propose_rule(policy_id, created_by="alice")
    agent.decide_rule(proposal["rule_id"], "approved", reviewer_id="bob")
    assert agent.active_overrides() != {}

    agent.retire_rule(proposal["rule_id"], retired_by="carol", reason="threshold revised")

    assert agent.active_overrides() == {}


def test_a_decided_rule_cannot_be_redecided(agent, db):
    policy_id = _seed_policy(db)
    proposal = agent.propose_rule(policy_id, created_by="alice")
    agent.decide_rule(proposal["rule_id"], "approved", reviewer_id="bob")

    with pytest.raises(RuleConflictError):
        agent.decide_rule(proposal["rule_id"], "rejected", reviewer_id="mallory")


def test_only_an_approved_rule_can_be_retired(agent, db):
    policy_id = _seed_policy(db)
    proposal = agent.propose_rule(policy_id, created_by="alice")

    with pytest.raises(RuleConflictError):
        agent.retire_rule(proposal["rule_id"], retired_by="carol", reason="x")

    agent.decide_rule(proposal["rule_id"], "rejected", reviewer_id="bob")
    with pytest.raises(RuleConflictError):
        agent.retire_rule(proposal["rule_id"], retired_by="carol", reason="x")


def test_decide_rejects_an_unrecognised_verdict(agent, db):
    policy_id = _seed_policy(db)
    proposal = agent.propose_rule(policy_id, created_by="alice")
    with pytest.raises(ValueError, match="verdict"):
        agent.decide_rule(proposal["rule_id"], "maybe", reviewer_id="bob")


def test_decide_requires_a_reviewer_identity(agent, db):
    policy_id = _seed_policy(db)
    proposal = agent.propose_rule(policy_id, created_by="alice")
    with pytest.raises(ValueError, match="reviewer_id"):
        agent.decide_rule(proposal["rule_id"], "approved", reviewer_id="   ")


def test_decide_404s_on_a_missing_rule(agent):
    with pytest.raises(RuleNotFoundError):
        agent.decide_rule("nonexistent", "approved", reviewer_id="bob")


@pytest.mark.parametrize(
    "role, can_approve",
    [("admin", True), ("analyst", False), ("operator", False), ("auditor", False), ("readonly", False)],
)
def test_rbac_gates_rule_decisions_to_admin_only(role, can_approve):
    """The 'X role can/cannot decide a proposed rule' half of the
    compilation-then-approval matrix — verified against the SAME RBAC
    matrix the middleware enforces, not a re-implementation of it."""
    from aeam.security.rbac import RBAC

    rbac = RBAC()
    assert rbac.check_permission([role], "admin", "config") is can_approve


def test_agent_has_no_method_that_applies_an_override():
    """AGENT-5, enforced structurally: the absence of an apply/enact method
    IS the boundary."""
    forbidden = [
        name for name in dir(PolicyAgent)
        if name.startswith("apply") or name.startswith("enact") or name == "enforce_rule"
    ]
    assert not forbidden, f"PolicyAgent gained an apply path: {forbidden}"


def test_active_overrides_resolves_a_collision_deterministically(agent, db):
    """The validator should have caught this before both were approved, but
    nothing stops an operator from approving both anyway — the most
    recently decided one must win, not dict-iteration-order luck."""
    p1 = _seed_policy(db, condition="sales drop > 20%", threshold="20%")
    p2 = _seed_policy(db, condition="sales drop > 40%", threshold="40%")

    r1 = agent.propose_rule(p1, created_by="alice")
    r2 = agent.propose_rule(p2, created_by="alice")
    agent.decide_rule(r1["rule_id"], "approved", reviewer_id="bob")
    agent.decide_rule(r2["rule_id"], "approved", reviewer_id="bob")  # decided later

    assert agent.active_overrides() == {"sales": {"daily_drop_percent": 40.0}}


def test_validate_corpus_excludes_retired_policies(agent, db):
    _seed_policy(db, condition="sales drop > 30%", threshold="30%", status=PolicyStatus.RETIRED)
    _seed_policy(db, condition="sales drop > 50%", threshold="50%")

    # Only one matchable policy for this domain/key -> no collision.
    assert agent.validate_corpus() == []


def test_extract_tier3_requires_a_configured_extractor(agent):
    with pytest.raises(PolicyAgentError, match="Tier-3"):
        agent.extract_tier3("some text")


def test_extract_tier3_delegates_to_the_configured_extractor(db):
    fake = _FakeLLM(json.dumps({"policies": [
        {"raw_text": "x", "business_rule": "y", "condition": "z", "related_metrics": ["sales"],
         "table_group": "g1", "table_row": 0},
    ]}))
    agent = PolicyAgent(
        policy_repository=PolicyRepository(db),
        compiled_rule_repository=CompiledRuleRepository(db),
        extractor=PolicyExtractor(llm_service=fake),
    )
    result = agent.extract_tier3("some table text")
    assert len(result) == 1
    assert result[0]["table_group"] == "g1"


# ===========================================================================
# 6. Byte-identical decision path for any unadopted policy
# ===========================================================================


def test_unadopted_proposal_never_changes_ruleengine_output(agent, db):
    """The literal F3 acceptance criterion. A policy that has NOT been
    adopted as a rule — proposed, rejected, or never touched — must leave
    the deterministic decision path byte-identical."""
    policy_id = _seed_policy(db)
    proposal = agent.propose_rule(policy_id, created_by="alice")

    baseline = RuleEngine()
    overridden = RuleEngine(overrides=agent.active_overrides())

    for current, previous in ((950000, 1000000), (500000, 1000000), (1000000, 1000000)):
        assert overridden.evaluate("sales", current, previous) == baseline.evaluate("sales", current, previous)

    agent.decide_rule(proposal["rule_id"], "rejected", reviewer_id="bob")
    overridden_after_rejection = RuleEngine(overrides=agent.active_overrides())
    for current, previous in ((950000, 1000000), (500000, 1000000)):
        assert overridden_after_rejection.evaluate("sales", current, previous) == baseline.evaluate("sales", current, previous)


def test_adopted_rule_measurably_changes_ruleengine_output(agent, db):
    """The complement: an APPROVED rule must actually change behaviour, or
    the whole feature does nothing."""
    policy_id = _seed_policy(db, condition="sales drop > 1%", threshold="1%")
    proposal = agent.propose_rule(policy_id, created_by="alice")
    agent.decide_rule(proposal["rule_id"], "approved", reviewer_id="bob")

    baseline = RuleEngine()
    overridden = RuleEngine(overrides=agent.active_overrides())

    baseline_result = baseline.evaluate("sales", 950000, 1000000)
    overridden_result = overridden.evaluate("sales", 950000, 1000000)

    assert baseline_result["rule_triggered"] is False
    assert overridden_result["rule_triggered"] is True


# ===========================================================================
# 7. Knowledge Center API surface
# ===========================================================================


@pytest.fixture()
def client(db):
    class _Container:
        pass

    container = _Container()
    container.db = db
    container.settings = Settings(
        DATABASE_URL="sqlite:///:memory:", REDIS_URL="redis://localhost:6379/0",
        VECTOR_DB_URL="http://localhost", ENVIRONMENT="development",
    )
    container.audit_logger = None

    app = FastAPI()
    app.include_router(knowledge_router)
    app.state.container = container
    return TestClient(app)


def test_api_compile_preview_never_persists(client, db):
    policy_id = _seed_policy(db)

    body = client.get(f"/api/v1/knowledge/policies/{policy_id}/compile").json()

    assert body["compilable"] is True
    assert body["domain"] == "sales"
    assert CompiledRuleRepository(db).list_all() == [], "Preview must not persist."


def test_api_compile_preview_404s_on_missing_policy(client):
    assert client.get("/api/v1/knowledge/policies/nonexistent/compile").status_code == 404


def test_api_conflicts_endpoint_reports_the_contradictory_pair(client, db):
    _seed_policy(db, condition="sales drop > 30%", threshold="30%")
    _seed_policy(db, condition="sales drop > 50%", threshold="50%")

    body = client.get("/api/v1/knowledge/policies/conflicts").json()

    assert body["count"] == 1
    assert body["conflicts"][0]["conflict_type"] == "threshold_collision"
    assert "threshold_collision" in body["conflict_types"]


def test_api_full_propose_approve_cycle(client, db):
    policy_id = _seed_policy(db)

    proposal = client.post(
        f"/api/v1/knowledge/curate/rules/{policy_id}/propose", json={"actor_id": "alice"},
    ).json()
    assert proposal["status"] == "proposed"

    listed = client.get("/api/v1/knowledge/rules?status=proposed").json()
    assert listed["count"] == 1

    decided = client.post(
        f"/api/v1/knowledge/curate/rules/{proposal['rule_id']}/decide",
        json={"verdict": "approved", "reviewer_id": "bob"},
    ).json()
    assert decided["status"] == "approved"
    assert decided["effective"] == "next restart — the composition root loads adopted overrides at startup"

    retired = client.post(
        f"/api/v1/knowledge/curate/rules/{proposal['rule_id']}/retire",
        json={"reason": "revised elsewhere", "actor_id": "carol"},
    ).json()
    assert retired["status"] == "retired"


def test_api_propose_422s_on_uncompilable_policy(client, db):
    policy_id = _seed_policy(db, related_metrics=["latency_ms"], condition="x", threshold="")
    response = client.post(f"/api/v1/knowledge/curate/rules/{policy_id}/propose", json={})
    assert response.status_code == 422


def test_api_propose_409s_on_retired_policy(client, db):
    policy_id = _seed_policy(db, status=PolicyStatus.RETIRED)
    response = client.post(f"/api/v1/knowledge/curate/rules/{policy_id}/propose", json={})
    assert response.status_code == 409


def test_api_decide_400s_on_bad_verdict(client, db):
    policy_id = _seed_policy(db)
    proposal = client.post(f"/api/v1/knowledge/curate/rules/{policy_id}/propose", json={}).json()
    response = client.post(
        f"/api/v1/knowledge/curate/rules/{proposal['rule_id']}/decide",
        json={"verdict": "maybe", "reviewer_id": "bob"},
    )
    assert response.status_code == 400


def test_api_redecide_409s(client, db):
    policy_id = _seed_policy(db)
    proposal = client.post(f"/api/v1/knowledge/curate/rules/{policy_id}/propose", json={}).json()
    path = f"/api/v1/knowledge/curate/rules/{proposal['rule_id']}/decide"
    client.post(path, json={"verdict": "approved", "reviewer_id": "bob"})

    response = client.post(path, json={"verdict": "rejected", "reviewer_id": "mallory"})
    assert response.status_code == 409


def test_api_retire_409s_on_a_never_approved_rule(client, db):
    policy_id = _seed_policy(db)
    proposal = client.post(f"/api/v1/knowledge/curate/rules/{policy_id}/propose", json={}).json()
    response = client.post(
        f"/api/v1/knowledge/curate/rules/{proposal['rule_id']}/retire", json={"reason": "x"},
    )
    assert response.status_code == 409


def test_rule_list_serialises_native_datetime_timestamps():
    """Regression: a live run against real PostgreSQL (which returns
    TIMESTAMP columns as native datetime objects, unlike SQLite's strings)
    surfaced a 500 here — json.dumps cannot serialise a bare datetime.
    _compiled_rule_to_dict must normalise both driver shapes."""
    import datetime as dt
    import json as _json

    from aeam.api.knowledge import _compiled_rule_to_dict

    class _Row:
        rule_id = "r1"
        policy_id = "p1"
        domain = "sales"
        rule_key = "daily_drop_percent"
        comparison = "percent_drop_gt"
        value = 3.0
        rationale = "compiled"
        status = "approved"
        created_at = dt.datetime(2026, 7, 30, 12, 0, 0)
        created_by = "alice"
        reviewer_id = "bob"
        reviewer_roles = ["admin"]
        attribution_source = "jwt"
        note = ""
        decided_at = dt.datetime(2026, 7, 30, 12, 5, 0)
        retired_at = None
        retired_by = None
        retired_reason = None

    body = _compiled_rule_to_dict(_Row())
    _json.dumps(body)  # must not raise
    assert body["created_at"] == "2026-07-30 12:00:00"
    assert body["decided_at"] == "2026-07-30 12:05:00"
    assert body["retired_at"] is None


def test_api_curation_disabled_blocks_writes_not_reads(db):
    class _Container:
        pass

    container = _Container()
    container.db = db
    container.settings = Settings(
        DATABASE_URL="sqlite:///:memory:", REDIS_URL="redis://localhost:6379/0",
        VECTOR_DB_URL="http://localhost", ENVIRONMENT="development",
        KNOWLEDGE_CURATION_ENABLED=False,
    )
    container.audit_logger = None
    app = FastAPI()
    app.include_router(knowledge_router)
    app.state.container = container
    disabled_client = TestClient(app)

    policy_id = _seed_policy(db)

    assert disabled_client.get(f"/api/v1/knowledge/policies/{policy_id}/compile").status_code == 200
    assert disabled_client.get("/api/v1/knowledge/policies/conflicts").status_code == 200
    assert disabled_client.post(
        f"/api/v1/knowledge/curate/rules/{policy_id}/propose", json={},
    ).status_code == 503


# ===========================================================================
# 8. RBAC prefix separation (SEC-3) — the F2 lesson applied here too
# ===========================================================================


def test_read_and_write_rule_endpoints_never_share_an_rbac_prefix():
    from aeam.middleware.security_middleware import _ENDPOINT_RBAC_MAP

    def resolve(path):
        for prefix, resource, action in _ENDPOINT_RBAC_MAP:
            if path.startswith(prefix):
                return resource, action
        return None

    # Reads: broadly reachable (documents:search).
    assert resolve("/api/v1/knowledge/policies/abc/compile") == ("documents", "search")
    assert resolve("/api/v1/knowledge/policies/conflicts") == ("documents", "search")
    assert resolve("/api/v1/knowledge/rules") == ("documents", "search")

    # Writes: strictest tier (admin:config), via the EXISTING /curate entry.
    assert resolve("/api/v1/knowledge/curate/rules/abc/propose") == ("admin", "config")
    assert resolve("/api/v1/knowledge/curate/rules/abc/decide") == ("admin", "config")
    assert resolve("/api/v1/knowledge/curate/rules/abc/retire") == ("admin", "config")


def test_every_new_f3_route_is_rbac_mapped():
    from aeam.middleware.security_middleware import _ENDPOINT_RBAC_MAP

    for path in (
        "/api/v1/knowledge/policies/x/compile",
        "/api/v1/knowledge/policies/conflicts",
        "/api/v1/knowledge/rules",
        "/api/v1/knowledge/curate/rules/x/propose",
        "/api/v1/knowledge/curate/rules/x/decide",
        "/api/v1/knowledge/curate/rules/x/retire",
    ):
        assert any(path.startswith(prefix) for prefix, _, _ in _ENDPOINT_RBAC_MAP), path
