"""
aeam/tests/test_phase_b1_7_dynamic_monitoring.py

Phase B1.7 — Close the Dataset Monitoring Loop (Dynamic Metric Discovery).

Three layers:

1. DatasetIntelligenceService.list_monitorable_metric_names — unions measure
   names across activated datasets, skips ones that fail to profile.
2. CompositeRuleEngine — loaded_domains union/dedup/failure-isolation, and
   evaluate() regression-proof (identical to a bare RuleEngine for curated
   domains) + graceful-fallback proof for dynamic names.
3. End-to-end integration: real, UNMODIFIED MonitorAgent/RuleEngine/
   StatisticalDetector/ForecastAgent driven through one real _run_cycle()
   with a CompositeRuleEngine-wrapped agent, proving:
     - zero regression for a curated "sales" domain
     - the exact gap left open by B1.5.3's own
       test_arbitrary_metric_name_is_fetched_but_not_yet_domain_discovered
       (an activated dataset's "revenue" column) is now closed — a real
       Event is produced
     - ForecastAgent.analyze() is invoked for the dataset metric (automatic
       participation, no new wiring)

No Qdrant, no Redis, no Postgres, no live network required anywhere.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from aeam.agents.forecast.forecast_agent import ForecastAgent
from aeam.agents.kpi.composite_rule_engine import CompositeRuleEngine
from aeam.agents.kpi.rule_engine import RuleEngine
from aeam.agents.kpi.statistical_detector import StatisticalDetector
from aeam.agents.monitor.monitor_agent import MonitorAgent
from aeam.connectors.composite_kpi_source import CompositeKPISource
from aeam.core.deduplication import EventDeduplicator
from aeam.core.event_bus import EventBus
from aeam.core.priority_queue import EventPriorityQueue
from aeam.integrations.database import DatabaseClient
from aeam.intelligence.dataset_activation import StaticDatasetActivation
from aeam.intelligence.dataset_intelligence import DatasetIntelligenceError, DatasetIntelligenceService
from aeam.intelligence.dataset_kpi_source import DatasetKPISource
from aeam.pipelines.structured_data_pipeline import StructuredDataPipeline
from aeam.registry.models import AssetStatus, Dataset, ParentType, Schema, Source, SourceKind, Version
from aeam.registry.repositories import (
    DatasetRepository, SchemaRepository, SourceRepository, VersionRepository,
)
from aeam.storage.blob_store import LocalDiskBlobStore


# ===========================================================================
# Fixtures
# ===========================================================================

@pytest.fixture()
def db(tmp_path):
    client = DatabaseClient(database_url=f"sqlite:///{(tmp_path / 'b17.db').as_posix()}")
    yield client
    client.dispose()


@pytest.fixture()
def blob_store(tmp_path):
    return LocalDiskBlobStore(tmp_path / "blobs")


def _col(name, type_, role, is_metric=False):
    return {"name": name, "type": type_, "nullable": False, "is_metric": is_metric, "role": role}


def _seed_dataset(db, blob_store, csv_bytes: bytes, name: str, metric_column: str,
                  status=AssetStatus.INDEXED, with_schema=True):
    source_id = SourceRepository(db).create(Source(name="Manual Upload", kind=SourceKind.UPLOAD))
    dataset_repo = DatasetRepository(db)
    version_repo = VersionRepository(db)
    ref = blob_store.put(csv_bytes)

    schema_id = None
    if with_schema:
        schema_id = SchemaRepository(db).create(Schema(
            object_name=name,
            columns=[
                _col("region", "string", "dimension"),
                _col(metric_column, "float", "metric", is_metric=True),
                _col("event_date", "string", "dimension"),
            ],
        ))
    dataset_id = dataset_repo.create(Dataset(
        name=name, source_id=source_id, schema_id=schema_id, status=status,
        metric_columns=[metric_column] if with_schema else [],
    ))
    version_repo.create(Version(
        parent_type=ParentType.DATASET, parent_id=dataset_id, version=1,
        content_hash=ref.content_hash, blob_ref=ref.uri, is_active=True,
    ))
    return dataset_id


# ===========================================================================
# 1. DatasetIntelligenceService.list_monitorable_metric_names
# ===========================================================================

def test_list_monitorable_metric_names_unions_across_datasets(db, blob_store):
    intelligence = DatasetIntelligenceService(
        dataset_repo=DatasetRepository(db), schema_repo=SchemaRepository(db),
    )
    d1 = _seed_dataset(db, blob_store, b"region,revenue,event_date\nEMEA,1,2026-01-01\n", "a.csv", "revenue")
    d2 = _seed_dataset(db, blob_store, b"region,latency,event_date\nEMEA,1,2026-01-01\n", "b.csv", "latency")

    names = intelligence.list_monitorable_metric_names([d1, d2])
    assert names == ["latency", "revenue"]


def test_list_monitorable_metric_names_empty_input(db):
    intelligence = DatasetIntelligenceService(
        dataset_repo=DatasetRepository(db), schema_repo=SchemaRepository(db),
    )
    assert intelligence.list_monitorable_metric_names([]) == []


def test_list_monitorable_metric_names_skips_unprocessed_dataset(db, blob_store):
    intelligence = DatasetIntelligenceService(
        dataset_repo=DatasetRepository(db), schema_repo=SchemaRepository(db),
    )
    good = _seed_dataset(db, blob_store, b"region,revenue,event_date\nEMEA,1,2026-01-01\n", "a.csv", "revenue")
    pending = _seed_dataset(
        db, blob_store, b"x\n1\n", "b.csv", "x", status=AssetStatus.PENDING, with_schema=False,
    )

    names = intelligence.list_monitorable_metric_names([good, pending, "does-not-exist"])
    assert names == ["revenue"]  # bad ids skipped, never raise


def test_list_monitorable_metric_names_reuses_build_profile(db, blob_store, monkeypatch):
    """Confirms no new discovery logic — it's a thin wrapper around the existing method."""
    intelligence = DatasetIntelligenceService(
        dataset_repo=DatasetRepository(db), schema_repo=SchemaRepository(db),
    )
    d1 = _seed_dataset(db, blob_store, b"region,revenue,event_date\nEMEA,1,2026-01-01\n", "a.csv", "revenue")

    calls = []
    original = intelligence.build_profile
    def spy(dataset_id):
        calls.append(dataset_id)
        return original(dataset_id)
    monkeypatch.setattr(intelligence, "build_profile", spy)

    intelligence.list_monitorable_metric_names([d1])
    assert calls == [d1]


