"""
aeam/tests/test_phase_b1_5_2_dataset_kpi_source.py

Phase B1.5.2 — DatasetKPISource.

Real components throughout (no mocks of AEAM internals): SQLite
DatabaseClient, LocalDiskBlobStore, DatasetIntelligenceService (B1.5.1),
DatasetRepository/SchemaRepository/VersionRepository. Dataset/Schema/Version
rows are seeded directly (mirroring what DatasetIngestJobProcessor produces),
since this phase does not touch ingestion.

Also verifies structural conformance to the existing
``aeam.agents.monitor.monitor_agent.KPIRowSource`` protocol, and that
MonitorAgent's real ``_extract_series`` matching logic works unmodified
against rows produced by DatasetKPISource.
"""

from __future__ import annotations

import pytest

from aeam.agents.monitor.monitor_agent import KPIRowSource, MonitorAgent
from aeam.intelligence import DatasetIntelligenceService, DatasetKPISource
from aeam.integrations.database import DatabaseClient
from aeam.storage.blob_store import LocalDiskBlobStore
from aeam.registry.models import AssetStatus, Dataset, ParentType, Schema, Version
from aeam.registry.repositories import DatasetRepository, SchemaRepository, VersionRepository


# ===========================================================================
# Fixtures
# ===========================================================================

@pytest.fixture()
def db(tmp_path):
    client = DatabaseClient(database_url=f"sqlite:///{(tmp_path / 'b152.db').as_posix()}")
    yield client
    client.dispose()


@pytest.fixture()
def blob_store(tmp_path):
    return LocalDiskBlobStore(tmp_path / "blobs")


class _CountingBlobStore:
    """Wraps a real BlobStore, counting .get() calls (for cache-hit assertions)."""

    def __init__(self, inner):
        self._inner = inner
        self.get_calls = 0

    def put(self, data, *, content_type=None):
        return self._inner.put(data, content_type=content_type)

    def get(self, content_hash):
        self.get_calls += 1
        return self._inner.get(content_hash)

    def exists(self, content_hash):
        return self._inner.exists(content_hash)

    def delete(self, content_hash):
        return self._inner.delete(content_hash)

    def stat(self, content_hash):
        return self._inner.stat(content_hash)


def _source(db, blob_store, max_rows=500, max_cache_entries=32, counting=False):
    store = _CountingBlobStore(blob_store) if counting else blob_store
    dataset_repo = DatasetRepository(db)
    schema_repo = SchemaRepository(db)
    version_repo = VersionRepository(db)
    intelligence = DatasetIntelligenceService(dataset_repo=dataset_repo, schema_repo=schema_repo)
    kpi_source = DatasetKPISource(
        blob_store=store, dataset_repo=dataset_repo, version_repo=version_repo,
        intelligence=intelligence, max_rows=max_rows, max_cache_entries=max_cache_entries,
    )
    return kpi_source, store, dataset_repo, schema_repo, version_repo


def _seed_dataset(db, blob_store, csv_bytes: bytes, columns: list[dict], name="sales.csv",
                  row_count=None, status=AssetStatus.INDEXED):
    """Seed Dataset + Schema + active Version rows, mirroring DatasetIngestJobProcessor's output."""
    dataset_repo = DatasetRepository(db)
    schema_repo = SchemaRepository(db)
    version_repo = VersionRepository(db)

    ref = blob_store.put(csv_bytes)
    metric_cols = [c["name"] for c in columns if c.get("is_metric")]
    schema_id = schema_repo.create(Schema(object_name=name, columns=columns))
    dataset_id = dataset_repo.create(Dataset(
        name=name, schema_id=schema_id,
        row_count=row_count if row_count is not None else csv_bytes.count(b"\n") - 1,
        metric_columns=metric_cols, status=status,
    ))
    version_repo.create(Version(
        parent_type=ParentType.DATASET, parent_id=dataset_id, version=1,
        content_hash=ref.content_hash, blob_ref=ref.uri, is_active=True,
    ))
    return dataset_id


def _col(name, type_, role, is_metric=False):
    return {"name": name, "type": type_, "nullable": False, "is_metric": is_metric, "role": role}


