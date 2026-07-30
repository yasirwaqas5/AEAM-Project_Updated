"""
aeam/tests/test_phase_f6_agent_mesh.py

Phase F6 — Agent Mesh Formalization: Supervisor & Planning Agents.

Acceptance criteria under test:

1. **The Planning Agent's output is byte-identical to the C7 engine's for the
   same input** — a promotion, not a rewrite. Asserted across the engine's
   whole behavioural range (no evidence, policy matches, memory matches,
   conflicts, escalation), and structurally: the agent returns the engine's
   OWN object and adds no key.
2. **The Supervisor surfaces mesh health and a seeded behaviour anomaly
   without ever taking coordination authority** — asserted at the import
   graph, at the method surface, at the route table, and behaviourally with
   exploding stand-ins that fail the moment anything is dispatched.
3. **Both agents appear in ``agent_roster``, ``/metrics``, and the console
   mesh**, and both expose a heartbeat.
4. **The full C7 planning regression ledger passes unchanged** — verified by
   running that suite, and here by proving the wrapper is transparent.

Infrastructure: in-process only — real FastAPI TestClient, the real
Prometheus collectors, deterministic fixtures (TEST-3). No LLM, no network,
no live services.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aeam.agents.planning.planning_agent import AGENT_NAME as PLANNING_AGENT_NAME
from aeam.agents.planning.planning_agent import PlanningAgent
from aeam.agents.supervisor.supervisor_agent import AGENT_NAME as SUPERVISOR_AGENT_NAME
from aeam.agents.supervisor.supervisor_agent import SupervisorAgent
from aeam.api.mesh import router as mesh_router
from aeam.config.settings import Settings
from aeam.intelligence.execution_planning import ExecutionPlanningEngine
from aeam.monitoring.metrics import (
    HeartbeatTracker,
    agent_execution_time,
    heartbeat_tracker,
)


# ===========================================================================
# Fixtures
# ===========================================================================


def _settings(**overrides) -> Settings:
    base = dict(
        DATABASE_URL="sqlite:///:memory:", REDIS_URL="redis://localhost:6379/0",
        VECTOR_DB_URL="http://localhost", ENVIRONMENT="development", LLM_ENABLED=False,
    )
    base.update(overrides)
    return Settings(**base)


def _plan_kwargs(**overrides) -> dict:
    """The same call shape the C7 suite uses, so equivalence is checked
    against the engine's real contract rather than a simplified one."""
    kwargs = dict(
        event_type="DB_LATENCY", metric="db_latency_ms", severity="HIGH",
        current_value=950, expected_value=1900,
        findings=[], root_cause=None, confidence=None, requires_human=False,
        runbook_recommended_actions=["Optimize indexes"],
    )
    kwargs.update(overrides)
    return kwargs


_POLICY_FINDINGS = [{
    "type": "policy",
    "data": {"query": "q", "matches": [{
        "policy_id": "p1", "business_rule": "DB Latency Escalation",
        "condition": "latency > 5s", "actions": "Page on-call DB team",
        "approval_required": True, "department": "DB Eng", "priority": "high",
        "match_reason": "metric",
    }]},
}]

_MEMORY_FINDINGS = [{
    "type": "memory",
    "data": {"matches": [{
        "incident_id": "past-1", "similarity": 0.91, "root_cause": "Missing index",
        "resolution_status": "resolved", "severity": "HIGH",
    }]},
}]

_RAG_FINDINGS = [{
    "type": "rag",
    "data": {"retrieved_count": 3, "possible_causes": [
        {"cause": "Missing index", "confidence": 0.8, "chunk_id": "c1"},
    ]},
}]

#: Every distinct behavioural region of the C7 engine. Equivalence proved on
#: one input would say almost nothing; proved across the engine's whole range
#: it says the wrapper is transparent.
_EQUIVALENCE_CASES: list[tuple[str, dict]] = [
    ("no_evidence", _plan_kwargs()),
    ("policy_match", _plan_kwargs(findings=_POLICY_FINDINGS)),
    ("memory_match", _plan_kwargs(findings=_MEMORY_FINDINGS)),
    ("rag_causes", _plan_kwargs(findings=_RAG_FINDINGS, root_cause="Missing index", confidence=0.8)),
    (
        "all_sources",
        _plan_kwargs(
            findings=_POLICY_FINDINGS + _MEMORY_FINDINGS + _RAG_FINDINGS,
            root_cause="Missing index", confidence=0.77,
        ),
    ),
    ("requires_human", _plan_kwargs(requires_human=True, root_cause="Unknown", confidence=0.2)),
    ("low_severity", _plan_kwargs(severity="LOW", findings=_MEMORY_FINDINGS, confidence=0.5)),
    ("no_runbook", _plan_kwargs(runbook_recommended_actions=[])),
]


