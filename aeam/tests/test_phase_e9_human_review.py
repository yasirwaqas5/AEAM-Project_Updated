"""
aeam/tests/test_phase_e9_human_review.py

Phase E9 — Human-in-the-Loop Enforcement (AGENT-5, SEC-7, EXPL-5/6,
COMPAT-1/5/6, MEM-2).

Acceptance criteria under test:

1. **Zero gated actions until approval.** An approval-required incident
   executes no gated runbook step; notifications still dispatch, because
   informing humans is never gated.
2. **Approval executes exactly the recorded steps, idempotently.** The
   released calls are the ones the Orchestrator built and withheld — same
   steps, same parameters, same order — and a repeated approval never runs
   them twice.
3. **Verdicts survive restart and carry the acting principal.** Rows are
   read back through a fresh repository instance, and every verdict records
   how its principal was established.
4. **Tiered approval.** A multi-tier chain executes only after EVERY tier
   approves, in order; a rejection at any tier halts the chain and records
   which tier and principal rejected. A single-tier chain behaves exactly
   like today's one-step approval.
5. **COMPAT-1.** Incidents that predate this phase (no approval row) render
   unchanged, and a non-approval incident behaves exactly as before.
6. **Rollback.** ``HUMAN_APPROVAL_ENFORCED=false`` reproduces pre-E9
   behaviour byte-for-byte.
7. **RBAC + migration.** The review endpoints resolve to the intended
   grants, and both schema paths (migration and startup DDL) carry the new
   tables.

Infrastructure: real SQLite via the real ``DatabaseClient`` and the real
repositories — no mocked persistence, since "survives restart" is one of
the claims being tested. ActionAgent is a recording fake (the real one
would call Slack/Jira); the contract asserted is that the SAME call the
Orchestrator would have made is the one that eventually runs.
"""

from __future__ import annotations

import uuid

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect

from aeam.agents.orchestrator.decision_engine import DecisionEngine
from aeam.agents.orchestrator.evaluation_engine import EvaluationEngine
from aeam.agents.orchestrator.orchestrator import Orchestrator
from aeam.agents.orchestrator.runbooks import NEVER_GATED_STEPS, get_runbook, is_gated_step
from aeam.api.review import router as review_router
from aeam.config.settings import Settings
from aeam.core.event_bus import EventBus
from aeam.core.event_models import Event
from aeam.governance.human_review import (
    ApprovalConflictError,
    ApprovalNotFoundError,
    HumanReviewService,
    InvalidVerdictError,
    parse_chain_overrides,
    parse_tier_chain,
    policy_driven_chain,
    resolve_approval_chain,
)
from aeam.integrations.database import DatabaseClient
from aeam.memory.long_term import LongTermMemory
from aeam.middleware.security_middleware import SecurityMiddleware
from aeam.registry.models import ApprovalStatus, AttributionSource, Verdict
from aeam.registry.repositories import (
    IncidentApprovalRepository,
    ReviewVerdictRepository,
)
from aeam.security.rbac import RBAC


# ===========================================================================
# Fixtures & fakes
# ===========================================================================

def _settings(**overrides) -> Settings:
    base = dict(
        DATABASE_URL="sqlite:///:memory:",
        REDIS_URL="redis://localhost:6379/0",
        VECTOR_DB_URL="http://localhost",
        ENVIRONMENT="development",
        LLM_ENABLED=False,
    )
    base.update(overrides)
    return Settings(**base)


class _RecordingActionAgent:
    """
    Stands in for the real ActionAgent, recording every execute() call.

    Deliberately duck-typed on the exact ``execute(action_type, parameters,
    incident_id)`` contract — the phase's claim is that the UNCHANGED agent
    contract is used, so a fake that accepted anything else would prove
    nothing.
    """

    def __init__(self, fail: set[str] | None = None) -> None:
        self.calls: list[dict] = []
        self._fail = fail or set()

    def execute(self, action_type: str, parameters: dict, incident_id: str) -> dict:
        self.calls.append({
            "action_type": action_type,
            "parameters": parameters,
            "incident_id": incident_id,
        })
        if action_type in self._fail:
            return {"status": "FAILED", "failure_reason": "simulated failure"}
        return {"status": "SUCCESS", "result": {}}

    @property
    def executed_types(self) -> list[str]:
        return [c["action_type"] for c in self.calls]


class _ApprovalRequiredPlanner:
    """
    ExecutionPlanningEngine stand-in returning a plan whose
    ``human_approval_required`` is fixed by the test.
    """

    def __init__(self, required: bool) -> None:
        self._required = required

    def plan(self, **kwargs) -> dict:
        return {
            "executive_summary": "test plan",
            "recommended_actions": [],
            "order_rationale": None,
            "supporting_evidence": [],
            "business_risk_assessment": None,
            "expected_impact": None,
            "confidence": 0.9,
            "evidence_quality": "high",
            "evidence_conflicts": [],
            "human_approval_required": self._required,
            "explanation": "test",
            "insufficient_evidence": False,
            "sources_consulted": {},
            "sources_with_signal": {},
        }


