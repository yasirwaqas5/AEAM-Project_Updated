"""
aeam/tests/test_phase_b1_5_3_composition_activation.py

Phase B1.5.3 — Dataset Source Composition & Activation.

Three layers:

1. StaticDatasetActivation / parse_activated_dataset_ids — the explicit,
   never-automatic activation policy.
2. CompositeKPISource — protocol conformance, pass-through/multi composition,
   failure isolation, and the exact Sheets-may-be-None safety fix applied in
   main.py's wiring.
3. End-to-end integration: replicates the ACTUAL main.py wiring pattern
   (DatasetRepository -> VersionRepository -> DatasetIntelligenceService ->
   DatasetKPISource -> StaticDatasetActivation -> CompositeKPISource) against
   real SQLite + real BlobStore, then drives the real, UNMODIFIED
   MonitorAgent through one real ``_run_cycle()`` — proving:
     - an activated dataset's anomalous metric produces a real Event
     - a non-activated (registered but not activated) dataset produces
       nothing — activation actually gates monitoring, not auto-monitor-all
     - MonitorAgent, RuleEngine, StatisticalDetector are all touched with
       ZERO source changes.

No Qdrant, no Redis, no Postgres, no live network required anywhere in this
suite.
"""

from __future__ import annotations

import pytest

from aeam.agents.kpi.rule_engine import RuleEngine
from aeam.agents.kpi.statistical_detector import StatisticalDetector
from aeam.agents.monitor.monitor_agent import KPIRowSource, MonitorAgent
from aeam.connectors.composite_kpi_source import CompositeKPISource
from aeam.core.deduplication import EventDeduplicator
from aeam.core.event_bus import EventBus
from aeam.core.priority_queue import EventPriorityQueue
from aeam.integrations.database import DatabaseClient
from aeam.intelligence.dataset_activation import (
    DatasetActivation,
    StaticDatasetActivation,
    parse_activated_dataset_ids,
)
from aeam.intelligence.dataset_intelligence import DatasetIntelligenceService
from aeam.intelligence.dataset_kpi_source import DatasetKPISource
from aeam.pipelines.structured_data_pipeline import StructuredDataPipeline
from aeam.registry.models import AssetStatus, Dataset, ParentType, Schema, Version
from aeam.registry.repositories import DatasetRepository, SchemaRepository, VersionRepository
from aeam.storage.blob_store import LocalDiskBlobStore


# ===========================================================================
# 1. Activation policy
# ===========================================================================

def test_parse_activated_dataset_ids_basic():
    assert parse_activated_dataset_ids("a,b,c") == ["a", "b", "c"]


def test_parse_activated_dataset_ids_strips_and_drops_blanks():
    assert parse_activated_dataset_ids(" a , , b ,") == ["a", "b"]


def test_parse_activated_dataset_ids_dedupes_preserving_order():
    assert parse_activated_dataset_ids("a,b,a,c,b") == ["a", "b", "c"]


def test_parse_activated_dataset_ids_empty_or_none():
    assert parse_activated_dataset_ids("") == []
    assert parse_activated_dataset_ids(None) == []
    assert parse_activated_dataset_ids("   ") == []


def test_static_dataset_activation_returns_list():
    activation = StaticDatasetActivation(["ds-1", "ds-2"])
    assert activation.list_activated_dataset_ids() == ["ds-1", "ds-2"]


def test_static_dataset_activation_empty_by_default():
    activation = StaticDatasetActivation()
    assert activation.list_activated_dataset_ids() == []


def test_static_dataset_activation_drops_blanks():
    activation = StaticDatasetActivation(["ds-1", "  ", "", "ds-2"])
    assert activation.list_activated_dataset_ids() == ["ds-1", "ds-2"]


def test_static_dataset_activation_satisfies_protocol():
    assert isinstance(StaticDatasetActivation(["ds-1"]), DatasetActivation)


# ===========================================================================
# 2. CompositeKPISource
# ===========================================================================

class _StubSource:
    """Minimal KPIRowSource for isolated composite tests."""

    def __init__(self, rows_by_selector: dict[str, list[dict]] | None = None, raises: bool = False):
        self._rows = rows_by_selector or {}
        self._raises = raises
        self.calls: list[str] = []

    def fetch_rows(self, selector: str) -> list[dict]:
        self.calls.append(selector)
        if self._raises:
            raise RuntimeError("boom")
        return self._rows.get(selector, [])