# ===========================================================================
# 1. PlanningAgent — a promotion, not a rewrite
# ===========================================================================


@pytest.mark.parametrize("case_name, kwargs", _EQUIVALENCE_CASES, ids=[c[0] for c in _EQUIVALENCE_CASES])
def test_planning_agent_output_is_identical_to_the_c7_engine(case_name, kwargs):
    # Two independent engines with identical configuration, so the comparison
    # cannot be satisfied by shared mutable state.
    engine_direct = ExecutionPlanningEngine()
    agent = PlanningAgent(engine=ExecutionPlanningEngine())

    expected = engine_direct.plan(**kwargs)
    actual = agent.plan(**kwargs)

    assert actual == expected, f"{case_name}: PlanningAgent changed the plan"
    assert set(actual) == set(expected), f"{case_name}: PlanningAgent altered the key set"


def test_planning_agent_returns_the_engines_own_object_not_a_copy():
    # The strongest form of "byte-identical": there is nothing between the
    # engine's return and the caller. A copy could diverge later; the same
    # object cannot.
    engine = ExecutionPlanningEngine()
    sentinel = {"executive_summary": "sentinel", "recommended_actions": []}

    class _Recording:
        def plan(self, **_kwargs):
            return sentinel

    assert PlanningAgent(engine=_Recording()).plan(**_plan_kwargs()) is sentinel
    # And the real engine's result survives the round trip unchanged.
    kwargs = _plan_kwargs(findings=_POLICY_FINDINGS)
    assert PlanningAgent(engine=engine).plan(**kwargs) == ExecutionPlanningEngine().plan(**kwargs)


def test_planning_agent_forwards_every_argument_verbatim():
    captured: dict = {}

    class _Capturing:
        def plan(self, **kwargs):
            captured.update(kwargs)
            return {}

    kwargs = _plan_kwargs(findings=_MEMORY_FINDINGS, root_cause="X", confidence=0.42)
    PlanningAgent(engine=_Capturing()).plan(**kwargs)

    # No dropped, renamed, reordered, or defaulted argument — the wrapper is
    # opaque by design so it cannot silently reshape the contract.
    assert captured == kwargs


def test_planning_agent_holds_no_planning_logic_of_its_own():
    with pytest.raises(ValueError, match="composition wrapper"):
        PlanningAgent(engine=None)


def test_planning_agent_exposes_the_wrapped_engine_for_inspection():
    engine = ExecutionPlanningEngine()
    agent = PlanningAgent(engine=engine)
    # Provable identity: the agent and the engine are one planner, not two.
    assert agent.engine is engine
    assert agent.name == PLANNING_AGENT_NAME == "planning"


def test_planning_agent_is_not_a_coordinator():
    # It plans when asked, exactly as the engine did. A planning agent that
    # could dispatch its own plan would be a second coordinator (ARCH-1).
    forbidden = {"handle_event", "dispatch", "coordinate", "execute", "run", "restart"}
    assert not (forbidden & set(dir(PlanningAgent)))


def test_planning_agent_records_a_heartbeat_and_its_own_metric():
    before = _metric_count(PLANNING_AGENT_NAME) or 0

    PlanningAgent(engine=ExecutionPlanningEngine()).plan(**_plan_kwargs())

    # The roster entry needs a metric and a heartbeat of its own, or it is a
    # name in a list rather than an observable agent.
    assert heartbeat_tracker.age_seconds(PLANNING_AGENT_NAME) is not None
    assert _metric_count(PLANNING_AGENT_NAME) == before + 1


