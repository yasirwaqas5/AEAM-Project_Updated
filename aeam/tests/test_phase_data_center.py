"""
aeam/tests/test_phase_data_center.py

Enterprise Data Center — dataset activation (mutable, Redis-backed) and the
composed business/monitoring profile endpoint.

Three layers:

1. RedisClient.sadd/srem/smembers — new SET-operation methods, against a
   real Redis instance (skipped if unreachable).
2. RedisDatasetActivation — the new DatasetActivation implementation, same
   real Redis.
3. aeam.api.data_center via a real FastAPI TestClient + real SQLite +
   real Redis — activation endpoints and the profile endpoint (measures/
   dimensions/identifiers/timestamp/forecast candidates/monitorable metrics/
   rule coverage), reusing DatasetIntelligenceService unmodified.

No Qdrant, no BlobStore file I/O, no live network beyond localhost Redis.
"""

from __future__ import annotations

import socket
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aeam.api.data_center import router
from aeam.integrations.database import DatabaseClient
from aeam.integrations.redis_client import RedisClient
from aeam.intelligence.dataset_activation import RedisDatasetActivation, StaticDatasetActivation
from aeam.registry.models import AssetStatus, Dataset, ParentType, Schema, Source, SourceKind, Version
from aeam.registry.repositories import DatasetRepository, SchemaRepository, SourceRepository, VersionRepository


def _redis_available() -> bool:
    try:
        with socket.create_connection(("localhost", 6379), timeout=1):
            return True
    except OSError:
        return False


REDIS_UP = _redis_available()
pytestmark = pytest.mark.skipif(not REDIS_UP, reason="Redis not reachable at localhost:6379")


@pytest.fixture()
def redis_client():
    client = RedisClient(redis_url="redis://localhost:6379/0")
    yield client
    client.close()


@pytest.fixture()
def test_key():
    """A unique key per test — never collides with the real app's activation key."""
    return f"test:aeam:activated_datasets:{uuid.uuid4().hex}"


# ===========================================================================
# 1. RedisClient set operations
# ===========================================================================

def test_sadd_srem_smembers_roundtrip(redis_client, test_key):
    assert redis_client.sadd(test_key, "a") == 1
    assert redis_client.sadd(test_key, "b") == 1
    assert redis_client.sadd(test_key, "a") == 0  # already present
    assert redis_client.smembers(test_key) == {"a", "b"}

    assert redis_client.srem(test_key, "a") == 1
    assert redis_client.srem(test_key, "a") == 0  # already gone
    assert redis_client.smembers(test_key) == {"b"}
    redis_client.srem(test_key, "b")  # cleanup


def test_smembers_empty_for_missing_key(redis_client, test_key):
    assert redis_client.smembers(test_key) == set()


def test_sadd_rejects_empty_key(redis_client):
    with pytest.raises(ValueError):
        redis_client.sadd("", "x")


# ===========================================================================
# 2. RedisDatasetActivation
# ===========================================================================

def test_redis_activation_activate_deactivate(redis_client, test_key):
    activation = RedisDatasetActivation(redis_client, key=test_key)
    assert activation.list_activated_dataset_ids() == []

    activation.activate("ds-1")
    assert activation.list_activated_dataset_ids() == ["ds-1"]
    assert activation.is_activated("ds-1") is True
    assert activation.is_activated("ds-2") is False

    activation.activate("ds-2")
    assert activation.list_activated_dataset_ids() == ["ds-1", "ds-2"]

    activation.deactivate("ds-1")
    assert activation.list_activated_dataset_ids() == ["ds-2"]
    redis_client.srem(test_key, "ds-2")  # cleanup


def test_redis_activation_seeds_only_when_key_absent(redis_client, test_key):
    a1 = RedisDatasetActivation(redis_client, seed=["ds-1", "ds-2"], key=test_key)
    assert a1.list_activated_dataset_ids() == ["ds-1", "ds-2"]

    # Operator deactivates one.
    a1.deactivate("ds-1")
    assert a1.list_activated_dataset_ids() == ["ds-2"]

    # A "restart" with the SAME seed must NOT silently re-add ds-1 — the key
    # already exists, so seeding is skipped (preserves the operator's choice).
    a2 = RedisDatasetActivation(redis_client, seed=["ds-1", "ds-2"], key=test_key)
    assert a2.list_activated_dataset_ids() == ["ds-2"]
    redis_client.srem(test_key, "ds-2")  # cleanup


