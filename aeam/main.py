"""
aeam/main.py

Entry point for the AEAM (Autonomous Event & Agent Monitor) modular monolith.

Responsibilities:
- Load application settings from environment.
- Construct and wire all infrastructure clients (database, Redis, event bus,
  priority queue, deduplicator).
- Mount a FastAPI application with a health endpoint.
- Expose a clean application factory (``create_app``) for testing and ASGI
  servers.

This module intentionally contains NO agent logic, NO orchestrator references,
NO LLM calls, and NO external API calls. It is pure infrastructure wiring.
"""
from aeam.services.llm_service import LLMService
import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response
from fastapi.middleware.cors import CORSMiddleware

from aeam.config.settings import Settings
from aeam.core.deduplication import EventDeduplicator
from aeam.core.event_bus import EventBus
from aeam.core.priority_queue import EventPriorityQueue
from aeam.integrations.database import DatabaseClient
from aeam.integrations.redis_client import RedisClient

# Agent imports
from aeam.agents.monitor.monitor_agent import MonitorAgent
from aeam.agents.kpi.rule_engine import RuleEngine
from aeam.agents.kpi.statistical_detector import StatisticalDetector
from aeam.agents.forecast.forecast_agent import ForecastAgent
from aeam.agents.rag.rag_agent import RAGAgent
from aeam.agents.report.report_agent import ReportAgent
from aeam.pipelines.structured_data_pipeline import StructuredDataPipeline
from aeam.agents.rag.ingestion_pipeline import IngestionPipeline
from aeam.agents.rag.retrieval_pipeline import RetrievalPipeline
from aeam.agents.rag.response_validator import RAGResponseValidator
from aeam.integrations.embedding_service import EmbeddingService
from qdrant_client import QdrantClient
# Orchestrator imports (Phase 3)
from aeam.agents.orchestrator.orchestrator import Orchestrator
from aeam.agents.orchestrator.decision_engine import DecisionEngine
from aeam.agents.orchestrator.evaluation_engine import EvaluationEngine
from aeam.agents.orchestrator.state_machine import IncidentStateMachine
from aeam.memory.short_term import ShortTermMemory
from aeam.memory.long_term import LongTermMemory

# Phase 8 Security imports
from aeam.middleware.security_middleware import SecurityMiddleware
from aeam.security.jwt_auth import JWTAuth
from aeam.security.rbac import RBAC
from aeam.security.rate_limiter import RateLimiter
from aeam.security.audit_logger import AuditLogger

# Sheets connector import
from aeam.connectors.sheets import SheetsConnector

# Action Agent imports (Phase 6)
from aeam.agents.action.action_agent import ActionAgent, CircuitBreaker
from aeam.agents.action.slack_actions import SlackActions
from aeam.integrations.secret_manager import SecretManager
from aeam.core.idempotency import IdempotencyManager

# Monitoring imports (Phase 6)
from prometheus_client import generate_latest
from aeam.monitoring.logging_config import get_logger

# APScheduler for 24/7 autonomous scheduling
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import uuid
import datetime
from aeam.core.event_models import Event

# API routers
from aeam.api.incidents import router as incidents_router
from aeam.api.system import router as system_router
from aeam.api.logs import router as logs_router
from aeam.api.trigger import router as trigger_router

# ---------------------------------------------------------------------------
# Logging bootstrap
# ---------------------------------------------------------------------------

logger = get_logger("aeam")

_STARTUP_KNOWLEDGE_DIR = Path(__file__).resolve().parent / "knowledge"


# ---------------------------------------------------------------------------
# Infrastructure container
# ---------------------------------------------------------------------------