def test_planning_agent_records_its_metric_and_heartbeat_even_when_the_engine_raises():
    class _Exploding:
        def plan(self, **_kwargs):
            raise RuntimeError("engine failed")

    before = _metric_count(PLANNING_AGENT_NAME) or 0

    with pytest.raises(RuntimeError, match="engine failed"):
        PlanningAgent(engine=_Exploding()).plan(**_plan_kwargs())

    # "The planning agent was invoked and failed" must not read as "the
    # planning agent was never invoked".
    assert _metric_count(PLANNING_AGENT_NAME) == before + 1
    assert heartbeat_tracker.age_seconds(PLANNING_AGENT_NAME) is not None


def test_planning_agent_never_swallows_an_engine_failure():
    class _Exploding:
        def plan(self, **_kwargs):
            raise RuntimeError("boom")

    # A swallowed failure would make the plan silently absent from the
    # investigation; the Orchestrator's own handler must see the exception.
    with pytest.raises(RuntimeError):
        PlanningAgent(engine=_Exploding()).plan(**_plan_kwargs())


def _metric_count(agent: str) -> float | None:
    for metric in agent_execution_time.collect():
        for sample in metric.samples:
            if sample.labels.get("agent") == agent and sample.name.endswith("_count"):
                return float(sample.value)
    return None


# ===========================================================================
# 2. SupervisorAgent — observation only
# ===========================================================================


@pytest.fixture()
def tracker():
    """A fresh HeartbeatTracker per test, so one test's heartbeats cannot
    make another's staleness assertion pass by accident."""
    return HeartbeatTracker()


def _supervisor(roster, observability=None, tracker=None, **settings_overrides) -> SupervisorAgent:
    return SupervisorAgent(
        settings=_settings(**settings_overrides),
        roster_provider=lambda: list(roster),
        observability_provider=(lambda: observability) if observability is not None else None,
        tracker=tracker,
    )


def _observability(total=10, overall_health=0.8, **rates) -> dict:
    summary: dict = {
        "total_investigations": total,
        "overall_ai_health": {"available": True, "score": overall_health},
    }
    summary.update(rates)
    return summary


def test_supervisor_has_no_coordination_method_at_all():
    # The absence IS the enforcement, exactly as on LearningAgent (F2) and
    # PolicyAgent (F3). A supervisor that could restart a worker is not
    # advisory; it is a second coordinator with a confirmation dialog.
    forbidden = {
        "handle_event", "execute", "dispatch", "coordinate", "restart",
        "plan", "act", "run", "kill", "stop", "start", "trigger",
    }
    assert not (forbidden & set(dir(SupervisorAgent)))
    # `observe` is the only public method that produces anything.
    public = {n for n in dir(SupervisorAgent) if not n.startswith("_")}
    assert public == {"observe", "name"}


def test_supervisor_module_never_imports_anything_that_could_coordinate():
    from pathlib import Path

    forbidden = (
        "Orchestrator", "ActionAgent", "PlanningAgent", "EventBus", "RuleEngine",
        "LLMService", "llm_service", "MonitorAgent", "HumanReviewService",
        "ExecutionPlanningEngine",
    )
    source = (
        Path(__file__).resolve().parents[1]
        / "agents" / "supervisor" / "supervisor_agent.py"
    ).read_text(encoding="utf-8")
    imports = [
        line for line in source.splitlines()
        if line.lstrip().startswith(("import ", "from "))
    ]
    for line in imports:
        for name in forbidden:
            assert name not in line, f"supervisor_agent.py must not import {name}: {line!r}"


def test_supervisor_cannot_be_given_a_coordinator_to_call():
    # Its constructor takes telemetry providers and nothing else, so there is
    # no collaborator for it to dispatch through.
    class _Exploding:
        def __getattr__(self, name):
            raise AssertionError(f"supervisor invoked {name}() — it must only observe")

    with pytest.raises(TypeError):
        SupervisorAgent(
            settings=_settings(), roster_provider=lambda: [], orchestrator=_Exploding()
        )  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        SupervisorAgent(
            settings=_settings(), roster_provider=lambda: [], action_agent=_Exploding()
        )  # type: ignore[call-arg]


def test_supervisor_report_declares_its_advisory_contract(tracker):
    report = _supervisor(["orchestrator", "monitor"], tracker=tracker).observe()
    contract = report["advisory_contract"]
    assert contract["observes_only"] is True
    for capability in (
        "coordinates", "executes_actions", "modifies_incidents",
        "modifies_plans", "restarts_agents",
    ):
        assert contract[capability] is False