SALES_COLUMNS = [
    _col("region", "string", "dimension"),
    _col("revenue", "float", "metric", is_metric=True),
    _col("event_date", "string", "dimension"),  # promoted by B1.5.1 name heuristic
]


# ===========================================================================
# 1. Protocol conformance
# ===========================================================================

def test_dataset_kpi_source_satisfies_kpirowsource_protocol(db, blob_store):
    kpi_source, *_ = _source(db, blob_store)
    assert isinstance(kpi_source, KPIRowSource)


# ===========================================================================
# 2. Never-raise degrade-to-empty-list contract
# ===========================================================================

def test_fetch_rows_unknown_dataset_returns_empty(db, blob_store):
    kpi_source, *_ = _source(db, blob_store)
    assert kpi_source.fetch_rows("does-not-exist") == []


def test_fetch_rows_blank_selector_returns_empty(db, blob_store):
    kpi_source, *_ = _source(db, blob_store)
    assert kpi_source.fetch_rows("") == []
    assert kpi_source.fetch_rows("   ") == []


def test_fetch_rows_dataset_without_schema_returns_empty(db, blob_store):
    dataset_repo = DatasetRepository(db)
    dataset_id = dataset_repo.create(Dataset(name="pending.csv", status=AssetStatus.PENDING))
    kpi_source, *_ = _source(db, blob_store)
    assert kpi_source.fetch_rows(dataset_id) == []


def test_fetch_rows_dataset_without_active_version_returns_empty(db, blob_store):
    dataset_repo = DatasetRepository(db)
    schema_repo = SchemaRepository(db)
    schema_id = schema_repo.create(Schema(object_name="x.csv", columns=SALES_COLUMNS))
    dataset_id = dataset_repo.create(Dataset(
        name="x.csv", schema_id=schema_id, status=AssetStatus.INDEXED,
    ))  # no Version row created
    kpi_source, *_ = _source(db, blob_store)
    assert kpi_source.fetch_rows(dataset_id) == []


def test_fetch_rows_no_monitorable_measures_returns_empty(db, blob_store):
    columns = [_col("region", "string", "dimension"), _col("event_date", "datetime", "timestamp")]
    csv = b"region,event_date\nEMEA,2026-01-01\n"
    dataset_id = _seed_dataset(db, blob_store, csv, columns)
    kpi_source, *_ = _source(db, blob_store)
    assert kpi_source.fetch_rows(dataset_id) == []


def test_fetch_rows_corrupt_blob_never_raises(db, blob_store):
    # A blob that exists but isn't valid CSV/Excel for the declared category.
    dataset_id = _seed_dataset(db, blob_store, b"\x00\x01\x02not a table", SALES_COLUMNS, name="bad.csv")
    kpi_source, *_ = _source(db, blob_store)
    assert kpi_source.fetch_rows(dataset_id) == []


# ===========================================================================
# 3. Happy path: projection, chronological ordering, NaN handling
# ===========================================================================

def test_fetch_rows_happy_path_projects_and_sorts_chronologically(db, blob_store):
    # Rows deliberately OUT of chronological order in the file.
    csv = (
        b"region,revenue,event_date\n"
        b"EMEA,300.0,2026-03-01\n"
        b"EMEA,100.0,2026-01-01\n"
        b"EMEA,200.0,2026-02-01\n"
    )
    dataset_id = _seed_dataset(db, blob_store, csv, SALES_COLUMNS)
    kpi_source, *_ = _source(db, blob_store)

    rows = kpi_source.fetch_rows(dataset_id)

    assert len(rows) == 3
    # Projected: only timestamp_column + measures — no 'region' dimension.
    assert set(rows[0].keys()) == {"event_date", "revenue"}
    # Sorted ascending by the (heuristically-promoted, then value-parsed) date.
    assert [r["revenue"] for r in rows] == [100.0, 200.0, 300.0]