def test_redis_activation_satisfies_datasetactivation_protocol(redis_client, test_key):
    from aeam.intelligence.dataset_activation import DatasetActivation
    assert isinstance(RedisDatasetActivation(redis_client, key=test_key), DatasetActivation)


def test_redis_activation_rejects_none_client():
    with pytest.raises(ValueError):
        RedisDatasetActivation(None)


def test_redis_activation_degrades_gracefully_on_failure(test_key):
    class _BrokenRedis:
        def smembers(self, key):
            raise RuntimeError("connection lost")
        def exists(self, key):
            return False
        def sadd(self, key, member):
            raise RuntimeError("connection lost")

    activation = RedisDatasetActivation(_BrokenRedis(), key=test_key)
    assert activation.list_activated_dataset_ids() == []  # never raises
    assert activation.is_activated("ds-1") is False  # never raises


def test_static_activation_untouched_and_still_frozen():
    """The old implementation must remain exactly as it was — no mutation methods added to it."""
    activation = StaticDatasetActivation(["ds-1"])
    assert activation.list_activated_dataset_ids() == ["ds-1"]
    assert not hasattr(activation, "activate")
    assert not hasattr(activation, "deactivate")


# ===========================================================================
# 3. aeam.api.data_center — real TestClient + real SQLite + real Redis
# ===========================================================================

@pytest.fixture()
def db(tmp_path):
    client = DatabaseClient(database_url=f"sqlite:///{(tmp_path / 'dc.db').as_posix()}")
    yield client
    client.dispose()


@pytest.fixture()
def client(db, redis_client, test_key):
    class _Container:
        pass
    container = _Container()
    container.db = db
    container.redis = redis_client
    container.dataset_activation = RedisDatasetActivation(redis_client, key=test_key)

    app = FastAPI()
    app.include_router(router)
    app.state.container = container
    yield TestClient(app), container
    redis_client._client.delete(test_key)  # full cleanup regardless of test outcome


def _col(name, type_, role, is_metric=False):
    return {"name": name, "type": type_, "nullable": False, "is_metric": is_metric, "role": role}


def _seed_dataset(db, name="sales.csv", columns=None, status=AssetStatus.INDEXED):
    source_id = SourceRepository(db).create(Source(name="Manual Upload", kind=SourceKind.UPLOAD))
    schema_repo = SchemaRepository(db)
    dataset_repo = DatasetRepository(db)
    columns = columns or [
        _col("region", "string", "dimension"),
        _col("sales", "float", "metric", is_metric=True),
        _col("revenue", "float", "metric", is_metric=True),
        _col("event_time", "datetime", "timestamp"),
    ]
    schema_id = schema_repo.create(Schema(object_name=name, columns=columns))
    dataset_id = dataset_repo.create(Dataset(
        name=name, source_id=source_id, schema_id=schema_id, status=status,
        metric_columns=[c["name"] for c in columns if c["is_metric"]],
    ))
    VersionRepository(db).create(Version(
        parent_type=ParentType.DATASET, parent_id=dataset_id, version=1,
        content_hash="abc", blob_ref="local://abc", is_active=True,
    ))
    return dataset_id


# --- activation endpoints ---

def test_activation_list_empty_by_default(client):
    tc, _ = client
    r = tc.get("/api/v1/data-center/activation")
    assert r.status_code == 200
    assert r.json() == {"activated_dataset_ids": []}