def test_supervisor_requires_a_roster_provider():
    with pytest.raises(ValueError, match="roster_provider"):
        SupervisorAgent(settings=_settings(), roster_provider=None)  # type: ignore[arg-type]


def test_supervisor_observation_is_repeatable_and_writes_nothing(tracker):
    supervisor = _supervisor(["orchestrator", "monitor"], tracker=tracker)
    first = supervisor.observe()
    second = supervisor.observe()

    # Only the generated_at timestamp and its own heartbeat move; the
    # observation itself is a pure function of the telemetry.
    first.pop("generated_at"), second.pop("generated_at")
    assert first["roster"] == second["roster"]
    assert first["issues"] == second["issues"]


def test_supervisor_records_its_own_heartbeat_and_metric(tracker):
    before = _metric_count(SUPERVISOR_AGENT_NAME) or 0
    _supervisor(["orchestrator"], tracker=tracker).observe()

    # The Supervisor must be as observable as the agents it observes.
    assert tracker.age_seconds(SUPERVISOR_AGENT_NAME) is not None
    assert _metric_count(SUPERVISOR_AGENT_NAME) == before + 1


# ===========================================================================
# 3. Mesh health — evidence, never invention
# ===========================================================================


def test_a_stale_thread_worker_heartbeat_is_a_critical_issue_with_evidence(tracker, monkeypatch):
    tracker.record("monitor")
    # Age the heartbeat past the threshold without sleeping.
    monkeypatch.setattr(
        tracker, "age_seconds", lambda worker: 400.0 if worker == "monitor" else None
    )
    report = _supervisor(["monitor"], tracker=tracker, HEARTBEAT_STALE_SECONDS=120).observe()

    issue = next(i for i in report["issues"] if i["kind"] == "stale_heartbeat")
    assert issue["agent"] == "monitor"
    assert issue["severity"] == "critical"
    # Affected agent, supporting evidence, observed metrics, and reason — all
    # four disclosed, per the mesh-health contract.
    assert issue["evidence"]["heartbeat_age_seconds"] == 400.0
    assert issue["evidence"]["stale_after_seconds"] == 120.0
    assert "HeartbeatTracker" in issue["evidence"]["source"]
    assert "wedged" in issue["reason"]
    assert "does not restart agents" in issue["recommended_escalation"]


def test_a_request_scoped_agent_with_an_old_heartbeat_is_idle_not_stale(tracker, monkeypatch):
    # A quiet platform is not a broken platform. Reporting planning as stale
    # because nothing was investigated recently would be a fabricated fault.
    monkeypatch.setattr(tracker, "age_seconds", lambda worker: 9999.0)
    report = _supervisor(["planning"], tracker=tracker, HEARTBEAT_STALE_SECONDS=120).observe()

    planning = next(a for a in report["agents"] if a["agent"] == "planning")
    assert planning["heartbeat"]["state"] == "idle"
    assert planning["scope"] == "request_scoped"
    assert "not a liveness signal" in planning["heartbeat"]["note"]
    assert [i for i in report["issues"] if i["kind"] == "stale_heartbeat"] == []


def test_a_thread_worker_that_never_reported_is_a_warning_not_a_critical(tracker):
    report = _supervisor(["ingestion"], tracker=tracker).observe()
    issue = next(i for i in report["issues"] if i["kind"] == "no_heartbeat")
    assert issue["severity"] == "warning"
    assert issue["evidence"]["heartbeat_age_seconds"] is None
    assert "may still be starting" in issue["reason"]


def test_zero_participation_is_reported_with_the_measured_rate(tracker):
    observability = _observability(
        total=25,
        memory_hit_rate={"available": True, "rate": 0, "consulted": 25},
    )
    report = _supervisor(["memory"], observability=observability, tracker=tracker).observe()

    issue = next(i for i in report["issues"] if i["kind"] == "zero_participation")
    assert issue["agent"] == "memory"
    assert issue["severity"] == "info"
    assert issue["evidence"]["rate"] == 0
    assert issue["evidence"]["consulted"] == 25
    assert issue["evidence"]["metric"] == "memory_hit_rate"