def test_composite_satisfies_kpirowsource_protocol():
    assert isinstance(CompositeKPISource(), KPIRowSource)


def test_composite_empty_returns_empty_list():
    assert CompositeKPISource().fetch_rows("Sheet1") == []


def test_composite_passthrough_forwards_caller_selector_verbatim():
    stub = _StubSource({"Sheet1": [{"sales": 100.0}]})
    composite = CompositeKPISource().add_passthrough(stub)

    rows = composite.fetch_rows("Sheet1")

    assert rows == [{"sales": 100.0}]
    assert stub.calls == ["Sheet1"]  # exact selector MonitorAgent would have passed


def test_composite_multi_ignores_caller_selector_uses_provided_list():
    stub = _StubSource({"ds-a": [{"revenue": 1.0}], "ds-b": [{"revenue": 2.0}]})
    composite = CompositeKPISource().add_multi(stub, lambda: ["ds-a", "ds-b"])

    rows = composite.fetch_rows("Sheet1")  # irrelevant to the multi member

    assert rows == [{"revenue": 1.0}, {"revenue": 2.0}]
    assert stub.calls == ["ds-a", "ds-b"]


def test_composite_merges_passthrough_and_multi():
    sheets = _StubSource({"Sheet1": [{"sales": 500.0}]})
    datasets = _StubSource({"ds-1": [{"revenue": 10.0}]})
    composite = CompositeKPISource().add_passthrough(sheets).add_multi(datasets, lambda: ["ds-1"])

    rows = composite.fetch_rows("Sheet1")

    assert rows == [{"sales": 500.0}, {"revenue": 10.0}]
    assert composite.member_count == 2


def test_composite_multi_selectors_reevaluated_every_call():
    calls = {"n": 0}

    def dynamic_ids():
        calls["n"] += 1
        return ["ds-1"] if calls["n"] == 1 else ["ds-1", "ds-2"]

    stub = _StubSource({"ds-1": [{"x": 1}], "ds-2": [{"x": 2}]})
    composite = CompositeKPISource().add_multi(stub, dynamic_ids)

    first = composite.fetch_rows("_")
    second = composite.fetch_rows("_")

    assert first == [{"x": 1}]
    assert second == [{"x": 1}, {"x": 2}]  # a newly-activated id shows up with no rewiring


def test_composite_isolates_a_failing_member_never_raises():
    good = _StubSource({"Sheet1": [{"sales": 1.0}]})
    bad = _StubSource(raises=True)
    composite = CompositeKPISource().add_passthrough(good).add_passthrough(bad)

    rows = composite.fetch_rows("Sheet1")

    assert rows == [{"sales": 1.0}]  # the good member's rows still come through


def test_composite_isolates_a_failing_selector_provider():
    def boom():
        raise RuntimeError("provider exploded")

    good = _StubSource({"Sheet1": [{"sales": 1.0}]})
    bad = _StubSource()
    composite = CompositeKPISource().add_passthrough(good).add_multi(bad, boom)

    rows = composite.fetch_rows("Sheet1")

    assert rows == [{"sales": 1.0}]


def test_composite_repr_reports_member_count():
    composite = CompositeKPISource().add_passthrough(_StubSource())
    assert "members=1" in repr(composite)


# ===========================================================================
# 3. End-to-end integration — real MonitorAgent, real activation gate
# ===========================================================================

@pytest.fixture()
def db(tmp_path):
    client = DatabaseClient(database_url=f"sqlite:///{(tmp_path / 'b153.db').as_posix()}")
    yield client
    client.dispose()


@pytest.fixture()
def blob_store(tmp_path):
    return LocalDiskBlobStore(tmp_path / "blobs")


def _col(name, type_, role, is_metric=False):
    return {"name": name, "type": type_, "nullable": False, "is_metric": is_metric, "role": role}


def _dataset_columns(metric_column: str) -> list[dict]:
    return [
        _col("region", "string", "dimension"),
        _col(metric_column, "float", "metric", is_metric=True),
        _col("event_date", "string", "dimension"),  # B1.5.1 name-heuristic-promoted
    ]


