"""
aeam/tests/test_phase5_monitor_pipeline.py

Phase 5 activation test — proves MonitorAgent._run_cycle() drives the full
production execution chain end to end:

    MonitorAgent._run_cycle()
        -> RuleEngine.evaluate()
        -> StatisticalDetector.detect()
        -> ForecastAgent.analyze()  (real Prophet model, trained in-test)
        -> Event created with rule/statistical/forecast metadata
        -> EventDeduplicator
        -> EventPriorityQueue
        -> EventBus.publish()
        -> a wildcard subscriber (stand-in for Orchestrator.handle_event,
           registered the same way main.py registers the real Orchestrator)

No mocking of the detection logic itself — RuleEngine, StatisticalDetector,
and ForecastAgent (with a real ForecastModel/Prophet) all run for real. Only
the KPI row feed (normally SheetsConnector) and LongTermMemory (normally
Postgres) are substituted with in-memory fakes, mirroring the existing
DummyLTM / DummyForecastAgent conventions already used in
test_phase5_forecast.py and test_phase2.py.
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from typing import Any

from aeam.agents.forecast.forecast_agent import ForecastAgent
from aeam.agents.kpi.rule_engine import RuleEngine
from aeam.agents.kpi.statistical_detector import StatisticalDetector
from aeam.agents.monitor.monitor_agent import MonitorAgent
from aeam.config.settings import Settings
from aeam.core.event_bus import EventBus
from aeam.core.priority_queue import EventPriorityQueue
from aeam.pipelines.forecast_data_pipeline import ForecastDataPipeline
from aeam.pipelines.structured_data_pipeline import StructuredDataPipeline


# ---------------------------------------------------------------------
# In-memory fakes (mirror existing test conventions in this repo)
# ---------------------------------------------------------------------

class InMemoryDeduplicator:
    def __init__(self) -> None:
        self._seen: set[tuple[str, str, float]] = set()

    def is_duplicate(self, event) -> bool:
        key = (event.event_type, event.metric, event.current_value)
        if key in self._seen:
            return True
        self._seen.add(key)
        return False


class FakeKPISheet:
    """Stand-in for SheetsConnector — same fetch_rows(sheet_name) shape."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def fetch_rows(self, sheet_name: str) -> list[dict[str, Any]]:
        return self._rows


class DummyLTM:
    """Stand-in for LongTermMemory's forecast-training data source."""

    def get_metric_history(self, metric_name: str, limit: int | None = None):
        now = datetime.now(timezone.utc)
        rows = 35
        return [
            {"timestamp": now - timedelta(days=rows - i), "value": 500_000 + i * 300}
            for i in range(rows)
        ]


def build_settings() -> Settings:
    return Settings(
        DATABASE_URL="sqlite:///test.db",
        REDIS_URL="redis://localhost:6379/0",
        VECTOR_DB_URL="http://localhost:6333",
        ENVIRONMENT="development",
    )


def build_pipeline_agent():
    """Wire a MonitorAgent with real detection logic + in-memory fakes."""
    settings = build_settings()

    forecast_agent = ForecastAgent(
        long_term_memory=DummyLTM(),
        data_pipeline=ForecastDataPipeline(),
        settings=settings,
        model_dir=tempfile.mkdtemp(),
    )

    # Healthy sales history, then a sharp drop on the latest row — should
    # trip the rule engine (absolute_minimum / daily_drop_percent), the
    # statistical detector (z-score + percentile), and the forecast
    # deviation check simultaneously.
    sheet_rows = [
        {"sales": "520000"},
        {"sales": "518000"},
        {"sales": "525000"},
        {"sales": "519000"},
        {"sales": "522000"},
        {"sales": "521000"},
        {"sales": "523000"},
        {"sales": "40000"},
    ]

    bus = EventBus()
    received: list[Any] = []
    bus.register_handler("ALL", received.append)

    agent = MonitorAgent(
        event_bus=bus,
        queue=EventPriorityQueue(),
        deduplicator=InMemoryDeduplicator(),
        rule_engine=RuleEngine(),
        statistical_detector=StatisticalDetector(window_size=7),
        forecast_agent=forecast_agent,
        pipeline=StructuredDataPipeline(),
        settings=settings,
        kpi_source=FakeKPISheet(sheet_rows),
    )
    return agent, received


# ---------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------

def test_run_cycle_noop_without_kpi_source():
    """Unchanged prior behaviour: no source configured -> safe no-op tick."""
    settings = build_settings()
    bus = EventBus()
    received: list[Any] = []
    bus.register_handler("ALL", received.append)

    class NeverCalledForecast:
        def analyze(self, metric_name: str, actual_value: float) -> dict:
            raise AssertionError("ForecastAgent must not run without a KPI source.")

    agent = MonitorAgent(
        event_bus=bus,
        queue=EventPriorityQueue(),
        deduplicator=InMemoryDeduplicator(),
        rule_engine=RuleEngine(),
        statistical_detector=StatisticalDetector(window_size=7),
        forecast_agent=NeverCalledForecast(),
        pipeline=StructuredDataPipeline(),
        settings=settings,
        kpi_source=None,
    )

    agent._run_cycle()

    assert received == []


def test_run_cycle_drives_full_pipeline_to_event_bus():
    """
    Runtime evidence for Tasks 4-8: a single _run_cycle() call — the exact
    method MonitorAgent.start()'s background thread invokes every
    MONITOR_INTERVAL_SECONDS — exercises RuleEngine, StatisticalDetector,
    and ForecastAgent for real, and the resulting event reaches an
    EventBus "ALL" subscriber exactly like the Orchestrator does in main.py.
    """
    agent, received = build_pipeline_agent()

    agent._run_cycle()

    assert len(received) == 1
    event = received[0]

    # EventBus / Orchestrator entry point (Task 8).
    assert event.event_type == "KPI_ANOMALY"
    assert event.metric == "sales"
    assert event.severity == "HIGH"  # >= 2 signals fired

    # RuleEngine executed (Task 4).
    assert any(m.startswith("rule:sales.") for m in event.detection_methods)
    assert event.metadata["rule"]["rule_triggered"] is True

    # StatisticalDetector executed (Task 5).
    assert any(m.startswith("statistical:") for m in event.detection_methods)
    assert event.metadata["statistical"]["statistical_anomaly"] is True

    # ForecastAgent executed and produced a deviation (Task 6 & 7).
    assert "FORECAST" in event.detection_methods
    assert event.metadata["forecast"]["is_deviation"] is True
    assert event.metadata["forecast"]["deviation_percent"] is not None


def test_run_cycle_skips_metric_with_insufficient_rows():
    """Domains present in RuleEngine but absent/short in the sheet are skipped, not errored."""
    agent, received = build_pipeline_agent()
    # "complaints" and "inventory" are known RuleEngine domains but have no
    # columns in the fake sheet at all -> _extract_series returns [] -> skipped.
    agent._run_cycle()

    metrics_seen = {e.metric for e in received}
    assert metrics_seen == {"sales"}