def test_activate_and_deactivate_dataset(client, db):
    tc, _ = client
    dataset_id = _seed_dataset(db)

    r = tc.post(f"/api/v1/data-center/datasets/{dataset_id}/activate")
    assert r.status_code == 200
    assert r.json() == {"dataset_id": dataset_id, "activated": True}

    listed = tc.get("/api/v1/data-center/activation").json()
    assert dataset_id in listed["activated_dataset_ids"]

    r2 = tc.post(f"/api/v1/data-center/datasets/{dataset_id}/deactivate")
    assert r2.json() == {"dataset_id": dataset_id, "activated": False}
    listed2 = tc.get("/api/v1/data-center/activation").json()
    assert dataset_id not in listed2["activated_dataset_ids"]


def test_activate_404_for_missing_dataset(client):
    tc, _ = client
    assert tc.post("/api/v1/data-center/datasets/nope/activate").status_code == 404


def test_activate_503_when_activation_not_mutable(db):
    """StaticDatasetActivation has no activate() — must fail loudly, not silently no-op."""
    class _Container:
        pass
    container = _Container()
    container.db = db
    container.dataset_activation = StaticDatasetActivation(["seed"])

    app = FastAPI()
    app.include_router(router)
    app.state.container = container
    tc = TestClient(app)

    dataset_id = _seed_dataset(db)
    r = tc.post(f"/api/v1/data-center/datasets/{dataset_id}/activate")
    assert r.status_code == 503


# --- profile endpoint ---

def test_profile_full_business_view(client, db):
    tc, _ = client
    dataset_id = _seed_dataset(db)

    r = tc.get(f"/api/v1/data-center/datasets/{dataset_id}/profile")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is True
    assert set(body["measures"]) == {"sales", "revenue"}
    assert body["dimensions"] == ["region"]
    assert body["timestamp_column"] == "event_time"
    assert set(body["forecastable_metrics"]) == {"sales", "revenue"}
    assert body["forecast_enabled"] is True
    assert body["activated"] is False  # not yet activated


def test_profile_reflects_activation_state(client, db):
    tc, _ = client
    dataset_id = _seed_dataset(db)
    tc.post(f"/api/v1/data-center/datasets/{dataset_id}/activate")

    body = tc.get(f"/api/v1/data-center/datasets/{dataset_id}/profile").json()
    assert body["activated"] is True


def test_profile_rule_coverage_matches_curated_domain(client, db):
    """
    'sales' is a real curated domain in detection_rules.yaml (unmodified);
    'revenue' is not — this proves rule_coverage is computed correctly
    against the REAL RuleEngine, not hardcoded.
    """
    tc, _ = client
    dataset_id = _seed_dataset(db)

    body = tc.get(f"/api/v1/data-center/datasets/{dataset_id}/profile").json()
    by_col = {m["column"]: m for m in body["monitorable_metrics"]}
    assert by_col["sales"]["rule_coverage"] is True
    assert by_col["revenue"]["rule_coverage"] is False
    assert by_col["sales"]["forecastable"] is True


def test_profile_unavailable_for_unprocessed_dataset(client, db):
    tc, _ = client
    source_id = SourceRepository(db).create(Source(name="Manual Upload", kind=SourceKind.UPLOAD))
    dataset_id = DatasetRepository(db).create(Dataset(
        name="pending.csv", source_id=source_id, status=AssetStatus.PENDING,
    ))  # no schema_id yet

    r = tc.get(f"/api/v1/data-center/datasets/{dataset_id}/profile")
    assert r.status_code == 200  # expected outcome, not a server error
    body = r.json()
    assert body["available"] is False
    assert body["reason"] == "dataset_missing_schema"


def test_profile_404_for_missing_dataset(client):
    tc, _ = client
    assert tc.get("/api/v1/data-center/datasets/nope/profile").status_code == 404


def test_profile_no_timestamp_not_forecastable(client, db):
    tc, _ = client
    dataset_id = _seed_dataset(db, columns=[
        _col("region", "string", "dimension"),
        _col("revenue", "float", "metric", is_metric=True),
    ])

    body = tc.get(f"/api/v1/data-center/datasets/{dataset_id}/profile").json()
    assert body["timestamp_column"] is None
    assert body["forecast_enabled"] is False
    assert body["forecastable_metrics"] == []
    assert body["monitorable_metrics"][0]["forecastable"] is False
