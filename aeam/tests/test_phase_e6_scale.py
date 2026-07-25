"""
aeam/tests/test_phase_e6_scale.py

Phase E6 — Scale Contracts (COMPAT-2/4/6, OBS-2, ENG-6, PHIL-5).

Acceptance criteria under test:

1. **Parameter-less calls are byte-compatible with today.** A bare
   ``GET /api/v1/incidents/`` returns the full list as a JSON array, same
   shape and order as before E6 (COMPAT-2).
2. **Paged calls are bounded regardless of table size,** and expose the
   total via the ``X-Total-Count`` header so a client can page.
3. **Filtering** (severity/event_type/requires_human) is applied
   server-side.
4. **Observability applies a sane default window and discloses it**
   (OBS-2) without ever mutating a persisted row.
5. **Policy-match cost is flat with respect to policy count:** a stored
   embedding is used instead of re-embedding the corpus per incident, and
   a policy with no stored vector still matches via on-the-fly fallback
   (COMPAT-6).
6. **Resource-management settings exist and default to today's values.**

Infrastructure: real FastAPI TestClient + real SQLite (TEST-3). No live
services.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aeam.api.incidents import router as incidents_router
from aeam.api.observability import router as observability_router
from aeam.config.settings import Settings
from aeam.integrations.database import DatabaseClient


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path):
    client = DatabaseClient(database_url=f"sqlite:///{(tmp_path / 'e6.db').as_posix()}")
    yield client
    client.dispose()


def _seed_incident(db, *, severity="HIGH", event_type="kpi_anomaly", ts=None, requires_human=False):
    iid = str(uuid.uuid4())
    db.insert(
        table="incidents",
        data={
            "incident_id": iid,
            "event_id": str(uuid.uuid4()),
            "event_type": event_type,
            "metric": "sales",
            "severity": severity,
            "timestamp": ts or "2026-07-01T00:00:00Z",
            "requires_human": requires_human,
            "findings": "[]",
        },
    )
    return iid


@pytest.fixture()
def incidents_client(db):
    class _Container:
        pass
    container = _Container()
    container.db = db
    container.settings = Settings(
        DATABASE_URL="sqlite:///:memory:",
        REDIS_URL="redis://localhost:6379/0",
        VECTOR_DB_URL="http://localhost",
        ENVIRONMENT="development",
    )

    app = FastAPI()
    app.include_router(incidents_router)
    app.state.container = container
    return TestClient(app), db


@pytest.fixture()
def observability_client(db):
    class _Container:
        pass
    container = _Container()
    container.db = db
    container.settings = Settings(
        DATABASE_URL="sqlite:///:memory:",
        REDIS_URL="redis://localhost:6379/0",
        VECTOR_DB_URL="http://localhost",
        ENVIRONMENT="development",
    )
    app = FastAPI()
    app.include_router(observability_router)
    app.state.container = container
    return TestClient(app), db, container


# ===========================================================================
# 1. Byte-compatibility of the parameter-less call (COMPAT-2)
# ===========================================================================

def test_parameterless_incidents_returns_full_bare_array(incidents_client):
    client, db = incidents_client
    for i in range(5):
        _seed_incident(db, ts=f"2026-07-{i + 1:02d}T00:00:00Z")

    resp = client.get("/api/v1/incidents/")
    assert resp.status_code == 200
    body = resp.json()
    # Still a bare JSON array, not an envelope.
    assert isinstance(body, list)
    assert len(body) == 5
    # No pagination header when the caller didn't paginate/filter.
    assert "X-Total-Count" not in resp.headers


def test_parameterless_incidents_ordered_newest_first(incidents_client):
    client, db = incidents_client
    _seed_incident(db, ts="2026-07-01T00:00:00Z")
    _seed_incident(db, ts="2026-07-09T00:00:00Z")
    _seed_incident(db, ts="2026-07-05T00:00:00Z")

    body = client.get("/api/v1/incidents/").json()
    timestamps = [r["timestamp"] for r in body]
    assert timestamps == sorted(timestamps, reverse=True)


# ===========================================================================
# 2. Bounded paged calls + X-Total-Count
# ===========================================================================

def test_limit_bounds_the_payload(incidents_client):
    client, db = incidents_client
    for i in range(25):
        _seed_incident(db, ts=f"2026-07-{(i % 28) + 1:02d}T00:00:00Z")

    resp = client.get("/api/v1/incidents/?limit=10")
    assert resp.status_code == 200
    assert len(resp.json()) == 10
    assert resp.headers["X-Total-Count"] == "25"


def test_offset_paginates(incidents_client):
    client, db = incidents_client
    for i in range(30):
        _seed_incident(db, ts=f"2026-07-{i + 1:02d}T00:00:00Z" if i < 28 else "2026-08-01T00:00:00Z")

    page1 = client.get("/api/v1/incidents/?limit=10&offset=0").json()
    page2 = client.get("/api/v1/incidents/?limit=10&offset=10").json()
    ids1 = {r["incident_id"] for r in page1}
    ids2 = {r["incident_id"] for r in page2}
    assert len(ids1) == 10 and len(ids2) == 10
    assert ids1.isdisjoint(ids2)  # no overlap between pages


def test_limit_is_clamped_to_configured_max(incidents_client):
    client, db = incidents_client
    _seed_incident(db)
    # The static Query ceiling is 1000; requesting above it is a 422, so we
    # verify the CONFIGURED clamp instead by lowering it and requesting more.
    container = client.app.state.container
    container.settings = Settings(
        DATABASE_URL="sqlite:///:memory:",
        REDIS_URL="redis://localhost:6379/0",
        VECTOR_DB_URL="http://localhost",
        ENVIRONMENT="development",
        API_MAX_PAGE_SIZE=1,
    )
    for _ in range(5):
        _seed_incident(db)
    resp = client.get("/api/v1/incidents/?limit=100")
    assert resp.status_code == 200
    assert len(resp.json()) == 1  # clamped to API_MAX_PAGE_SIZE


# ===========================================================================
# 3. Server-side filtering
# ===========================================================================

def test_severity_filter_is_applied_server_side(incidents_client):
    client, db = incidents_client
    _seed_incident(db, severity="HIGH")
    _seed_incident(db, severity="CRITICAL")
    _seed_incident(db, severity="HIGH")

    resp = client.get("/api/v1/incidents/?severity=HIGH")
    body = resp.json()
    assert len(body) == 2
    assert all(r["severity"] == "HIGH" for r in body)
    assert resp.headers["X-Total-Count"] == "2"


def test_event_type_and_requires_human_filters(incidents_client):
    client, db = incidents_client
    _seed_incident(db, event_type="kpi_anomaly", requires_human=True)
    _seed_incident(db, event_type="cpu_high", requires_human=False)

    r1 = client.get("/api/v1/incidents/?event_type=cpu_high")
    assert len(r1.json()) == 1
    assert r1.json()[0]["event_type"] == "cpu_high"

    r2 = client.get("/api/v1/incidents/?requires_human=true")
    assert len(r2.json()) == 1
    assert r2.json()[0]["requires_human"] in (True, 1)


# ===========================================================================
# 4. Observability windowing disclosure (OBS-2)
# ===========================================================================

def test_observability_discloses_default_window(observability_client):
    client, db, container = observability_client
    for i in range(3):
        _seed_incident(db, ts=f"2026-07-{i + 1:02d}T00:00:00Z")

    body = client.get("/api/v1/observability/").json()
    assert "retention" in body
    ret = body["retention"]
    assert ret["source"] == "default"
    assert ret["window"] == 1000
    assert ret["incidents_available"] == 3
    assert ret["incidents_considered"] == 3
    assert ret["windowed"] is False


def test_observability_windowing_is_disclosed_when_it_drops_rows(observability_client):
    client, db, container = observability_client
    # Configure a tiny window so the disclosure reports windowing=True.
    container.settings = Settings(
        DATABASE_URL="sqlite:///:memory:",
        REDIS_URL="redis://localhost:6379/0",
        VECTOR_DB_URL="http://localhost",
        ENVIRONMENT="development",
        OBSERVABILITY_RETENTION_LIMIT=2,
    )
    for i in range(5):
        _seed_incident(db, ts=f"2026-07-{i + 1:02d}T00:00:00Z")

    body = client.get("/api/v1/observability/").json()
    ret = body["retention"]
    assert ret["source"] == "configured"
    assert ret["window"] == 2
    assert ret["incidents_available"] == 5
    assert ret["incidents_considered"] == 2
    assert ret["windowed"] is True


def test_observability_never_mutates_rows(observability_client):
    client, db, container = observability_client
    for i in range(3):
        _seed_incident(db, ts=f"2026-07-{i + 1:02d}T00:00:00Z")
    client.get("/api/v1/observability/")
    # All rows still present after the windowed read.
    total = db.fetch_one("SELECT COUNT(*) AS n FROM incidents")
    assert int(total["n"]) == 3


# ===========================================================================
# 5. Stored policy embeddings (flat cost with policy count)
# ===========================================================================

def test_policy_registry_uses_stored_embedding_instead_of_recomputing():
    """A policy with a stored embedding must be scored WITHOUT calling the
    embedding service for that policy — the O(policies) audit finding fixed."""
    from aeam.intelligence.policy_registry import PolicyRegistry
    from aeam.registry.models import Policy

    # Fake embedding service that counts how many times it embeds.
    class _CountingEmbed:
        def __init__(self):
            self.calls = 0

        def encode_text(self, text):
            self.calls += 1
            # Deterministic 3-dim vector so cosine similarity is stable.
            return [1.0, 0.0, 0.0] if "sales" in text.lower() else [0.0, 1.0, 0.0]

    class _StubRepo:
        def __init__(self, policies):
            self._policies = policies

        def list_all(self):
            return self._policies

    class _StubRules:
        loaded_domains = ["sales"]

    embed = _CountingEmbed()
    # One policy WITH a stored embedding aligned to the "sales" query vector.
    stored = Policy(
        policy_id="p-stored", doc_id="d", raw_text="sales guidance",
        related_metrics=["unrelated"], embedding=[1.0, 0.0, 0.0],
        embedding_model="all-MiniLM-L6-v2",
    )
    registry = PolicyRegistry(
        policy_repository=_StubRepo([stored]),
        rule_engine=_StubRules(),
        embedding_service=embed,
    )

    # Metric tier won't match (related_metrics != the incident metric), so it
    # falls to the semantic tier — which must use the STORED vector.
    matches = registry.match_for_incident(metric="latency", query="sales drop")

    assert len(matches) == 1
    assert matches[0]["policy_id"] == "p-stored"
    # The embedding service was called ONCE for the query only — never for
    # the stored policy (its vector was reused).
    assert embed.calls == 1


def test_policy_registry_falls_back_to_on_the_fly_for_unembedded_policy():
    """COMPAT-6: a policy with no stored vector still matches, via on-the-fly
    embedding — exact pre-E6 behaviour for legacy rows."""
    from aeam.intelligence.policy_registry import PolicyRegistry
    from aeam.registry.models import Policy

    class _CountingEmbed:
        def __init__(self):
            self.calls = 0

        def encode_text(self, text):
            self.calls += 1
            return [1.0, 0.0, 0.0]

    class _StubRepo:
        def list_all(self):
            return [Policy(policy_id="p-legacy", doc_id="d", raw_text="sales guidance", embedding=None)]

    class _StubRules:
        loaded_domains = ["sales"]

    embed = _CountingEmbed()
    registry = PolicyRegistry(
        policy_repository=_StubRepo(),
        rule_engine=_StubRules(),
        embedding_service=embed,
    )
    matches = registry.match_for_incident(metric="latency", query="sales drop")
    assert len(matches) == 1
    # Query embed + one on-the-fly policy embed for the legacy row.
    assert embed.calls == 2


def test_policy_model_roundtrips_embedding_through_the_db(db):
    from aeam.registry.models import Policy
    from aeam.registry.repositories import PolicyRepository

    repo = PolicyRepository(db)
    pid = repo.create(Policy(
        policy_id="p-1", doc_id="d-1", raw_text="text",
        embedding=[0.1, 0.2, 0.3], embedding_model="all-MiniLM-L6-v2",
    ))
    loaded = repo.get(pid)
    assert loaded.embedding == [0.1, 0.2, 0.3]
    assert loaded.embedding_model == "all-MiniLM-L6-v2"

    # A policy stored without an embedding roundtrips as None (not []).
    pid2 = repo.create(Policy(policy_id="p-2", doc_id="d-2", raw_text="text"))
    loaded2 = repo.get(pid2)
    assert loaded2.embedding is None


# ===========================================================================
# 6. Resource-management settings default to today's values (COMPAT-1)
# ===========================================================================

def test_resource_settings_defaults_match_pre_e6_values():
    s = Settings(
        DATABASE_URL="sqlite:///:memory:",
        REDIS_URL="redis://localhost:6379/0",
        VECTOR_DB_URL="http://localhost",
        ENVIRONMENT="development",
    )
    assert s.DB_POOL_SIZE == 5
    assert s.DB_MAX_OVERFLOW == 10
    assert s.DB_POOL_TIMEOUT_SECONDS == 30
    assert s.API_MAX_PAGE_SIZE == 1000