class _CapturingLongTermMemory(LongTermMemory):
    """Records the finalize payload and hands back a stable incident id."""

    def __init__(self, incident_id: str = "db-incident-1") -> None:
        self.recorded: dict | None = None
        self._incident_id = incident_id

    def record_incident(self, payload):  # type: ignore[override]
        self.recorded = payload
        return self._incident_id


@pytest.fixture()
def db(tmp_path):
    """A real DatabaseClient over a file-backed SQLite database."""
    client = DatabaseClient(database_url=f"sqlite:///{tmp_path / 'e9.db'}")
    yield client
    client.dispose()


@pytest.fixture()
def service(db):
    return HumanReviewService(
        approval_repo=IncidentApprovalRepository(db),
        verdict_repo=ReviewVerdictRepository(db),
        settings=_settings(HUMAN_APPROVAL_ENFORCED=True),
        action_agent=_RecordingActionAgent(),
    )


def _record(service: HumanReviewService, *, incident_id="inc-1", tiers=("reviewer",),
            steps=(("diagnostics", {"incident_id": "inv-1", "kind": "diagnostics"}),)):
    return service.record_pending_approval(
        approval_id=str(uuid.uuid4()),
        incident_id=incident_id,
        investigation_id="inv-1",
        event_type="DB_LATENCY",
        metric="db_latency",
        severity="HIGH",
        required_tiers=list(tiers),
        pending_actions=[{"step": s, "params": p} for s, p in steps],
    )


def _event(event_type: str = "DB_LATENCY") -> Event:
    return Event(
        event_id="evt-1",
        event_type=event_type,
        metric="db_latency",
        severity="HIGH",
        current_value=900.0,
        expected_value=200.0,
        detection_methods=["rule"],
        timestamp="2026-07-26T00:00:00Z",
    )


def _orchestrator(settings, ltm, action_agent, review_service, approval_required=True):
    return Orchestrator(
        event_bus=EventBus(),
        decision_engine=DecisionEngine(settings=settings),
        evaluation_engine=EvaluationEngine(settings=settings),
        long_term_memory=ltm,
        settings=settings,
        action_agent=action_agent,
        execution_planning_engine=_ApprovalRequiredPlanner(approval_required),
        human_review_service=review_service,
    )


# ===========================================================================
# 1. Runbook step classification — what is gated, what never is
# ===========================================================================

def test_notification_steps_are_never_gated():
    """Informing humans is never withheld — otherwise the gate would
    suppress the very message telling a reviewer approval is waiting."""
    for step in ("slack", "jira", "marketing_slack", "email"):
        assert step in NEVER_GATED_STEPS
        assert is_gated_step(step) is False


def test_state_changing_steps_are_gated():
    for step in ("diagnostics", "monitoring", "webhook", "sheets"):
        assert is_gated_step(step) is True


def test_unknown_step_defaults_to_gated():
    """Conservative direction: an unclassified step waits for a human."""
    assert is_gated_step("some_future_destructive_step") is True


def test_every_runbook_still_notifies_when_gated():
    """Every shipped runbook contains at least one never-gated step, so a
    gated incident always reaches a human."""
    for event_type in ("DB_LATENCY", "SALES_DROP", "CPU_HIGH", "UNKNOWN_TYPE"):
        plan = get_runbook(event_type)["action_plan"]
        assert any(not is_gated_step(s) for s in plan), event_type


# ===========================================================================
# 2. Approval-chain resolution
# ===========================================================================

def test_parse_tier_chain_preserves_order_and_dedupes():
    assert parse_tier_chain("analyst, manager , analyst") == ["analyst", "manager"]
    assert parse_tier_chain("") == []
    assert parse_tier_chain(None) == []


def test_parse_chain_overrides_is_case_insensitive_on_severity():
    parsed = parse_chain_overrides("CRITICAL:analyst,manager;high:analyst")
    assert parsed["CRITICAL"] == ["analyst", "manager"]
    assert parsed["HIGH"] == ["analyst"]


def test_parse_chain_overrides_skips_malformed_without_raising():
    """A typo in one override must not stop the platform from starting."""
    parsed = parse_chain_overrides("garbage;CRITICAL:analyst;:;EMPTY:")
    assert parsed == {"CRITICAL": ["analyst"]}


def test_default_chain_is_single_tier():
    """COMPAT-1/6: the default is one tier — today's one-step approval."""
    s = _settings()
    assert resolve_approval_chain("HIGH", None, s) == ["reviewer"]


def test_severity_override_produces_ordered_chain():
    s = _settings(APPROVAL_TIER_CHAIN_OVERRIDES="CRITICAL:analyst,manager,risk")
    assert resolve_approval_chain("critical", None, s) == ["analyst", "manager", "risk"]
    assert resolve_approval_chain("LOW", None, s) == ["reviewer"]


def test_policy_named_roles_outrank_configuration():
    findings = [{"type": "policy", "data": {"matches": [
        {"approval_required": True, "role": "Risk Lead"},
        {"approval_required": True, "role": "CFO"},
    ]}}]
    s = _settings(APPROVAL_TIER_CHAIN_OVERRIDES="HIGH:analyst")
    assert resolve_approval_chain("HIGH", findings, s) == ["Risk Lead", "CFO"]


