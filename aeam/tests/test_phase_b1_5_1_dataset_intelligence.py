"""
aeam/tests/test_phase_b1_5_1_dataset_intelligence.py

Phase B1.5.1 — Dataset Intelligence Service.

Two layers:

1. Pure discovery functions (aeam.intelligence.dataset_intelligence) — operate
   only on a Schema.columns-shaped list, no repository/DB involved.
2. DatasetIntelligenceService — profile_from_schema (pure) and build_profile
   (repository-backed, real SQLite DatabaseClient, no blob/ingestion needed:
   this phase only reads Dataset + Schema registry rows).

No Qdrant, no BlobStore, no monitoring/rules/forecasting/incidents are
touched anywhere in this suite — enforcing the B1.5.1 scope boundary.
"""

from __future__ import annotations

import pytest

from aeam.intelligence import (
    DatasetIntelligenceError,
    DatasetIntelligenceService,
    MonitorableMetric,
    build_monitorable_metrics,
    discover_dimensions,
    discover_forecast_candidates,
    discover_identifiers,
    discover_measures,
    discover_timestamp_column,
)
from aeam.integrations.database import DatabaseClient
from aeam.registry.models import AssetStatus, Dataset, Schema
from aeam.registry.repositories import DatasetRepository, SchemaRepository


# ===========================================================================
# Fixtures — column-list builders (Schema.columns shape from B1.4)
# ===========================================================================

def _col(name, type_, role, is_metric=False, nullable=False):
    return {"name": name, "type": type_, "nullable": nullable, "is_metric": is_metric, "role": role}


SALES_COLUMNS = [
    _col("user_id", "integer", "identifier"),
    _col("region", "string", "dimension"),
    _col("revenue", "float", "metric", is_metric=True),
    _col("units", "integer", "metric", is_metric=True),
    _col("event_time", "datetime", "timestamp"),
]

NO_TIMESTAMP_COLUMNS = [
    _col("region", "string", "dimension"),
    _col("revenue", "float", "metric", is_metric=True),
]

HEURISTIC_DATE_COLUMNS = [
    _col("created_at", "string", "dimension"),
    _col("region", "string", "dimension"),
    _col("revenue", "float", "metric", is_metric=True),
]

FALSE_POSITIVE_COLUMNS = [
    # None of these should be mistaken for a timestamp column.
    _col("validated", "string", "dimension"),
    _col("candidate", "string", "dimension"),
    _col("update", "string", "dimension"),
    _col("revenue", "float", "metric", is_metric=True),
]

NO_METRIC_COLUMNS = [
    _col("region", "string", "dimension"),
    _col("event_time", "datetime", "timestamp"),
]


# ===========================================================================
# 1. Pure discovery functions
# ===========================================================================

def test_discover_measures():
    assert discover_measures(SALES_COLUMNS) == ["revenue", "units"]


def test_discover_measures_none_present():
    assert discover_measures(NO_METRIC_COLUMNS) == []


def test_discover_dimensions_excludes_metric_identifier_timestamp():
    assert discover_dimensions(SALES_COLUMNS) == ["region"]


def test_discover_identifiers():
    assert discover_identifiers(SALES_COLUMNS) == ["user_id"]
    assert discover_identifiers(NO_METRIC_COLUMNS) == []


def test_discover_timestamp_column_authoritative_role():
    assert discover_timestamp_column(SALES_COLUMNS) == "event_time"


def test_discover_timestamp_column_none_when_absent():
    assert discover_timestamp_column(NO_TIMESTAMP_COLUMNS) is None


def test_discover_timestamp_column_name_heuristic_promotion():
    assert discover_timestamp_column(HEURISTIC_DATE_COLUMNS) == "created_at"


@pytest.mark.parametrize("name", ["validated", "candidate", "update"])
def test_discover_timestamp_column_avoids_substring_false_positives(name):
    # Whole-token matching must not be fooled by "date" appearing inside these words.
    result = discover_timestamp_column(FALSE_POSITIVE_COLUMNS)
    assert result is None, f"heuristic incorrectly promoted a false positive (got {result!r})"


def test_discover_forecast_candidates_with_timestamp():
    measures = ["revenue", "units"]
    assert discover_forecast_candidates(measures, "event_time") == ["revenue", "units"]


def test_discover_forecast_candidates_without_timestamp():
    assert discover_forecast_candidates(["revenue"], None) == []