def test_a_healthy_participation_rate_raises_no_issue(tracker):
    observability = _observability(
        total=25, memory_hit_rate={"available": True, "rate": 0.6, "consulted": 25},
    )
    report = _supervisor(["memory"], observability=observability, tracker=tracker).observe()
    assert [i for i in report["issues"] if i["kind"] == "zero_participation"] == []


def test_an_unmeasurable_participation_states_why_rather_than_scoring_zero(tracker):
    report = _supervisor(["memory"], tracker=tracker).observe()
    participation = next(a for a in report["agents"] if a["agent"] == "memory")["participation"]
    assert participation["measured"] is False
    assert "No observability summary" in participation["reason"]


def test_an_agent_with_no_participation_metric_says_so_honestly(tracker):
    report = _supervisor(["orchestrator"], observability=_observability(), tracker=tracker).observe()
    participation = next(a for a in report["agents"] if a["agent"] == "orchestrator")["participation"]
    assert participation["measured"] is False
    assert "not measured per incident" in participation["reason"]


def test_an_empty_roster_is_a_critical_issue_that_admits_its_ambiguity(tracker):
    report = _supervisor([], tracker=tracker).observe()
    issue = next(i for i in report["issues"] if i["kind"] == "empty_roster")
    assert issue["severity"] == "critical"
    # The Supervisor cannot know which of the two causes it is, and says so.
    assert "cannot distinguish" in issue["recommended_escalation"]


def test_a_failing_roster_provider_never_fabricates_an_agent_list(tracker):
    def _boom():
        raise RuntimeError("roster unreadable")

    supervisor = SupervisorAgent(
        settings=_settings(), roster_provider=_boom, tracker=tracker
    )
    report = supervisor.observe()
    assert report["roster"] == []
    assert any(i["kind"] == "empty_roster" for i in report["issues"])


def test_a_failing_observability_provider_degrades_to_unmeasured(tracker):
    def _boom():
        raise RuntimeError("summary unavailable")

    supervisor = SupervisorAgent(
        settings=_settings(), roster_provider=lambda: ["memory"],
        observability_provider=_boom, tracker=tracker,
    )
    report = supervisor.observe()
    participation = report["agents"][0]["participation"]
    assert participation["measured"] is False


def test_issues_are_sorted_worst_first(tracker, monkeypatch):
    monkeypatch.setattr(
        tracker, "age_seconds", lambda worker: 400.0 if worker == "monitor" else None
    )
    observability = _observability(
        total=25, memory_hit_rate={"available": True, "rate": 0, "consulted": 25},
    )
    report = _supervisor(
        ["monitor", "ingestion", "memory"], observability=observability,
        tracker=tracker, HEARTBEAT_STALE_SECONDS=120,
    ).observe()

    severities = [i["severity"] for i in report["issues"]]
    rank = {"critical": 0, "warning": 1, "info": 2}
    assert severities == sorted(severities, key=lambda s: rank[s])


def test_recommended_escalations_cover_critical_and_warning_only(tracker, monkeypatch):
    monkeypatch.setattr(
        tracker, "age_seconds", lambda worker: 400.0 if worker == "monitor" else None
    )
    observability = _observability(
        total=25, memory_hit_rate={"available": True, "rate": 0, "consulted": 25},
    )
    report = _supervisor(
        ["monitor", "memory"], observability=observability,
        tracker=tracker, HEARTBEAT_STALE_SECONDS=120,
    ).observe()

    escalated = {e["severity"] for e in report["recommended_escalations"]}
    assert "info" not in escalated
    assert "critical" in escalated


# ===========================================================================
# 4. Mesh health score — computed from observable components, or withheld
# ===========================================================================


def test_mesh_health_is_withheld_when_no_component_is_computable(tracker):
    # No thread workers, no investigations, no observability summary. A score
    # here would be pure invention.
    report = _supervisor(["planning", "supervisor"], tracker=tracker).observe()
    health = report["mesh_health"]

    assert health["available"] is False
    assert health["score"] is None
    assert health["components"] == {}
    assert "withheld rather than estimated" in health["reason"]


def test_mesh_health_computes_from_heartbeats_alone_when_that_is_all_there_is(tracker):
    tracker.record("monitor")
    report = _supervisor(["monitor"], tracker=tracker).observe()
    health = report["mesh_health"]

    assert health["available"] is True
    assert health["score"] == 1.0
    assert set(health["components"]) == {"heartbeat_health"}
    assert health["components"]["heartbeat_health"]["live"] == 1
    assert "HeartbeatTracker" in health["components"]["heartbeat_health"]["evidence"]