def test_policy_without_a_role_contributes_nothing():
    """No honest way to guess who was meant — fall through to config, but
    the gate still applies."""
    findings = [{"type": "policy", "data": {"matches": [
        {"approval_required": True, "role": None},
        {"approval_required": False, "role": "Ignored"},
    ]}}]
    assert policy_driven_chain(findings) == []
    assert resolve_approval_chain("HIGH", findings, _settings()) == ["reviewer"]


# ===========================================================================
# 3. Verdict workflow — single tier
# ===========================================================================

def test_approval_executes_exactly_the_recorded_steps(db, service):
    approval = _record(service, steps=(
        ("diagnostics", {"incident_id": "inv-1", "kind": "diagnostics"}),
        ("monitoring", {"incident_id": "inv-1", "metric": "db_latency"}),
    ))
    agent = service._action

    assert agent.calls == []  # nothing ran at recording time

    out = service.submit_verdict(
        incident_id=approval.incident_id, verdict=Verdict.APPROVED,
        reviewer_id="alice", reviewer_roles=["admin"],
        attribution_source=AttributionSource.JWT,
    )

    assert out["status"] == ApprovalStatus.APPROVED
    assert out["executed_actions"] == ["diagnostics", "monitoring"]
    assert agent.executed_types == ["diagnostics", "monitoring"]
    # Parameters are the ones recorded — not re-derived.
    assert agent.calls[0]["parameters"]["kind"] == "diagnostics"
    assert agent.calls[0]["incident_id"] == "inv-1"


def test_double_approval_is_idempotent_and_never_re_executes(db, service):
    approval = _record(service)
    agent = service._action

    first = service.submit_verdict(
        incident_id=approval.incident_id, verdict=Verdict.APPROVED,
        reviewer_id="alice", attribution_source=AttributionSource.JWT,
    )
    second = service.submit_verdict(
        incident_id=approval.incident_id, verdict=Verdict.APPROVED,
        reviewer_id="alice", attribution_source=AttributionSource.JWT,
    )

    assert first["idempotent"] is False
    assert second["idempotent"] is True
    assert len(agent.calls) == 1
    assert second["executed_actions"] == first["executed_actions"]


def test_approval_by_a_different_principal_after_completion_is_a_conflict(db, service):
    approval = _record(service)
    service.submit_verdict(
        incident_id=approval.incident_id, verdict=Verdict.APPROVED,
        reviewer_id="alice", attribution_source=AttributionSource.JWT,
    )
    with pytest.raises(ApprovalConflictError):
        service.submit_verdict(
            incident_id=approval.incident_id, verdict=Verdict.REJECTED,
            reviewer_id="bob", attribution_source=AttributionSource.JWT,
        )


def test_rejection_executes_nothing_and_records_who_halted_it(db, service):
    approval = _record(service)
    agent = service._action

    out = service.submit_verdict(
        incident_id=approval.incident_id, verdict=Verdict.REJECTED,
        reviewer_id="bob", reviewer_roles=["admin"],
        attribution_source=AttributionSource.JWT, note="Not during freeze.",
    )

    assert out["status"] == ApprovalStatus.REJECTED
    assert agent.calls == []
    assert out["executed_actions"] == []
    assert out["skipped_actions"][0]["action"] == "diagnostics"
    assert "bob" in out["skipped_actions"][0]["reason"]


def test_approval_after_rejection_is_refused_with_the_halting_reason(db, service):
    approval = _record(service)
    service.submit_verdict(
        incident_id=approval.incident_id, verdict=Verdict.REJECTED,
        reviewer_id="bob", attribution_source=AttributionSource.JWT,
    )
    with pytest.raises(ApprovalConflictError) as exc:
        service.submit_verdict(
            incident_id=approval.incident_id, verdict=Verdict.APPROVED,
            reviewer_id="alice", attribution_source=AttributionSource.JWT,
        )
    assert "bob" in str(exc.value)
    assert service._action.calls == []


def test_repeat_rejection_is_idempotent(db, service):
    approval = _record(service)
    service.submit_verdict(
        incident_id=approval.incident_id, verdict=Verdict.REJECTED,
        reviewer_id="bob", attribution_source=AttributionSource.JWT,
    )
    out = service.submit_verdict(
        incident_id=approval.incident_id, verdict=Verdict.REJECTED,
        reviewer_id="bob", attribution_source=AttributionSource.JWT,
    )
    assert out["idempotent"] is True
    assert out["status"] == ApprovalStatus.REJECTED


@pytest.mark.parametrize("verdict", [Verdict.CHANGES_REQUESTED, Verdict.ESCALATED])
def test_non_deciding_verdicts_record_but_never_move_the_chain(db, service, verdict):
    approval = _record(service, tiers=("analyst", "manager"))
    out = service.submit_verdict(
        incident_id=approval.incident_id, verdict=verdict,
        reviewer_id="carol", attribution_source=AttributionSource.JWT,
    )
    assert out["status"] == ApprovalStatus.PENDING
    assert out["current_tier"] == 0
    assert service._action.calls == []
    # ...but it IS recorded, with attribution.
    verdicts = service.verdicts_for(approval.approval_id)
    assert [v.verdict for v in verdicts] == [verdict]
    assert verdicts[0].reviewer_id == "carol"


