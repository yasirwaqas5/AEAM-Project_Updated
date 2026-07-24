"""
aeam/tests/test_phase_e2_concurrency.py

Phase E2 — Concurrent Investigation Integrity (ARCH-8, TEST-6).

These tests prove the single acceptance criterion the E2 roadmap gives:
N concurrent triggers yield N finalized incidents whose findings each
reference only their own event, and interleaved Monitor + trigger runs
never cross-contaminate.

Design under test:
* Every call to :meth:`Orchestrator.handle_event` allocates its own
  :class:`~aeam.agents.orchestrator.incident_context.IncidentContext`
  (fresh STM, fresh FSM).
* No per-incident state lives on the Orchestrator instance.
* The event bus is synchronous — publishing from a threadpool worker
  runs the wildcard Orchestrator handler on THAT worker's thread. Two
  workers (Monitor + trigger, or trigger + trigger) are the real
  concurrency vector the audit flagged; that is what these tests
  exercise.

Infrastructure: SQLite temp DBs and in-process fakes only (TEST-3).
"""

from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest

from aeam.agents.orchestrator.decision_engine import DecisionEngine
from aeam.agents.orchestrator.evaluation_engine import EvaluationEngine
from aeam.agents.orchestrator.incident_context import IncidentContext
from aeam.agents.orchestrator.orchestrator import Orchestrator
from aeam.config.settings import Settings
from aeam.core.event_bus import EventBus
from aeam.core.event_models import Event
from aeam.memory.long_term import LongTermMemory


# ---------------------------------------------------------------------------
# Test doubles — thread-safe recorders, no real DB/Redis/Qdrant.
# ---------------------------------------------------------------------------