def test_mesh_health_degrades_when_a_thread_worker_is_stale(tracker, monkeypatch):
    monkeypatch.setattr(
        tracker,
        "age_seconds",
        lambda worker: 1.0 if worker == "ingestion" else 400.0,
    )
    report = _supervisor(
        ["monitor", "ingestion"], tracker=tracker, HEARTBEAT_STALE_SECONDS=120
    ).observe()
    assert report["mesh_health"]["components"]["heartbeat_health"]["value"] == 0.5


def test_mesh_health_omits_agent_activity_when_no_investigations_exist(tracker):
    # With zero investigations, agent silence is correct behaviour, so
    # scoring it would penalise a healthy idle platform.
    tracker.record("monitor")
    report = _supervisor(
        ["monitor"], observability=_observability(total=0), tracker=tracker
    ).observe()
    assert "agent_activity" not in report["mesh_health"]["components"]


def test_mesh_health_reuses_the_observability_engines_own_score(tracker):
    tracker.record("monitor")
    report = _supervisor(
        ["monitor"], observability=_observability(total=5, overall_health=0.4), tracker=tracker
    ).observe()
    components = report["mesh_health"]["components"]

    # ENG-6: the Supervisor does not recompute investigation health, so its
    # number and the Analytics page's are the same number.
    assert components["investigation_health"]["value"] == 0.4
    assert "observability summary" in components["investigation_health"]["evidence"]


def test_mesh_health_discloses_its_formula_and_which_components_fed_it(tracker):
    tracker.record("monitor")
    report = _supervisor(
        ["monitor"], observability=_observability(total=5), tracker=tracker
    ).observe()
    health = report["mesh_health"]

    assert "unweighted mean" in health["formula"]
    for component in health["components"]:
        assert component in health["formula"]
    assert "never defaulted to zero" in health["formula"]


def test_mesh_health_is_the_mean_of_the_components_present(tracker):
    tracker.record("monitor")
    report = _supervisor(
        ["monitor"], observability=_observability(total=5, overall_health=0.5), tracker=tracker
    ).observe()
    health = report["mesh_health"]
    values = [c["value"] for c in health["components"].values()]
    assert health["score"] == pytest.approx(round(sum(values) / len(values), 4))


def test_an_unavailable_overall_health_is_omitted_not_zeroed(tracker):
    tracker.record("monitor")
    report = _supervisor(
        ["monitor"],
        observability={
            "total_investigations": 0,
            "overall_ai_health": {"available": False, "reason": "no investigations"},
        },
        tracker=tracker,
    ).observe()
    assert "investigation_health" not in report["mesh_health"]["components"]


# ===========================================================================
# 5. API surface
# ===========================================================================


def _client(supervisor, settings=None) -> TestClient:
    class _Container:
        pass

    container = _Container()
    container.supervisor_agent = supervisor
    container.settings = settings or _settings()

    app = FastAPI()
    app.include_router(mesh_router)
    app.state.container = container
    return TestClient(app)


def test_api_mesh_health_returns_the_supervisor_report(tracker):
    tracker.record("monitor")
    client = _client(_supervisor(["monitor", "planning"], tracker=tracker))
    body = client.get("/api/v1/mesh/health").json()

    assert body["supervisor_enabled"] is True
    assert body["roster"] == ["monitor", "planning"]
    assert body["mesh_health"]["available"] is True
    assert body["advisory_contract"]["coordinates"] is False


def test_api_distinguishes_disabled_oversight_from_a_healthy_mesh(tracker):
    body = _client(None).get("/api/v1/mesh/health").json()

    # "oversight is off" and "the mesh is healthy" must never look alike.
    assert body["supervisor_enabled"] is False
    assert "distinct from a healthy mesh" in body["reason"]
    assert body["mesh_health"]["available"] is False
    assert body["issues"] == []


def test_api_disabled_payload_uses_the_same_key_set_as_a_live_report(tracker):
    tracker.record("monitor")
    live = _client(_supervisor(["monitor"], tracker=tracker)).get("/api/v1/mesh/health").json()
    disabled = _client(None).get("/api/v1/mesh/health").json()

    # One console code path, no chance of mistaking absence for health.
    assert set(disabled) <= set(live)
    for key in ("supervisor_enabled", "reason", "roster", "agents", "issues", "mesh_health"):
        assert key in disabled