def test_unknown_verdict_is_rejected(db, service):
    approval = _record(service)
    with pytest.raises(InvalidVerdictError):
        service.submit_verdict(
            incident_id=approval.incident_id, verdict="looks_fine_to_me",
            reviewer_id="alice",
        )


def test_verdict_on_an_incident_with_no_approval_record(db, service):
    """Pre-E9 incidents (and non-gated ones) have nothing to approve."""
    with pytest.raises(ApprovalNotFoundError):
        service.submit_verdict(
            incident_id="legacy-incident", verdict=Verdict.APPROVED, reviewer_id="alice",
        )


def test_missing_action_agent_reports_skipped_never_claims_executed(db):
    """PHIL-1: an approval with no action backend is honest about it."""
    svc = HumanReviewService(
        approval_repo=IncidentApprovalRepository(db),
        verdict_repo=ReviewVerdictRepository(db),
        settings=_settings(),
        action_agent=None,
    )
    approval = _record(svc)
    out = svc.submit_verdict(
        incident_id=approval.incident_id, verdict=Verdict.APPROVED, reviewer_id="alice",
    )
    assert out["status"] == ApprovalStatus.APPROVED
    assert out["executed_actions"] == []
    assert out["skipped_actions"][0]["reason"] == "ActionAgent not available."


def test_failed_action_is_reported_skipped_not_executed(db):
    svc = HumanReviewService(
        approval_repo=IncidentApprovalRepository(db),
        verdict_repo=ReviewVerdictRepository(db),
        settings=_settings(),
        action_agent=_RecordingActionAgent(fail={"monitoring"}),
    )
    approval = _record(svc, steps=(
        ("diagnostics", {"incident_id": "inv-1"}),
        ("monitoring", {"incident_id": "inv-1"}),
    ))
    out = svc.submit_verdict(
        incident_id=approval.incident_id, verdict=Verdict.APPROVED, reviewer_id="alice",
    )
    assert out["executed_actions"] == ["diagnostics"]
    assert out["skipped_actions"] == [{"action": "monitoring", "reason": "simulated failure"}]


# ===========================================================================
# 4. Tiered (multi-level) approval
# ===========================================================================

def test_tiered_chain_executes_only_after_every_tier_approves_in_order(db, service):
    approval = _record(service, tiers=("analyst", "manager", "risk"))
    agent = service._action

    first = service.submit_verdict(
        incident_id=approval.incident_id, verdict=Verdict.APPROVED,
        reviewer_id="analyst-alice", attribution_source=AttributionSource.JWT,
    )
    assert first["status"] == ApprovalStatus.PENDING
    assert first["tier_label"] == "analyst"
    assert first["remaining_tiers"] == ["manager", "risk"]
    assert agent.calls == []

    second = service.submit_verdict(
        incident_id=approval.incident_id, verdict=Verdict.APPROVED,
        reviewer_id="manager-mo", attribution_source=AttributionSource.JWT,
    )
    assert second["status"] == ApprovalStatus.PENDING
    assert second["tier_label"] == "manager"
    assert agent.calls == []

    third = service.submit_verdict(
        incident_id=approval.incident_id, verdict=Verdict.APPROVED,
        reviewer_id="risk-rita", attribution_source=AttributionSource.JWT,
    )
    assert third["status"] == ApprovalStatus.APPROVED
    assert third["tier_label"] == "risk"
    assert agent.executed_types == ["diagnostics"]


def test_one_principal_cannot_satisfy_a_whole_chain(db, service):
    """The chain exists to require several people; a repeat from the same
    principal is idempotent, not a second signature."""
    approval = _record(service, tiers=("analyst", "manager"))
    service.submit_verdict(
        incident_id=approval.incident_id, verdict=Verdict.APPROVED,
        reviewer_id="alice", attribution_source=AttributionSource.JWT,
    )
    second = service.submit_verdict(
        incident_id=approval.incident_id, verdict=Verdict.APPROVED,
        reviewer_id="alice", attribution_source=AttributionSource.JWT,
    )
    assert second["idempotent"] is True
    assert second["status"] == ApprovalStatus.PENDING
    assert service._action.calls == []


def test_rejection_at_a_middle_tier_halts_the_chain_and_names_the_tier(db, service):
    approval = _record(service, tiers=("analyst", "manager", "risk"))
    service.submit_verdict(
        incident_id=approval.incident_id, verdict=Verdict.APPROVED,
        reviewer_id="analyst-alice", attribution_source=AttributionSource.JWT,
    )
    out = service.submit_verdict(
        incident_id=approval.incident_id, verdict=Verdict.REJECTED,
        reviewer_id="manager-mo", attribution_source=AttributionSource.JWT,
    )

    assert out["status"] == ApprovalStatus.REJECTED
    assert out["tier"] == 1
    assert out["tier_label"] == "manager"
    assert service._action.calls == []
    assert "manager" in out["skipped_actions"][0]["reason"]
    assert "manager-mo" in out["skipped_actions"][0]["reason"]

    # Which tier and principal halted it is recoverable from the rows alone.
    verdicts = service.verdicts_for(approval.approval_id)
    halting = [v for v in verdicts if v.verdict == Verdict.REJECTED]
    assert len(halting) == 1
    assert (halting[0].tier, halting[0].tier_label, halting[0].reviewer_id) == (
        1, "manager", "manager-mo",
    )