def _seed_dataset(db, blob_store, csv_bytes: bytes, name: str, metric_column: str = "revenue") -> str:
    dataset_repo = DatasetRepository(db)
    schema_repo = SchemaRepository(db)
    version_repo = VersionRepository(db)

    ref = blob_store.put(csv_bytes)
    schema_id = schema_repo.create(Schema(object_name=name, columns=_dataset_columns(metric_column)))
    dataset_id = dataset_repo.create(Dataset(
        name=name, schema_id=schema_id, status=AssetStatus.INDEXED,
        metric_columns=[metric_column], row_count=csv_bytes.count(b"\n") - 1,
    ))
    version_repo.create(Version(
        parent_type=ParentType.DATASET, parent_id=dataset_id, version=1,
        content_hash=ref.content_hash, blob_ref=ref.uri, is_active=True,
    ))
    return dataset_id


def _wire_composite_like_main(db, blob_store, activated_ids: list[str], sheets_connector=None):
    """
    Mirrors main.py's B1.5.3 wiring block exactly: same classes, same
    construction order, same conditional-Sheets-membership guard.
    """
    dataset_repo = DatasetRepository(db)
    version_repo = VersionRepository(db)
    intelligence = DatasetIntelligenceService(dataset_repo=dataset_repo, schema_repo=SchemaRepository(db))
    dataset_kpi_source = DatasetKPISource(
        blob_store=blob_store, dataset_repo=dataset_repo,
        version_repo=version_repo, intelligence=intelligence,
    )
    activation = StaticDatasetActivation(activated_ids)

    composite = CompositeKPISource()
    if sheets_connector is not None:
        composite.add_passthrough(sheets_connector)
    composite.add_multi(dataset_kpi_source, activation.list_activated_dataset_ids)
    return composite


def _make_monitor_agent(kpi_source) -> tuple[MonitorAgent, EventBus]:
    """Real, UNMODIFIED MonitorAgent + its minimal real dependencies."""
    from unittest.mock import MagicMock

    bus = EventBus()
    queue = EventPriorityQueue()
    redis_mock = MagicMock()
    # EventDeduplicator uses SET key val EX ttl NX — True means "newly set"
    # (not a duplicate); every event in these tests should be treated as novel.
    redis_mock.set.return_value = True
    dedup = EventDeduplicator(redis_client=redis_mock)
    settings = MagicMock()
    settings.MONITOR_INTERVAL_SECONDS = 60
    settings.MAX_INVESTIGATION_DEPTH = 3
    settings.SHEET_RANGE = "Sheet1!A2:C10"

    agent = MonitorAgent(
        event_bus=bus, queue=queue, deduplicator=dedup,
        rule_engine=RuleEngine(), statistical_detector=StatisticalDetector(window_size=3),
        forecast_agent=None, pipeline=StructuredDataPipeline(), settings=settings,
        kpi_source=kpi_source,
    )
    return agent, bus


def test_activated_dataset_feeds_real_monitor_agent_cycle(db, blob_store):
    """
    End-to-end proof: an activated dataset's metric column, named to match an
    EXISTING curated RuleEngine domain ("sales" — a real top-level key in
    detection_rules.yaml, unmodified), flows through the real MonitorAgent
    cycle it always has, and raises a real Event — with zero agent changes.

    MonitorAgent._run_cycle only iterates `rule_engine.loaded_domains` (a
    pre-existing, unmodified constraint — see test below for the documented
    boundary this implies for an arbitrary/uncurated metric name).
    """
    # A clear statistical anomaly: steady ~10, then a spike to 100.
    csv = (
        b"region,sales,event_date\n"
        b"EMEA,10.0,2026-01-01\n"
        b"EMEA,11.0,2026-01-02\n"
        b"EMEA,10.5,2026-01-03\n"
        b"EMEA,9.8,2026-01-04\n"
        b"EMEA,100.0,2026-01-05\n"
    )
    dataset_id = _seed_dataset(db, blob_store, csv, "sales.csv", metric_column="sales")
    composite = _wire_composite_like_main(db, blob_store, activated_ids=[dataset_id])
    agent, bus = _make_monitor_agent(composite)

    events: list = []
    bus.register_handler("*", lambda evt: events.append(evt))

    agent._run_cycle()  # exactly what MonitorAgent.start()'s loop calls — unmodified

    assert len(events) >= 1, "an activated dataset's anomalous metric should raise a real Event"
    assert any(e.metric == "sales" for e in events)