class ConcurrentSafeLTM(LongTermMemory):
    """Thread-safe capture of every finalized incident payload."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.recorded: list[dict] = []

    def record_incident(self, payload: dict) -> str:
        with self._lock:
            self.recorded.append(payload)
        # Return whatever id the payload carries; the real LTM does this too.
        return payload.get("incident_id") or payload.get("event_id") or "fake"


def _build_orchestrator() -> tuple[Orchestrator, ConcurrentSafeLTM]:
    settings = Settings(
        DATABASE_URL="sqlite:///:memory:",
        REDIS_URL="redis://localhost:6379/0",
        VECTOR_DB_URL="http://localhost",
        ENVIRONMENT="development",
        LLM_ENABLED=False,
    )
    bus = EventBus()
    decision = DecisionEngine(settings=settings)
    evaluation = EvaluationEngine(settings=settings)
    ltm = ConcurrentSafeLTM()

    orchestrator = Orchestrator(
        event_bus=bus,
        decision_engine=decision,
        evaluation_engine=evaluation,
        long_term_memory=ltm,
        settings=settings,
    )
    return orchestrator, ltm


def _event(tag: str, *, event_type: str = "kpi_anomaly", metric: str = "sales") -> Event:
    """A unique event tagged so we can assert which incident 'owns' each finding."""
    return Event(
        event_id=f"event-{tag}",
        event_type=event_type,
        metric=f"{metric}-{tag}",
        severity="HIGH",
        current_value=100.0 + hash(tag) % 200,
        expected_value=200.0,
        detection_methods=["rule"],
        timestamp="2026-07-01T00:00:00Z",
        # Stash the tag on metadata too — findings that carry through the
        # investigation should preserve it, letting us prove per-incident
        # isolation from any angle.
        metadata={"tag": tag},
    )


# ===========================================================================
# 1. Basic reentrancy — sequential calls still work, ctx doesn't leak.
# ===========================================================================


def test_orchestrator_holds_no_per_incident_instance_attrs():
    """
    Structural guard: if a regression puts a per-incident attribute back
    on the Orchestrator instance, this test fails loudly. Every incident
    lives on an :class:`IncidentContext`; nothing per-incident lives on
    ``self``.
    """
    orchestrator, ltm = _build_orchestrator()

    banned = {"_stm", "_sm", "_active_event", "_investigation_started_at"}
    present = set(vars(orchestrator))
    leaked = banned & present
    assert not leaked, (
        f"Orchestrator instance leaks per-incident attributes: {leaked}. "
        "Move them onto IncidentContext (Phase E2, ARCH-8)."
    )


def test_two_sequential_events_do_not_leak_state_between_incidents():
    orchestrator, ltm = _build_orchestrator()

    orchestrator.handle_event(_event("A", metric="sales"))
    orchestrator.handle_event(_event("B", metric="checkout"))

    assert len(ltm.recorded) == 2
    # Each recorded incident carries only its own event's data.
    for rec, tag, expected_metric in [
        (ltm.recorded[0], "A", "sales-A"),
        (ltm.recorded[1], "B", "checkout-B"),
    ]:
        assert rec["event_id"] == f"event-{tag}"
        assert rec["metric"] == expected_metric
        # No findings entry from incident A should mention B's metric
        # (the metric string is unique per incident).
        for finding in rec.get("findings", []):
            _assert_no_cross_metric_leak(finding, expected_metric)


# ===========================================================================
# 2. Concurrent triggers — N threads driving handle_event() in parallel.
# ===========================================================================


def _run_concurrent(orchestrator, tags, workers):
    """Fan tags out across a threadpool; return list of exceptions (empty if all OK)."""
    errors: list[BaseException] = []

    def _drive(tag):
        try:
            orchestrator.handle_event(_event(tag))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(as_completed([pool.submit(_drive, t) for t in tags]))
    return errors


@pytest.mark.parametrize("n_concurrent", [4, 8, 16])
def test_n_concurrent_handle_events_all_finalize_without_error(n_concurrent):
    """N parallel handle_event() calls each finalize their own incident."""
    orchestrator, ltm = _build_orchestrator()
    tags = [f"c{n_concurrent}-{i}" for i in range(n_concurrent)]

    errors = _run_concurrent(orchestrator, tags, workers=n_concurrent)

    assert not errors, f"Concurrent handle_event raised: {errors}"
    assert len(ltm.recorded) == n_concurrent, (
        f"Expected {n_concurrent} finalized incidents, got {len(ltm.recorded)}"
    )

    # Every recorded incident's event_id and metric must be one of the
    # tags we submitted, and each tag must appear exactly once.
    recorded_event_ids = [r["event_id"] for r in ltm.recorded]
    assert set(recorded_event_ids) == {f"event-{t}" for t in tags}
    assert len(set(recorded_event_ids)) == n_concurrent  # all unique


def test_concurrent_incidents_have_distinct_incident_ids():
    orchestrator, ltm = _build_orchestrator()
    tags = [f"iso-{i}" for i in range(8)]

    _run_concurrent(orchestrator, tags, workers=8)

    incident_ids = [r.get("event_id") for r in ltm.recorded]
    assert len(set(incident_ids)) == len(incident_ids), (
        "Concurrent invocations produced duplicate event_ids — "
        "per-incident isolation is broken."
    )


# ===========================================================================
# 3. Findings isolation — the acceptance criterion in words.
# ===========================================================================


def _assert_no_cross_metric_leak(finding, own_metric: str):
    """
    A finding may legitimately mention its own metric anywhere. What it
    must NEVER mention is *another* incident's metric — that would be
    proof of cross-contamination. Walk the finding structure.
    """
    stack = [finding]
    while stack:
        v = stack.pop()
        if isinstance(v, dict):
            stack.extend(v.values())
        elif isinstance(v, list):
            stack.extend(v)
        elif isinstance(v, str):
            # Any "metric-*" token in the string is a metric tag.
            for token in v.split():
                if (
                    token.startswith(("sales-", "checkout-"))
                    and token != own_metric
                    and not token.startswith(own_metric)
                ):
                    raise AssertionError(
                        f"Cross-incident leak: {token!r} appeared in a "
                        f"finding for incident owning {own_metric!r}. "
                        f"Full finding: {finding!r}"
                    )


def test_every_finding_belongs_only_to_its_own_incident():
    """
    Acceptance criterion (E2 roadmap): N concurrent triggers yield N
    finalized incidents whose findings each reference only their own
    event. Every tag is unique per event; a finding on incident A must
    never mention incident B's tag.
    """
    orchestrator, ltm = _build_orchestrator()
    n = 12
    tags = [f"iso-{i}-{uuid.uuid4().hex[:6]}" for i in range(n)]

    _run_concurrent(orchestrator, tags, workers=8)

    assert len(ltm.recorded) == n

    # Map event_id -> expected metric (each incident's private tag).
    for rec in ltm.recorded:
        own_metric = rec["metric"]  # e.g. "sales-iso-3-a1b2c3"
        for finding in rec.get("findings", []):
            _assert_no_cross_metric_leak(finding, own_metric)


# ===========================================================================
# 4. Monitor-vs-trigger race — the specific interleaving the audit flagged.
# ===========================================================================


def test_monitor_thread_and_trigger_thread_do_not_cross_contaminate():
    """
    Two long-running threads driving handle_event() with a synchronised
    start (barrier) — simulating the exact race the audit flagged: a
    MonitorAgent-thread event landing at the same instant as an HTTP
    trigger. Both must finalize independently and correctly.
    """
    orchestrator, ltm = _build_orchestrator()

    barrier = threading.Barrier(2)

    def _drive(tag):
        barrier.wait()  # both threads start together
        orchestrator.handle_event(_event(tag))

    monitor_thread = threading.Thread(
        target=_drive, args=("monitor-race",), name="MonitorSim"
    )
    trigger_thread = threading.Thread(
        target=_drive, args=("trigger-race",), name="TriggerSim"
    )
    monitor_thread.start()
    trigger_thread.start()
    monitor_thread.join(timeout=30)
    trigger_thread.join(timeout=30)

    assert not monitor_thread.is_alive() and not trigger_thread.is_alive()
    assert len(ltm.recorded) == 2

    recorded = {r["event_id"]: r for r in ltm.recorded}
    assert "event-monitor-race" in recorded
    assert "event-trigger-race" in recorded
    assert recorded["event-monitor-race"]["metric"] == "sales-monitor-race"
    assert recorded["event-trigger-race"]["metric"] == "sales-trigger-race"


# ===========================================================================
# 5. Soak — modest concurrency at higher volume.
# ===========================================================================


def test_soak_20_concurrent_incidents_all_isolated():
    """Not a stress test — a soak. 20 concurrent handle_event() calls with
    8 threadpool workers, everything must finalize cleanly and stay isolated."""
    orchestrator, ltm = _build_orchestrator()
    n = 20
    tags = [f"soak-{i}" for i in range(n)]

    t0 = time.perf_counter()
    errors = _run_concurrent(orchestrator, tags, workers=8)
    elapsed = time.perf_counter() - t0

    assert not errors, f"Soak run raised: {errors}"
    assert len(ltm.recorded) == n

    # Isolation: every tag appears in exactly one incident's own metric.
    for rec in ltm.recorded:
        for finding in rec.get("findings", []):
            _assert_no_cross_metric_leak(finding, rec["metric"])

    # Purely informational — pytest -s prints this.
    print(f"\n[soak] {n} concurrent incidents finalized in {elapsed:.2f}s")