def test_single_tier_chain_is_behaviourally_a_one_step_approval(db, service):
    """COMPAT-1/6: the default chain is indistinguishable from a one-step
    approval — one verdict releases execution."""
    approval = _record(service, tiers=("reviewer",))
    out = service.submit_verdict(
        incident_id=approval.incident_id, verdict=Verdict.APPROVED, reviewer_id="alice",
    )
    assert out["status"] == ApprovalStatus.APPROVED
    assert service._action.executed_types == ["diagnostics"]


# ===========================================================================
# 5. Persistence & attribution (survives restart)
# ===========================================================================

def test_verdicts_survive_a_fresh_repository_instance(db, service):
    """'Survives restart' means the rows are the truth, not in-memory state —
    read them back through repositories the service never touched."""
    approval = _record(service, tiers=("analyst", "manager"))
    service.submit_verdict(
        incident_id=approval.incident_id, verdict=Verdict.APPROVED,
        reviewer_id="alice", reviewer_roles=["admin"],
        attribution_source=AttributionSource.JWT, note="looks right",
    )

    fresh_approvals = IncidentApprovalRepository(db)
    fresh_verdicts = ReviewVerdictRepository(db)

    reloaded = fresh_approvals.get_by_incident(approval.incident_id)
    assert reloaded is not None
    assert reloaded.status == ApprovalStatus.PENDING
    assert reloaded.current_tier == 1
    assert reloaded.required_tiers == ["analyst", "manager"]
    assert reloaded.pending_actions[0]["step"] == "diagnostics"

    rows = fresh_verdicts.list_for_incident(approval.incident_id)
    assert len(rows) == 1
    assert rows[0].reviewer_id == "alice"
    assert rows[0].reviewer_roles == ["admin"]
    assert rows[0].attribution_source == AttributionSource.JWT
    assert rows[0].note == "looks right"


def test_attribution_source_is_recorded_per_verdict(db, service):
    approval = _record(service, tiers=("a", "b"))
    service.submit_verdict(
        incident_id=approval.incident_id, verdict=Verdict.CHANGES_REQUESTED,
        reviewer_id="dev-user", attribution_source=AttributionSource.REQUEST,
    )
    service.submit_verdict(
        incident_id=approval.incident_id, verdict=Verdict.APPROVED,
        reviewer_id="alice", attribution_source=AttributionSource.JWT,
    )
    sources = {v.reviewer_id: v.attribution_source
               for v in service.verdicts_for(approval.approval_id)}
    assert sources == {"dev-user": AttributionSource.REQUEST, "alice": AttributionSource.JWT}


def test_queue_lists_only_pending_and_reports_total(db, service):
    a = _record(service, incident_id="inc-a")
    _record(service, incident_id="inc-b")
    service.submit_verdict(
        incident_id=a.incident_id, verdict=Verdict.APPROVED, reviewer_id="alice",
    )
    queue, total = service.list_queue()
    assert total == 1
    assert [x.incident_id for x in queue] == ["inc-b"]


# ===========================================================================
# 6. Orchestrator gating (the execution boundary itself)
# ===========================================================================

def test_approval_required_incident_executes_zero_gated_actions(db):
    settings = _settings(HUMAN_APPROVAL_ENFORCED=True)
    agent = _RecordingActionAgent()
    ltm = _CapturingLongTermMemory()
    svc = HumanReviewService(
        approval_repo=IncidentApprovalRepository(db),
        verdict_repo=ReviewVerdictRepository(db),
        settings=settings, action_agent=agent,
    )

    _orchestrator(settings, ltm, agent, svc).handle_event(_event())

    # Notifications ran; gated steps did not.
    assert "slack" in agent.executed_types
    assert "jira" in agent.executed_types
    assert "diagnostics" not in agent.executed_types
    assert "monitoring" not in agent.executed_types

    approval = svc.get_approval("db-incident-1")
    assert approval is not None
    assert approval.status == ApprovalStatus.PENDING
    assert [p["step"] for p in approval.pending_actions] == ["diagnostics", "monitoring"]


def test_approval_then_releases_exactly_those_steps(db):
    """End-to-end approve flow: gate → verdict → the withheld calls run."""
    settings = _settings(HUMAN_APPROVAL_ENFORCED=True)
    agent = _RecordingActionAgent()
    ltm = _CapturingLongTermMemory()
    svc = HumanReviewService(
        approval_repo=IncidentApprovalRepository(db),
        verdict_repo=ReviewVerdictRepository(db),
        settings=settings, action_agent=agent,
    )
    _orchestrator(settings, ltm, agent, svc).handle_event(_event())
    before = list(agent.executed_types)

    out = svc.submit_verdict(
        incident_id="db-incident-1", verdict=Verdict.APPROVED,
        reviewer_id="alice", attribution_source=AttributionSource.JWT,
    )

    assert out["executed_actions"] == ["diagnostics", "monitoring"]
    released = agent.executed_types[len(before):]
    assert released == ["diagnostics", "monitoring"]
    # The released calls carry the parameters the Orchestrator built.
    diagnostics_call = agent.calls[len(before)]
    assert diagnostics_call["parameters"]["metric"] == "db_latency"
    assert diagnostics_call["parameters"]["kind"] == "diagnostics"