def test_fetch_rows_multiple_measures(db, blob_store):
    columns = [
        _col("region", "string", "dimension"),
        _col("revenue", "float", "metric", is_metric=True),
        _col("units", "integer", "metric", is_metric=True),
        _col("event_date", "string", "dimension"),
    ]
    csv = b"region,revenue,units,event_date\nEMEA,100.0,5,2026-01-01\nAPAC,200.0,10,2026-01-02\n"
    dataset_id = _seed_dataset(db, blob_store, csv, columns)
    kpi_source, *_ = _source(db, blob_store)

    rows = kpi_source.fetch_rows(dataset_id)
    assert len(rows) == 2
    assert set(rows[0].keys()) == {"event_date", "revenue", "units"}


def test_fetch_rows_missing_values_become_none(db, blob_store):
    csv = b"region,revenue,event_date\nEMEA,,2026-01-01\nAPAC,50.0,2026-01-02\n"
    dataset_id = _seed_dataset(db, blob_store, csv, SALES_COLUMNS)
    kpi_source, *_ = _source(db, blob_store)

    rows = kpi_source.fetch_rows(dataset_id)
    revenues = [r["revenue"] for r in rows]
    assert None in revenues, f"expected a None for the blank cell, got {revenues!r}"
    assert 50.0 in revenues


def test_fetch_rows_without_timestamp_returns_unsorted_but_complete(db, blob_store):
    columns = [
        _col("region", "string", "dimension"),
        _col("revenue", "float", "metric", is_metric=True),
    ]
    csv = b"region,revenue\nEMEA,300.0\nAPAC,100.0\n"
    dataset_id = _seed_dataset(db, blob_store, csv, columns)
    kpi_source, *_ = _source(db, blob_store)

    rows = kpi_source.fetch_rows(dataset_id)
    assert len(rows) == 2
    assert set(rows[0].keys()) == {"revenue"}  # no timestamp column to include
    assert sorted(r["revenue"] for r in rows) == [100.0, 300.0]


def test_fetch_rows_windows_to_max_rows_keeping_most_recent(db, blob_store):
    lines = [b"region,revenue,event_date"]
    for i in range(1, 11):
        lines.append(f"EMEA,{float(i)},2026-01-{i:02d}".encode())
    csv = b"\n".join(lines) + b"\n"
    dataset_id = _seed_dataset(db, blob_store, csv, SALES_COLUMNS)
    kpi_source, *_ = _source(db, blob_store, max_rows=3)

    rows = kpi_source.fetch_rows(dataset_id)
    assert len(rows) == 3
    # The 3 most recent (highest) values, still ascending.
    assert [r["revenue"] for r in rows] == [8.0, 9.0, 10.0]


# ===========================================================================
# 4. Caching — content-hash identity, LRU eviction
# ===========================================================================

def test_fetch_rows_caches_by_content_hash_avoids_reread(db, blob_store):
    csv = b"region,revenue,event_date\nEMEA,100.0,2026-01-01\n"
    dataset_id = _seed_dataset(db, blob_store, csv, SALES_COLUMNS)
    kpi_source, counting_store, *_ = _source(db, blob_store, counting=True)

    rows1 = kpi_source.fetch_rows(dataset_id)
    rows2 = kpi_source.fetch_rows(dataset_id)

    assert rows1 == rows2
    assert counting_store.get_calls == 1, "second fetch should be served from cache, not re-read the blob"
    assert kpi_source.cache_size == 1


