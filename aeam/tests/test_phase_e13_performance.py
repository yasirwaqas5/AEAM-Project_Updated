"""
aeam/tests/test_phase_e13_performance.py

Phase E13 — Performance baselines with recorded budgets.

The E13 acceptance criterion is "load tests meet recorded budgets". The
budgets live in ``aeam/tests/fixtures/performance_budgets.json`` — one
file, versioned with the code, stating for every budget what is measured,
over what window, from what source, and what the reference hardware
actually observed (OBS-2). This suite asserts them.

Four axes, one per subsystem the roadmap names:

1. **Concurrent investigation throughput** (E2) — the isolation Phase E2
   proved did not cost throughput.
2. **Ingestion throughput** — extraction + chunking, the CPU-bound work
   AEAM owns. Embedding latency is a model property (TECH-6), not a
   platform budget, so it is deliberately outside the measurement.
3. **Console responsiveness at volume** (E6) — a year of incidents behind
   the paginated endpoint the console actually calls.
4. **Autonomous-loop cycle stability** (E7) — the supervised poll loop
   keeps its cadence and its heartbeat stays fresh.

Budgets are CI ceilings set well above the recorded local figures, so a
failure here means a structural regression (an accidental O(n²), a
per-row query, a lost index), not runner noise. Every assertion message
names the budget it violated and by how much — a red build should not
require reading this file to understand.

Infrastructure: in-process only — real SQLite, real FastAPI TestClient,
no DB server, no Redis, no Qdrant, no LLM (TEST-3).
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aeam.agents.orchestrator.decision_engine import DecisionEngine
from aeam.agents.orchestrator.evaluation_engine import EvaluationEngine
from aeam.agents.orchestrator.orchestrator import Orchestrator
from aeam.agents.rag.chunking import TextChunker
from aeam.api.incidents import router as incidents_router
from aeam.config.settings import Settings
from aeam.core.event_bus import EventBus
from aeam.core.event_models import Event
from aeam.ingestion import extraction
from aeam.integrations.database import DatabaseClient
from aeam.memory.long_term import LongTermMemory
from aeam.monitoring.metrics import HeartbeatTracker


# ---------------------------------------------------------------------------
# Budgets
# ---------------------------------------------------------------------------

_BUDGETS_PATH = Path(__file__).parent / "fixtures" / "performance_budgets.json"


@pytest.fixture(scope="module")
def budgets() -> dict:
    """The recorded budgets. A missing/corrupt file fails the suite loudly:
    silently skipping would let the gate disappear without anyone noticing."""
    assert _BUDGETS_PATH.exists(), f"Performance budgets missing at {_BUDGETS_PATH}"
    return json.loads(_BUDGETS_PATH.read_text(encoding="utf-8"))


def test_budget_file_declares_its_measurement_semantics(budgets):
    """OBS-2: a published number states its window, source and meaning.

    Guards the budget file itself — a budget added later without its
    semantics is exactly the "process-lifetime counter masquerading as
    historical truth" the law was written against.
    """
    measured = {k: v for k, v in budgets.items() if not k.startswith("_") and k != "baseline_environment"}
    assert measured, "No budgets declared."

    for name, budget in measured.items():
        for field in ("what", "why", "source", "window", "observed_local"):
            assert field in budget, f"Budget {name!r} does not declare {field!r}."
        assert any(
            key.startswith("max_") or key.startswith("min_") for key in budget
        ), f"Budget {name!r} declares no threshold."

    assert "hardware" in budgets["baseline_environment"]
    assert "recorded_on" in budgets["baseline_environment"]


# ===========================================================================
# 1. Concurrent investigation throughput (E2 substrate)
# ===========================================================================


class _RecordingLTM(LongTermMemory):
    """Thread-safe capture of finalized incidents — same double the E2
    concurrency suite uses, so throughput is measured over the identical
    code path that phase proved isolated."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.recorded: list[dict] = []

    def record_incident(self, payload: dict) -> str:
        with self._lock:
            self.recorded.append(payload)
        return payload.get("incident_id") or payload.get("event_id") or "fake"


def _build_orchestrator() -> tuple[Orchestrator, _RecordingLTM]:
    settings = Settings(
        DATABASE_URL="sqlite:///:memory:",
        REDIS_URL="redis://localhost:6379/0",
        VECTOR_DB_URL="http://localhost",
        ENVIRONMENT="development",
        LLM_ENABLED=False,
    )
    ltm = _RecordingLTM()
    orchestrator = Orchestrator(
        event_bus=EventBus(),
        decision_engine=DecisionEngine(settings=settings),
        evaluation_engine=EvaluationEngine(settings=settings),
        long_term_memory=ltm,
        settings=settings,
    )
    return orchestrator, ltm


def _event(tag: str) -> Event:
    return Event(
        event_id=f"perf-{tag}",
        event_type="kpi_anomaly",
        metric=f"sales-{tag}",
        severity="HIGH",
        current_value=100.0,
        expected_value=200.0,
        detection_methods=["rule"],
        timestamp="2026-07-01T00:00:00Z",
        metadata={"tag": tag},
    )