def test_rejection_end_to_end_never_executes_a_gated_step(db):
    settings = _settings(HUMAN_APPROVAL_ENFORCED=True)
    agent = _RecordingActionAgent()
    svc = HumanReviewService(
        approval_repo=IncidentApprovalRepository(db),
        verdict_repo=ReviewVerdictRepository(db),
        settings=settings, action_agent=agent,
    )
    _orchestrator(settings, _CapturingLongTermMemory(), agent, svc).handle_event(_event())
    before = list(agent.executed_types)

    svc.submit_verdict(
        incident_id="db-incident-1", verdict=Verdict.REJECTED,
        reviewer_id="bob", attribution_source=AttributionSource.JWT,
    )

    assert agent.executed_types == before
    assert "diagnostics" not in agent.executed_types


def test_gated_incident_records_a_human_approval_finding(db):
    settings = _settings(HUMAN_APPROVAL_ENFORCED=True)
    agent = _RecordingActionAgent()
    ltm = _CapturingLongTermMemory()
    svc = HumanReviewService(
        approval_repo=IncidentApprovalRepository(db),
        verdict_repo=ReviewVerdictRepository(db),
        settings=settings, action_agent=agent,
    )
    _orchestrator(settings, ltm, agent, svc).handle_event(_event())

    findings = ltm.recorded["findings"]
    entry = next(f for f in findings if f.get("type") == "human_approval")
    assert entry["data"]["required"] is True
    assert entry["data"]["status"] == "pending"
    assert entry["data"]["required_tiers"] == ["reviewer"]
    assert entry["data"]["pending_actions"] == ["diagnostics", "monitoring"]

    # And the audit_summary reports them honestly as withheld, not executed.
    audit = next(f for f in findings if f.get("type") == "audit_summary")
    assert "diagnostics" not in audit["executed_actions"]
    reasons = {s["action"]: s["reason"] for s in audit["skipped_actions"]}
    assert "pending human approval" in reasons["diagnostics"].lower()


def test_non_approval_incident_behaves_exactly_as_before(db):
    """COMPAT-1: when the plan does not require approval, nothing is gated
    and no approval row is written."""
    settings = _settings(HUMAN_APPROVAL_ENFORCED=True)
    agent = _RecordingActionAgent()
    ltm = _CapturingLongTermMemory()
    svc = HumanReviewService(
        approval_repo=IncidentApprovalRepository(db),
        verdict_repo=ReviewVerdictRepository(db),
        settings=settings, action_agent=agent,
    )
    _orchestrator(settings, ltm, agent, svc, approval_required=False).handle_event(_event())

    assert "diagnostics" in agent.executed_types
    assert "monitoring" in agent.executed_types
    assert svc.get_approval("db-incident-1") is None
    assert not any(f.get("type") == "human_approval" for f in ltm.recorded["findings"])


def test_enforcement_disabled_reproduces_pre_e9_behaviour(db):
    """The documented rollback switch."""
    settings = _settings(HUMAN_APPROVAL_ENFORCED=False)
    agent = _RecordingActionAgent()
    ltm = _CapturingLongTermMemory()
    svc = HumanReviewService(
        approval_repo=IncidentApprovalRepository(db),
        verdict_repo=ReviewVerdictRepository(db),
        settings=settings, action_agent=agent,
    )
    assert svc.enforced is False

    _orchestrator(settings, ltm, agent, svc).handle_event(_event())

    assert "diagnostics" in agent.executed_types
    assert "monitoring" in agent.executed_types
    assert svc.get_approval("db-incident-1") is None
    assert not any(f.get("type") == "human_approval" for f in ltm.recorded["findings"])


def test_no_review_service_wired_leaves_finalization_untouched(db):
    """A platform built without the service has no gate to enforce."""
    settings = _settings(HUMAN_APPROVAL_ENFORCED=True)
    agent = _RecordingActionAgent()
    ltm = _CapturingLongTermMemory()
    _orchestrator(settings, ltm, agent, None).handle_event(_event())
    assert "diagnostics" in agent.executed_types


def test_tiered_gate_uses_the_configured_severity_chain(db):
    settings = _settings(
        HUMAN_APPROVAL_ENFORCED=True,
        APPROVAL_TIER_CHAIN_OVERRIDES="HIGH:analyst,manager",
    )
    agent = _RecordingActionAgent()
    svc = HumanReviewService(
        approval_repo=IncidentApprovalRepository(db),
        verdict_repo=ReviewVerdictRepository(db),
        settings=settings, action_agent=agent,
    )
    _orchestrator(settings, _CapturingLongTermMemory(), agent, svc).handle_event(_event())

    approval = svc.get_approval("db-incident-1")
    assert approval.required_tiers == ["analyst", "manager"]


# ===========================================================================
# 7. Review API (routing, shapes, and the honest no-gate answer)
# ===========================================================================