def test_fetch_rows_new_version_invalidates_cache(db, blob_store):
    csv_v1 = b"region,revenue,event_date\nEMEA,100.0,2026-01-01\n"
    dataset_id = _seed_dataset(db, blob_store, csv_v1, SALES_COLUMNS)
    kpi_source, counting_store, dataset_repo, schema_repo, version_repo = _source(
        db, blob_store, counting=True
    )

    rows_v1 = kpi_source.fetch_rows(dataset_id)
    assert rows_v1[0]["revenue"] == 100.0
    assert counting_store.get_calls == 1

    # Re-ingest: new bytes -> new content_hash -> new active Version + Schema,
    # exactly as DatasetIngestJobProcessor would produce.
    csv_v2 = b"region,revenue,event_date\nEMEA,999.0,2026-02-01\n"
    ref2 = blob_store.put(csv_v2)
    version_repo.deactivate_all(ParentType.DATASET, dataset_id)
    new_schema_id = schema_repo.create(Schema(object_name="sales.csv", columns=SALES_COLUMNS))
    version_repo.create(Version(
        parent_type=ParentType.DATASET, parent_id=dataset_id, version=2,
        content_hash=ref2.content_hash, blob_ref=ref2.uri, is_active=True,
    ))
    dataset_repo.update(dataset_id, {"schema_id": new_schema_id})

    rows_v2 = kpi_source.fetch_rows(dataset_id)
    assert rows_v2[0]["revenue"] == 999.0, "must reflect the new version, not the stale cache"
    assert counting_store.get_calls == 2, "a new content_hash must trigger a real re-read"
    assert kpi_source.cache_size == 2  # both (dataset_id, hash_v1) and (dataset_id, hash_v2) retained


def test_fetch_rows_lru_evicts_least_recently_used(db, blob_store):
    ds_a = _seed_dataset(db, blob_store, b"region,revenue,event_date\nEMEA,1.0,2026-01-01\n",
                         SALES_COLUMNS, name="a.csv")
    ds_b = _seed_dataset(db, blob_store, b"region,revenue,event_date\nEMEA,2.0,2026-01-01\n",
                         SALES_COLUMNS, name="b.csv")
    kpi_source, counting_store, *_ = _source(db, blob_store, counting=True, max_cache_entries=1)

    kpi_source.fetch_rows(ds_a)   # cache: {a}
    kpi_source.fetch_rows(ds_b)   # cache: {b}  (a evicted, cap=1)
    assert kpi_source.cache_size == 1
    assert counting_store.get_calls == 2

    kpi_source.fetch_rows(ds_a)   # a no longer cached -> real re-read
    assert counting_store.get_calls == 3


# ===========================================================================
# 5. Integration with the real, UNMODIFIED MonitorAgent._extract_series
# ===========================================================================

def test_rows_are_consumable_by_real_extract_series(db, blob_store):
    csv = b"region,revenue,event_date\nEMEA,100.0,2026-01-01\nEMEA,150.0,2026-01-02\n"
    dataset_id = _seed_dataset(db, blob_store, csv, SALES_COLUMNS)
    kpi_source, *_ = _source(db, blob_store)

    rows = kpi_source.fetch_rows(dataset_id)
    # MonitorAgent._extract_series is a @staticmethod — call it directly,
    # exactly as MonitorAgent._run_cycle does, with zero changes to the agent.
    series = MonitorAgent._extract_series(rows, "revenue")
    assert series == [100.0, 150.0]


# ===========================================================================
# 6. Constructor validation
# ===========================================================================

def test_constructor_rejects_none_dependencies(db, blob_store):
    dataset_repo = DatasetRepository(db)
    schema_repo = SchemaRepository(db)
    version_repo = VersionRepository(db)
    intelligence = DatasetIntelligenceService(dataset_repo=dataset_repo, schema_repo=schema_repo)

    with pytest.raises(ValueError):
        DatasetKPISource(None, dataset_repo, version_repo, intelligence)
    with pytest.raises(ValueError):
        DatasetKPISource(blob_store, None, version_repo, intelligence)
    with pytest.raises(ValueError):
        DatasetKPISource(blob_store, dataset_repo, None, intelligence)
    with pytest.raises(ValueError):
        DatasetKPISource(blob_store, dataset_repo, version_repo, None)


def test_constructor_rejects_non_positive_limits(db, blob_store):
    dataset_repo = DatasetRepository(db)
    schema_repo = SchemaRepository(db)
    version_repo = VersionRepository(db)
    intelligence = DatasetIntelligenceService(dataset_repo=dataset_repo, schema_repo=schema_repo)

    with pytest.raises(ValueError):
        DatasetKPISource(blob_store, dataset_repo, version_repo, intelligence, max_rows=0)
    with pytest.raises(ValueError):
        DatasetKPISource(blob_store, dataset_repo, version_repo, intelligence, max_cache_entries=0)
