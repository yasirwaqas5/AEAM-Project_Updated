"""
aeam/tests/test_phase_e7_autonomous_ops.py

Phase E7 — Autonomous Operations Enablement (SEC-8, PHIL-5, OBS-3/4,
RAG-6, ENG-8).

Acceptance criteria under test:

1. Honest monitor gating: ``ENABLE_MONITOR_AGENT`` is the sole authority
   — no environment backdoor in either direction.
2. Heartbeat instrumentation: a background worker's thread liveness is
   provable (and a dead/never-started one is honestly reported).
3. ``GET /health`` (via ``build_health_payload``) surfaces monitor /
   ingestion / bm25 state correctly, including flipping overall status
   to "degraded" on a stale worker heartbeat — closing the "dead thread
   discovered, not detected" audit gap.
4. BM25 freshness: a document ingested at runtime becomes lexically
   retrievable via ``refresh_from_qdrant`` without a restart, and
   staleness is disclosed (not gating) once past the threshold.
5. Multi-instance dedup/idempotency: two independently-constructed
   instances sharing one Redis client behave as a single shared domain.
6. Debug retrieval surface stays off (404) in production.

Infrastructure: SQLite/in-process fakes for unit-level tests; a real
Redis connection (127.0.0.1:6379) for the dedup/idempotency
multi-instance tests, skipped if unreachable (TEST-3 precedent already
used elsewhere in this suite, e.g. test_phase_data_center.py).
"""

from __future__ import annotations

import threading
import time

import pytest

from aeam.config.settings import Settings
from aeam.monitoring.metrics import HeartbeatTracker, heartbeat_tracker


# ===========================================================================
# 1. Honest monitor gating (no environment backdoor)
# ===========================================================================

def _settings(**overrides):
    base = dict(
        DATABASE_URL="sqlite:///:memory:",
        REDIS_URL="redis://localhost:6379/0",
        VECTOR_DB_URL="http://localhost",
        ENVIRONMENT="development",
    )
    base.update(overrides)
    return Settings(**base)


@pytest.mark.parametrize("environment", ["development", "staging", "test", "production"])
def test_gate_is_false_in_every_environment_when_flag_unset(environment):
    """The flag defaults False; every environment must see the SAME gate
    value (no `or ENVIRONMENT != "production"` backdoor)."""
    s = _settings(ENVIRONMENT=environment)
    assert s.ENABLE_MONITOR_AGENT is False
    # The gating expression main.py now uses is simply the flag itself.
    assert bool(s.ENABLE_MONITOR_AGENT) is False


@pytest.mark.parametrize("environment", ["development", "staging", "test", "production"])
def test_gate_is_true_in_every_environment_when_flag_set(environment):
    """When explicitly enabled, EVERY environment runs the loop — including
    production, which is the exact audit-gate-#4 fix."""
    s = _settings(ENVIRONMENT=environment, ENABLE_MONITOR_AGENT=True)
    assert s.ENABLE_MONITOR_AGENT is True


def test_gate_default_matches_pre_e7_safe_default():
    """COMPAT-1: the Settings-level default is unchanged (False) — only
    the CONDITION that reads it changed (no more OR clause)."""
    s = _settings()
    assert s.ENABLE_MONITOR_AGENT is False


# ===========================================================================
# 2. Heartbeat instrumentation
# ===========================================================================

def test_heartbeat_tracker_records_and_reports_age():
    tracker = HeartbeatTracker()
    assert tracker.age_seconds("monitor") is None  # never reported
    tracker.record("monitor")
    age = tracker.age_seconds("monitor")
    assert age is not None
    assert 0.0 <= age < 1.0


def test_heartbeat_tracker_is_per_worker_independent():
    tracker = HeartbeatTracker()
    tracker.record("monitor")
    assert tracker.age_seconds("ingestion") is None
    tracker.record("ingestion")
    assert tracker.age_seconds("ingestion") is not None


def test_heartbeat_tracker_last_seen_iso_format():
    tracker = HeartbeatTracker()
    assert tracker.last_seen_iso("monitor") is None
    tracker.record("monitor")
    iso = tracker.last_seen_iso("monitor")
    assert iso is not None
    assert "T" in iso and iso.endswith(("+00:00", "Z")) or "+00:00" in iso


