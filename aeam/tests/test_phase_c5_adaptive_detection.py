"""
aeam/tests/test_phase_c5_adaptive_detection.py

Adaptive Detection Engine (Phase C5) tests.

Three layers, matching this codebase's established test conventions (fakes
duck-typing the real classes' public interface; no live Qdrant/DB/blob I/O):

1. AdaptiveDetectionEngine's own baseline/seasonality logic against a FAKE
   LongTermMemory, using the REAL StatisticalDetector (already exists and is
   exercised, not re-implemented).
2. Orchestrator wiring: adaptive detection runs exactly once per incident
   lifecycle, is appended as its own `type: "adaptive"` findings entry
   distinct from `type: "rag"` / `type: "memory"` / `type: "policy"` /
   `type: "cross_dataset"`, and never reaches DecisionEngine/RuleEngine/
   ActionAgent.
3. ReportAgent: the "Adaptive Detection" section appears honestly in all
   states (never consulted / insufficient baseline / insufficient
   seasonality / real signals found).
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
from aeam.intelligence.adaptive_detection import AdaptiveDetectionEngine
from aeam.memory.long_term import LongTermMemory
from aeam.memory.short_term import ShortTermMemory


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeLTMForAdaptive:
    def __init__(self, rows=None):
        self._rows = rows or []

    def get_metric_history(self, metric_name, limit=None):
        return list(self._rows)


def _row(ts, value):
    return {"timestamp": ts, "value": value}


def _flat_rows(n, value=100.0, start="2026-01-01"):
    import datetime
    base = datetime.date.fromisoformat(start)
    return [_row((base + datetime.timedelta(days=i)).isoformat(), value) for i in range(n)]


# ===========================================================================
# 1. AdaptiveDetectionEngine
# ===========================================================================

def test_insufficient_baseline_with_too_few_points():
    engine = AdaptiveDetectionEngine(long_term_memory=FakeLTMForAdaptive(_flat_rows(5)))
    result = engine.analyze(metric="sales", current_value=100.0)
    assert result["adaptive_baseline"] is None
    assert "at least 10" in result["adaptive_baseline_insufficient"]


def test_adaptive_baseline_computed_with_sufficient_history():
    rows = _flat_rows(20, value=100.0)
    engine = AdaptiveDetectionEngine(long_term_memory=FakeLTMForAdaptive(rows))
    result = engine.analyze(metric="sales", current_value=500.0)  # sharp spike
    assert result["adaptive_baseline_insufficient"] is None
    assert result["adaptive_baseline"]["statistical_anomaly"] is True
    assert result["history_points_used"] == 20


def test_insufficient_seasonality_with_too_few_dated_points():
    rows = _flat_rows(8)  # < MIN_SEASONALITY_POINTS(14)
    engine = AdaptiveDetectionEngine(long_term_memory=FakeLTMForAdaptive(rows))
    result = engine.analyze(metric="sales", current_value=100.0)
    assert result["seasonality"] is None
    assert "distinct weekday" in result["seasonality_insufficient"]


def test_seasonality_detected_with_strong_weekday_pattern():
    import datetime
    base = datetime.date(2026, 1, 5)  # a Monday
    rows = []
    for week in range(4):
        for wd, val in enumerate([10, 10, 10, 10, 10, 100, 100]):  # weekends spike
            rows.append(_row((base + datetime.timedelta(days=week * 7 + wd)).isoformat(), val))
    engine = AdaptiveDetectionEngine(long_term_memory=FakeLTMForAdaptive(rows))
    result = engine.analyze(metric="sales", current_value=10.0)
    assert result["seasonality_insufficient"] is None
    assert result["seasonality"]["detected"] is True
    assert result["seasonality"]["strength"] >= 0.5


def test_no_seasonality_when_values_are_flat():
    rows = _flat_rows(21, value=50.0)
    engine = AdaptiveDetectionEngine(long_term_memory=FakeLTMForAdaptive(rows))
    result = engine.analyze(metric="sales", current_value=50.0)
    assert result["seasonality_insufficient"] is None
    assert result["seasonality"]["detected"] is False


def test_combines_with_existing_statistical_and_forecast_evidence():
    rows = _flat_rows(5)  # too few for adaptive baseline itself
    engine = AdaptiveDetectionEngine(long_term_memory=FakeLTMForAdaptive(rows))
    result = engine.analyze(
        metric="sales", current_value=100.0,
        event_metadata={
            "statistical": {"statistical_anomaly": True},
            "forecast": {"is_deviation": True},
        },
    )
    assert result["combined_signal"] is True
    assert "existing_statistical" in result["corroborating_signals"]
    assert "existing_forecast" in result["corroborating_signals"]


def test_no_combined_signal_when_nothing_corroborates():
    rows = _flat_rows(5)
    engine = AdaptiveDetectionEngine(long_term_memory=FakeLTMForAdaptive(rows))
    result = engine.analyze(metric="sales", current_value=100.0, event_metadata={})
    assert result["combined_signal"] is False
    assert result["corroborating_signals"] == []


def test_never_raises_on_unexpected_error():
    class _Broken:
        def get_metric_history(self, metric_name, limit=None):
            raise RuntimeError("boom")

    engine = AdaptiveDetectionEngine(long_term_memory=_Broken())
    result = engine.analyze(metric="sales", current_value=100.0)
    assert result["adaptive_baseline_insufficient"] is not None
    assert "failed unexpectedly" in result["adaptive_baseline_insufficient"]


def test_engine_rejects_none_long_term_memory():
    with pytest.raises(ValueError):
        AdaptiveDetectionEngine(long_term_memory=None)


# ===========================================================================
# 2. Orchestrator wiring
# ===========================================================================

class FakeLongTermMemory(LongTermMemory):
    def __init__(self):
        self.recorded = None

    def record_incident(self, payload):
        self.recorded = payload
        return payload.get("incident_id", "fake-id")


class FakeAdaptiveDetectionEngineForOrchestrator:
    def __init__(self, result=None):
        self._result = result or {
            "history_points_used": 20, "adaptive_baseline": {"moving_avg": 100.0, "z_score": 4.0, "statistical_anomaly": True},
            "adaptive_baseline_insufficient": None, "seasonality": None,
            "seasonality_insufficient": "insufficient dated points",
            "existing_statistical": None, "existing_forecast": None,
            "combined_signal": True, "corroborating_signals": ["adaptive_baseline"],
        }
        self.calls = []

    def analyze(self, metric, current_value, event_metadata=None):
        self.calls.append((metric, current_value))
        return self._result


def _build_orchestrator(adaptive_detection_engine=None):
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
        adaptive_detection_engine=adaptive_detection_engine,
    )
    return orchestrator, ltm, stm


def _event():
    return Event(
        event_id="1", event_type="DB_LATENCY", metric="latency_ms", severity="HIGH",
        current_value=900, expected_value=200, detection_methods=["rule"],
        timestamp="2026-01-01T00:00:00Z",
    )


def test_orchestrator_without_adaptive_detection_engine_unaffected():
    orchestrator, ltm, stm = _build_orchestrator(adaptive_detection_engine=None)
    orchestrator.handle_event(_event())
    assert ltm.recorded is not None
    assert [f for f in ltm.recorded["findings"] if f.get("type") == "adaptive"] == []


def test_orchestrator_appends_adaptive_finding_distinctly():
    engine = FakeAdaptiveDetectionEngineForOrchestrator()
    orchestrator, ltm, stm = _build_orchestrator(adaptive_detection_engine=engine)

    orchestrator.handle_event(_event())

    assert engine.calls == [("latency_ms", 900)]
    findings = ltm.recorded["findings"]
    types_seen = {f.get("type") for f in findings}
    assert "adaptive" in types_seen
    adaptive_findings = [f for f in findings if f.get("type") == "adaptive"]
    assert len(adaptive_findings) == 1
    assert adaptive_findings[0]["data"]["combined_signal"] is True


def test_adaptive_detection_runs_exactly_once_per_incident():
    engine = FakeAdaptiveDetectionEngineForOrchestrator()
    orchestrator, ltm, stm = _build_orchestrator(adaptive_detection_engine=engine)
    orchestrator.handle_event(_event())
    assert len(engine.calls) == 1


def test_broken_adaptive_detection_engine_does_not_break_investigation():
    class _Broken:
        def analyze(self, metric, current_value, event_metadata=None):
            raise RuntimeError("boom")

    orchestrator, ltm, stm = _build_orchestrator(adaptive_detection_engine=_Broken())
    orchestrator.handle_event(_event())  # must not raise
    assert ltm.recorded is not None
    adaptive_findings = [f for f in ltm.recorded["findings"] if f.get("type") == "adaptive"]
    assert adaptive_findings[0]["data"]["combined_signal"] is False  # honest degradation, not a crash


# ===========================================================================
# 3. ReportAgent — "Adaptive Detection"
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


def test_report_states_adaptive_not_consulted_honestly():
    report = _report_agent().generate_report(_memory_with_findings([]))
    assert "Adaptive Detection" in report["detailed_report"]
    assert "not consulted" in report["detailed_report"]


def test_report_states_insufficient_baseline_honestly():
    findings = [{"type": "adaptive", "data": {
        "history_points_used": 3, "adaptive_baseline": None,
        "adaptive_baseline_insufficient": "Only 3 historical point(s) available for 'latency_ms'; at least 10 are required for an adaptive baseline.",
        "seasonality": None, "seasonality_insufficient": "Only 3 dated historical point(s) across 2 distinct weekday(s) for 'latency_ms'; at least 14 points across 2+ weekdays are required for a seasonality judgement.",
        "existing_statistical": None, "existing_forecast": None,
        "combined_signal": False, "corroborating_signals": [],
    }}]
    report = _report_agent().generate_report(_memory_with_findings(findings))
    assert "Adaptive Detection" in report["detailed_report"]
    assert "insufficient data" in report["detailed_report"]
    assert "at least 10" in report["detailed_report"]


def test_report_lists_real_adaptive_signals():
    findings = [{"type": "adaptive", "data": {
        "history_points_used": 30,
        "adaptive_baseline": {"moving_avg": 210.5, "z_score": 4.1, "statistical_anomaly": True},
        "adaptive_baseline_insufficient": None,
        "seasonality": {"detected": True, "strength": 0.72, "highest_weekday": "Saturday", "lowest_weekday": "Tuesday"},
        "seasonality_insufficient": None,
        "existing_statistical": {"statistical_anomaly": True}, "existing_forecast": None,
        "combined_signal": True, "corroborating_signals": ["adaptive_baseline", "existing_statistical"],
    }}]
    report = _report_agent().generate_report(_memory_with_findings(findings))
    assert "Adaptive Detection" in report["detailed_report"]
    assert "z_score=4.1" in report["detailed_report"]
    assert "Saturday" in report["detailed_report"]
    assert "Combined signal: corroborated by adaptive_baseline, existing_statistical" in report["detailed_report"]