def test_api_roster_endpoint_reports_per_agent_observed_state(tracker):
    tracker.record("monitor")
    body = _client(_supervisor(["monitor"], tracker=tracker)).get("/api/v1/mesh/roster").json()

    assert body["roster"] == ["monitor"]
    assert body["agents"][0]["heartbeat"]["state"] == "live"
    assert "container.agent_roster" in body["roster_source"]


def test_api_issues_endpoint_filters_by_severity(tracker, monkeypatch):
    monkeypatch.setattr(
        tracker, "age_seconds", lambda worker: 400.0 if worker == "monitor" else None
    )
    observability = _observability(
        total=25, memory_hit_rate={"available": True, "rate": 0, "consulted": 25},
    )
    client = _client(_supervisor(
        ["monitor", "memory"], observability=observability,
        tracker=tracker, HEARTBEAT_STALE_SECONDS=120,
    ))

    critical = client.get("/api/v1/mesh/issues?severity=critical").json()
    assert critical["count"] >= 1
    assert all(i["severity"] == "critical" for i in critical["issues"])

    info = client.get("/api/v1/mesh/issues?severity=info").json()
    assert all(i["severity"] == "info" for i in info["issues"])


def test_api_rejects_an_unknown_severity(tracker):
    resp = _client(_supervisor(["monitor"], tracker=tracker)).get(
        "/api/v1/mesh/issues?severity=catastrophic"
    )
    assert resp.status_code == 400
    assert "Unknown severity" in resp.json()["detail"]


def test_api_surfaces_a_supervisor_failure_rather_than_reporting_health():
    class _Broken:
        def observe(self):
            raise RuntimeError("telemetry unreadable")

    resp = _client(_Broken()).get("/api/v1/mesh/health")
    assert resp.status_code == 500
    assert "telemetry unreadable" in resp.json()["detail"]


def test_the_mesh_router_exposes_no_actionable_route():
    # An oversight API with a "restart this agent" button would hand the
    # Supervisor the coordination authority ARCH-1 reserves for the
    # Orchestrator. Every route is a GET, and this keeps it that way.
    for route in mesh_router.routes:
        assert set(route.methods) <= {"GET", "HEAD"}, (
            f"{route.path} exposes {route.methods}; mesh oversight is read-only"
        )


def test_mesh_api_is_graded_by_the_observability_tier():
    from aeam.middleware.security_middleware import _ENDPOINT_RBAC_MAP

    entries = {p: (r, a) for p, r, a in _ENDPOINT_RBAC_MAP if p.startswith("/api/v1/mesh")}
    assert entries == {"/api/v1/mesh": ("logs", "view")}


def test_the_mesh_api_module_never_imports_a_coordinator():
    from pathlib import Path

    forbidden = ("Orchestrator", "ActionAgent", "EventBus", "RuleEngine", "LLMService")
    source = (Path(__file__).resolve().parents[1] / "api" / "mesh.py").read_text(encoding="utf-8")
    imports = [
        line for line in source.splitlines()
        if line.lstrip().startswith(("import ", "from "))
    ]
    for line in imports:
        for name in forbidden:
            assert name not in line, f"mesh.py must not import {name}: {line!r}"


# ===========================================================================
# 6. Roster, flags, and end-to-end composition
# ===========================================================================


def test_both_agents_default_to_enabled_because_neither_can_change_an_outcome():
    settings = _settings()
    assert settings.PLANNING_AGENT_ENABLED is True
    assert settings.SUPERVISOR_AGENT_ENABLED is True


def test_flags_can_be_turned_off_for_the_documented_rollback():
    settings = _settings(PLANNING_AGENT_ENABLED=False, SUPERVISOR_AGENT_ENABLED=False)
    assert settings.PLANNING_AGENT_ENABLED is False
    assert settings.SUPERVISOR_AGENT_ENABLED is False


def test_the_composition_root_lists_both_agents_in_the_roster():
    # Asserted against the wiring source rather than a live startup (which
    # needs Redis/Postgres): the roster must gain both entries, and each only
    # when the object was actually constructed.
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
    assert '(["planning"] if planning_agent is not None else [])' in source
    assert '(["supervisor"] if settings.SUPERVISOR_AGENT_ENABLED else [])' in source