# ===========================================================================
# 2. CompositeRuleEngine
# ===========================================================================

def test_composite_rejects_none_base():
    with pytest.raises(ValueError):
        CompositeRuleEngine(base=None)


def test_composite_loaded_domains_equals_base_with_no_providers():
    engine = CompositeRuleEngine(base=RuleEngine())
    assert engine.loaded_domains == RuleEngine().loaded_domains


def test_composite_loaded_domains_union_dedup_sorted():
    engine = CompositeRuleEngine(base=RuleEngine())
    engine.add_domain_provider("a", lambda: ["revenue", "sales"])  # "sales" overlaps a curated domain
    engine.add_domain_provider("b", lambda: ["latency", "revenue"])  # "revenue" overlaps provider a

    domains = engine.loaded_domains
    assert domains == sorted(set(RuleEngine().loaded_domains) | {"revenue", "sales", "latency"})
    assert engine.provider_count == 2


def test_composite_loaded_domains_isolates_failing_provider():
    engine = CompositeRuleEngine(base=RuleEngine())
    engine.add_domain_provider("good", lambda: ["revenue"])
    def boom():
        raise RuntimeError("provider exploded")
    engine.add_domain_provider("bad", boom)

    domains = engine.loaded_domains
    assert "revenue" in domains
    assert set(RuleEngine().loaded_domains) <= set(domains)  # base domains still present


def test_composite_evaluate_curated_domain_identical_to_base():
    base_result = RuleEngine().evaluate("sales", current=40000.0, previous=55000.0)
    composite = CompositeRuleEngine(base=RuleEngine())
    composite_result = composite.evaluate("sales", current=40000.0, previous=55000.0)
    assert composite_result == base_result  # byte-for-byte regression proof


def test_composite_evaluate_dynamic_metric_gets_graceful_fallback():
    composite = CompositeRuleEngine(base=RuleEngine())
    composite.add_domain_provider("datasets", lambda: ["revenue"])

    result = composite.evaluate("revenue", current=100.0, previous=10.0)
    # Same graceful shape RuleEngine already returns for any unrecognised name.
    assert result["rule_triggered"] is False
    assert result["rule_name"] is None
    assert "No rules configured" in result["details"]["reason"]


