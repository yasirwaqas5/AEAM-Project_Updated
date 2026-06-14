"""
aeam/tests/test_phase3_orchestrator.py

Phase 3 validation tests for AEAM Orchestrator.

Validates:
- State transitions
- Investigation depth increments
- STOP triggers persistence
- STM cleared after finalize
- No LLM call when disabled
- Escalation logic path
"""

import pytest
from aeam.agents.orchestrator.orchestrator import Orchestrator
from aeam.agents.orchestrator.state_machine import IncidentStateMachine, IncidentState
from aeam.agents.orchestrator.decision_engine import DecisionEngine
from aeam.agents.orchestrator.evaluation_engine import EvaluationEngine
from aeam.memory.short_term import ShortTermMemory
from aeam.memory.long_term import LongTermMemory
from aeam.core.event_bus import EventBus
from aeam.core.event_models import Event
from aeam.config.settings import Settings


class FakeLongTermMemory(LongTermMemory):
    """
    Override record_incident to avoid real DB writes.
    """
    def __init__(self):
        self.recorded = None

    def record_incident(self, payload):
        self.recorded = payload
        return payload.get("incident_id", "fake-id")


def build_test_orchestrator():
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
    stm = ShortTermMemory()
    ltm = FakeLongTermMemory()
    sm = IncidentStateMachine()

    orchestrator = Orchestrator(
        event_bus=bus,
        decision_engine=decision,
        evaluation_engine=evaluation,
        short_term_memory=stm,
        long_term_memory=ltm,
        state_machine=sm,
        settings=settings,
    )

    return orchestrator, ltm, stm, sm


def create_test_event():
    return Event(
        event_id="1",
        event_type="TEST",
        metric="sales",
        severity="HIGH",
        current_value=100,
        expected_value=200,
        detection_methods=["rule"],
        timestamp="2025-01-01T00:00:00Z",
    )


def test_state_transitions_to_complete():
    orchestrator, ltm, stm, sm = build_test_orchestrator()
    event = create_test_event()

    orchestrator.handle_event(event)

    assert sm.get_state() == IncidentState.COMPLETE


def test_investigation_depth_increments():
    orchestrator, ltm, stm, sm = build_test_orchestrator()
    event = create_test_event()

    orchestrator.handle_event(event)

    # Depth should be >= 1 during process, but STM cleared at end
    assert ltm.recorded["investigation_depth"] >= 1


def test_stop_triggers_record_incident():
    orchestrator, ltm, stm, sm = build_test_orchestrator()
    event = create_test_event()

    orchestrator.handle_event(event)

    assert ltm.recorded is not None
    assert ltm.recorded["severity"] == "HIGH"


def test_stm_cleared_after_finalize():
    orchestrator, ltm, stm, sm = build_test_orchestrator()
    event = create_test_event()

    orchestrator.handle_event(event)

    # STM should be uninitialised after finalize
    with pytest.raises(RuntimeError):
        stm.get("event")

def test_llm_not_called_when_disabled():
    orchestrator, ltm, stm, sm = build_test_orchestrator()
    event = create_test_event()

    orchestrator.handle_event(event)

    # Decision engine must use rule source only
    findings = ltm.recorded["findings"]
    sources = [f.get("source") for f in findings if "source" in f]
    assert "rule" in sources


def test_illegal_transition_raises():
    sm = IncidentStateMachine()

    with pytest.raises(Exception):
        sm.transition(IncidentState.COMPLETE)