class _Container:
    def __init__(self, service, settings):
        self.human_review_service = service
        self.settings = settings


@pytest.fixture()
def client(db, service):
    app = FastAPI()
    app.include_router(review_router)
    app.state.container = _Container(service, _settings())
    app.state.audit_logger = None
    return TestClient(app)


def test_queue_endpoint_reports_chain_and_withheld_steps(client, service):
    _record(service, incident_id="inc-api", tiers=("analyst", "manager"))
    body = client.get("/api/v1/review/queue").json()

    assert body["total"] == 1
    assert body["enforced"] is True
    entry = body["queue"][0]
    assert entry["incident_id"] == "inc-api"
    assert entry["required_tiers"] == ["analyst", "manager"]
    assert entry["awaiting_tier"] == "analyst"
    assert entry["pending_actions"] == ["diagnostics"]


def test_approve_endpoint_executes_and_returns_attribution(client, service):
    _record(service, incident_id="inc-api")
    res = client.post("/api/v1/review/incidents/inc-api/approve",
                      json={"note": "ok", "reviewer_id": "dev-alice"})
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == ApprovalStatus.APPROVED
    assert body["executed_actions"] == ["diagnostics"]
    # No JWT principal available (middleware not installed) — honestly tagged.
    assert body["attribution_source"] == AttributionSource.REQUEST
    assert body["reviewer_id"] == "dev-alice"


def test_reject_endpoint_halts_and_second_approve_is_409(client, service):
    _record(service, incident_id="inc-api")
    assert client.post("/api/v1/review/incidents/inc-api/reject",
                       json={"reviewer_id": "bob"}).status_code == 200
    conflict = client.post("/api/v1/review/incidents/inc-api/approve",
                           json={"reviewer_id": "alice"})
    assert conflict.status_code == 409
    assert "bob" in conflict.json()["detail"]


def test_verdict_endpoint_supports_the_full_vocabulary(client, service):
    _record(service, incident_id="inc-api", tiers=("analyst", "manager"))
    res = client.post("/api/v1/review/incidents/inc-api/verdict",
                      json={"verdict": "changes_requested", "reviewer_id": "carol"})
    assert res.status_code == 200
    assert res.json()["status"] == ApprovalStatus.PENDING


def test_verdict_endpoint_rejects_an_unknown_verdict(client, service):
    _record(service, incident_id="inc-api")
    res = client.post("/api/v1/review/incidents/inc-api/verdict",
                      json={"verdict": "vibes", "reviewer_id": "carol"})
    assert res.status_code == 422


def test_unknown_incident_returns_404_on_write(client):
    res = client.post("/api/v1/review/incidents/never-existed/approve", json={})
    assert res.status_code == 404


def test_legacy_incident_detail_reads_as_no_gate_not_as_missing(client):
    """COMPAT-1: an incident predating this phase has no approval row. That
    is a 200 saying 'no gate' — not a 404 implying the incident is unknown."""
    body = client.get("/api/v1/review/incidents/legacy-incident").json()
    assert body["approval"] is None
    assert body["verdicts"] == []
    assert "no approval requirement" in body["reason"]


def test_verdict_history_endpoint_is_newest_first(client, service):
    _record(service, incident_id="inc-1")
    _record(service, incident_id="inc-2")
    client.post("/api/v1/review/incidents/inc-1/approve", json={"reviewer_id": "alice"})
    client.post("/api/v1/review/incidents/inc-2/reject", json={"reviewer_id": "bob"})

    body = client.get("/api/v1/review/verdicts").json()
    assert body["total"] == 2
    assert {v["reviewer_id"] for v in body["verdicts"]} == {"alice", "bob"}
    assert all("attribution_source" in v for v in body["verdicts"])


def test_timestamps_serialise_under_both_dialects():
    """PostgreSQL returns a real datetime for a TIMESTAMP column while
    SQLite returns the stored string. Serialising the raw value works on
    one and 500s on the other — this asserts the normalisation that keeps
    both dialects producing the same JSON."""
    from datetime import datetime, timezone

    from aeam.api.review import _approval_dict, _iso, _verdict_dict
    from aeam.registry.models import IncidentApproval, ReviewVerdict

    stamp = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)
    assert _iso(stamp) == stamp.isoformat()
    assert _iso("2026-07-26T12:00:00+00:00") == "2026-07-26T12:00:00+00:00"
    assert _iso(None) is None

    approval = IncidentApproval(incident_id="i", created_at=stamp, updated_at=stamp)
    verdict = ReviewVerdict(incident_id="i", created_at=stamp)
    assert isinstance(_approval_dict(approval)["created_at"], str)
    assert isinstance(_approval_dict(approval)["updated_at"], str)
    assert isinstance(_verdict_dict(verdict)["created_at"], str)


def test_review_endpoints_503_when_service_is_not_wired(db):
    app = FastAPI()
    app.include_router(review_router)

    class _Empty:
        settings = _settings()
    app.state.container = _Empty()
    app.state.audit_logger = None

    assert TestClient(app).get("/api/v1/review/queue").status_code == 503


# ===========================================================================
# 8. RBAC integration (SEC-3 / SEC-7)
# ===========================================================================

