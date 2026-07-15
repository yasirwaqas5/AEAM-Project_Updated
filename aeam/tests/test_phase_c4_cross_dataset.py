"""
aeam/tests/test_phase_c4_cross_dataset.py

Cross-Dataset Intelligence (Phase C4) tests.

Three layers, matching this codebase's established test conventions (fakes
duck-typing the real classes' public interface; no live Qdrant/DB/blob I/O):

1. CrossDatasetAnalyzer's own correlation logic against FAKE
   DatasetActivation/DatasetIntelligenceService/DatasetKPISource, using the
   REAL DatasetMonitoringProfile dataclass and the REAL StatisticalDetector
   (both already exist and are exercised, not re-implemented).
2. Orchestrator wiring: cross-dataset analysis runs exactly once per
   incident lifecycle, is appended as its own `type: "cross_dataset"`
   findings entry distinct from `type: "rag"` / `type: "memory"` /
   `type: "policy"`, and never reaches DecisionEngine/RuleEngine/
   ActionAgent.
3. ReportAgent: the "Cross-Dataset Analysis" section appears honestly in
   all states (never consulted / insufficient data / nothing found /
   real signals found).
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
from aeam.intelligence.cross_dataset_analyzer import CrossDatasetAnalyzer
from aeam.intelligence.dataset_intelligence import DatasetIntelligenceError
from aeam.intelligence.models import DatasetMonitoringProfile
from aeam.memory.long_term import LongTermMemory
from aeam.memory.short_term import ShortTermMemory


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeDatasetActivation:
    def __init__(self, ids=None):
        self._ids = list(ids or [])

    def list_activated_dataset_ids(self):
        return list(self._ids)


class FakeDatasetIntelligenceService:
    def __init__(self, profiles=None, fail_for=None):
        self._profiles = profiles or {}
        self._fail_for = set(fail_for or [])

    def build_profile(self, dataset_id):
        if dataset_id in self._fail_for:
            raise DatasetIntelligenceError("dataset_not_found", f"no such dataset {dataset_id}")
        profile = self._profiles.get(dataset_id)
        if profile is None:
            raise DatasetIntelligenceError("dataset_not_found", f"no such dataset {dataset_id}")
        return profile


class FakeDatasetKPISource:
    def __init__(self, rows_by_dataset=None):
        self._rows = rows_by_dataset or {}

    def fetch_rows(self, dataset_id):
        return list(self._rows.get(dataset_id, []))


def _profile(dataset_id, name=None, measures=None, dimensions=None, timestamp_column=None):
    return DatasetMonitoringProfile(
        dataset_id=dataset_id, dataset_name=name or dataset_id, schema_id="s1", row_count=10,
        measures=measures or [], dimensions=dimensions or [], timestamp_column=timestamp_column,
    )


def _row(date, **values):
    row = {"event_time": date}
    row.update(values)
    return row


# ===========================================================================
# 1. CrossDatasetAnalyzer
# ===========================================================================

def test_insufficient_data_with_fewer_than_two_activated_datasets():
    analyzer = CrossDatasetAnalyzer(
        dataset_activation=FakeDatasetActivation(["d1"]),
        intelligence=FakeDatasetIntelligenceService({"d1": _profile("d1", measures=["sales"])}),
        kpi_source=FakeDatasetKPISource(),
    )
    result = analyzer.analyze(metric="sales")
    assert result["insufficient_data"] is True
    assert "at least 2" in result["reason"]
    assert result["supporting"] == []


def test_origin_dataset_resolved_by_measure_name():
    profiles = {
        "d1": _profile("d1", name="Sales", measures=["sales"], dimensions=["region"]),
        "d2": _profile("d2", name="Complaints", measures=["complaints"], dimensions=["region"]),
    }
    analyzer = CrossDatasetAnalyzer(
        dataset_activation=FakeDatasetActivation(["d1", "d2"]),
        intelligence=FakeDatasetIntelligenceService(profiles),
        kpi_source=FakeDatasetKPISource(),
    )
    result = analyzer.analyze(metric="sales")
    assert result["origin_dataset_id"] == "d1"
    assert result["origin_dataset_name"] == "Sales"
    assert result["candidates_checked"] == 1


def test_supporting_when_related_dataset_also_anomalous():
    profiles = {
        "d1": _profile("d1", name="Sales", measures=["sales"], dimensions=["region"], timestamp_column="event_time"),
        "d2": _profile("d2", name="Complaints", measures=["complaints"], dimensions=["region"], timestamp_column="event_time"),
    }
    # Complaints spikes sharply on the last point -- a genuine statistical anomaly.
    rows_d2 = [_row(f"2026-01-0{i}", complaints=10) for i in range(1, 7)] + [_row("2026-01-07", complaints=95)]
    analyzer = CrossDatasetAnalyzer(
        dataset_activation=FakeDatasetActivation(["d1", "d2"]),
        intelligence=FakeDatasetIntelligenceService(profiles),
        kpi_source=FakeDatasetKPISource({"d2": rows_d2}),
    )
    result = analyzer.analyze(metric="sales")
    assert len(result["supporting"]) == 1
    entry = result["supporting"][0]
    assert entry["dataset_id"] == "d2"
    assert entry["relation"] == "shared_dimension:region"
    assert entry["statistical_anomaly"] is True


def test_contradicting_when_related_dataset_stays_normal():
    profiles = {
        "d1": _profile("d1", name="Sales", measures=["sales"], dimensions=["region"], timestamp_column="event_time"),
        "d2": _profile("d2", name="Inventory", measures=["inventory"], dimensions=["region"], timestamp_column="event_time"),
    }
    # Flat, unremarkable series -- no anomaly.
    rows_d2 = [_row(f"2026-01-0{i}", inventory=100) for i in range(1, 8)]
    analyzer = CrossDatasetAnalyzer(
        dataset_activation=FakeDatasetActivation(["d1", "d2"]),
        intelligence=FakeDatasetIntelligenceService(profiles),
        kpi_source=FakeDatasetKPISource({"d2": rows_d2}),
    )
    result = analyzer.analyze(metric="sales")
    assert len(result["contradicting"]) == 1
    assert result["contradicting"][0]["dataset_id"] == "d2"
    assert result["supporting"] == []


def test_unrelated_normal_dataset_is_omitted_not_fabricated_as_contradicting():
    """No shared dimension AND no anomaly -- must not appear in either
    supporting or contradicting (never padded in as fake evidence)."""
    profiles = {
        "d1": _profile("d1", name="Sales", measures=["sales"], dimensions=["region"], timestamp_column="event_time"),
        "d2": _profile("d2", name="Unrelated", measures=["widgets"], dimensions=["warehouse"], timestamp_column="event_time"),
    }
    rows_d2 = [_row(f"2026-01-0{i}", widgets=50) for i in range(1, 8)]
    analyzer = CrossDatasetAnalyzer(
        dataset_activation=FakeDatasetActivation(["d1", "d2"]),
        intelligence=FakeDatasetIntelligenceService(profiles),
        kpi_source=FakeDatasetKPISource({"d2": rows_d2}),
    )
    result = analyzer.analyze(metric="sales")
    assert result["supporting"] == []
    assert result["contradicting"] == []


def test_missing_signal_when_too_few_data_points():
    profiles = {
        "d1": _profile("d1", measures=["sales"], dimensions=["region"], timestamp_column="event_time"),
        "d2": _profile("d2", name="Sparse", measures=["complaints"], dimensions=["region"], timestamp_column="event_time"),
    }
    analyzer = CrossDatasetAnalyzer(
        dataset_activation=FakeDatasetActivation(["d1", "d2"]),
        intelligence=FakeDatasetIntelligenceService(profiles),
        kpi_source=FakeDatasetKPISource({"d2": [_row("2026-01-01", complaints=5)]}),  # only 1 point
    )
    result = analyzer.analyze(metric="sales")
    assert len(result["missing_signals"]) == 1
    assert result["missing_signals"][0]["dataset_id"] == "d2"
    assert "data point" in result["missing_signals"][0]["reason"]


def test_dataset_with_no_measures_is_a_missing_signal():
    profiles = {
        "d1": _profile("d1", measures=["sales"]),
        "d2": _profile("d2", name="Empty", measures=[]),
    }
    analyzer = CrossDatasetAnalyzer(
        dataset_activation=FakeDatasetActivation(["d1", "d2"]),
        intelligence=FakeDatasetIntelligenceService(profiles),
        kpi_source=FakeDatasetKPISource(),
    )
    result = analyzer.analyze(metric="sales")
    assert len(result["missing_signals"]) == 1
    assert "no monitorable measures" in result["missing_signals"][0]["reason"].lower()


def test_strong_correlation_requires_aligned_overlapping_dates():
    profiles = {
        "d1": _profile("d1", name="Sales", measures=["sales"], timestamp_column="event_time"),
        "d2": _profile("d2", name="AdSpend", measures=["ad_spend"], timestamp_column="event_time"),
    }
    dates = [f"2026-02-0{i}" for i in range(1, 8)]
    sales_values = [100, 90, 80, 70, 60, 50, 40]  # steadily dropping
    ad_values = [10, 20, 30, 40, 50, 60, 95]  # steadily rising then a jump -> anti-correlated but let's make same-direction
    rows_d1 = [_row(d, sales=v) for d, v in zip(dates, sales_values)]
    rows_d2 = [_row(d, ad_spend=v) for d, v in zip(dates, [100, 90, 80, 70, 60, 50, 30])]  # also dropping -> correlated

    analyzer = CrossDatasetAnalyzer(
        dataset_activation=FakeDatasetActivation(["d1", "d2"]),
        intelligence=FakeDatasetIntelligenceService(profiles),
        kpi_source=FakeDatasetKPISource({"d1": rows_d1, "d2": rows_d2}),
        correlation_threshold=0.7,
    )
    result = analyzer.analyze(metric="sales")
    assert len(result["strong_correlations"]) == 1
    corr_entry = result["strong_correlations"][0]
    assert corr_entry["dataset_id"] == "d2"
    assert corr_entry["correlation"] > 0.7
    assert corr_entry["overlapping_dates"] == 7


def test_no_correlation_claimed_without_timestamp_column():
    """Neither dataset has a resolvable timestamp axis on the candidate side
    -- correlation must never be fabricated without genuine date alignment."""
    profiles = {
        "d1": _profile("d1", measures=["sales"], timestamp_column="event_time"),
        "d2": _profile("d2", name="NoTime", measures=["widgets"], timestamp_column=None),
    }
    rows_d1 = [_row(f"2026-01-0{i}", sales=100 - i * 5) for i in range(1, 8)]
    rows_d2 = [{"widgets": 100 - i * 5} for i in range(1, 8)]  # no timestamp key at all
    analyzer = CrossDatasetAnalyzer(
        dataset_activation=FakeDatasetActivation(["d1", "d2"]),
        intelligence=FakeDatasetIntelligenceService(profiles),
        kpi_source=FakeDatasetKPISource({"d1": rows_d1, "d2": rows_d2}),
    )
    result = analyzer.analyze(metric="sales")
    assert result["strong_correlations"] == []


def test_unprofilable_dataset_is_skipped_not_fatal():
    profiles = {"d1": _profile("d1", measures=["sales"])}
    analyzer = CrossDatasetAnalyzer(
        dataset_activation=FakeDatasetActivation(["d1", "d2"]),
        intelligence=FakeDatasetIntelligenceService(profiles, fail_for=["d2"]),
        kpi_source=FakeDatasetKPISource(),
    )
    result = analyzer.analyze(metric="sales")
    assert result["insufficient_data"] is False
    assert result["candidates_checked"] == 0  # d2 failed to profile, never counted


def test_analyze_never_raises_on_unexpected_error():
    class _Broken:
        def list_activated_dataset_ids(self):
            raise RuntimeError("boom")

    analyzer = CrossDatasetAnalyzer(
        dataset_activation=_Broken(),
        intelligence=FakeDatasetIntelligenceService({}),
        kpi_source=FakeDatasetKPISource(),
    )
    result = analyzer.analyze(metric="sales")
    assert result["insufficient_data"] is True
    assert "failed unexpectedly" in result["reason"]


def test_analyzer_rejects_none_dependencies():
    with pytest.raises(ValueError):
        CrossDatasetAnalyzer(dataset_activation=None, intelligence=FakeDatasetIntelligenceService({}), kpi_source=FakeDatasetKPISource())
    with pytest.raises(ValueError):
        CrossDatasetAnalyzer(dataset_activation=FakeDatasetActivation([]), intelligence=None, kpi_source=FakeDatasetKPISource())
    with pytest.raises(ValueError):
        CrossDatasetAnalyzer(dataset_activation=FakeDatasetActivation([]), intelligence=FakeDatasetIntelligenceService({}), kpi_source=None)


# ===========================================================================
# 2. Orchestrator wiring
# ===========================================================================

class FakeLongTermMemory(LongTermMemory):
    def __init__(self):
        self.recorded = None

    def record_incident(self, payload):
        self.recorded = payload
        return payload.get("incident_id", "fake-id")


class FakeCrossDatasetAnalyzerForOrchestrator:
    def __init__(self, result=None):
        self._result = result or {
            "insufficient_data": False, "reason": None, "origin_dataset_id": "d1",
            "origin_dataset_name": "Sales", "candidates_checked": 1,
            "supporting": [], "contradicting": [], "strong_correlations": [], "missing_signals": [],
        }
        self.calls = []

    def analyze(self, metric):
        self.calls.append(metric)
        return self._result


def _build_orchestrator(cross_dataset_analyzer=None):
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
        cross_dataset_analyzer=cross_dataset_analyzer,
    )
    return orchestrator, ltm, stm


def _event():
    return Event(
        event_id="1", event_type="DB_LATENCY", metric="latency_ms", severity="HIGH",
        current_value=900, expected_value=200, detection_methods=["rule"],
        timestamp="2026-01-01T00:00:00Z",
    )


def test_orchestrator_without_cross_dataset_analyzer_unaffected():
    orchestrator, ltm, stm = _build_orchestrator(cross_dataset_analyzer=None)
    orchestrator.handle_event(_event())
    assert ltm.recorded is not None
    assert [f for f in ltm.recorded["findings"] if f.get("type") == "cross_dataset"] == []


def test_orchestrator_appends_cross_dataset_finding_distinctly():
    analyzer = FakeCrossDatasetAnalyzerForOrchestrator()
    orchestrator, ltm, stm = _build_orchestrator(cross_dataset_analyzer=analyzer)

    orchestrator.handle_event(_event())

    assert analyzer.calls == ["latency_ms"]
    findings = ltm.recorded["findings"]
    types_seen = {f.get("type") for f in findings}
    assert "cross_dataset" in types_seen
    cross_findings = [f for f in findings if f.get("type") == "cross_dataset"]
    assert len(cross_findings) == 1
    assert cross_findings[0]["data"]["origin_dataset_id"] == "d1"


def test_cross_dataset_analysis_runs_exactly_once_per_incident():
    analyzer = FakeCrossDatasetAnalyzerForOrchestrator()
    orchestrator, ltm, stm = _build_orchestrator(cross_dataset_analyzer=analyzer)
    orchestrator.handle_event(_event())
    assert len(analyzer.calls) == 1


def test_broken_cross_dataset_analyzer_does_not_break_investigation():
    class _Broken:
        def analyze(self, metric):
            raise RuntimeError("boom")

    orchestrator, ltm, stm = _build_orchestrator(cross_dataset_analyzer=_Broken())
    orchestrator.handle_event(_event())  # must not raise
    assert ltm.recorded is not None
    cross_findings = [f for f in ltm.recorded["findings"] if f.get("type") == "cross_dataset"]
    assert cross_findings[0]["data"]["insufficient_data"] is True  # honest degradation, not a crash


# ===========================================================================
# 3. ReportAgent — "Cross-Dataset Analysis"
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


def test_report_states_cross_dataset_not_consulted_honestly():
    report = _report_agent().generate_report(_memory_with_findings([]))
    assert "Cross-Dataset Analysis" in report["detailed_report"]
    assert "not consulted" in report["detailed_report"]


def test_report_states_insufficient_data_honestly():
    findings = [{"type": "cross_dataset", "data": {"insufficient_data": True, "reason": "Only 1 dataset(s) currently activated; cross-dataset correlation requires at least 2."}}]
    report = _report_agent().generate_report(_memory_with_findings(findings))
    assert "Cross-Dataset Analysis" in report["detailed_report"]
    assert "Insufficient data" in report["detailed_report"]
    assert "at least 2" in report["detailed_report"]


def test_report_states_nothing_found_honestly():
    findings = [{"type": "cross_dataset", "data": {
        "insufficient_data": False, "origin_dataset_id": "d1", "origin_dataset_name": "Sales",
        "candidates_checked": 2, "supporting": [], "contradicting": [], "strong_correlations": [], "missing_signals": [],
    }}]
    report = _report_agent().generate_report(_memory_with_findings(findings))
    assert "Cross-Dataset Analysis" in report["detailed_report"]
    assert "No supporting, contradicting, or strongly-correlated signals" in report["detailed_report"]


def test_report_lists_real_cross_dataset_signals():
    findings = [{"type": "cross_dataset", "data": {
        "insufficient_data": False, "origin_dataset_id": "d1", "origin_dataset_name": "Sales",
        "candidates_checked": 1,
        "supporting": [{"dataset_name": "Complaints", "metric": "complaints", "z_score": 4.2, "relation": "shared_dimension:region"}],
        "contradicting": [], "strong_correlations": [], "missing_signals": [],
    }}]
    report = _report_agent().generate_report(_memory_with_findings(findings))
    assert "Cross-Dataset Analysis" in report["detailed_report"]
    assert "Supporting: Complaints" in report["detailed_report"]
    assert "z=4.2" in report["detailed_report"]
