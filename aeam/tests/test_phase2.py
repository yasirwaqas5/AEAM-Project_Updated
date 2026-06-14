import math
from aeam.agents.kpi.rule_engine import RuleEngine
from aeam.agents.kpi.statistical_detector import StatisticalDetector
from aeam.agents.monitor.monitor_agent import MonitorAgent
from aeam.pipelines.structured_data_pipeline import StructuredDataPipeline
from aeam.core.event_bus import EventBus
from aeam.core.priority_queue import EventPriorityQueue
from aeam.config.settings import Settings


# ---------------------------------------------------------------------
# Dummy Forecast Agent (Unit Test Safe)
# ---------------------------------------------------------------------

class DummyForecastAgent:
    """Minimal forecast agent that never signals a deviation."""
    def analyze(self, metric_name: str, actual_value: float) -> dict:
        return {"is_deviation": False}


# ---------------------------------------------------------------------
# In-Memory Deduplicator (Unit Test Safe)
# ---------------------------------------------------------------------

class InMemoryDeduplicator:
    def __init__(self):
        self._seen = set()

    def is_duplicate(self, event):
        key = (event.event_type, event.metric, event.current_value)
        if key in self._seen:
            return True
        self._seen.add(key)
        return False


# ---------------------------------------------------------------------
# Agent Builder (No Redis, No External Infra)
# ---------------------------------------------------------------------

def build_agent():
    settings = Settings(
        DATABASE_URL="sqlite:///test.db",
        REDIS_URL="redis://localhost:6379/0",
        VECTOR_DB_URL="http://localhost:6333",
        ENVIRONMENT="development",
    )

    bus = EventBus()
    queue = EventPriorityQueue()
    dedup = InMemoryDeduplicator()
    dummy_forecast = DummyForecastAgent()

    return MonitorAgent(
        event_bus=bus,
        queue=queue,
        deduplicator=dedup,
        rule_engine=RuleEngine(),
        statistical_detector=StatisticalDetector(window_size=7),
        forecast_agent=dummy_forecast,          # ← ADDED
        pipeline=StructuredDataPipeline(),
        settings=settings,
    )


# ---------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------

def test_rule_trigger():
    engine = RuleEngine()
    result = engine.evaluate("sales", current=40000, previous=60000)
    assert result["rule_triggered"] is True


def test_z_score_trigger():
    detector = StatisticalDetector()
    history = [100, 102, 101, 98, 99, 100, 101]
    result = detector.detect(current=200, history=history)
    assert result["statistical_anomaly"] is True


def test_pipeline_clean_missing():
    pipeline = StructuredDataPipeline()
    values = [1.0, math.nan, 3.0]
    cleaned = pipeline.clean_missing(values)
    assert cleaned[1] == 2.0


def test_monitor_single_signal_creates_event():
    agent = build_agent()
    history = [100, 101, 99, 100, 102, 98, 100]

    event = agent.process_kpi(
        metric_name="sales",
        current=40000,
        previous=60000,
        history=history,
    )

    assert event is not None
    assert event.severity in ["MEDIUM", "HIGH"]


def test_deduplication_blocks_duplicate():
    agent = build_agent()
    history = [100, 101, 99, 100, 102, 98, 100]

    event1 = agent.process_kpi("sales", 40000, 60000, history)
    event2 = agent.process_kpi("sales", 40000, 60000, history)

    assert event1 is not None
    assert event2 is None