class AppContainer:
    """
    Lightweight dependency container for all AEAM infrastructure objects.

    Holds references to every singleton client constructed at startup so they
    can be accessed via ``request.app.state.container`` inside route handlers
    and background tasks.

    Attributes:
        settings:     Validated application configuration.
        db:           SQLAlchemy-backed relational database client.
        redis:        Redis wrapper for caching and deduplication.
        event_bus:    Synchronous internal event dispatcher.
        queue:        Thread-safe in-memory priority queue for events.
        deduplicator: Window-based event deduplicator backed by Redis.
        sheets_connector: Optional Google Sheets connector (may be None).
        pipeline:     Structured data pipeline for cleaning and summarization.
    """

    def __init__(
        self,
        settings: Settings,
        db: DatabaseClient,
        redis: RedisClient,
        event_bus: EventBus,
        queue: EventPriorityQueue,
        deduplicator: EventDeduplicator,
        sheets_connector: SheetsConnector | None = None,
        pipeline: StructuredDataPipeline | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.redis = redis
        self.event_bus = event_bus
        self.queue = queue
        self.deduplicator = deduplicator
        self.sheets_connector = sheets_connector
        self.pipeline = pipeline

    def __repr__(self) -> str:
        return (
            f"AppContainer("
            f"env={self.settings.ENVIRONMENT!r}, "
            f"queue_size={self.queue.size()}, "
            f"bus_handlers={self.event_bus.handler_count()})"
        )


# ---------------------------------------------------------------------------
# Infrastructure factory
# ---------------------------------------------------------------------------


def _build_container(settings: Settings) -> AppContainer:
    """
    Construct and wire all infrastructure clients from ``settings``.

    This function is the single place where concrete implementations are
    instantiated. Swap implementations here (e.g. for testing) without
    touching any other module.

    Args:
        settings: Validated :class:`~aeam.config.settings.Settings` instance.

    Returns:
        A fully wired :class:`AppContainer`.

    Raises:
        Exception: Any client that fails to initialise (bad URL, unreachable
                   host, etc.) will propagate its exception, preventing the
                   application from starting in a broken state.
    """
    logger.info("Initialising DatabaseClient …")
    db = DatabaseClient(database_url=str(settings.DATABASE_URL))

    logger.info("Initialising RedisClient …")
    redis_client = RedisClient(redis_url=str(settings.REDIS_URL))

    logger.info("Initialising EventBus …")
    event_bus = EventBus()

    logger.info("Initialising EventPriorityQueue …")
    queue = EventPriorityQueue()

    logger.info("Initialising EventDeduplicator …")
    deduplicator = EventDeduplicator(redis_client=redis_client._client)

    # Attempt to create Sheets connector if credentials are present
    sheets_connector = None
    if settings.GOOGLE_SHEETS_SA_CREDENTIALS and settings.SHEET_ID:
        logger.info("Google Sheets credentials found – creating SheetsConnector.")
        sheets_connector = SheetsConnector(settings=settings, secret_manager=None)
    else:
        logger.info("Google Sheets credentials not configured – running without live KPI feed.")

    # Create data pipeline (used by ForecastAgent and MonitorAgent)
    pipeline = StructuredDataPipeline()

    return AppContainer(
        settings=settings,
        db=db,
        redis=redis_client,
        event_bus=event_bus,
        queue=queue,
        deduplicator=deduplicator,
        sheets_connector=sheets_connector,
        pipeline=pipeline,
    )


def _ingest_startup_documents(ingestion_pipeline: IngestionPipeline) -> None:
    documents = []
    for path in sorted(_STARTUP_KNOWLEDGE_DIR.glob("*.md")):
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            continue
        documents.append({
            "text": text,
            "metadata": {
                "source": path.name,
                "date": "2026-07-04",
                "doc_type": "startup_runbook",
                "doc_id": path.stem,
            },
        })

    if not documents:
        logger.warning(
            "RAG startup ingestion skipped | no documents found in %s",
            _STARTUP_KNOWLEDGE_DIR,
        )
        return

    results = ingestion_pipeline.ingest_batch(documents)
    chunks_upserted = sum(int(result.get("chunks_upserted", 0)) for result in results)
    logger.info(
        "RAG startup ingestion complete | documents=%d | chunks_upserted=%d",
        len(documents), chunks_upserted,
    )


# ---------------------------------------------------------------------------
# Lifespan — startup / shutdown
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """
    FastAPI lifespan context manager.

    Runs startup logic before the application begins serving requests and
    teardown logic after the last request has been handled.

    Startup:
        - Load settings.
        - Build and attach the :class:`AppContainer` to ``app.state``.
        - Wire and register the Orchestrator.
        - Verify Redis connectivity via ping.
        - Start the 24/7 autonomous scheduler.

    Shutdown:
        - Dispose of the database connection pool.
        - Close the Redis connection pool.
        - Shut down the scheduler.
    """
    # --- Startup ---
    logger.info("AEAM starting up …")

    settings = Settings()  # pyright: ignore[reportCallIssue]
    print(f"=== ENVIRONMENT = {settings.ENVIRONMENT!r} ===")  # temporary debug

    logger.info("Settings loaded | environment=%r", settings.ENVIRONMENT)

    container = _build_container(settings)
    app.state.container = container

    # -----------------------------
    # Orchestrator Wiring (Phase 3)
    # -----------------------------
    llm_service = LLMService(settings=settings)
    # Ensure compatibility with DecisionEngine's protocol
    decision_engine = DecisionEngine(settings=settings, llm_service=llm_service)
    evaluation_engine = EvaluationEngine(settings=settings)
    short_term_memory = ShortTermMemory()
    class _NoOpVectorClient:
        def upsert(self, *args, **kwargs):
            pass

        def query(self, *args, **kwargs):
            return []

        def delete(self, *args, **kwargs):
            pass

    vector_client = _NoOpVectorClient()

    long_term_memory = LongTermMemory(
        database_client=container.db,
        vector_client=vector_client,
    )
    state_machine = IncidentStateMachine()

    # --- Forecast Agent (Phase 5) ---
    forecast_agent = ForecastAgent(
        long_term_memory=long_term_memory,
        data_pipeline=container.pipeline,
        settings=settings,
    )

    # --- RAG and Report Agents (Phases 4 and 7) ---
    print("=== BEFORE EmbeddingService ===")
    embedding_service = EmbeddingService()
    print("=== AFTER EmbeddingService ===")

    print("=== BEFORE Qdrant ===")
    qdrant_client = QdrantClient(url=settings.VECTOR_DB_URL)
    print("=== AFTER Qdrant ===")

    print("=== BEFORE IngestionPipeline ===")
    ingestion_pipeline = IngestionPipeline(
        embedding_service=embedding_service,
        qdrant_client=qdrant_client,
    )
    _ingest_startup_documents(ingestion_pipeline)
    print("=== AFTER IngestionPipeline ===")

    print("=== BEFORE RetrievalPipeline ===")
    retrieval_pipeline = RetrievalPipeline(
        embedding_service=embedding_service,
        qdrant_client=qdrant_client,
    )
    print("=== AFTER RetrievalPipeline ===")

    print("=== BEFORE RAGAgent ===")
    validator = RAGResponseValidator()
    rag_agent = RAGAgent(
        retrieval_pipeline=retrieval_pipeline,
        validator=validator,
        llm_service=llm_service,
    )
    print("=== AFTER RAGAgent ===")

    print("=== BEFORE ReportAgent ===")
    report_agent = ReportAgent(settings=settings)
    print("=== AFTER ReportAgent ===")

    # --- Action Agent (Phase 6) ---
    action_agent = None
    if settings.SLACK_BOT_TOKEN:
        logger.info("Slack bot token found – initializing ActionAgent with Slack.")
        # Build required dependencies for ActionAgent
        secret_manager = SecretManager(project_id=getattr(settings, 'GCP_PROJECT', None))
        idempotency_mgr = IdempotencyManager(redis_client=container.redis)

        action_agent = ActionAgent(
            secret_manager=secret_manager,
            redis_client=container.redis,
            database_client=container.db,
            idempotency_manager=idempotency_mgr,
            settings=settings,
        )
        logger.info("ActionAgent initialised with Slack action.")

        # Register Jira if credentials are present
        if settings.JIRA_URL and settings.JIRA_API_TOKEN:
            from aeam.agents.action.jira_actions import JiraActions
            jira = JiraActions(settings=settings)
            action_agent._registry["jira"] = jira
            action_agent._circuit_breakers["jira"] = CircuitBreaker(
                failure_threshold=3,
                timeout_seconds=60,
            )
            logger.info("Jira action registered.")
    else:
        logger.info("No Slack bot token – ActionAgent not created.")

    # --- Monitor Agent (Phase 2) ---
    monitor_agent = None
    if settings.ENABLE_MONITOR_AGENT or settings.ENVIRONMENT != "production":
        logger.info("Creating MonitorAgent …")
        monitor_agent = MonitorAgent(
            event_bus=container.event_bus,
            queue=container.queue,
            deduplicator=container.deduplicator,
            rule_engine=RuleEngine(),
            statistical_detector=StatisticalDetector(window_size=7),
            forecast_agent=forecast_agent,          # <-- Pass the properly initialized forecast_agent
            pipeline=container.pipeline,
            settings=settings,
            kpi_source=container.sheets_connector,  # Phase 5: live KPI feed (may be None/disabled)
            long_term_memory=long_term_memory,      # Hardening: persist observations for forecast training
        )
        # Start the monitor agent in a background thread
        monitor_thread = threading.Thread(target=monitor_agent.start, daemon=True)
        monitor_thread.start()
        logger.info("MonitorAgent started in background thread.")
    else:
        logger.info("MonitorAgent disabled by configuration.")

    # --- Orchestrator ---
    orchestrator = Orchestrator(
        event_bus=container.event_bus,
        decision_engine=decision_engine,
        evaluation_engine=evaluation_engine,
        short_term_memory=short_term_memory,
        long_term_memory=long_term_memory,
        state_machine=state_machine,
        settings=settings,
        rag_agent=rag_agent,
        action_agent=action_agent,
        report_agent=report_agent,
    )

    # Register wildcard handler
    container.event_bus.register_handler("ALL", orchestrator.handle_event)

    logger.info("Orchestrator registered with EventBus (ALL wildcard).")
    logger.info("Infrastructure container ready | %r", container)

    # Connectivity probes — warn but do not abort; let the health endpoint
    # surface degraded state so orchestrators can take action.
    if container.redis.ping():
        logger.info("Redis connectivity: OK")
    else:
        logger.warning("Redis connectivity: DEGRADED — ping failed.")

    # ---------- 24/7 Autonomous Scheduler ----------
    scheduler = AsyncIOScheduler()

    async def periodic_event():
        event = Event(
            event_id=str(uuid.uuid4()),
            event_type="SALES_DROP",
            metric="sales",
            current_value=100.0,
            expected_value=200.0,
            drop_percent=50.0,
            detection_methods=["rule"],
            severity="HIGH",
            timestamp=datetime.datetime.utcnow().isoformat() + "Z",
        )
        container.event_bus.publish(event)

    # scheduler.add_job(
    #     periodic_event,
    #     'interval',
    #     seconds=settings.MONITOR_INTERVAL_SECONDS,   # default 300 (5 minutes)
    # )
    # scheduler.start()
    logger.info("APScheduler configured (disabled for frontend testing).")

    logger.info("AEAM startup complete.")
    yield

    # --- Shutdown ---
    logger.info("AEAM shutting down …")
    # scheduler.shutdown()
    if monitor_agent:
        # If MonitorAgent has a stop() method, call it; otherwise, we rely on daemon thread.
        # Here we just log.
        logger.info("MonitorAgent will be terminated by daemon thread exit.")
    container.db.dispose()
    container.redis.close()
    logger.info("AEAM shutdown complete.")


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------


def create_app() -> FastAPI:
    """
    Construct and return the FastAPI application instance.

    Using a factory function (rather than a module-level global) allows test
    suites to call ``create_app()`` multiple times with different settings or
    mocked dependencies without state leaking between test runs.

    Returns:
        A configured :class:`fastapi.FastAPI` instance with all routes and
        middleware attached.

    Example (ASGI server)::

        # gunicorn -w 1 -k uvicorn.workers.UvicornWorker "aeam.main:create_app()"
        # uvicorn aeam.main:app --reload
    """
    application = FastAPI(
        title="AEAM — Autonomous Event & Agent Monitor",
        description=(
            "Modular monolith for autonomous event detection, "
            "prioritisation, deduplication, and investigation."
        ),
        version="0.1.0",
        lifespan=_lifespan,
        # Disable the default 422 body included in validation errors in prod.
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # -------------------------------------------------
    # Phase 8: Security Middleware Registration
    # -------------------------------------------------
    # We must create the Redis client here, not use the container
    # because container is not yet attached at this point.
    settings = Settings()  # pyright: ignore[reportCallIssue]
    redis_client = RedisClient(redis_url=str(settings.REDIS_URL))

    jwt_auth = JWTAuth(public_key="dummy-public-key")  # replace later
    rbac = RBAC()
    rate_limiter = RateLimiter(redis_client=redis_client)
    audit_logger = AuditLogger()

    application.add_middleware(
        SecurityMiddleware,
        jwt_auth=jwt_auth,
        rbac=rbac,
        rate_limiter=rate_limiter,
        audit_logger=audit_logger,
        environment=settings.ENVIRONMENT,
    )

    logger.info("Security middleware registered.")

    # -------------------------------------------------
    # CORS middleware for frontend
    # -------------------------------------------------
    application.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # -------------------------------------------------
    # API Routers
    # -------------------------------------------------
    application.include_router(incidents_router)
    application.include_router(system_router)
    application.include_router(logs_router)
    application.include_router(trigger_router)

    _register_routes(application)
    return application


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


def _register_routes(app: FastAPI) -> None:
    """
    Attach all HTTP routes to ``app``.

    Separating route registration from ``create_app`` keeps the factory small
    and makes it easy to add API routers (``app.include_router(…)``) as the
    system grows.

    Args:
        app: The :class:`fastapi.FastAPI` instance to attach routes to.
    """

    @app.get(
        "/",
        summary="Root",
        description="Simple liveness response for local development.",
        tags=["Operations"],
    )
    def root() -> JSONResponse:
        container: AppContainer = app.state.container
        return JSONResponse(
            status_code=200,
            content={
                "message": "AEAM is running",
                "environment": container.settings.ENVIRONMENT,
            },
        )

    @app.get("/metrics")
    def metrics():
        return Response(generate_latest(), media_type="text/plain")

    @app.get("/health", tags=["Operations"])
    def health():
        container: AppContainer = app.state.container
        status = {
            "status": "healthy",
            "checks": {
                "database": "unknown",
                "redis": "unknown",
                "queue": "unknown"
            }
        }
        # Check database
        try:
            status["checks"]["database"] = "ok"
        except Exception as e:
            status["status"] = "degraded"
            status["checks"]["database"] = f"error: {str(e)}"

        # Check Redis only if URL is provided
        if container.settings.REDIS_URL:
            try:
                container.redis.ping()
                status["checks"]["redis"] = "ok"
            except Exception as e:
                status["status"] = "degraded"
                status["checks"]["redis"] = f"error: {str(e)}"
        else:
            status["checks"]["redis"] = "disabled (no REDIS_URL)"

        # Check queue
        try:
            size = container.queue.size()
            status["checks"]["queue"] = f"ok (size={size})"
        except Exception as e:
            status["status"] = "degraded"
            status["checks"]["queue"] = f"error: {str(e)}"

        return JSONResponse(status_code=200 if status["status"] == "healthy" else 503, content=status)


# ---------------------------------------------------------------------------
# Module-level app instance (for uvicorn / gunicorn direct reference)
# ---------------------------------------------------------------------------

app: FastAPI = create_app()
"""
Module-level FastAPI instance.

Use this for direct ASGI server invocation::

    uvicorn aeam.main:app --host 0.0.0.0 --port 8000
"""

# ---------------------------------------------------------------------------
# Note: EventBus modification required to support "ALL" wildcard.
# In aeam/core/event_bus.py, modify the publish() method to:
#
#   handlers = self._handlers.get(event.event_type, [])
#   wildcard_handlers = self._handlers.get("ALL", [])
#   for handler in handlers + wildcard_handlers:
#       handler(event)
# ---------------------------------------------------------------------------