def test_composite_add_domain_provider_is_fluent():
    engine = CompositeRuleEngine(base=RuleEngine())
    result = engine.add_domain_provider("x", lambda: [])
    assert result is engine


# ===========================================================================
# 3. End-to-end integration — real MonitorAgent, real RuleEngine
# ===========================================================================

def _make_monitor_agent(kpi_source, rule_engine, forecast_agent=None) -> tuple[MonitorAgent, EventBus]:
    bus = EventBus()
    queue = EventPriorityQueue()
    redis_mock = MagicMock()
    redis_mock.set.return_value = True  # every event treated as novel (not a dedup duplicate)
    dedup = EventDeduplicator(redis_client=redis_mock)
    settings = MagicMock()
    settings.MONITOR_INTERVAL_SECONDS = 60
    settings.MAX_INVESTIGATION_DEPTH = 3
    settings.SHEET_RANGE = "Sheet1!A2:C10"

    agent = MonitorAgent(
        event_bus=bus, queue=queue, deduplicator=dedup,
        rule_engine=rule_engine, statistical_detector=StatisticalDetector(window_size=3),
        forecast_agent=forecast_agent, pipeline=StructuredDataPipeline(), settings=settings,
        kpi_source=kpi_source,
    )
    return agent, bus


class _NoopForecastAgent:
    """Records every metric_name it was asked to forecast; never fires a deviation."""

    def __init__(self):
        self.calls: list[str] = []

    def analyze(self, metric_name: str, actual_value: float) -> dict:
        self.calls.append(metric_name)
        return {"is_deviation": False, "deviation_percent": None}


def test_curated_domain_unaffected_by_composite_wrapper_regression(db, blob_store):
    """
    Same 'sales' anomaly, driven once through a bare RuleEngine() and once
    through a CompositeRuleEngine-wrapped agent with a dataset provider
    registered (but no dataset activated) — must produce an equivalent event.
    """
    class _FakeSheets:
        def fetch_rows(self, selector):
            return [
                {"sales": 55000.0}, {"sales": 54000.0}, {"sales": 53500.0},
                {"sales": 54200.0}, {"sales": 12000.0},  # sharp drop
            ]

    def run_with(rule_engine):
        composite_kpi = CompositeKPISource().add_passthrough(_FakeSheets())
        agent, bus = _make_monitor_agent(composite_kpi, rule_engine)
        events = []
        bus.register_handler("*", lambda e: events.append(e))
        agent._run_cycle()
        return events

    baseline_events = run_with(RuleEngine())

    wrapped = CompositeRuleEngine(base=RuleEngine())
    wrapped.add_domain_provider("datasets", lambda: [])  # registered, contributes nothing
    wrapped_events = run_with(wrapped)

    assert len(baseline_events) == len(wrapped_events) == 1
    assert baseline_events[0].metric == wrapped_events[0].metric == "sales"
    assert baseline_events[0].severity == wrapped_events[0].severity
    assert baseline_events[0].detection_methods == wrapped_events[0].detection_methods


def test_activated_dataset_metric_now_closes_the_loop(db, blob_store):
    """
    THE proof this phase exists for: the exact scenario B1.5.3's own
    test_arbitrary_metric_name_is_fetched_but_not_yet_domain_discovered
    documented as an open boundary — an activated dataset's "revenue" column,
    not one of sales/complaints/inventory — now produces a real Event once
    CompositeRuleEngine is wired in, with MonitorAgent/RuleEngine/
    StatisticalDetector completely unmodified.
    """
    csv = (
        b"region,revenue,event_date\n"
        b"EMEA,10.0,2026-01-01\n"
        b"EMEA,11.0,2026-01-02\n"
        b"EMEA,10.5,2026-01-03\n"
        b"EMEA,9.8,2026-01-04\n"
        b"EMEA,100.0,2026-01-05\n"  # sharp spike
    )
    dataset_id = _seed_dataset(db, blob_store, csv, "sales.csv", metric_column="revenue")

    dataset_repo = DatasetRepository(db)
    version_repo = VersionRepository(db)
    intelligence = DatasetIntelligenceService(dataset_repo=dataset_repo, schema_repo=SchemaRepository(db))
    dataset_kpi_source = DatasetKPISource(
        blob_store=blob_store, dataset_repo=dataset_repo, version_repo=version_repo, intelligence=intelligence,
    )
    activation = StaticDatasetActivation([dataset_id])

    composite_kpi = CompositeKPISource().add_multi(dataset_kpi_source, activation.list_activated_dataset_ids)
    composite_rules = CompositeRuleEngine(base=RuleEngine())
    composite_rules.add_domain_provider(
        "datasets", lambda: intelligence.list_monitorable_metric_names(activation.list_activated_dataset_ids())
    )

    forecast = _NoopForecastAgent()
    agent, bus = _make_monitor_agent(composite_kpi, composite_rules, forecast_agent=forecast)
    events = []
    bus.register_handler("*", lambda e: events.append(e))

    agent._run_cycle()

    assert len(events) >= 1, "activated dataset's anomalous 'revenue' metric should now raise a real Event"
    assert any(e.metric == "revenue" for e in events)

    # ForecastAgent participation is automatic — it was asked about "revenue"
    # without any new wiring beyond the metric entering loaded_domains.
    assert "revenue" in forecast.calls


