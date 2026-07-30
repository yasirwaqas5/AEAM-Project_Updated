"""
aeam/tests/test_final_hardening.py

Regression tests for the FINAL HARDENING pass.

Every test here pins a defect that was found either by tracing the frozen
implementation or by observing the RUNNING system, and that a future change
could plausibly reintroduce. Each test names the defect it guards, because a
regression test whose purpose is not obvious gets deleted the first time it
becomes inconvenient.

Nothing here changes architecture, adds a feature, or asserts a new
behavioural contract beyond the fix it covers.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import textwrap
import threading
import time
from typing import Any

import pytest

from aeam.config.settings import Settings


def _executable_source(module_path: str, qualname: str | None = None) -> str:
    """Return a module's (or method's) code with comments and docstrings removed.

    Several assertions below are about what the code DOES, not about what the
    file says. Grepping raw source conflates the two: an explanatory comment
    that quotes a removed literal would satisfy — or wrongly fail — the check.
    Round-tripping through the AST keeps only executable statements.
    """
    module = importlib.import_module(module_path)
    if qualname is None:
        source = inspect.getsource(module)
    else:
        target: Any = module
        for part in qualname.split("."):
            target = getattr(target, part)
        source = inspect.getsource(target)
    # A method's source arrives indented by its class body; ast.parse needs a
    # module-level block. textwrap.dedent (not inspect.cleandoc, which only
    # normalises docstrings) is the correct tool here.
    return ast.unparse(ast.parse(textwrap.dedent(source)))


# ===========================================================================
# 1. Composition root: the metrics-connector NameError
# ===========================================================================


def test_main_binds_source_repository_for_metric_connector_composition():
    """`SourceRepository` must be bound in main.py's module namespace.

    Defect: main.py called ``SourceRepository(container.db).list_all()`` inside
    the METRICS-connector composition block without ever importing the name.
    The resulting NameError was swallowed by a broad ``except Exception``, so
    every enabled metrics connector (SAP/Salesforce/Snowflake/BigQuery) was
    silently dropped from CompositeKPISource while connector health kept
    reporting it enabled.
    """
    import aeam.main as main_module

    assert hasattr(main_module, "SourceRepository"), (
        "main.py must import SourceRepository — the metrics-connector "
        "composition block calls it directly."
    )
    from aeam.registry.repositories import SourceRepository

    assert main_module.SourceRepository is SourceRepository


def test_metric_connector_composition_reraises_wiring_bugs_but_not_upstream_failures():
    """A NameError/AttributeError in composition must not be swallowed again.

    The broad handler is still correct for genuine upstream failures (an
    unreachable warehouse must not block startup); it must no longer absorb
    programming errors in the composition root itself.
    """
    import inspect

    import aeam.main as main_module

    source = inspect.getsource(main_module._lifespan)
    assert "except (NameError, AttributeError, ImportError, TypeError):" in source, (
        "composition-root programming errors must be re-raised, not absorbed "
        "into a log line"
    )


# ===========================================================================
# 2. /health must actually probe the database
# ===========================================================================


class _StubQueue:
    def size(self) -> int:
        return 0


class _StubRedis:
    def ping(self) -> bool:
        return True


class _HealthySettings:
    REDIS_URL = "redis://localhost:6379/0"
    HEARTBEAT_STALE_SECONDS = 120
    MONITOR_INTERVAL_SECONDS = 300
    BM25_STALE_SECONDS = 3600
    PLANNING_AGENT_ENABLED = False
    SUPERVISOR_AGENT_ENABLED = False


class _StubContainer:
    def __init__(self, db: Any) -> None:
        self.settings = _HealthySettings()
        self.db = db
        self.redis = _StubRedis()
        self.queue = _StubQueue()
        self.monitor_agent = None
        self.ingestion_worker = None
        self.bm25_index = None


def test_health_reports_degraded_when_the_database_is_unreachable():
    """Defect: the database check was `checks["database"] = "ok"` inside a try
    whose body could not raise, so the handler was unreachable and the value
    unconditional. A fully unreachable database still reported "healthy",
    defeating the restart/de-route decisions this endpoint exists to drive.
    """
    from aeam.main import build_health_payload

    class _BrokenDB:
        def fetch_one(self, *_a, **_k):
            raise RuntimeError("connection refused")

    payload = build_health_payload(_StubContainer(_BrokenDB()))

    assert payload["status"] == "degraded"
    assert payload["checks"]["database"].startswith("error:")
    assert "connection refused" in payload["checks"]["database"]


def test_health_reports_ok_when_the_database_answers():
    from aeam.main import build_health_payload

    class _WorkingDB:
        def __init__(self) -> None:
            self.queries: list[str] = []

        def fetch_one(self, query: str, *_a, **_k):
            self.queries.append(query)
            return {"ok": 1}

    db = _WorkingDB()
    payload = build_health_payload(_StubContainer(db))

    assert payload["status"] == "healthy"
    assert payload["checks"]["database"] == "ok"
    # It must be a real round-trip, not an assumption.
    assert db.queries, "the database check must actually issue a query"


def test_health_reports_degraded_when_no_database_client_exists():
    from aeam.main import build_health_payload

    container = _StubContainer(None)
    payload = build_health_payload(container)
    assert payload["status"] == "degraded"
    assert "no database client" in payload["checks"]["database"]


# ===========================================================================
# 3. Monitor heartbeat threshold vs poll interval
# ===========================================================================


def test_monitor_heartbeat_threshold_respects_the_poll_interval():
    """Defect: HEARTBEAT_STALE_SECONDS=120 with MONITOR_INTERVAL_SECONDS=300
    declared a healthy MonitorAgent stale for ~60% of every cycle. Since
    docker-compose.yml defaults ENABLE_MONITOR_AGENT=true, `docker compose up`
    produced a permanently-503 platform (a restart loop wherever /health is a
    liveness probe).
    """
    from aeam.main import build_health_payload
    from aeam.monitoring.metrics import heartbeat_tracker

    class _WorkingDB:
        def fetch_one(self, *_a, **_k):
            return {"ok": 1}

    container = _StubContainer(_WorkingDB())
    container.monitor_agent = object()  # constructed => supervised

    heartbeat_tracker.record("monitor")
    # Simulate a heartbeat 200s old: older than the raw 120s threshold, but
    # well within one 300s poll interval, so it is NOT a fault.
    heartbeat_tracker._last_seen["monitor"] = time.time() - 200.0  # noqa: SLF001

    payload = build_health_payload(container)
    assert payload["checks"]["monitor_agent"].startswith("ok"), (
        "a heartbeat younger than one poll interval must not be called stale"
    )
    assert payload["status"] == "healthy"


def test_monitor_heartbeat_still_detects_a_genuinely_dead_thread():
    """The floor must not disable staleness detection — only calibrate it."""
    from aeam.main import build_health_payload
    from aeam.monitoring.metrics import heartbeat_tracker

    class _WorkingDB:
        def fetch_one(self, *_a, **_k):
            return {"ok": 1}

    container = _StubContainer(_WorkingDB())
    container.monitor_agent = object()

    heartbeat_tracker.record("monitor")
    # Far beyond 2 intervals + grace (630s) => a real fault.
    heartbeat_tracker._last_seen["monitor"] = time.time() - 5000.0  # noqa: SLF001

    payload = build_health_payload(container)
    assert payload["checks"]["monitor_agent"].startswith("stale")
    assert payload["status"] == "degraded"


# ===========================================================================
# 4. Incident report email must fail closed
# ===========================================================================


def test_incident_report_recipients_defaults_to_empty():
    """Defect: the recipient list was the hardcoded literal
    'ops@company.com' — a registered THIRD-PARTY domain — so any deployment
    with working SMTP silently emailed every finalized incident's full report
    (root cause, evidence, confidence) off-organization.
    """
    settings = Settings(
        DATABASE_URL="sqlite:///:memory:",
        REDIS_URL="redis://localhost:6379/0",
        VECTOR_DB_URL="http://localhost:6333",
        ENVIRONMENT="test",
    )
    assert settings.INCIDENT_REPORT_RECIPIENTS == "", (
        "an egress path must fail closed by default"
    )


def test_orchestrator_no_longer_contains_a_hardcoded_recipient():
    """Asserted against EXECUTABLE code, not raw text.

    ``ast.unparse(ast.parse(...))`` drops comments and docstrings, so this
    cannot be satisfied (or broken) by prose that merely mentions the old
    literal — only by the code that would actually send the mail.
    """
    assert "ops@company.com" not in _executable_source(
        "aeam.agents.orchestrator.orchestrator"
    ), "the hardcoded third-party recipient must not return"
    assert "INCIDENT_REPORT_RECIPIENTS" in _executable_source(
        "aeam.agents.orchestrator.orchestrator"
    )


# ===========================================================================
# 5. Grounded root cause must survive depth->=3 LLM reasoning
# ===========================================================================


def test_llm_reasoning_does_not_overwrite_a_grounded_root_cause():
    """Defect: the depth->=3 LLM block wrote root_cause UNCONDITIONALLY, with
    `insight.get("root_cause", "Unknown")`. It discarded RAG's chunk-cited,
    guardrail-checked, grounding-VALIDATED cause in favour of unvalidated free
    text (this path passes only parse_llm_json), flipped root_cause_source to
    "llm_reasoning" while the persisted chunk_ids still cited the superseded
    finding, and could write the literal string "Unknown" over a real cause.
    """
    source = _executable_source(
        "aeam.agents.orchestrator.orchestrator", "Orchestrator._investigate"
    )

    assert "'root_cause', 'Unknown'" not in source, (
        'the "Unknown" default must not overwrite a grounded root cause'
    )
    # The write must be guarded by "nothing better is already there",
    # mirroring KPIAgent's deliberate precedence rule.
    assert "not existing_root_cause" in source


def test_llm_reasoning_reuses_the_shared_llm_service():
    """Defect: this path constructed its own LLMService per pass, so it could
    keep hammering a provider the shared client had already circuit-broken,
    and LLM_TIMEOUT_SECONDS' documented "one setting governs all six call
    sites" was false.
    """
    import inspect

    from aeam.agents.orchestrator.orchestrator import Orchestrator

    source = inspect.getsource(Orchestrator._investigate)
    assert 'getattr(self._decision, "_llm", None)' in source


# ===========================================================================
# 6. LLMService: diagnosis, retries, breaker
# ===========================================================================


def _llm_settings(**over: Any) -> Any:
    class _S:
        LLM_ENABLED = True
        USE_MOCK_LLM = False
        LLM_PROVIDER = "groq"
        LLM_API_KEY = "test-key"
        LLM_TIMEOUT_SECONDS = 5.0
        LLM_MODEL = ""
        LLM_COST_PER_1K_PROMPT_TOKENS_USD = 0.0
        LLM_COST_PER_1K_COMPLETION_TOKENS_USD = 0.0

    s = _S()
    for k, v in over.items():
        setattr(s, k, v)
    return s


def test_llm_failure_message_carries_the_real_provider_error():
    """Defect: the retry loop swallowed the real exception into a
    `logger.warning` and raised only "Failed to generate LLM response after
    retries". That string was what got PERSISTED into every failed incident,
    so an operator could not distinguish an expired key from a decommissioned
    model from a rate limit. Confirmed on the running system, where seven
    incidents recorded exactly that opaque string.
    """
    from aeam.services.llm_service import LLMService, LLMServiceException

    svc = LLMService(settings=_llm_settings())

    def _boom():
        raise RuntimeError("upstream exploded: quota exceeded")

    svc._groq_client = _boom  # noqa: SLF001

    with pytest.raises(LLMServiceException) as exc_info:
        svc.query("hello", temperature=0.0, max_tokens=5)

    message = str(exc_info.value)
    assert "quota exceeded" in message, f"real cause must survive: {message!r}"
    assert "RuntimeError" in message


def test_llm_does_not_retry_a_permanent_error():
    """Defect: an invalid key (401) or decommissioned model (404) was retried
    three times with 1+2+4s of sleep. Across five RAG passes that added ~35s
    of dead latency to every investigation before it could report the failure.
    """
    from aeam.services.llm_service import LLMService, LLMServiceException

    svc = LLMService(settings=_llm_settings())
    calls = {"n": 0}

    class _AuthError(Exception):
        status_code = 401

    def _client():
        calls["n"] += 1
        raise _AuthError("invalid api key")

    svc._groq_client = _client  # noqa: SLF001

    with pytest.raises(LLMServiceException):
        svc.query("hello", temperature=0.0, max_tokens=5)

    assert calls["n"] == 1, (
        f"a permanent error must not be retried (got {calls['n']} attempts)"
    )


def test_llm_retries_a_transient_error():
    """The no-retry rule must apply ONLY to permanent errors."""
    from aeam.services.llm_service import LLMService, LLMServiceException

    svc = LLMService(settings=_llm_settings())
    calls = {"n": 0}

    def _client():
        calls["n"] += 1
        raise TimeoutError("temporarily unavailable")

    svc._groq_client = _client  # noqa: SLF001

    with pytest.raises(LLMServiceException):
        svc.query("hello", temperature=0.0, max_tokens=5)

    assert calls["n"] == 3, f"a transient error must exhaust retries (got {calls['n']})"


def test_llm_success_resets_the_failure_tally():
    """Defect: _failure_count only ever grew — a success never cleared it — so
    a service with two historical failures tripped its breaker on the next
    one no matter how many thousands of calls had succeeded in between.
    """
    from aeam.services.llm_service import LLMService

    svc = LLMService(settings=_llm_settings())
    svc._failure_count = 2  # noqa: SLF001

    class _Msg:
        content = "ok"

    class _Choice:
        message = _Msg()

    class _Chat:
        choices = [_Choice()]
        usage = None

    class _Completions:
        def create(self, **_kw):
            return _Chat()

    class _ChatNS:
        completions = _Completions()

    class _Client:
        chat = _ChatNS()

    svc._groq_client = lambda: _Client()  # noqa: SLF001

    assert svc.query("hi", temperature=0.0, max_tokens=5) == "ok"
    assert svc._failure_count == 0, "a success must clear the failure tally"  # noqa: SLF001


def test_llm_model_id_is_configurable_but_default_is_unchanged():
    """A hardcoded model id becomes a permanent 404 the day the vendor
    decommissions it, with no configuration path out.
    """
    from aeam.services.llm_service import LLMService

    assert LLMService(settings=_llm_settings())._model_id() == "llama-3.1-8b-instant"
    assert (
        LLMService(settings=_llm_settings(LLM_MODEL="llama-3.3-70b-versatile"))._model_id()
        == "llama-3.3-70b-versatile"
    )


# ===========================================================================
# 7. BM25 index: concurrent in-place refresh vs live search
# ===========================================================================


def _bm25_docs(n: int, tag: str) -> list[dict[str, Any]]:
    return [
        {
            "chunk_id": f"{tag}-{i}",
            "text": f"database latency index scan slow query {tag} {i}",
            "metadata": {"source": f"{tag}.md"},
        }
        for i in range(n)
    ]


def test_bm25_search_survives_a_concurrent_in_place_refresh():
    """Defect (race condition): refresh_from_qdrant() rebuilt seven parallel
    structures IN PLACE from the IngestionWorker thread while search() read
    them from request threads. search() collects indices by enumerating
    _doc_freqs and then dereferences _docs[i], so a reader holding the old
    _doc_freqs (556 entries) against a new, mid-rebuild _docs (12 entries)
    raised `IndexError: list index out of range` — surfacing as a spurious
    "Retrieval failed" RAG pass whenever a document was ingested during an
    investigation.
    """
    from aeam.agents.rag.hybrid_retrieval import BM25Index

    index = BM25Index()
    index.build(_bm25_docs(400, "initial"))

    errors: list[BaseException] = []
    stop = threading.Event()

    def _search_loop() -> None:
        try:
            while not stop.is_set():
                index.search("database latency slow query", top_k=5)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def _rebuild_loop() -> None:
        try:
            for i in range(40):
                index.build(_bm25_docs(20 + (i % 300), f"refresh{i}"))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    readers = [threading.Thread(target=_search_loop) for _ in range(4)]
    writer = threading.Thread(target=_rebuild_loop)
    for t in readers:
        t.start()
    writer.start()
    writer.join()
    stop.set()
    for t in readers:
        t.join()

    assert errors == [], f"concurrent refresh corrupted a live search: {errors[:3]}"


def test_bm25_results_are_internally_consistent_after_refresh():
    """Every returned chunk_id must come from the corpus actually indexed."""
    from aeam.agents.rag.hybrid_retrieval import BM25Index

    index = BM25Index()
    index.build(_bm25_docs(50, "initial"))
    index.build(_bm25_docs(10, "second"))

    hits = index.search("database latency slow query", top_k=10)
    assert hits
    assert all(h["chunk_id"].startswith("second-") for h in hits)
    assert index.size == 10


# ===========================================================================
# 8. SQLite concurrency: busy timeout + WAL
# ===========================================================================


def test_sqlite_engine_sets_a_busy_timeout(tmp_path):
    """Defect: SQLite's default busy timeout is ZERO, so the first write-lock
    contention between AEAM's OWN threads (IngestionWorker + MonitorAgent +
    request workers) raised "database is locked" immediately. That is the
    mechanism behind the repository's one failing test, where a locked read
    was swallowed into BusinessGraphBuilder's `skipped_sources` and the build
    silently produced a partial graph while reporting success.
    """
    from aeam.integrations.database import DatabaseClient

    client = DatabaseClient(
        database_url=f"sqlite:///{(tmp_path / 'busy.db').as_posix()}",
        pool_timeout=7,
    )
    try:
        row = client.fetch_one("PRAGMA busy_timeout")
        assert row is not None
        assert int(list(row.values())[0]) == 7000
    finally:
        client.dispose()


def test_sqlite_engine_enables_wal(tmp_path):
    """WAL is what lets an investigation's reads proceed during an ingestion
    write instead of blocking or failing.
    """
    from aeam.integrations.database import DatabaseClient

    client = DatabaseClient(database_url=f"sqlite:///{(tmp_path / 'wal.db').as_posix()}")
    try:
        row = client.fetch_one("PRAGMA journal_mode")
        assert str(list(row.values())[0]).lower() == "wal"
    finally:
        client.dispose()


def test_postgres_url_is_not_given_sqlite_connect_args():
    """The SQLite tuning must not leak into the production backend."""
    import inspect

    from aeam.integrations import database

    source = inspect.getsource(database.DatabaseClient.__init__)
    assert 'startswith("sqlite")' in source
    assert "check_same_thread" in source


def test_concurrent_writes_on_sqlite_do_not_raise_database_is_locked(tmp_path):
    """The end-to-end property the two pragmas exist to deliver."""
    from aeam.integrations.database import DatabaseClient

    client = DatabaseClient(
        database_url=f"sqlite:///{(tmp_path / 'concurrent.db').as_posix()}",
        pool_timeout=30,
    )
    errors: list[BaseException] = []

    def _write(worker: int) -> None:
        # execute(), not insert(): insert() generates and returns a primary
        # key column, which `metrics` does not have. The property under test
        # is lock contention, not the insert helper's PK behaviour.
        try:
            for i in range(25):
                client.execute(
                    "INSERT INTO metrics (metric, value, timestamp) "
                    "VALUES (:metric, :value, :timestamp)",
                    params={
                        "metric": f"w{worker}",
                        "value": float(i),
                        "timestamp": f"2026-07-30T00:00:{i:02d}+00:00",
                    },
                )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    try:
        threads = [threading.Thread(target=_write, args=(w,)) for w in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == [], f"concurrent SQLite writes failed: {errors[:2]}"
        rows = client.fetch_all("SELECT COUNT(*) AS n FROM metrics")
        assert int(rows[0]["n"]) == 150
    finally:
        client.dispose()


# ===========================================================================
# 9. last_event_time must be measured, not synthesised
# ===========================================================================


def test_last_event_time_reads_the_newest_incident(tmp_path):
    """Defect: it was derived from container.queue.size() and then returned
    datetime.now() on BOTH branches, so the field reported "just now" on every
    call. Verified on the running system: two requests 1.7s apart returned two
    timestamps 1.7s apart. A liveness indicator structurally incapable of
    indicating anything.
    """
    from aeam.api.system import _derive_last_event_time
    from aeam.integrations.database import DatabaseClient

    client = DatabaseClient(database_url=f"sqlite:///{(tmp_path / 'evt.db').as_posix()}")

    class _C:
        db = client
        queue = _StubQueue()

    try:
        assert _derive_last_event_time(_C()) is None, (
            "a platform that has processed nothing must say so, not fabricate 'now'"
        )

        client.insert(table="incidents", data={
            "incident_id": "i-old", "event_type": "DB_LATENCY", "metric": "m",
            "severity": "HIGH", "timestamp": "2026-07-01T00:00:00+00:00",
        })
        client.insert(table="incidents", data={
            "incident_id": "i-new", "event_type": "DB_LATENCY", "metric": "m",
            "severity": "HIGH", "timestamp": "2026-07-20T12:00:00+00:00",
        })

        assert "2026-07-20" in str(_derive_last_event_time(_C()))
    finally:
        client.dispose()


# ===========================================================================
# 10. IngestionWorker must not silently no-op
# ===========================================================================


def test_ingestion_worker_requires_an_explicit_processor():
    """Defect: `processor or PlaceholderJobProcessor()` meant an omitted
    argument produced a worker that marked every job 100% complete and DONE
    having indexed nothing — documents appearing successfully ingested while
    permanently invisible to retrieval. Silent success is the most expensive
    failure shape available and must not be the default.
    """
    from aeam.ingestion.worker import IngestionWorker, PlaceholderJobProcessor

    class _Repo:
        def next_queued(self):
            return None

    with pytest.raises(ValueError, match="processor must be provided"):
        IngestionWorker(job_repo=_Repo())

    # Still available when explicitly, knowingly requested.
    assert IngestionWorker(job_repo=_Repo(), processor=PlaceholderJobProcessor())


# ===========================================================================
# 11. RAG evidence integrity on failure paths
# ===========================================================================


def test_rag_error_result_preserves_the_real_retrieved_count():
    """Defect: retrieved_count was hardcoded 0 on every error path, so a pass
    that retrieved five chunks and then hit an LLM failure was persisted as
    "retrieved 0" — indistinguishable from a retrieval that genuinely found
    nothing, which is a different fault with a different fix.
    audit_summary.evidence_count reads this field, so the incident record
    claimed no evidence existed when it did.
    """
    from aeam.agents.rag.rag_agent import RAGAgent

    result = RAGAgent._error_result(
        "LLM call failed: boom", query="q", attempt=1, strategy="original",
        threshold=0.5, retrieved_count=5,
    )
    assert result["findings"]["retrieved_count"] == 5
    assert result["findings"]["validation_passed"] is False
    assert "boom" in result["findings"]["error"]


def test_rag_error_result_still_reports_zero_when_nothing_was_retrieved():
    """Paths that fail BEFORE retrieval must keep reporting a true zero."""
    from aeam.agents.rag.rag_agent import RAGAgent

    result = RAGAgent._error_result("Retrieval failed: boom", query="q")
    assert result["findings"]["retrieved_count"] == 0


# ===========================================================================
# 12. Supervisor must not report a false negative
# ===========================================================================


def test_supervisor_resolves_action_and_kpi_metric_labels():
    """Defect (instrumentation mismatch, found only at runtime): the roster
    names agents ('action', 'kpi'), the E11 histogram is labelled by STAGE
    ('action:jira', 'action:slack', 'kpi_analysis'). The Supervisor's
    exact-label lookup therefore answered observed=false for both
    IMMEDIATELY AFTER they executed, and scored agent_activity 4/8 instead of
    6/8 — a false negative presented as observed fact, which its own
    "evidence, never invention" contract forbids.
    """
    from aeam.agents.supervisor.supervisor_agent import _resolve_executions
    from aeam.monitoring.metrics import agent_execution_time, end_timer, start_timer

    # Record under the labels the platform ACTUALLY uses.
    for label in ("action:jira", "action:slack", "kpi_analysis", "decision"):
        end_timer(agent_execution_time.labels(agent=label), start_timer())

    action_count, action_label = _resolve_executions("action")
    assert action_count is not None and action_count >= 2, (
        "ActionAgent executions must be found across its action:* series"
    )
    assert action_label == "action:*"

    kpi_count, kpi_label = _resolve_executions("kpi")
    assert kpi_count is not None and kpi_count >= 1
    assert kpi_label == "kpi_analysis"

    orch_count, orch_label = _resolve_executions("orchestrator")
    assert orch_count is not None and orch_count >= 1
    assert orch_label == "decision"


def test_supervisor_still_reports_unobserved_for_a_genuinely_idle_agent():
    """The alias map must not manufacture activity for an agent that never ran."""
    from aeam.agents.supervisor.supervisor_agent import _resolve_executions

    count, label = _resolve_executions("a-name-nothing-records-under")
    assert count is None
    assert label == "a-name-nothing-records-under"


# ===========================================================================
# 13. Graph build must disclose a partial result
# ===========================================================================


def test_graph_build_response_declares_completeness():
    """Defect: the builder records a failed evidence source in
    `skipped_sources` and continues (correct), but the response said
    `{"built": true}` either way — so a build that silently lost an entire
    evidence source was indistinguishable from a complete one. Reproduced:
    concurrent builds produced a 2-node graph where a sequential build
    produced 4, and still reported success.
    """
    import inspect

    from aeam.api import graph as graph_module

    source = inspect.getsource(graph_module.build_graph)
    assert 'payload["complete"] = not _skipped' in source
    assert "incomplete_reason" in source