def test_non_activated_dataset_produces_nothing(db, blob_store):
    """
    Same anomalous "sales" dataset as the previous test, but NOT activated —
    isolates the activation variable specifically (using a domain RuleEngine
    already recognises, so a pass here proves activation actually gates
    monitoring, not merely that the metric name went unrecognised).
    """
    csv = (
        b"region,sales,event_date\n"
        b"EMEA,10.0,2026-01-01\n"
        b"EMEA,11.0,2026-01-02\n"
        b"EMEA,100.0,2026-01-03\n"
    )
    dataset_id = _seed_dataset(db, blob_store, csv, "sales.csv", metric_column="sales")
    # Registered but NOT in the activation list.
    composite = _wire_composite_like_main(db, blob_store, activated_ids=[])
    agent, bus = _make_monitor_agent(composite)

    events: list = []
    bus.register_handler("*", lambda evt: events.append(evt))

    agent._run_cycle()

    assert events == [], "activation must actually gate monitoring — no auto-monitor-all"


def test_arbitrary_metric_name_is_fetched_but_not_yet_domain_discovered(db, blob_store):
    """
    Documents a real, disclosed scope boundary (not a bug): MonitorAgent's
    unmodified `_run_cycle` only evaluates metric names present in
    `rule_engine.loaded_domains` (RuleEngine is explicitly not modified in
    this phase). An activated dataset whose metric column is NOT one of the
    curated domains ("revenue", vs. sales/complaints/inventory) is correctly
    composed and fetchable — proven directly against the composite below —
    but is not iterated by a stock RuleEngine() this cycle. Making arbitrary
    dataset metrics domain-discoverable is deferred (would require a
    RuleEngine-side domain-merge wrapper, out of scope here since RuleEngine
    must not be modified/wrapped-and-substituted in this phase).
    """
    csv = b"region,revenue,event_date\nEMEA,10.0,2026-01-01\nEMEA,100.0,2026-01-02\n"
    dataset_id = _seed_dataset(db, blob_store, csv, "sales.csv", metric_column="revenue")
    composite = _wire_composite_like_main(db, blob_store, activated_ids=[dataset_id])

    # The data IS reachable through the composed source (activation + fetch work).
    rows = composite.fetch_rows("Sheet1!A2:C10")
    assert any("revenue" in row for row in rows), "activated dataset's rows must be fetchable"

    # But the unmodified MonitorAgent cycle does not evaluate it, since
    # "revenue" is not in RuleEngine().loaded_domains.
    agent, bus = _make_monitor_agent(composite)
    events: list = []
    bus.register_handler("*", lambda evt: events.append(evt))
    agent._run_cycle()
    assert events == []


def test_sheets_none_composite_is_still_a_clean_noop(db, blob_store):
    """
    Reproduces the exact regression risk this phase had to avoid: when
    container.sheets_connector is None and no dataset is activated, the
    composite must behave as a functional no-op (empty rows), matching
    MonitorAgent's pre-B1.5.3 `kpi_source=None` fast-path behaviour.
    """
    composite = _wire_composite_like_main(db, blob_store, activated_ids=[], sheets_connector=None)
    agent, bus = _make_monitor_agent(composite)

    events: list = []
    bus.register_handler("*", lambda evt: events.append(evt))

    agent._run_cycle()  # must not raise, must not error-log a None.fetch_rows() call

    assert events == []


def test_composite_with_both_sheets_and_activated_dataset(db, blob_store):
    csv = b"region,revenue,event_date\nEMEA,10.0,2026-01-01\nEMEA,10.5,2026-01-02\n"
    dataset_id = _seed_dataset(db, blob_store, csv, "sales.csv")

    class _FakeSheets:
        def fetch_rows(self, selector):
            return [{"sales": 1000.0}, {"sales": 950.0}]

    composite = _wire_composite_like_main(
        db, blob_store, activated_ids=[dataset_id], sheets_connector=_FakeSheets()
    )
    assert composite.member_count == 2

    rows = composite.fetch_rows("Sheet1!A2:C10")
    # Both sources' columns present in the merged feed.
    keys = {k for row in rows for k in row}
    assert "sales" in keys
    assert "revenue" in keys