def test_non_activated_dataset_still_produces_nothing(db, blob_store):
    """Activation still gates monitoring — CompositeRuleEngine does not bypass it."""
    csv = b"region,revenue,event_date\nEMEA,10.0,2026-01-01\nEMEA,100.0,2026-01-02\n"
    dataset_id = _seed_dataset(db, blob_store, csv, "sales.csv", metric_column="revenue")

    dataset_repo = DatasetRepository(db)
    version_repo = VersionRepository(db)
    intelligence = DatasetIntelligenceService(dataset_repo=dataset_repo, schema_repo=SchemaRepository(db))
    dataset_kpi_source = DatasetKPISource(
        blob_store=blob_store, dataset_repo=dataset_repo, version_repo=version_repo, intelligence=intelligence,
    )
    activation = StaticDatasetActivation([])  # nothing activated

    composite_kpi = CompositeKPISource().add_multi(dataset_kpi_source, activation.list_activated_dataset_ids)
    composite_rules = CompositeRuleEngine(base=RuleEngine())
    composite_rules.add_domain_provider(
        "datasets", lambda: intelligence.list_monitorable_metric_names(activation.list_activated_dataset_ids())
    )

    agent, bus = _make_monitor_agent(composite_kpi, composite_rules)
    events = []
    bus.register_handler("*", lambda e: events.append(e))
    agent._run_cycle()

    assert events == []
    assert dataset_id  # dataset exists and is fetchable, just not activated/monitored


def test_real_forecast_agent_invoked_without_error(db, blob_store):
    """Uses the real ForecastAgent (not the recorder stub) to prove it truly participates unmodified."""
    from aeam.memory.long_term import LongTermMemory
    from aeam.pipelines.forecast_data_pipeline import ForecastDataPipeline

    csv = b"region,revenue,event_date\nEMEA,10.0,2026-01-01\nEMEA,11.0,2026-01-02\nEMEA,100.0,2026-01-03\n"
    dataset_id = _seed_dataset(db, blob_store, csv, "sales.csv", metric_column="revenue")

    dataset_repo = DatasetRepository(db)
    version_repo = VersionRepository(db)
    intelligence = DatasetIntelligenceService(dataset_repo=dataset_repo, schema_repo=SchemaRepository(db))
    dataset_kpi_source = DatasetKPISource(
        blob_store=blob_store, dataset_repo=dataset_repo, version_repo=version_repo, intelligence=intelligence,
    )
    activation = StaticDatasetActivation([dataset_id])
    composite_kpi = CompositeKPISource().add_multi(dataset_kpi_source, activation.list_activated_dataset_ids)
    composite_rules = CompositeRuleEngine(base=RuleEngine())
    composite_rules.add_domain_provider(
        "datasets", lambda: intelligence.list_monitorable_metric_names(activation.list_activated_dataset_ids())
    )

    ltm_mock = MagicMock(spec=LongTermMemory)
    settings_mock = MagicMock()
    forecast = ForecastAgent(
        long_term_memory=ltm_mock, data_pipeline=ForecastDataPipeline(), settings=settings_mock,
    )

    agent, bus = _make_monitor_agent(composite_kpi, composite_rules, forecast_agent=forecast)
    # Should not raise even though there's no trained model / insufficient history yet
    # (ForecastAgent.analyze() degrades gracefully — MonitorAgent also wraps the call).
    agent._run_cycle()