def test_concurrent_investigation_throughput_meets_budget(budgets):
    budget = budgets["concurrent_investigation_throughput"]
    n_events = int(budget["events"])
    workers = int(budget["workers"])

    orchestrator, ltm = _build_orchestrator()
    errors: list[Exception] = []

    def _run(tag: str) -> None:
        try:
            orchestrator.handle_event(_event(tag))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(_run, [f"t{i}" for i in range(n_events)]))
    elapsed = time.perf_counter() - started

    assert not errors, f"Investigations raised under concurrency: {errors[:3]}"
    assert len(ltm.recorded) == n_events, (
        f"Only {len(ltm.recorded)}/{n_events} investigations finalized — "
        "throughput is meaningless if work was dropped."
    )

    throughput = n_events / elapsed if elapsed > 0 else float("inf")
    assert elapsed <= budget["max_total_seconds"], (
        f"Concurrent investigation budget exceeded: {n_events} events across "
        f"{workers} workers took {elapsed:.2f}s, budget {budget['max_total_seconds']}s "
        f"(local reference: {budget['observed_local']['total_seconds']}s)."
    )
    assert throughput >= budget["min_events_per_second"], (
        f"Throughput {throughput:.2f} ev/s is below the "
        f"{budget['min_events_per_second']} ev/s floor."
    )


# ===========================================================================
# 2. Ingestion throughput (extraction + chunking)
# ===========================================================================