def test_the_composition_root_passes_the_planning_agent_into_the_planning_slot():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
    # The promotion is a drop-in: the same Orchestrator parameter receives the
    # agent when enabled and the bare engine when not.
    assert "execution_planning_engine=planning_target," in source
    assert "planning_target: Any = execution_planning_engine" in source


def test_the_supervisor_is_constructed_without_any_coordinator_reference():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
    start = source.index("supervisor_agent = SupervisorAgent(")
    block = source[start:source.index(")", source.index("observability_provider", start))]
    for forbidden in ("orchestrator", "action_agent", "planning_agent", "event_bus"):
        assert forbidden not in block, (
            f"SupervisorAgent must not be given {forbidden}: {block!r}"
        )


def test_end_to_end_an_orchestrator_using_the_planning_agent_plans_identically():
    """The full investigation path with the PlanningAgent in the planning slot
    must produce the same execution_plan finding as the bare C7 engine."""
    from aeam.agents.orchestrator.decision_engine import DecisionEngine
    from aeam.agents.orchestrator.evaluation_engine import EvaluationEngine
    from aeam.agents.orchestrator.orchestrator import Orchestrator
    from aeam.core.event_bus import EventBus
    from aeam.core.event_models import Event

    class _RecordingLTM:
        def __init__(self):
            self.recorded = None

        def record_incident(self, payload):
            self.recorded = payload
            return "inc-1"

        def get_metric_history(self, *_a, **_k):
            return []

    def _run(planner):
        settings = _settings()
        ltm = _RecordingLTM()
        orch = Orchestrator(
            event_bus=EventBus(),
            decision_engine=DecisionEngine(settings=settings),
            evaluation_engine=EvaluationEngine(settings=settings),
            long_term_memory=ltm,
            settings=settings,
            execution_planning_engine=planner,
        )
        orch.handle_event(Event(
            event_id="f6-1", event_type="DB_LATENCY", metric="db_latency_ms", severity="HIGH",
            current_value=950, expected_value=1900, detection_methods=["rule:latency"],
            timestamp="2026-07-05T00:00:00Z",
        ))
        assert ltm.recorded is not None
        return next(
            f["data"] for f in ltm.recorded["findings"] if f.get("type") == "execution_plan"
        )

    engine_plan = _run(ExecutionPlanningEngine())
    agent_plan = _run(PlanningAgent(engine=ExecutionPlanningEngine()))

    assert agent_plan == engine_plan, "the promotion changed the persisted execution plan"


def test_end_to_end_the_supervisor_observes_a_real_roster_without_coordinating(tracker):
    """A Supervisor over a realistic roster surfaces health and a seeded
    anomaly while holding no reference it could coordinate through."""
    roster = ["action", "forecast", "ingestion", "monitor", "orchestrator", "planning",
              "rag", "report", "supervisor"]
    tracker.record("monitor")
    supervisor = SupervisorAgent(
        settings=_settings(HEARTBEAT_STALE_SECONDS=120),
        roster_provider=lambda: roster,
        observability_provider=lambda: _observability(
            total=12, memory_hit_rate={"available": True, "rate": 0, "consulted": 12},
        ),
        tracker=tracker,
    )
    # It holds telemetry providers and nothing else: there is no attribute on
    # this instance through which work could be dispatched.
    for forbidden in ("_orchestrator", "_action", "_planner", "_planning", "_bus", "_event_bus"):
        assert not hasattr(supervisor, forbidden), (
            f"SupervisorAgent must hold no {forbidden} reference"
        )

    report = supervisor.observe()

    assert report["roster"] == sorted(roster)
    assert len(report["agents"]) == len(roster)
    # A seeded anomaly: ingestion is on the roster and has never beaten.
    assert any(
        i["kind"] == "no_heartbeat" and i["agent"] == "ingestion" for i in report["issues"]
    )
    assert report["mesh_health"]["available"] is True
    # And every issue carries the four required disclosures.
    for issue in report["issues"]:
        assert issue["agent"]
        assert issue["reason"]
        assert issue["evidence"]
        assert issue["recommended_escalation"]