@pytest.mark.parametrize("path,resource,action", [
    ("/api/v1/review/queue", "incidents", "view"),
    ("/api/v1/review/verdicts", "incidents", "view"),
    ("/api/v1/review/incidents/inc-1/approve", "actions", "approve"),
    ("/api/v1/review/incidents/inc-1/reject", "actions", "approve"),
    ("/api/v1/review/incidents/inc-1/verdict", "actions", "approve"),
    ("/api/v1/review/incidents/inc-1", "actions", "approve"),
])
def test_review_paths_resolve_to_the_intended_grant(path, resource, action):
    assert SecurityMiddleware._resolve_rbac(path) == (resource, action)


@pytest.mark.parametrize("role,allowed", [
    ("admin", True),
    ("operator", False),
    ("analyst", False),
    ("auditor", False),
    ("readonly", False),
])
def test_only_admin_can_cast_a_verdict(role, allowed):
    """Casting a verdict can RELEASE withheld execution, so it carries the
    strictest action grant — the same one /api/v1/actions/approve uses."""
    assert RBAC().check_permission([role], "actions", "approve") is allowed


@pytest.mark.parametrize("role", ["admin", "operator", "analyst", "auditor", "readonly"])
def test_every_role_can_see_governance_state(role):
    """Seeing what is gated is not releasing it — the read surfaces stay
    reachable by the roles that must audit them."""
    assert RBAC().check_permission([role], "incidents", "view") is True


def test_no_new_permission_vocabulary_was_invented():
    """ENG-6: E9 reuses the existing grants; it adds no second model."""
    assert RBAC.permissions_for("admin") >= {"actions:approve", "incidents:view"}
    assert "review:approve" not in RBAC.permissions_for("admin")


# ===========================================================================
# 9. Migration parity (E5 contract extended to the new tables)
# ===========================================================================

def _alembic_config(url: str) -> Config:
    """Same construction the E5 suite uses — env.py reads the URL from the
    ``-x db_url`` argument, so setting sqlalchemy.url alone would be
    silently ignored and the real (Postgres) URL would be used."""
    from pathlib import Path
    repo_root = Path(__file__).resolve().parents[2]
    cfg = Config(str(repo_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(repo_root / "migrations"))
    cfg.cmd_opts = type("O", (), {"x": [f"db_url={url}"]})()  # minimal shim
    return cfg


_E9_TABLES = ("incident_approvals", "review_verdicts")


def test_new_tables_exist_in_both_schema_paths(tmp_path):
    mig_url = f"sqlite:///{tmp_path / 'mig.db'}"
    command.upgrade(_alembic_config(mig_url), "head")
    mig_engine = create_engine(mig_url)

    ddl_client = DatabaseClient(database_url=f"sqlite:///{tmp_path / 'ddl.db'}")
    try:
        mig_tables = set(inspect(mig_engine).get_table_names())
        ddl_tables = set(inspect(ddl_client._engine).get_table_names())
    finally:
        mig_engine.dispose()
        ddl_client.dispose()

    for table in _E9_TABLES:
        assert table in mig_tables, f"migration path missing {table}"
        assert table in ddl_tables, f"startup path missing {table}"


def test_new_tables_have_identical_columns_in_both_paths(tmp_path):
    mig_url = f"sqlite:///{tmp_path / 'mig.db'}"
    command.upgrade(_alembic_config(mig_url), "head")
    mig_engine = create_engine(mig_url)
    ddl_client = DatabaseClient(database_url=f"sqlite:///{tmp_path / 'ddl.db'}")
    try:
        for table in _E9_TABLES:
            mig_cols = {c["name"] for c in inspect(mig_engine).get_columns(table)}
            ddl_cols = {c["name"] for c in inspect(ddl_client._engine).get_columns(table)}
            assert mig_cols == ddl_cols, f"column drift in {table}"
    finally:
        mig_engine.dispose()
        ddl_client.dispose()


def test_migration_is_additive_only(tmp_path):
    """COMPAT-5: existing incident data survives the upgrade untouched."""
    url = f"sqlite:///{tmp_path / 'upgrade.db'}"
    command.upgrade(_alembic_config(url), "0003_policy_embedding")

    engine = create_engine(url)
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO incidents (incident_id, event_type, metric) "
            "VALUES ('pre-e9', 'DB_LATENCY', 'db_latency')"
        )
    engine.dispose()

    command.upgrade(_alembic_config(url), "head")

    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            row = conn.exec_driver_sql(
                "SELECT event_type FROM incidents WHERE incident_id = 'pre-e9'"
            ).fetchone()
        assert row is not None and row[0] == "DB_LATENCY"
        assert set(_E9_TABLES) <= set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


# ===========================================================================
# 10. Documentation deliverable
# ===========================================================================

def test_governance_workflow_guide_exists():
    from pathlib import Path
    doc = Path(__file__).resolve().parents[2] / "docs" / "human_in_the_loop.md"
    assert doc.is_file()
    text = doc.read_text(encoding="utf-8")
    for token in ("HUMAN_APPROVAL_ENFORCED", "NEVER_GATED_STEPS", "attribution_source"):
        assert token in text