def test_build_monitorable_metrics_shape_and_determinism():
    measures = discover_measures(SALES_COLUMNS)
    dims = discover_dimensions(SALES_COLUMNS)
    ts = discover_timestamp_column(SALES_COLUMNS)

    metrics = build_monitorable_metrics("ds-1", measures, SALES_COLUMNS, ts, dims)
    metrics_again = build_monitorable_metrics("ds-1", measures, SALES_COLUMNS, ts, dims)

    assert len(metrics) == 2
    assert all(isinstance(m, MonitorableMetric) for m in metrics)
    by_col = {m.column: m for m in metrics}
    assert by_col["revenue"].metric_id == "ds-1:revenue"
    assert by_col["revenue"].data_type == "float"
    assert by_col["revenue"].timestamp_column == "event_time"
    assert by_col["revenue"].dimensions == ["region"]
    assert by_col["revenue"].forecastable is True
    assert by_col["units"].forecastable is True
    # metric_id is deterministic/stable across regenerations.
    assert [m.metric_id for m in metrics] == [m.metric_id for m in metrics_again]


def test_build_monitorable_metrics_not_forecastable_without_timestamp():
    measures = discover_measures(NO_TIMESTAMP_COLUMNS)
    dims = discover_dimensions(NO_TIMESTAMP_COLUMNS)
    metrics = build_monitorable_metrics("ds-2", measures, NO_TIMESTAMP_COLUMNS, None, dims)
    assert len(metrics) == 1
    assert metrics[0].forecastable is False
    assert metrics[0].timestamp_column is None


def test_build_monitorable_metrics_empty_measures():
    assert build_monitorable_metrics("ds-3", [], NO_METRIC_COLUMNS, "event_time", []) == []


# ===========================================================================
# 2a. profile_from_schema (pure)
# ===========================================================================

def _make(dataset_id="ds-1", schema_id="sc-1", columns=SALES_COLUMNS, row_count=3, name="sales.csv"):
    dataset = Dataset(
        dataset_id=dataset_id, name=name, schema_id=schema_id,
        row_count=row_count, status=AssetStatus.INDEXED,
        metric_columns=discover_measures(columns),
    )
    schema = Schema(schema_id=schema_id, object_name=name, columns=columns)
    return dataset, schema


def test_profile_from_schema_full_business_profile():
    dataset, schema = _make()
    service = DatasetIntelligenceService(dataset_repo=object(), schema_repo=object())
    profile = service.profile_from_schema(dataset, schema)

    assert profile.dataset_id == "ds-1"
    assert profile.dataset_name == "sales.csv"
    assert profile.schema_id == "sc-1"
    assert profile.row_count == 3
    assert profile.measures == ["revenue", "units"]
    assert profile.dimensions == ["region"]
    assert profile.identifiers == ["user_id"]
    assert profile.timestamp_column == "event_time"
    assert profile.forecastable_metrics == ["revenue", "units"]
    assert len(profile.monitorable_metrics) == 2
    assert profile.generated_at  # populated


def test_profile_from_schema_multiple_metrics_and_dimensions():
    columns = [
        _col("order_id", "integer", "identifier"),
        _col("country", "string", "dimension"),
        _col("channel", "string", "dimension"),
        _col("revenue", "float", "metric", is_metric=True),
        _col("units", "integer", "metric", is_metric=True),
        _col("returns", "integer", "metric", is_metric=True),
        _col("order_date", "datetime", "timestamp"),
    ]
    dataset, schema = _make(columns=columns)
    service = DatasetIntelligenceService(dataset_repo=object(), schema_repo=object())
    profile = service.profile_from_schema(dataset, schema)

    assert set(profile.measures) == {"revenue", "units", "returns"}
    assert set(profile.dimensions) == {"country", "channel"}
    assert profile.timestamp_column == "order_date"
    assert len(profile.monitorable_metrics) == 3
    assert all(m.forecastable for m in profile.monitorable_metrics)
    assert all(set(m.dimensions) == {"country", "channel"} for m in profile.monitorable_metrics)


def test_profile_from_schema_dataset_without_timestamp():
    dataset, schema = _make(columns=NO_TIMESTAMP_COLUMNS)
    service = DatasetIntelligenceService(dataset_repo=object(), schema_repo=object())
    profile = service.profile_from_schema(dataset, schema)

    assert profile.timestamp_column is None
    assert profile.forecastable_metrics == []
    assert len(profile.monitorable_metrics) == 1
    assert profile.monitorable_metrics[0].forecastable is False


def test_profile_from_schema_no_metrics_no_crash():
    dataset, schema = _make(columns=NO_METRIC_COLUMNS)
    service = DatasetIntelligenceService(dataset_repo=object(), schema_repo=object())
    profile = service.profile_from_schema(dataset, schema)

    assert profile.measures == []
    assert profile.monitorable_metrics == []
    assert profile.timestamp_column == "event_time"  # still discoverable independent of measures