def test_ingestion_throughput_meets_budget(budgets):
    budget = budgets["ingestion_throughput"]
    doc_count = int(budget["documents"])
    size_bytes = int(budget["document_size_bytes"])

    # A realistic runbook-shaped document: repeated prose with paragraph
    # breaks, so the chunker does real boundary work rather than slicing
    # one undifferentiated blob.
    paragraph = (
        "When the sales KPI drops more than fifteen percent below its "
        "seven-day moving average, the on-call analyst confirms the "
        "upstream pipeline is healthy before escalating to the revenue "
        "owner. Escalation requires the incident identifier.\n\n"
    )
    body = (paragraph * ((size_bytes // len(paragraph)) + 1))[:size_bytes]
    payload = body.encode("utf-8")

    chunker = TextChunker()

    started = time.perf_counter()
    total_chunks = 0
    for index in range(doc_count):
        result = extraction.extract_text(payload, "markdown", filename=f"runbook-{index}.md")
        chunks = chunker.chunk_text(result.text, {"source": f"runbook-{index}"})
        total_chunks += len(chunks)
    elapsed = time.perf_counter() - started

    assert total_chunks > doc_count, (
        "Chunking produced no more chunks than documents — the measurement "
        "did not exercise the chunker."
    )

    rate = doc_count / elapsed if elapsed > 0 else float("inf")
    assert elapsed <= budget["max_total_seconds"], (
        f"Ingestion budget exceeded: {doc_count} documents of {size_bytes}B took "
        f"{elapsed:.2f}s, budget {budget['max_total_seconds']}s "
        f"(local reference: {budget['observed_local']['total_seconds']}s)."
    )
    assert rate >= budget["min_documents_per_second"], (
        f"Ingestion rate {rate:.2f} docs/s is below the "
        f"{budget['min_documents_per_second']} docs/s floor."
    )


# ===========================================================================
# 3. Console responsiveness at a year of incident volume (E6)
# ===========================================================================


def _seed_incidents(db: DatabaseClient, count: int, batch: int = 500) -> None:
    """Bulk-insert ``count`` incidents.

    Batched multi-row INSERTs rather than DatabaseClient.insert() per row:
    the point of this test is to measure the READ path, and paying 5,000
    individual commits to set it up would dominate the runtime without
    measuring anything the console does.
    """
    columns = ("incident_id", "event_id", "event_type", "metric", "severity", "timestamp", "requires_human", "findings")
    severities = ("LOW", "MEDIUM", "HIGH", "CRITICAL")

    for start in range(0, count, batch):
        rows = []
        params: dict[str, object] = {}
        for offset in range(start, min(start + batch, count)):
            rows.append("(" + ", ".join(f":{c}_{offset}" for c in columns) + ")")
            params[f"incident_id_{offset}"] = str(uuid.uuid4())
            params[f"event_id_{offset}"] = str(uuid.uuid4())
            params[f"event_type_{offset}"] = "kpi_anomaly"
            params[f"metric_{offset}"] = f"metric-{offset % 25}"
            params[f"severity_{offset}"] = severities[offset % len(severities)]
            params[f"timestamp_{offset}"] = f"2026-{(offset % 12) + 1:02d}-01T00:00:00Z"
            params[f"requires_human_{offset}"] = bool(offset % 7 == 0)
            params[f"findings_{offset}"] = "[]"
        db.execute(
            f"INSERT INTO incidents ({', '.join(columns)}) VALUES {', '.join(rows)}",
            params=params,
        )


@pytest.fixture()
def volume_client(tmp_path, budgets):
    """A real SQLite incidents table seeded to a year of volume."""
    db = DatabaseClient(database_url=f"sqlite:///{(tmp_path / 'e13_perf.db').as_posix()}")
    _seed_incidents(db, int(budgets["console_query_at_volume"]["seeded_incidents"]))

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

    yield TestClient(app), db
    db.dispose()


def test_paged_console_query_meets_budget_at_volume(volume_client, budgets):
    client, _ = volume_client
    budget = budgets["console_query_at_volume"]
    page_size = int(budget["page_size"])
    samples = int(budget["requests_sampled"])

    worst = 0.0
    for page in range(samples):
        started = time.perf_counter()
        response = client.get(f"/api/v1/incidents/?limit={page_size}&offset={page * page_size}")
        worst = max(worst, time.perf_counter() - started)
        assert response.status_code == 200
        body = response.json()
        rows = body if isinstance(body, list) else body.get("items", [])
        assert len(rows) <= page_size, (
            f"Paged request returned {len(rows)} rows for limit={page_size} — "
            "the endpoint is not actually bounded."
        )

    assert worst <= budget["max_page_seconds"], (
        f"Console page budget exceeded: worst of {samples} paged requests took "
        f"{worst:.3f}s, budget {budget['max_page_seconds']}s "
        f"(local reference: {budget['observed_local']['page_seconds']}s)."
    )


def test_unpaged_console_query_stays_within_budget_at_volume(volume_client, budgets):
    """The parameter-less call still returns everything (COMPAT-2). It is
    slower by definition — this budget bounds how much slower, so the
    backward-compatible path cannot silently become unusable at volume."""
    client, _ = volume_client
    budget = budgets["console_query_at_volume"]

    started = time.perf_counter()
    response = client.get("/api/v1/incidents/")
    elapsed = time.perf_counter() - started

    assert response.status_code == 200
    body = response.json()
    rows = body if isinstance(body, list) else body.get("items", [])
    assert len(rows) == int(budget["seeded_incidents"])

    assert elapsed <= budget["max_unpaged_seconds"], (
        f"Unpaged console budget exceeded: {len(rows)} incidents took {elapsed:.2f}s, "
        f"budget {budget['max_unpaged_seconds']}s "
        f"(local reference: {budget['observed_local']['unpaged_seconds']}s)."
    )


# ===========================================================================
# 4. Autonomous-loop cycle stability (E7)
# ===========================================================================


def test_autonomous_loop_cycle_cadence_and_heartbeat_meet_budget(budgets):
    """The supervised poll loop keeps its declared cadence and its
    heartbeat stays fresh across consecutive cycles.

    Uses a stub loop with the same structure as ``MonitorAgent.start()``
    — heartbeat recorded before the cycle body, exception-caught body,
    fixed sleep — rather than a live MonitorAgent, because the budget is
    about *loop* behaviour and a real agent would be measuring its data
    sources instead. A tracker instance local to this test keeps it
    independent of the process-wide singleton other suites write to.
    """
    budget = budgets["autonomous_loop_stability"]
    cycles = int(budget["cycles"])
    interval = float(budget["interval_seconds"])

    tracker = HeartbeatTracker()
    ages: list[float] = []
    cycle_times: list[float] = []
    stop = threading.Event()

    def _loop() -> None:
        for _ in range(cycles):
            if stop.is_set():
                return
            cycle_started = time.perf_counter()
            tracker.record("monitor-perf")
            try:
                # Stand-in for _run_cycle(): a trivial, bounded body.
                sum(i * i for i in range(1000))
            except Exception:  # noqa: BLE001
                pass
            age = tracker.age_seconds("monitor-perf")
            if age is not None:
                ages.append(age)
            time.sleep(interval)
            cycle_times.append(time.perf_counter() - cycle_started)

    thread = threading.Thread(target=_loop, daemon=True)
    thread.start()
    thread.join(timeout=30)
    stop.set()

    assert len(cycle_times) == cycles, (
        f"Loop completed {len(cycle_times)}/{cycles} cycles — a loop that "
        "stalls is the exact failure supervision exists to catch."
    )

    worst_cycle = max(cycle_times)
    drift_ratio = worst_cycle / interval
    assert drift_ratio <= budget["max_cycle_drift_ratio"], (
        f"Cycle drift budget exceeded: slowest cycle took {worst_cycle:.3f}s "
        f"against a {interval}s interval (ratio {drift_ratio:.1f}x, budget "
        f"{budget['max_cycle_drift_ratio']}x)."
    )

    worst_age = max(ages)
    assert worst_age <= budget["max_heartbeat_age_seconds"], (
        f"Heartbeat freshness budget exceeded: oldest observed age "
        f"{worst_age:.3f}s, budget {budget['max_heartbeat_age_seconds']}s."
    )