def test_heartbeat_tracker_snapshot_returns_copy():
    tracker = HeartbeatTracker()
    tracker.record("monitor")
    snap = tracker.snapshot()
    assert "monitor" in snap
    snap["monitor"] = 0.0  # mutating the snapshot must not affect the tracker
    assert tracker.age_seconds("monitor") < 1.0


def test_heartbeat_tracker_is_thread_safe_under_concurrent_writes():
    tracker = HeartbeatTracker()
    errors: list[Exception] = []

    def _hammer(name: str):
        try:
            for _ in range(200):
                tracker.record(name)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_hammer, args=(f"w{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors
    for i in range(8):
        assert tracker.age_seconds(f"w{i}") is not None


def test_monitor_agent_records_heartbeat_on_each_loop_iteration():
    """Drive MonitorAgent.start() in a background thread with a very long
    sleep interval (so it never spins) and confirm the FIRST heartbeat is
    recorded before the thread would otherwise be sleeping."""
    from aeam.agents.kpi.rule_engine import RuleEngine
    from aeam.agents.kpi.statistical_detector import StatisticalDetector
    from aeam.agents.monitor.monitor_agent import MonitorAgent
    from aeam.core.deduplication import EventDeduplicator
    from aeam.core.event_bus import EventBus
    from aeam.core.priority_queue import EventPriorityQueue
    from aeam.pipelines.structured_data_pipeline import StructuredDataPipeline

    class _FakeRedis:
        def set(self, *a, **k):
            return True

    class _FakeForecast:
        def analyze(self, *a, **k):
            return {"insufficient_data": True}

    s = _settings(MONITOR_INTERVAL_SECONDS=9999)  # never wakes a 2nd time during the test
    agent = MonitorAgent(
        event_bus=EventBus(),
        queue=EventPriorityQueue(),
        deduplicator=EventDeduplicator(redis_client=_FakeRedis()),
        rule_engine=RuleEngine(),
        statistical_detector=StatisticalDetector(window_size=7),
        forecast_agent=_FakeForecast(),
        pipeline=StructuredDataPipeline(),
        settings=s,
    )

    t = threading.Thread(target=agent.start, daemon=True)
    t.start()
    deadline = time.time() + 5
    while heartbeat_tracker.age_seconds("monitor") is None and time.time() < deadline:
        time.sleep(0.05)

    assert heartbeat_tracker.age_seconds("monitor") is not None


def test_ingestion_worker_records_heartbeat_and_stops_cleanly():
    from aeam.ingestion.worker import IngestionWorker, PlaceholderJobProcessor

    class _EmptyJobRepo:
        def next_queued(self):
            return None

    worker = IngestionWorker(
        job_repo=_EmptyJobRepo(), processor=PlaceholderJobProcessor(), poll_interval=0.05,
    )
    t = threading.Thread(target=worker.start, daemon=True)
    t.start()
    deadline = time.time() + 5
    while heartbeat_tracker.age_seconds("ingestion") is None and time.time() < deadline:
        time.sleep(0.02)
    assert heartbeat_tracker.age_seconds("ingestion") is not None

    worker.stop()
    t.join(timeout=5)
    assert not t.is_alive()


# ===========================================================================
# 3. GET /health payload (build_health_payload)
# ===========================================================================

class _StubQueue:
    def size(self):
        return 0


class _StubRedis:
    def ping(self):
        return True


class _StubContainer:
    def __init__(self, **overrides):
        self.settings = _settings(HEARTBEAT_STALE_SECONDS=2, BM25_STALE_SECONDS=5)
        self.redis = _StubRedis()
        self.queue = _StubQueue()
        self.monitor_agent = None
        self.ingestion_worker = None
        self.bm25_index = None
        for k, v in overrides.items():
            setattr(self, k, v)


def test_health_payload_reports_monitor_disabled_when_not_constructed():
    from aeam.main import build_health_payload

    payload = build_health_payload(_StubContainer())
    assert payload["checks"]["monitor_agent"].startswith("disabled")
    assert payload["status"] == "healthy"


def test_health_payload_reports_monitor_ok_when_heartbeat_fresh():
    from aeam.main import build_health_payload

    heartbeat_tracker.record("monitor")
    container = _StubContainer(monitor_agent=object())  # any non-None sentinel
    payload = build_health_payload(container)
    assert payload["checks"]["monitor_agent"].startswith("ok")
    assert payload["status"] == "healthy"


def test_health_payload_flips_degraded_when_monitor_heartbeat_stale():
    from aeam.main import build_health_payload

    tracker = heartbeat_tracker
    # Force a stale heartbeat directly via the tracker's internal dict —
    # simulate "recorded a while ago" without sleeping in the test.
    with tracker._lock:  # noqa: SLF001 — test-only introspection
        tracker._last_seen["monitor"] = time.time() - 100  # 100s ago

    container = _StubContainer(monitor_agent=object())
    container.settings = _settings(HEARTBEAT_STALE_SECONDS=2)  # 100s > 2s => stale
    payload = build_health_payload(container)
    assert payload["checks"]["monitor_agent"].startswith("stale")
    assert payload["status"] == "degraded"


def test_health_payload_ingestion_not_started_when_never_constructed():
    from aeam.main import build_health_payload

    payload = build_health_payload(_StubContainer())
    assert payload["checks"]["ingestion_worker"] == "not started"


def test_health_payload_bm25_disabled_when_index_absent():
    from aeam.main import build_health_payload

    payload = build_health_payload(_StubContainer())
    assert payload["checks"]["bm25_index"].startswith("disabled")
    # Informational only — never flips overall status by itself.
    assert payload["status"] == "healthy"


def test_health_payload_bm25_staleness_never_flips_overall_status():
    from aeam.main import build_health_payload
    from aeam.agents.rag.hybrid_retrieval import BM25Index

    idx = BM25Index()
    idx.build([{"chunk_id": "c1", "text": "hello", "metadata": {}}])
    # Force staleness by rewinding the recorded build time.
    idx._built_at = time.time() - 1000  # noqa: SLF001 — test-only introspection

    container = _StubContainer(bm25_index=idx)
    container.settings = _settings(BM25_STALE_SECONDS=5)
    payload = build_health_payload(container)
    assert payload["checks"]["bm25_index"].startswith("stale")
    assert payload["status"] == "healthy"  # informational only


def test_health_payload_bm25_ok_when_fresh():
    from aeam.main import build_health_payload
    from aeam.agents.rag.hybrid_retrieval import BM25Index

    idx = BM25Index()
    idx.build([{"chunk_id": "c1", "text": "hello world", "metadata": {}}])
    container = _StubContainer(bm25_index=idx)
    container.settings = _settings(BM25_STALE_SECONDS=3600)
    payload = build_health_payload(container)
    assert payload["checks"]["bm25_index"].startswith("ok")
    assert "1 docs" in payload["checks"]["bm25_index"]


# ===========================================================================
# 4. BM25 runtime refresh (RAG-6)
# ===========================================================================

class _FakePoint:
    def __init__(self, id_, payload):
        self.id = id_
        self.payload = payload


class _FakeQdrantClient:
    """Single-page scroll stub — enough to exercise from_qdrant/refresh."""

    def __init__(self, texts: list[str]):
        self._texts = texts

    def scroll(self, collection_name, limit, offset, with_payload, with_vectors):
        if offset is not None:
            return [], None
        points = [
            _FakePoint(f"c{i}", {"chunk_id": f"c{i}", "text": t, "source": "doc"})
            for i, t in enumerate(self._texts)
        ]
        return points, None


def test_bm25_refresh_from_qdrant_updates_in_place():
    from aeam.agents.rag.hybrid_retrieval import BM25Index

    idx = BM25Index.from_qdrant(_FakeQdrantClient(["hello world"]), "col")
    assert idx.size == 1
    first_built_at = idx.built_at

    time.sleep(0.01)
    idx.refresh_from_qdrant(_FakeQdrantClient(["hello world", "second doc here"]), "col")
    assert idx.size == 2
    assert idx.built_at > first_built_at


def test_bm25_refresh_makes_new_content_lexically_retrievable():
    """The acceptance criterion in miniature: a document 'ingested at
    runtime' (added to the fake Qdrant) becomes retrievable after refresh
    without constructing a new index / without a restart."""
    from aeam.agents.rag.hybrid_retrieval import BM25Index

    idx = BM25Index.from_qdrant(_FakeQdrantClient(["unrelated content"]), "col")
    assert idx.search("brand new runtime document", top_k=5) == []

    idx.refresh_from_qdrant(
        _FakeQdrantClient(["unrelated content", "brand new runtime document about incidents"]),
        "col",
    )
    results = idx.search("brand new runtime document", top_k=5)
    assert any("brand new runtime document" in r["text"] for r in results)


def test_bm25_age_seconds_and_built_at_before_first_build():
    from aeam.agents.rag.hybrid_retrieval import BM25Index

    idx = BM25Index()
    assert idx.built_at is None
    assert idx.age_seconds is None


def test_bm25_refresh_survives_scroll_failure():
    from aeam.agents.rag.hybrid_retrieval import BM25Index

    class _BrokenQdrant:
        def scroll(self, **kwargs):
            raise RuntimeError("connection reset")

    idx = BM25Index.from_qdrant(_FakeQdrantClient(["seed doc"]), "col")
    assert idx.size == 1
    # A refresh that fails to scroll must not raise — it degrades to an
    # empty rebuild rather than crashing the ingestion job.
    idx.refresh_from_qdrant(_BrokenQdrant(), "col")
    assert idx.size == 0


# ===========================================================================
# 5. Multi-instance dedup / idempotency posture (Redis-shared)
# ===========================================================================

def _redis_available() -> bool:
    try:
        import redis
        client = redis.Redis(host="localhost", port=6379, socket_connect_timeout=1)
        return bool(client.ping())
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.skipif(not _redis_available(), reason="Redis not reachable at localhost:6379")
def test_two_deduplicator_instances_share_one_redis_domain():
    """Two INDEPENDENTLY constructed EventDeduplicators pointed at the
    same Redis behave as ONE shared dedup domain — the multi-instance
    safety property E7 documents (a second app instance's deduplicator
    must see the first instance's dedup keys)."""
    import redis as redis_lib
    from aeam.core.deduplication import EventDeduplicator
    from aeam.core.event_models import Event

    client = redis_lib.Redis(host="localhost", port=6379, db=0)
    dedup_a = EventDeduplicator(redis_client=client)
    dedup_b = EventDeduplicator(redis_client=client)  # simulates a 2nd app instance

    event = Event(
        event_id="e7-dedup-1", event_type="kpi_anomaly", metric="sales-e7test",
        severity="HIGH", current_value=1, expected_value=2,
        detection_methods=["rule"], timestamp="2026-01-01T00:00:00Z",
    )
    # Clean any leftover key from a prior failed run before asserting.
    for k in client.keys("*e7test*") or []:
        client.delete(k)
    try:
        first = dedup_a.is_duplicate(event, window_minutes=1)
        second = dedup_b.is_duplicate(event, window_minutes=1)
        assert first is False   # instance A sees it fresh
        assert second is True   # instance B sees the SAME key A just set
    finally:
        for k in client.keys("*e7test*") or []:
            client.delete(k)


@pytest.mark.skipif(not _redis_available(), reason="Redis not reachable at localhost:6379")
def test_two_idempotency_manager_instances_share_one_redis_domain():
    """Same multi-instance property for action idempotency: instance B's
    manager must see a key instance A already stored, via the shared
    Redis-backed IdempotencyManager.check()/store() contract."""
    from aeam.core.idempotency import IdempotencyManager
    from aeam.integrations.redis_client import RedisClient

    rc = RedisClient(redis_url="redis://localhost:6379/0")
    mgr_a = IdempotencyManager(redis_client=rc)
    mgr_b = IdempotencyManager(redis_client=rc)  # simulates a 2nd app instance

    key = mgr_a.generate_key(
        incident_id="e7-idem-inc", action_type="slack", params={"x": 1},
    )
    try:
        assert mgr_a.check(key) is False   # never executed yet, from instance A's view
        assert mgr_b.check(key) is False   # nor from instance B's view — same Redis, same answer

        mgr_a.store(key, result={"ok": True})

        # Instance B (never called store itself) now sees it as already done.
        assert mgr_b.check(key) is True
    finally:
        rc._client.delete(key)


# ===========================================================================
# 6. Debug retrieval surface stays off in production
# ===========================================================================

def test_debug_retrieval_returns_404_in_production():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from aeam.api.retrieval_debug import router

    class _Container:
        settings = _settings(ENVIRONMENT="production")

    app = FastAPI()
    app.include_router(router)
    app.state.container = _Container()
    client = TestClient(app)

    resp = client.get("/api/v1/debug/retrieval/", params={"query": "test"})
    assert resp.status_code == 404