def test_profile_from_schema_empty_columns_no_crash():
    dataset, schema = _make(columns=[])
    service = DatasetIntelligenceService(dataset_repo=object(), schema_repo=object())
    profile = service.profile_from_schema(dataset, schema)

    assert profile.measures == []
    assert profile.dimensions == []
    assert profile.identifiers == []
    assert profile.timestamp_column is None
    assert profile.monitorable_metrics == []


def test_profile_to_dict_is_json_safe_and_nests_metrics():
    dataset, schema = _make()
    service = DatasetIntelligenceService(dataset_repo=object(), schema_repo=object())
    profile = service.profile_from_schema(dataset, schema)
    d = profile.to_dict()

    assert d["dataset_id"] == "ds-1"
    assert isinstance(d["monitorable_metrics"], list)
    assert d["monitorable_metrics"][0]["metric_id"].startswith("ds-1:")


def test_generic_source_shape_profiles_identically():
    """
    A hypothetical future connector (e.g. PostgreSQL/Snowflake/REST) that
    populates a Schema row using the same B1.4 column vocabulary must profile
    identically — no source-specific branching exists in this service.
    """
    postgres_like_columns = [
        _col("customer_id", "integer", "identifier"),
        _col("plan", "string", "dimension"),
        _col("mrr", "float", "metric", is_metric=True),
        _col("signup_date", "datetime", "timestamp"),
    ]
    dataset, schema = _make(dataset_id="ds-pg", schema_id="sc-pg", columns=postgres_like_columns,
                            name="postgres:accounts")
    service = DatasetIntelligenceService(dataset_repo=object(), schema_repo=object())
    profile = service.profile_from_schema(dataset, schema)

    assert profile.measures == ["mrr"]
    assert profile.timestamp_column == "signup_date"
    assert profile.monitorable_metrics[0].forecastable is True


def test_service_rejects_none_repos():
    with pytest.raises(ValueError):
        DatasetIntelligenceService(dataset_repo=None, schema_repo=object())
    with pytest.raises(ValueError):
        DatasetIntelligenceService(dataset_repo=object(), schema_repo=None)


# ===========================================================================
# 2b. build_profile (repository-backed, real SQLite)
# ===========================================================================

@pytest.fixture()
def db(tmp_path):
    client = DatabaseClient(database_url=f"sqlite:///{(tmp_path / 'b151.db').as_posix()}")
    yield client
    client.dispose()


def test_build_profile_via_real_repositories(db):
    dataset_repo = DatasetRepository(db)
    schema_repo = SchemaRepository(db)

    schema_id = schema_repo.create(Schema(object_name="sales.csv", columns=SALES_COLUMNS))
    dataset_id = dataset_repo.create(Dataset(
        name="sales.csv", schema_id=schema_id, row_count=3,
        status=AssetStatus.INDEXED, metric_columns=["revenue", "units"],
    ))

    service = DatasetIntelligenceService(dataset_repo=dataset_repo, schema_repo=schema_repo)
    profile = service.build_profile(dataset_id)

    assert profile.dataset_id == dataset_id
    assert profile.schema_id == schema_id
    assert profile.measures == ["revenue", "units"]
    assert profile.timestamp_column == "event_time"
    assert len(profile.monitorable_metrics) == 2


def test_build_profile_dataset_not_found(db):
    service = DatasetIntelligenceService(dataset_repo=DatasetRepository(db), schema_repo=SchemaRepository(db))
    with pytest.raises(DatasetIntelligenceError) as exc:
        service.build_profile("does-not-exist")
    assert exc.value.reason == "dataset_not_found"


def test_build_profile_dataset_missing_schema(db):
    dataset_repo = DatasetRepository(db)
    dataset_id = dataset_repo.create(Dataset(name="pending.csv", status=AssetStatus.PENDING))
    service = DatasetIntelligenceService(dataset_repo=dataset_repo, schema_repo=SchemaRepository(db))

    with pytest.raises(DatasetIntelligenceError) as exc:
        service.build_profile(dataset_id)
    assert exc.value.reason == "dataset_missing_schema"


def test_build_profile_schema_not_found(db):
    dataset_repo = DatasetRepository(db)
    # Dataset references a schema_id that was never created (registry inconsistency).
    dataset_id = dataset_repo.create(Dataset(
        name="broken.csv", schema_id="ghost-schema", status=AssetStatus.INDEXED,
    ))
    service = DatasetIntelligenceService(dataset_repo=dataset_repo, schema_repo=SchemaRepository(db))

    with pytest.raises(DatasetIntelligenceError) as exc:
        service.build_profile(dataset_id)
    assert exc.value.reason == "schema_not_found"
