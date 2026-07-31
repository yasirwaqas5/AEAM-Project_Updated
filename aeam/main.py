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
from datetime import datetime, timezone
from pathlib import Path
import json
from typing import Any, AsyncIterator

from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from aeam.config.settings import Settings
from aeam.core.deduplication import EventDeduplicator
from aeam.core.event_bus import EventBus
from aeam.core.priority_queue import EventPriorityQueue
from aeam.integrations.database import DatabaseClient
from aeam.integrations.redis_client import RedisClient
from aeam.storage.blob_store import BlobStore, LocalDiskBlobStore
from aeam.storage.factory import build_blob_store
from aeam.registry.repositories import (
    IngestionJobRepository,
    DatasetRepository,
    SchemaRepository,
    VersionRepository,
    PolicyRepository,
    CompiledRuleRepository,
    GraphEdgeRepository,
    GraphNodeRepository,
    IncidentApprovalRepository,
    ReviewVerdictRepository,
    # Phase F7 hardening: required by the METRICS-connector composition
    # below. Its absence made that block raise NameError into a broad
    # `except Exception`, so every enabled metrics connector was silently
    # dropped from CompositeKPISource while connector health still
    # reported it enabled.
    SourceRepository,
)
from aeam.ingestion.worker import IngestionWorker
from aeam.ingestion.processor import DocumentIngestJobProcessor
from aeam.ingestion.dataset_processor import DatasetIngestJobProcessor
from aeam.ingestion.routing import RoutingJobProcessor
from aeam.intelligence.dataset_intelligence import DatasetIntelligenceService
from aeam.intelligence.dataset_kpi_source import DatasetKPISource
from aeam.intelligence.dataset_activation import RedisDatasetActivation, parse_activated_dataset_ids
from aeam.connectors.composite_kpi_source import CompositeKPISource

# Agent imports
from aeam.agents.monitor.monitor_agent import MonitorAgent
from aeam.agents.kpi.rule_engine import RuleEngine
from aeam.agents.kpi.composite_rule_engine import CompositeRuleEngine
from aeam.agents.kpi.statistical_detector import StatisticalDetector
from aeam.agents.forecast.forecast_agent import ForecastAgent
from aeam.agents.rag.rag_agent import RAGAgent
from aeam.agents.report.report_agent import ReportAgent
from aeam.pipelines.structured_data_pipeline import StructuredDataPipeline
from aeam.agents.rag.ingestion_pipeline import IngestionPipeline
from aeam.agents.rag.retrieval_pipeline import RetrievalPipeline
from aeam.memory.enterprise_memory import EnterpriseMemoryEngine
from aeam.intelligence.policy_extraction import PolicyExtractor
from aeam.intelligence.policy_registry import PolicyRegistry
from aeam.agents.policy.policy_agent import PolicyAgent
from aeam.intelligence.cross_dataset_analyzer import CrossDatasetAnalyzer
from aeam.intelligence.business_graph import BusinessGraphStore, TraversalBudget
from aeam.intelligence.graph_correlation import GraphCorrelationEngine
from aeam.intelligence.observability import ObservabilityEngine
from aeam.agents.planning.planning_agent import PlanningAgent
from aeam.agents.supervisor.supervisor_agent import SupervisorAgent
from aeam.connectors.health import ConnectorHealthReporter
from aeam.connectors.registry import ConnectorRegistry
from aeam.connectors.sync import ConnectorSyncEngine
from aeam.ingestion.submission import IngestionSubmitter
from aeam.intelligence.adaptive_detection import AdaptiveDetectionEngine
from aeam.intelligence.execution_planning import ExecutionPlanningEngine
from aeam.governance.human_review import HumanReviewService
from aeam.intelligence.explainability import ExplainabilityEngine
from aeam.intelligence.ai_evaluation import AIEvaluationEngine
from aeam.agents.rag.hybrid_retrieval import BM25Index, HybridRetrievalPipeline
from aeam.agents.rag.query_expansion import QueryExpansionAgent
from aeam.agents.rag.multi_query_retrieval import MultiQueryRetrievalPipeline
from aeam.agents.rag.reranker import CrossEncoderReranker, RerankingRetrievalPipeline
from aeam.agents.rag.evidence_diversity import EvidenceDiversityFilter, EvidenceDiversityPipeline
from aeam.agents.rag.advanced_retrieval import (
    AdvancedRetrievalPipeline, BusinessRelevanceScorer, IncidentEntityExtractor,
)
from aeam.agents.rag.retrieval_debug import RetrievalDebugTracer
from aeam.agents.rag.response_validator import RAGResponseValidator
from aeam.integrations.embedding_service import EmbeddingService
from qdrant_client import QdrantClient
# Orchestrator imports (Phase 3)
from aeam.agents.orchestrator.orchestrator import Orchestrator
from aeam.agents.orchestrator.decision_engine import DecisionEngine
from aeam.agents.orchestrator.evaluation_engine import EvaluationEngine
# Phase E2 (ARCH-8): the Orchestrator allocates its own per-incident
# ShortTermMemory and IncidentStateMachine inside handle_event(), so this
# module no longer constructs shared singletons for them. See
# aeam.agents.orchestrator.incident_context for the isolation contract.
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
from aeam.monitoring.metrics import heartbeat_tracker
from aeam.monitoring.tracing import configure_tracing

# API routers
from aeam.api.incidents import router as incidents_router
from aeam.api.system import router as system_router
from aeam.api.logs import router as logs_router
from aeam.api.trigger import router as trigger_router
from aeam.api.retrieval_debug import router as retrieval_debug_router
from aeam.api.ingest import router as ingest_router
from aeam.api.knowledge import router as knowledge_router
from aeam.api.data_center import router as data_center_router
from aeam.api.observability import router as observability_router
from aeam.api.administration import router as administration_router
from aeam.api.review import router as review_router
from aeam.api.auth import router as auth_router, resolve_oidc_endpoints
from aeam.api.audit import router as audit_router
from aeam.api.learning import router as learning_router
from aeam.api.graph import router as graph_router
from aeam.api.replay import router as replay_router
from aeam.api.mesh import router as mesh_router
from aeam.api.connectors import router as connectors_router

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
        blob_store: BlobStore | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.redis = redis
        self.event_bus = event_bus
        self.queue = queue
        self.deduplicator = deduplicator
        self.sheets_connector = sheets_connector
        self.pipeline = pipeline
        # Enterprise Data Layer (Phase B1.1) — storage foundation for later
        # ingestion phases. Present but not yet driven by any endpoint.
        self.blob_store = blob_store

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
    # Phase E6 (resource management): connection-pool bounds are now
    # configurable so a load spike is met with bounded back-pressure rather
    # than unbounded connection growth. Defaults match the pre-E6 values.
    db = DatabaseClient(
        database_url=str(settings.DATABASE_URL),
        pool_size=settings.DB_POOL_SIZE,
        max_overflow=settings.DB_MAX_OVERFLOW,
        pool_timeout=settings.DB_POOL_TIMEOUT_SECONDS,
    )

    logger.info("Initialising RedisClient …")
    redis_client = RedisClient(redis_url=str(settings.REDIS_URL))

    logger.info("Initialising EventBus …")
    event_bus = EventBus()

    # EventPriorityQueue disposition (Phase E1, ENG-8): a correct, tested
    # primitive with no production consumer yet — MonitorAgent's push-only
    # write was removed (unbounded, never drained). It stays wired here
    # because /health and /system/status report its size (COMPAT-4: the
    # response field must not disappear) and a real consumer is expected
    # with the Roadmap E2+ concurrency work.
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

    # Enterprise Data Layer (Phase B1.1) — content-addressable blob store for
    # original ingested files. Phase E4 (ARCH-7) delegates backend selection
    # to build_blob_store: 'local' preserves today's LocalDiskBlobStore
    # byte-identically; 's3' targets any S3-compatible endpoint (AWS, MinIO,
    # R2, GCS-via-HMAC) with credentials resolved through SecretManager.
    logger.info("Initialising BlobStore …")
    blob_store = build_blob_store(
        settings=settings,
        secret_manager=SecretManager(settings=settings),
    )

    return AppContainer(
        settings=settings,
        db=db,
        redis=redis_client,
        event_bus=event_bus,
        queue=queue,
        deduplicator=deduplicator,
        sheets_connector=sheets_connector,
        pipeline=pipeline,
        blob_store=blob_store,
    )


def _ingest_startup_documents(ingestion_pipeline: IngestionPipeline) -> None:
    documents = []
    for path in sorted(_STARTUP_KNOWLEDGE_DIR.glob("*.md")):
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            continue
        # Hardening: this was the frozen literal "2026-07-04". BusinessRelevanceScorer
        # reads `date` to award its recency bonus, so a hardcoded date meant the
        # startup runbooks' ranking silently decayed against uploaded documents as
        # wall-clock time passed — invisibly, because nothing disclosed the value's
        # provenance. The file's own mtime is a real, self-maintaining fact.
        try:
            doc_date = datetime.fromtimestamp(
                path.stat().st_mtime, tz=timezone.utc
            ).date().isoformat()
        except OSError:
            doc_date = datetime.now(tz=timezone.utc).date().isoformat()
        documents.append({
            "text": text,
            "metadata": {
                "source": path.name,
                "date": doc_date,
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

    Shutdown:
        - Dispose of the database connection pool.
        - Close the Redis connection pool.

    Autonomy note (Phase E1 removal, Phase E7 production enablement,
    ENG-8): there is no separate scheduler process. Autonomous detection
    is MonitorAgent's own polling loop below, gated solely by
    ``settings.ENABLE_MONITOR_AGENT`` (no environment backdoor — see the
    gating comment at its construction site). When disabled, events enter
    only via ``POST /api/v1/trigger`` or ``run_simulation.py``.
    """
    # --- Startup ---
    logger.info("AEAM starting up …")

    # Hardening: reuse the SAME Settings instance create_app() already built
    # and stashed on app.state, instead of constructing a second one. Two
    # live configuration objects meant SecurityMiddleware (which holds the
    # create_app instance, and with it the `development` auth bypass) and
    # every agent/engine (which held this one) could disagree about which
    # environment the process is in if anything mutated the environment
    # between the two constructions. One object, one answer. Falls back to a
    # fresh Settings only if app.state has none, so a caller that builds the
    # lifespan directly (tests) keeps working unchanged.
    settings = getattr(app.state, "settings", None)
    if not isinstance(settings, Settings):
        settings = Settings()  # pyright: ignore[reportCallIssue]
        app.state.settings = settings
    logger.info("Settings loaded | environment=%r", settings.ENVIRONMENT)

    # Phase E11 (OBS-6): configure OTLP tracing before any investigation can
    # run. A no-op unless OTEL_TRACING_ENABLED is set with a real endpoint AND
    # the optional OpenTelemetry SDK is installed — never a startup failure,
    # because a telemetry backend must not be able to stop the platform.
    configure_tracing(settings)

    container = _build_container(settings)
    app.state.container = container

    # Phase E3 (ARCH-7): upgrade the AuditLogger with the DB client now
    # that the container exists. Middleware holds the same AuditLogger
    # instance, so subsequent audit writes land in the audit_logs table
    # alongside the file sink. Development environments and any startup
    # posture without a DB simply keep file-only behaviour (COMPAT-1).
    audit_logger = getattr(app.state, "audit_logger", None)
    if audit_logger is not None and getattr(container, "db", None) is not None:
        audit_logger.attach_database(container.db)
        logger.info(
            "AuditLogger durable sink attached (audit_logs table). "
            "File sink at %s remains active.", settings.AUDIT_LOG_FILE,
        )

    # -----------------------------
    # Orchestrator Wiring (Phase 3)
    # -----------------------------
    llm_service = LLMService(settings=settings)
    # Ensure compatibility with DecisionEngine's protocol
    decision_engine = DecisionEngine(settings=settings, llm_service=llm_service)
    evaluation_engine = EvaluationEngine(settings=settings)
    # Phase E2 (ARCH-8): no shared ShortTermMemory singleton — the
    # Orchestrator allocates a fresh one per incident inside handle_event().
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
    # Phase E2 (ARCH-8): no shared IncidentStateMachine singleton — the
    # Orchestrator allocates a fresh one per incident inside handle_event().

    # --- Forecast Agent (Phase 5) ---
    # Phase E4 (ARCH-7): model_dir is sourced from Settings when set,
    # so ephemeral-compute deployments (Cloud Run) can point it at a
    # durable mount. Empty preserves the ForecastAgent engine-owned
    # default 'models/forecasting' byte-for-byte (COMPAT-1).
    _forecast_kwargs: dict[str, Any] = {}
    if settings.FORECAST_MODEL_DIR:
        _forecast_kwargs["model_dir"] = settings.FORECAST_MODEL_DIR
    forecast_agent = ForecastAgent(
        long_term_memory=long_term_memory,
        data_pipeline=container.pipeline,
        settings=settings,
        # Phase F1 (TECH-6/OBS-2): gives holdout measurements somewhere
        # durable to land. Inert unless FORECAST_BACKTEST_ENABLED is set.
        database_client=container.db,
        **_forecast_kwargs,
    )

    # --- RAG and Report Agents (Phases 4 and 7) ---
    embedding_service = EmbeddingService()

    qdrant_client = QdrantClient(url=settings.VECTOR_DB_URL)
    container.qdrant_client = qdrant_client

    ingestion_pipeline = IngestionPipeline(
        embedding_service=embedding_service,
        qdrant_client=qdrant_client,
    )
    container.ingestion_pipeline = ingestion_pipeline
    _ingest_startup_documents(ingestion_pipeline)

    retrieval_pipeline = RetrievalPipeline(
        embedding_service=embedding_service,
        qdrant_client=qdrant_client,
    )

    # --- Enterprise Memory Engine (Phase C1) ---
    # Reuses the SAME EmbeddingService + QdrantClient + IngestionPipeline/
    # RetrievalPipeline classes as the document RAG pipeline above — pointed
    # at a second, dedicated collection rather than a second vector store or
    # embedding model. Composition, not duplication: both pipeline classes
    # are already collection-parametrized.
    memory_ingestion_pipeline = IngestionPipeline(
        embedding_service=embedding_service,
        qdrant_client=qdrant_client,
        collection="aeam_incident_memories",
    )
    memory_retrieval_pipeline = RetrievalPipeline(
        embedding_service=embedding_service,
        qdrant_client=qdrant_client,
        collection="aeam_incident_memories",
    )
    enterprise_memory = EnterpriseMemoryEngine(
        ingestion_pipeline=memory_ingestion_pipeline,
        retrieval_pipeline=memory_retrieval_pipeline,
        similarity_threshold=settings.MEMORY_SIMILARITY_THRESHOLD,
    )
    container.enterprise_memory = enterprise_memory
    logger.info(
        "Enterprise Memory Engine initialised | collection=%s",
        enterprise_memory.collection,
    )

    # --- Phase 7.1: Hybrid (dense + BM25 + RRF) retrieval ---
    # Wrap the unchanged dense pipeline. BM25 corpus is built by scrolling the
    # same Qdrant collection. Any build failure falls back to dense-only so RAG
    # never breaks at startup.
    rag_retrieval = retrieval_pipeline
    bm25_index = None
    if settings.RAG_HYBRID_ENABLED:
        try:
            bm25_index = BM25Index.from_qdrant(
                qdrant_client=qdrant_client,
                collection=retrieval_pipeline.collection,
            )
            rag_retrieval = HybridRetrievalPipeline(
                dense_pipeline=retrieval_pipeline,
                bm25_index=bm25_index,
            )
            logger.info(
                "RAG hybrid retrieval ENABLED | bm25_docs=%d | collection=%s",
                bm25_index.size, retrieval_pipeline.collection,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "RAG hybrid retrieval init failed (%s) — falling back to dense-only.",
                exc,
            )
            rag_retrieval = retrieval_pipeline
            bm25_index = None
    else:
        logger.info("RAG hybrid retrieval DISABLED by configuration — dense-only.")

    # Phase E7 (RAG-6): exposed on the container so (a) GET /health can
    # disclose lexical-index freshness, and (b) DocumentIngestJobProcessor
    # below can refresh it in place after a runtime ingestion completes.
    # None when hybrid retrieval is disabled or failed to initialise.
    container.bm25_index = bm25_index

    # Snapshot the pipeline reference at this exact point — either the real
    # HybridRetrievalPipeline or plain dense retrieval — for the retrieval
    # debug tracer (Phase 7.4 explainability). Not used by production RAG
    # flow; RAGAgent only ever sees the fully-composed `rag_retrieval` below.
    hybrid_stage = rag_retrieval

    # --- Phase 7.3: Multi-Query Retrieval ---
    # Wrap the active retrieval (hybrid or dense) so each query is expanded
    # into diverse variants, retrieved separately, and fused. Reuses the
    # already-constructed llm_service. Falls back to the unwrapped pipeline on
    # any construction error (mirrors the hybrid/rerank fallback pattern).
    query_expander = None
    if settings.RAG_MULTI_QUERY_ENABLED:
        try:
            query_expander = QueryExpansionAgent(
                llm_service=llm_service,
                query_count=settings.RAG_MULTI_QUERY_COUNT,
            )
            rag_retrieval = MultiQueryRetrievalPipeline(
                inner_pipeline=rag_retrieval,
                query_expansion_agent=query_expander,
            )
            logger.info(
                "RAG multi-query retrieval ENABLED | query_count=%d",
                settings.RAG_MULTI_QUERY_COUNT,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "RAG multi-query init failed (%s) — falling back to prior retrieval stage.",
                exc,
            )
            query_expander = None
    else:
        logger.info("RAG multi-query retrieval DISABLED by configuration.")

    # --- Phase 7.2: Cross-encoder reranking ---
    # Wrap the active retrieval (hybrid or dense) in a retrieve-then-rerank
    # stage. If the cross-encoder model cannot initialize, keep the hybrid
    # pipeline so startup never breaks (requirement #13).
    reranker = None
    if settings.RAG_RERANK_ENABLED:
        try:
            reranker = CrossEncoderReranker(model_name=settings.RAG_RERANK_MODEL)
            rag_retrieval = RerankingRetrievalPipeline(
                inner_pipeline=rag_retrieval,
                reranker=reranker,
                rerank_top_n=settings.RAG_RERANK_TOP_N,
            )
            logger.info(
                "RAG cross-encoder reranking ENABLED | model=%s | top_n=%d",
                settings.RAG_RERANK_MODEL, settings.RAG_RERANK_TOP_N,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "RAG reranker init failed (%s) — falling back to hybrid retrieval.",
                exc,
            )
            reranker = None
    else:
        logger.info("RAG cross-encoder reranking DISABLED by configuration.")

    # --- Phase 7.4: Evidence diversity filter ---
    # Wrap the reranked output so the final Top-K spreads across documents
    # instead of clustering on near-duplicate/neighbouring chunks. Falls back
    # to the reranked pipeline on any construction error.
    diversity_filter = None
    if settings.RAG_DIVERSITY_ENABLED:
        try:
            diversity_filter = EvidenceDiversityFilter(
                similarity_threshold=settings.RAG_SIMILARITY_THRESHOLD,
                max_chunks_per_document=settings.RAG_MAX_CHUNKS_PER_DOCUMENT,
            )
            rag_retrieval = EvidenceDiversityPipeline(
                inner_pipeline=rag_retrieval,
                diversity_filter=diversity_filter,
            )
            logger.info(
                "RAG evidence diversity ENABLED | similarity_threshold=%.2f | "
                "max_chunks_per_document=%d",
                settings.RAG_SIMILARITY_THRESHOLD, settings.RAG_MAX_CHUNKS_PER_DOCUMENT,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "RAG diversity filter init failed (%s) — falling back to reranked retrieval.",
                exc,
            )
            diversity_filter = None
    else:
        logger.info("RAG evidence diversity DISABLED by configuration.")

    # --- Phase C6: Advanced Retrieval Engine ---
    # Outermost wrapper: entity extraction (from event.metadata) + metadata-
    # aware filtering (with automatic relaxation) + business-relevance
    # ranking. Wraps whatever the fully-composed pipeline above was (dense /
    # hybrid / multi-query / reranked / diversified) unchanged. Falls back to
    # the prior stage on any construction error, same as every Phase 7 stage.
    entity_extractor = None
    relevance_scorer = None
    if settings.RAG_ADVANCED_RETRIEVAL_ENABLED:
        try:
            entity_extractor = IncidentEntityExtractor()
            # Phase D4 Enterprise Configuration Engine: only override
            # recency_window_days when explicitly configured, so the
            # scorer's own module-default kwarg value is preserved
            # otherwise (that param, unlike the other four, is not itself
            # Optional -- pre-existing signature from Phase C6).
            _relevance_scorer_kwargs = {
                "entity_bonus_per_match": settings.RETRIEVAL_ENTITY_BONUS_PER_MATCH,
                "max_entity_bonus": settings.RETRIEVAL_MAX_ENTITY_BONUS,
                "doc_type_bonus": settings.RETRIEVAL_DOC_TYPE_BONUS,
                "recency_bonus": settings.RETRIEVAL_RECENCY_BONUS,
            }
            if settings.RETRIEVAL_RECENCY_WINDOW_DAYS is not None:
                _relevance_scorer_kwargs["recency_window_days"] = settings.RETRIEVAL_RECENCY_WINDOW_DAYS
            relevance_scorer = BusinessRelevanceScorer(**_relevance_scorer_kwargs)
            rag_retrieval = AdvancedRetrievalPipeline(
                inner_pipeline=rag_retrieval,
                relevance_scorer=relevance_scorer,
            )
            logger.info(
                "RAG advanced retrieval (entity extraction + metadata-aware "
                "filtering + business-relevance ranking) ENABLED"
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "RAG advanced retrieval init failed (%s) — falling back to prior retrieval stage.",
                exc,
            )
            entity_extractor = None
            relevance_scorer = None
    else:
        logger.info("RAG advanced retrieval DISABLED by configuration.")

    # --- Retrieval explainability: developer-only debug tracer ---
    # Built from the same real, shared component references collected above.
    # Does not alter retrieval behaviour — read-only introspection, exposed
    # via GET /api/v1/debug/retrieval (disabled outside development/staging).
    container.rag_debug_tracer = RetrievalDebugTracer(
        dense=retrieval_pipeline,
        bm25_index=bm25_index,
        hybrid_stage=hybrid_stage,
        query_expander=query_expander,
        reranker=reranker,
        diversity_filter=diversity_filter,
        rerank_top_n=settings.RAG_RERANK_TOP_N,
        entity_extractor=entity_extractor,
        relevance_scorer=relevance_scorer,
    )
    logger.info("Retrieval debug tracer initialised.")

    validator = RAGResponseValidator()
    rag_agent = RAGAgent(
        retrieval_pipeline=rag_retrieval,
        validator=validator,
        llm_service=llm_service,
        entity_extractor=entity_extractor,
    )

    report_agent = ReportAgent(settings=settings)

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

    # --- Dataset KPI source + activation + composition (Phase B1.5.3) ---
    # Reuses container.blob_store (B1.1) and container.db (via the existing
    # DatasetRepository/VersionRepository) — no new infrastructure clients.
    # DatasetKPISource never modifies MonitorAgent/RuleEngine/ForecastAgent; it
    # only satisfies the KPIRowSource protocol those already depend on.
    dataset_repo = DatasetRepository(container.db)
    version_repo = VersionRepository(container.db)
    dataset_intelligence = DatasetIntelligenceService(
        dataset_repo=dataset_repo, schema_repo=SchemaRepository(container.db),
    )
    dataset_kpi_source = DatasetKPISource(
        blob_store=container.blob_store,
        dataset_repo=dataset_repo,
        version_repo=version_repo,
        intelligence=dataset_intelligence,
    )
    # Explicit, never-automatic activation: only activated dataset ids become
    # live KPI feeds. RedisDatasetActivation (Enterprise Data Center) is
    # mutable at runtime via POST /api/v1/data-center/datasets/{id}/activate|
    # deactivate — StaticDatasetActivation, kept unmodified, cannot support
    # that. ACTIVATED_DATASET_IDS still seeds the initial state on first boot
    # (only if the Redis key doesn't already exist), so existing config-based
    # deployments keep working unchanged.
    dataset_activation = RedisDatasetActivation(
        container.redis, seed=parse_activated_dataset_ids(settings.ACTIVATED_DATASET_IDS)
    )
    logger.info(
        "Dataset monitoring activation | activated_count=%d",
        len(dataset_activation.list_activated_dataset_ids()),
    )
    # CompositeKPISource: Sheets keeps its exact current pass-through
    # behaviour (zero regression); activated datasets are queried once per
    # activated id, re-evaluated every cycle. MonitorAgent receives this one
    # object and is unaware either member exists.
    #
    # container.sheets_connector may be None (no Google Sheets credentials
    # configured) — only add it as a member when real, so a Sheets-less
    # deployment's composite still has zero members and MonitorAgent's own
    # `if not rows: return` no-op path behaves exactly as it did when
    # kpi_source was a bare None reference (see MonitorAgent._run_cycle).
    composite_kpi_source = CompositeKPISource()
    if container.sheets_connector is not None:
        composite_kpi_source.add_passthrough(container.sheets_connector)
    composite_kpi_source.add_multi(dataset_kpi_source, dataset_activation.list_activated_dataset_ids)

    # --- Enterprise Connector Framework (Phase F7) ---
    # Constructed here, before MonitorAgent, because a METRICS connector joins
    # the SAME CompositeKPISource the Sheets connector and activated datasets
    # already feed. MonitorAgent receives one object satisfying the existing
    # KPIRowSource protocol and never learns a connector exists — no agent
    # change, no second KPI path, no second detector (ENG-6).
    #
    # With CONNECTORS_ENABLED false (the default) the registry reports zero
    # enabled kinds, no member is added, and this is byte-identically the
    # pre-F7 composition: upload + Sheets, unchanged (COMPAT-4).
    # SEC-5: the ONE credential path every connector has. Held on the container
    # so eight connectors share one resolver rather than each constructing its
    # own — and so a connector has no way to read a credential except through it.
    if getattr(container, "secret_manager", None) is None:
        container.secret_manager = SecretManager(
            settings=settings, project_id=getattr(settings, "GCP_PROJECT", None)
        )
    connector_registry = ConnectorRegistry(
        settings=settings, secret_manager=container.secret_manager,
    )
    container.connector_registry = connector_registry

    # The ONE ingestion entry point, shared with the upload API. The sync
    # engine is given this object and has no other way to put content into the
    # platform, so connector documents cannot travel a path of their own.
    ingestion_submitter = IngestionSubmitter(db=container.db, blob_store=container.blob_store)
    container.ingestion_submitter = ingestion_submitter
    container.connector_sync = ConnectorSyncEngine(
        db=container.db,
        submitter=ingestion_submitter,
        registry=connector_registry,
        max_artifacts=settings.CONNECTOR_SYNC_MAX_ARTIFACTS,
    )
    container.connector_health = ConnectorHealthReporter(
        db=container.db,
        registry=connector_registry,
        stale_after_seconds=settings.CONNECTOR_STALE_AFTER_SECONDS,
    )

    # Metrics connectors as ordinary CompositeKPISource members. Each is added
    # in PASS-THROUGH mode with its own configured selector, so MonitorAgent's
    # incoming selector (a sheet name by origin, meaningless to a warehouse) is
    # replaced by the report/table the connector can actually answer.
    #
    # A connector that failed to authenticate is still added: its fetch_rows
    # returns an empty list and never raises, which is the same no-op
    # MonitorAgent already handles for a source with no data — so one broken
    # connector cannot stop KPI collection for the others.
    _metric_connectors: list[Any] = []
    if connector_registry.enabled_kinds():
        try:
            _metric_connectors = connector_registry.build_metric_sources(
                SourceRepository(container.db).list_all()
            )
        except (NameError, AttributeError, ImportError, TypeError):
            # Hardening: these four are never an upstream/tenant failure —
            # they are bugs in this composition root. Letting the broad
            # handler below absorb them is what allowed the missing
            # SourceRepository import to silently disable every metrics
            # connector for an entire release. Re-raised so a wiring bug is
            # a loud startup failure, which is the only way it gets fixed.
            raise
        except Exception as exc:  # noqa: BLE001
            # A genuine upstream failure (unreachable warehouse, bad
            # credential, malformed source row) must never block startup:
            # the connector reports zero rows and its health surface says
            # why, exactly as a connector that fails mid-sync does.
            logger.error("Connector metric composition failed (continuing): %s", exc)
            _metric_connectors = []
    for _connector in _metric_connectors:
        composite_kpi_source.add_multi(
            _connector, lambda c=_connector: [c.default_selector()] if c.default_selector() else []
        )
    container.metric_connectors = _metric_connectors
    logger.info(
        "Connector framework | enabled_kinds=%s | mock_mode=%s | metric_members=%d",
        connector_registry.enabled_kinds(), connector_registry.mock_mode,
        len(_metric_connectors),
    )
    container.dataset_kpi_source = dataset_kpi_source
    container.dataset_activation = dataset_activation
    container.kpi_source = composite_kpi_source

    # CompositeRuleEngine (Phase B1.7): wraps the real, unmodified RuleEngine
    # with a dynamic domain provider so activated dataset metrics actually
    # enter MonitorAgent's monitored domain set. evaluate() is a pure
    # passthrough to the base engine (curated domains: byte-identical
    # behaviour); only loaded_domains is widened, re-evaluated every cycle so
    # activation changes apply without a restart. Reuses the SAME
    # dataset_activation instance already driving CompositeKPISource above —
    # one activation list, in lockstep, for both what is fetched and what is
    # monitored.
    # Phase F3 (SEC-7, AGENT-5, MOD-6): adopted compiled-rule overrides are
    # read ONCE here, at construction, and merged into the base RuleEngine's
    # config — the same "restart-applied configuration" trade-off Phase D4's
    # Enterprise Configuration Engine already documents, reused rather than
    # introducing a second one. A fresh deployment (zero approved rules) —
    # or F3 simply never used — produces an empty overrides dict, which
    # RuleEngine treats as byte-identical to no overrides at all: the F3
    # acceptance criterion that the deterministic decision path is
    # unchanged for any policy that has not been adopted as a rule.
    policy_agent = PolicyAgent(
        policy_repository=PolicyRepository(container.db),
        compiled_rule_repository=CompiledRuleRepository(container.db),
    )
    try:
        rule_overrides = policy_agent.active_overrides()
    except Exception as exc:  # noqa: BLE001
        # A read failure here must never block startup — the platform has
        # simply not yet adopted any compiled rule, which is the same
        # posture as a fresh deployment.
        logger.warning("PolicyAgent.active_overrides() failed at startup: %s", exc)
        rule_overrides = {}
    if rule_overrides:
        logger.info("Adopted compiled-rule overrides loaded | %s", rule_overrides)
    container.policy_agent = policy_agent

    composite_rule_engine = CompositeRuleEngine(base=RuleEngine(overrides=rule_overrides))
    composite_rule_engine.add_domain_provider(
        "datasets",
        lambda: dataset_intelligence.list_monitorable_metric_names(
            dataset_activation.list_activated_dataset_ids()
        ),
    )
    container.rule_engine = composite_rule_engine

    # --- Monitor Agent (Phase 2; gating made honest in Phase E7) ---
    # Phase E7 (SEC-8, PHIL-1): ENABLE_MONITOR_AGENT is now the SOLE gate —
    # no environment backdoor. Before this phase, the condition was
    # `ENABLE_MONITOR_AGENT or ENVIRONMENT != "production"`, which meant
    # (a) production could NEVER run the autonomous loop without the flag
    # explicitly set (audit gate #4 — the exact "shipped production config
    # disables MonitorAgent" finding), while (b) every non-production
    # environment ran it unconditionally regardless of the flag. Both
    # directions were dishonest gating. The flag alone decides now, in
    # every environment; deployment artifacts (docker-compose.yml,
    # deploy/cloudrun.yaml) set it explicitly per posture — see
    # docs/autonomous_operations.md for the full matrix.
    monitor_agent = None
    if settings.ENABLE_MONITOR_AGENT:
        logger.info("Creating MonitorAgent …")
        monitor_agent = MonitorAgent(
            event_bus=container.event_bus,
            queue=container.queue,
            deduplicator=container.deduplicator,
            rule_engine=composite_rule_engine,  # Phase B1.7: curated + dynamic dataset domains, composed
            statistical_detector=StatisticalDetector(window_size=7),
            forecast_agent=forecast_agent,          # <-- Pass the properly initialized forecast_agent
            pipeline=container.pipeline,
            settings=settings,
            kpi_source=composite_kpi_source,  # Phase B1.5.3: Sheets + activated datasets, composed
            long_term_memory=long_term_memory,      # Hardening: persist observations for forecast training
        )
        # Start the monitor agent in a background thread
        monitor_thread = threading.Thread(target=monitor_agent.start, daemon=True)
        monitor_thread.start()
        logger.info("MonitorAgent started in background thread.")
    else:
        logger.info(
            "MonitorAgent disabled by configuration (ENABLE_MONITOR_AGENT=false)."
        )
    # Phase E7: exposed on the container so GET /health can report
    # "disabled" vs "supervised" honestly (None means never constructed).
    container.monitor_agent = monitor_agent

    # --- Ingestion Worker (Phases B1.3 + B1.4) ---
    # Drains the ingestion_jobs queue created by POST /api/v1/ingest/upload.
    # A RoutingJobProcessor dispatches each job by parent type:
    #   document -> DocumentIngestJobProcessor (B1.3): extract text -> reuse the
    #     RAG IngestionPipeline built above (chunk/embed/index into Qdrant) ->
    #     finalise Document/Version rows. Startup embedding model + Qdrant reused.
    #   dataset  -> DatasetIngestJobProcessor (B1.4): read the tabular file ->
    #     infer schema (columns/types/metrics) -> register Schema + finalise the
    #     Dataset/Version rows. No Qdrant.
    ingestion_job_repo = IngestionJobRepository(container.db)
    # Phase C2 — Policy Intelligence Engine: reuses the SAME llm_service
    # already constructed above for DecisionEngine/RAGAgent (no second LLM
    # client). Runs as an additional step inside the existing document
    # ingestion job — see DocumentIngestJobProcessor — not a second pipeline.
    policy_extractor = PolicyExtractor(llm_service=llm_service)
    document_processor = DocumentIngestJobProcessor(
        blob_store=container.blob_store,
        ingestion_pipeline=ingestion_pipeline,
        db=container.db,
        policy_extractor=policy_extractor,
        policy_extraction_enabled=settings.POLICY_EXTRACTION_ENABLED,
        # Phase E6: reuse the SAME embedding model already loaded above so
        # each policy's embedding is computed once, at extraction time.
        embedding_service=embedding_service,
        # Phase E7 (RAG-6): reuse the SAME bm25_index + qdrant_client
        # already constructed above so a document ingested at runtime
        # refreshes the lexical index in place — no restart required.
        # bm25_index is None when hybrid retrieval is disabled, in which
        # case the processor's refresh step is simply a no-op.
        bm25_index=bm25_index,
        qdrant_client=qdrant_client,
    )
    dataset_processor = DatasetIngestJobProcessor(
        blob_store=container.blob_store,
        db=container.db,
    )
    ingestion_processor = RoutingJobProcessor(
        document_processor=document_processor,
        dataset_processor=dataset_processor,
    )
    ingestion_worker = IngestionWorker(
        job_repo=ingestion_job_repo,
        processor=ingestion_processor,
        poll_interval=settings.INGEST_WORKER_POLL_SECONDS,
    )
    ingestion_worker_thread = threading.Thread(target=ingestion_worker.start, daemon=True)
    ingestion_worker_thread.start()
    container.ingestion_worker = ingestion_worker
    logger.info("IngestionWorker started in background thread.")

    # --- Enterprise Policy Registry (Phase C3) ---
    # Reuses the existing PolicyRepository (Phase C2 table, no new schema),
    # a fresh plain RuleEngine() for its curated-domain vocabulary only (the
    # SAME "cheap, side-effect-free, read-only" pattern
    # aeam/api/data_center.py's dataset-profile endpoint already uses --
    # never CompositeRuleEngine, never .evaluate()), and the SAME shared
    # embedding_service the RAG pipeline/Enterprise Memory already use.
    policy_registry = PolicyRegistry(
        policy_repository=PolicyRepository(container.db),
        rule_engine=RuleEngine(),
        embedding_service=embedding_service,
        semantic_threshold=settings.POLICY_SIMILARITY_THRESHOLD,
    )

    # --- Cross-Dataset Intelligence (Phase C4) ---
    # Reuses the EXACT SAME dataset_activation/dataset_intelligence/
    # dataset_kpi_source instances MonitorAgent's own CompositeKPISource
    # already depends on (constructed above) -- no second dataset reader,
    # no second profiler, no second activation store. StatisticalDetector
    # is constructed fresh inside CrossDatasetAnalyzer with the SAME
    # window_size=7 MonitorAgent itself uses (same class, not a second
    # detector implementation).
    # Phase D4: correlation_threshold's own signature default (0.7, set in
    # Phase C4) is not Optional -- only override when explicitly configured.
    # --- Business Graph (Phase F4) ---
    # The store is constructed whenever a database exists, because the
    # read-only /api/v1/graph surface must be able to report an empty graph
    # honestly rather than 503 on a deployment that has simply never run a
    # build. What BUSINESS_GRAPH_ENABLED gates is narrower and more
    # consequential: whether the graph participates in an INVESTIGATION —
    # whether the Orchestrator appends a graph finding, and whether
    # CrossDatasetAnalyzer consults known relationships. Flag off, both are
    # None/absent and the investigation path is byte-identical to F3's.
    #
    # Nothing here builds the graph. Building is an explicit, privileged,
    # audited act via POST /api/v1/graph/build — no startup build, no
    # timer, no agent deciding on its own that the graph should change.
    business_graph_store = BusinessGraphStore(
        node_repo=GraphNodeRepository(container.db),
        edge_repo=GraphEdgeRepository(container.db),
    )
    container.business_graph_store = business_graph_store
    container.dataset_intelligence = dataset_intelligence

    graph_budget = TraversalBudget.clamped(
        max_depth=settings.GRAPH_MAX_DEPTH,
        max_nodes=settings.GRAPH_MAX_NODES,
        max_edges=settings.GRAPH_MAX_EDGES,
        min_confidence=settings.GRAPH_MIN_EDGE_CONFIDENCE,
    )
    business_graph_engine = (
        GraphCorrelationEngine(store=business_graph_store, budget=graph_budget)
        if settings.BUSINESS_GRAPH_ENABLED
        else None
    )
    logger.info(
        "Business graph | enabled=%s | budget=%s",
        settings.BUSINESS_GRAPH_ENABLED, graph_budget.as_dict(),
    )

    _cross_dataset_kwargs = {}
    if settings.CROSS_DATASET_CORRELATION_THRESHOLD is not None:
        _cross_dataset_kwargs["correlation_threshold"] = settings.CROSS_DATASET_CORRELATION_THRESHOLD
    # Phase F4: the graph store reaches C4 only when the flag is on. Passed
    # as None otherwise, which CrossDatasetAnalyzer treats as "no graph" —
    # the same code path, the same result keys, the same values as C4.
    if settings.BUSINESS_GRAPH_ENABLED:
        _cross_dataset_kwargs["graph_store"] = business_graph_store
    cross_dataset_analyzer = CrossDatasetAnalyzer(
        dataset_activation=dataset_activation,
        intelligence=dataset_intelligence,
        kpi_source=dataset_kpi_source,
        **_cross_dataset_kwargs,
    )

    # --- Adaptive Detection Engine (Phase C5) ---
    # Reuses the EXACT SAME long_term_memory instance MonitorAgent's
    # ForecastAgent already depends on for get_metric_history() -- no second
    # LTM instance, no new table, no new Qdrant collection. StatisticalDetector
    # is constructed fresh inside AdaptiveDetectionEngine with a longer
    # window_size=30 (same class as MonitorAgent's own window_size=7
    # instance -- a second perspective, not a second implementation).
    adaptive_detection_engine = AdaptiveDetectionEngine(
        long_term_memory=long_term_memory,
        min_baseline_points=settings.ADAPTIVE_MIN_BASELINE_POINTS,
        min_seasonality_points=settings.ADAPTIVE_MIN_SEASONALITY_POINTS,
        seasonality_strength_threshold=settings.ADAPTIVE_SEASONALITY_STRENGTH_THRESHOLD,
        adaptive_window=settings.ADAPTIVE_WINDOW_SIZE,
    )

    # --- Enterprise Action Planning Engine (Phase C7) ---
    # Zero external dependencies -- pure synthesis over the findings the
    # Orchestrator has already accumulated by finalize_incident() time. No
    # new retrieval, no new detector, no LLM call. Always constructed
    # (mirrors the C1/C3/C4/C5 "always on if wired" precedent -- these
    # phases have no dedicated settings flag either, since they add no new
    # infrastructure dependency that could fail to initialize).
    _approval_quality_levels = (
        tuple(s.strip() for s in settings.HUMAN_APPROVAL_QUALITY_LEVELS.split(",") if s.strip())
        if settings.HUMAN_APPROVAL_QUALITY_LEVELS
        else None
    )
    execution_planning_engine = ExecutionPlanningEngine(
        ambiguous_cause_gap=settings.EXECUTION_PLAN_AMBIGUOUS_CAUSE_GAP,
        conflict_confidence_cap=settings.EXECUTION_PLAN_CONFLICT_CONFIDENCE_CAP,
        approval_required_quality_levels=_approval_quality_levels,
    )

    # --- Planning Agent (Phase F6) ---
    # A PROMOTION BY COMPOSITION, not a rewrite. PlanningAgent wraps the
    # EXACT SAME engine constructed above and returns its result unmodified,
    # so the Orchestrator's planning stage — which just calls `.plan(...)` on
    # whatever it was given — needs no change at all, and the
    # `execution_plan` finding it appends is identical field for field
    # (COMPAT-1). What the wrapper adds is standing: a roster entry, its own
    # heartbeat, its own agent_execution_time label, and its own span.
    #
    # Flag-off passes the bare engine, which is byte-identically the
    # pre-F6 path — the documented F6 rollback.
    planning_target: Any = execution_planning_engine
    planning_agent: PlanningAgent | None = None
    if settings.PLANNING_AGENT_ENABLED:
        planning_agent = PlanningAgent(engine=execution_planning_engine)
        planning_target = planning_agent
    container.planning_agent = planning_agent
    logger.info(
        "Planning agent | enabled=%s | planner=%s",
        settings.PLANNING_AGENT_ENABLED, type(planning_target).__name__,
    )

    # --- Human-in-the-Loop Enforcement (Phase E9, AGENT-5) ---
    # The single service both the Orchestrator (which records what it
    # withheld) and the review API (which releases it) use, so the two can
    # never disagree about what "approved" means. It reuses the EXACT SAME
    # action_agent instance the Orchestrator holds — an approved action runs
    # through the unchanged ActionAgent, not a second executor — and the
    # existing repository pattern over container.db. Always constructed:
    # whether the gate actually withholds anything is decided by
    # settings.HUMAN_APPROVAL_ENFORCED, read live via the service's
    # `enforced` property, not by whether this object exists.
    human_review_service = HumanReviewService(
        approval_repo=IncidentApprovalRepository(container.db),
        verdict_repo=ReviewVerdictRepository(container.db),
        settings=settings,
        action_agent=action_agent,
    )
    container.human_review_service = human_review_service
    logger.info(
        "Human review service | enforced=%s | default_chain=%s",
        human_review_service.enforced, settings.APPROVAL_TIER_CHAIN,
    )

    # --- Enterprise Explainability Engine (Phase D1) ---
    # Zero external dependencies -- pure synthesis over findings AND the
    # execution plan ExecutionPlanningEngine already produced. No new
    # retrieval, no new detector, no LLM call, no second planner. Always
    # constructed (same "always on if wired" precedent as C7).
    explainability_engine = ExplainabilityEngine()

    # --- Enterprise AI Evaluation & Quality Engine (Phase D2) ---
    # Zero external dependencies -- scores investigation quality from
    # findings, execution_plan, and explainability, never changing any of
    # them. No new retrieval, no new detector, no LLM call, no second
    # planner. Always constructed (same "always on if wired" precedent).
    ai_evaluation_engine = AIEvaluationEngine(
        strength_threshold=settings.AI_EVAL_STRENGTH_THRESHOLD,
        weakness_threshold=settings.AI_EVAL_WEAKNESS_THRESHOLD,
        conflict_penalty_weight=settings.AI_EVAL_CONFLICT_PENALTY_WEIGHT,
        memory_mixed_outcome_penalty=settings.AI_EVAL_MEMORY_MIXED_OUTCOME_PENALTY,
    )

    # --- Orchestrator ---
    # Phase E2 (ARCH-8): STM and FSM are per-incident inside the Orchestrator;
    # this call no longer passes shared singletons for them (see the design
    # note above and aeam/agents/orchestrator/incident_context.py).
    orchestrator = Orchestrator(
        event_bus=container.event_bus,
        decision_engine=decision_engine,
        evaluation_engine=evaluation_engine,
        long_term_memory=long_term_memory,
        settings=settings,
        rag_agent=rag_agent,
        action_agent=action_agent,
        report_agent=report_agent,
        memory_engine=enterprise_memory,
        policy_registry=policy_registry,
        cross_dataset_analyzer=cross_dataset_analyzer,
        adaptive_detection_engine=adaptive_detection_engine,
        # Phase F6: the PlanningAgent when enabled, else the bare C7 engine.
        # Both satisfy the same `.plan(...)` contract, which is what makes the
        # promotion a drop-in rather than an orchestrator change.
        execution_planning_engine=planning_target,
        explainability_engine=explainability_engine,
        ai_evaluation_engine=ai_evaluation_engine,
        human_review_service=human_review_service,
        business_graph_engine=business_graph_engine,
    )

    # Register wildcard handler
    container.event_bus.register_handler("ALL", orchestrator.handle_event)

    # Agent roster (Phase E1, DOC-2): the agents ACTUALLY constructed this
    # startup — monitor and action are conditional on configuration. Read by
    # GET /api/v1/system/status instead of a hardcoded count, so the figure
    # can never drift from the wiring above.
    # Phase F1: "kpi" is listed only when the KPI Agent was actually
    # constructed. The Orchestrator builds it itself from settings (the
    # placeholder it replaces was unconditional), so the roster reads back
    # what exists rather than asserting it.
    # Phase F6: "planning" and "supervisor" join the roster on the same
    # terms as every other entry — listed only when the object was actually
    # constructed, so the roster keeps reading back what exists.
    container.agent_roster = sorted(
        ["orchestrator", "rag", "forecast", "report"]
        + (["monitor"] if monitor_agent is not None else [])
        + (["action"] if action_agent is not None else [])
        + (["kpi"] if getattr(orchestrator, "_kpi_agent", None) is not None else [])
        + (["planning"] if planning_agent is not None else [])
        + (["supervisor"] if settings.SUPERVISOR_AGENT_ENABLED else [])
    )
    logger.info("Agent roster | %s", container.agent_roster)

    # --- Supervisor Agent (Phase F6, ARCH-1) ---
    # Constructed LAST and deliberately given only two read-only providers:
    # a roster reader and an observability reader. It receives no
    # Orchestrator, no ActionAgent, no PlanningAgent, and no EventBus — so
    # it has nothing to coordinate WITH. The single-coordinator invariant is
    # preserved by what this object cannot reach, not by what it declines to
    # do.
    #
    # The observability provider reuses the SAME ObservabilityEngine the
    # /api/v1/observability endpoint uses, over the same bounded read (E6),
    # so the Supervisor's participation figures and the Analytics page's are
    # the same numbers (ENG-6). A failure here yields None, and the report
    # says which components it therefore could not compute.
    supervisor_agent: SupervisorAgent | None = None
    if settings.SUPERVISOR_AGENT_ENABLED:
        _observability_engine = ObservabilityEngine(
            trend_window=settings.OBSERVABILITY_TREND_WINDOW
        ) if getattr(settings, "OBSERVABILITY_TREND_WINDOW", None) is not None else ObservabilityEngine()
        _mesh_window = int(getattr(settings, "OBSERVABILITY_RETENTION_LIMIT", None) or 500)

        def _mesh_observability_summary() -> dict[str, Any] | None:
            """Bounded, read-only observability summary for the Supervisor."""
            try:
                rows = container.db.fetch_all(
                    "SELECT * FROM incidents ORDER BY timestamp DESC LIMIT :limit",
                    {"limit": _mesh_window},
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("supervisor | incident read failed: %s", exc)
                return None
            incidents: list[dict[str, Any]] = []
            for row in rows:
                incident = dict(row)
                findings = incident.get("findings")
                if isinstance(findings, str):
                    try:
                        incident["findings"] = json.loads(findings) if findings else []
                    except (json.JSONDecodeError, TypeError):
                        incident["findings"] = []
                incidents.append(incident)
            try:
                return _observability_engine.summarize(incidents)
            except Exception as exc:  # noqa: BLE001
                logger.warning("supervisor | observability summarize failed: %s", exc)
                return None

        supervisor_agent = SupervisorAgent(
            settings=settings,
            roster_provider=lambda: list(getattr(container, "agent_roster", []) or []),
            observability_provider=_mesh_observability_summary,
        )
    container.supervisor_agent = supervisor_agent
    logger.info("Supervisor agent | enabled=%s", settings.SUPERVISOR_AGENT_ENABLED)

    logger.info("Orchestrator registered with EventBus (ALL wildcard).")
    logger.info("Infrastructure container ready | %r", container)

    # Connectivity probes — warn but do not abort; let the health endpoint
    # surface degraded state so orchestrators can take action.
    if container.redis.ping():
        logger.info("Redis connectivity: OK")
    else:
        logger.warning("Redis connectivity: DEGRADED — ping failed.")

    # Scheduler disposition (Phase E1 removal, Phase E7 completion, ENG-8):
    # the APScheduler stub that previously lived here (constructed, never
    # started, publishing a SYNTHETIC hardcoded SALES_DROP event) was
    # removed in E1. Autonomous detection is MonitorAgent's own polling
    # loop above — no separate scheduler process is needed or wanted. Its
    # production enablement (honest, flag-only gating; no environment
    # backdoor) landed in Phase E7. A synthetic-event generator would
    # violate PHIL-1 (honesty over capability) if ever reintroduced.

    logger.info("AEAM startup complete.")
    yield

    # --- Shutdown ---
    logger.info("AEAM shutting down …")
    if monitor_agent:
        # If MonitorAgent has a stop() method, call it; otherwise, we rely on daemon thread.
        # Here we just log.
        logger.info("MonitorAgent will be terminated by daemon thread exit.")
    if getattr(container, "ingestion_worker", None) is not None:
        container.ingestion_worker.stop()
        logger.info("IngestionWorker stop signalled.")
    container.db.dispose()
    container.redis.close()
    # Hardening: the RateLimiter's RedisClient is constructed in create_app()
    # (it must exist before the container does, because middleware is
    # registered there) and was never closed — one connection pool leaked per
    # process lifetime. It is now closed alongside the container's.
    middleware_redis = getattr(app.state, "middleware_redis", None)
    if middleware_redis is not None:
        try:
            middleware_redis.close()
            logger.info("Security-middleware RedisClient closed.")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Security-middleware RedisClient close failed: %s", exc)
    logger.info("AEAM shutdown complete.")


# ---------------------------------------------------------------------------
# Phase E3 — JWT key material resolution (SEC-1/SEC-4)
# ---------------------------------------------------------------------------
#
# The RS256 public key the JWTAuth verifier uses is resolved through
# SecretManager -- the very component the audit found had never been
# used. Precedence:
#
#   1. env / settings JWT_PUBLIC_KEY   (PEM literal)
#   2. env / settings JWT_PUBLIC_KEY_PATH (filesystem path to a PEM file)
#   3. fail (non-development)  |  well-known placeholder (development)
#
# The "fail" case is SEC-4's fail-closed contract: refuse to bring the
# platform up in an environment where anyone could authenticate, rather
# than silently accepting the placeholder in production. Development
# keeps the pre-E3 placeholder so local dev behaves exactly as before
# (COMPAT-1).

_DEV_PLACEHOLDER_KEY: str = "dummy-public-key"


def _build_jwt_auth(settings: Settings) -> JWTAuth:
    """Resolve JWT key material and construct JWTAuth.

    Phase E13 adds a preceding branch: when ``OIDC_ENABLED`` is set, key
    material comes from the enterprise IdP's JWKS document instead of a
    static PEM, and the expected issuer/audience default to the IdP's
    issuer and the registered client id. Everything downstream of the
    verifier -- RBAC, rate limiting, audit -- is untouched by the switch.

    Fails closed in non-development environments if no real key material
    is configured, and fails closed in *every* environment if OIDC is
    enabled but incompletely configured (a half-configured federation must
    never silently degrade to a weaker posture). In development without
    OIDC, falls back to the well-known placeholder with a loud WARNING so
    nobody deploys it by accident.
    """
    secret_manager = SecretManager(settings=settings)
    environment = (settings.ENVIRONMENT or "").strip().lower()

    # 0. Phase E13 — enterprise SSO. Checked first because when it is on it
    #    is the whole answer: the static PEM plays no part.
    if bool(getattr(settings, "OIDC_ENABLED", False)):
        return _build_oidc_jwt_auth(settings)

    # 1. PEM literal via SecretManager (env-first, settings-fallback).
    pem: str = str(secret_manager.get_secret("JWT_PUBLIC_KEY", default="") or "").strip()

    # 2. Filesystem path fallback.
    if not pem:
        path: str = str(secret_manager.get_secret("JWT_PUBLIC_KEY_PATH", default="") or "").strip()
        if path:
            try:
                pem = Path(path).read_text(encoding="utf-8").strip()
                logger.info("JWT public key loaded from JWT_PUBLIC_KEY_PATH=%s", path)
            except OSError as exc:
                # In non-development, refusing to start is safer than
                # falling through to the placeholder.
                if environment != "development":
                    raise RuntimeError(
                        f"JWT_PUBLIC_KEY_PATH={path!r} could not be read: {exc}. "
                        "Startup aborted (Phase E3, SEC-4)."
                    ) from exc
                logger.warning(
                    "JWT_PUBLIC_KEY_PATH=%s unreadable (%s); development "
                    "environment will use the placeholder key.", path, exc,
                )

    # 3. Fail-closed / dev placeholder.
    if not pem:
        if environment != "development":
            raise RuntimeError(
                "No JWT public key configured. Set JWT_PUBLIC_KEY (PEM "
                "literal) or JWT_PUBLIC_KEY_PATH (file path) via the "
                "environment or Settings. Startup aborted (Phase E3, SEC-4)."
            )
        logger.warning(
            "JWT public key not configured; ENVIRONMENT=development is using "
            "the placeholder key %r. This MUST NOT reach staging/production.",
            _DEV_PLACEHOLDER_KEY,
        )
        pem = _DEV_PLACEHOLDER_KEY

    return JWTAuth(
        public_key=pem,
        issuer=settings.JWT_ISSUER,
        audience=settings.JWT_AUDIENCE,
    )


def _build_oidc_jwt_auth(settings: Settings) -> JWTAuth:
    """Construct the JWKS-backed verifier for an enterprise SSO deployment.

    Fail-closed contract (SEC-4), applied in every environment including
    development: enabling federation without the issuer, client id, or a
    resolvable JWKS endpoint aborts startup. The alternative -- quietly
    falling back to the static-key or placeholder path -- would mean an
    operator who believes SSO is enforcing identity is running a posture
    that is not.

    The JWKS URL is taken from OIDC_JWKS_URL when pinned, otherwise from
    the IdP's discovery document. Discovery happens once, here at startup,
    so a misconfigured issuer is a loud startup failure rather than a
    mysterious 401 on the first sign-in.

    Raises:
        RuntimeError: If OIDC is enabled but issuer/client id are missing,
                      or the JWKS endpoint cannot be determined.
    """
    issuer = str(settings.OIDC_ISSUER or "").strip()
    client_id = str(settings.OIDC_CLIENT_ID or "").strip()

    missing = [
        name
        for name, value in (("OIDC_ISSUER", issuer), ("OIDC_CLIENT_ID", client_id))
        if not value
    ]
    if missing:
        raise RuntimeError(
            f"OIDC_ENABLED is true but {', '.join(missing)} "
            f"{'is' if len(missing) == 1 else 'are'} not configured. "
            "Startup aborted (Phase E13, SEC-4)."
        )

    try:
        endpoints = resolve_oidc_endpoints(settings)
    except Exception as exc:  # noqa: BLE001
        # resolve_oidc_endpoints raises HTTPException(502) for an
        # unreachable IdP -- correct inside a request, meaningless at
        # startup, so it is restated as the fail-closed abort it is here.
        raise RuntimeError(
            f"OIDC discovery failed for issuer {issuer!r}: {exc}. "
            "Startup aborted (Phase E13, SEC-4)."
        ) from exc

    jwks_uri = endpoints["jwks_uri"]
    if not jwks_uri:
        raise RuntimeError(
            f"OIDC issuer {issuer!r} published no jwks_uri and OIDC_JWKS_URL "
            "is not set; token signatures could not be verified. Startup "
            "aborted (Phase E13, SEC-4)."
        )

    algorithms = [
        part.strip()
        for part in str(settings.OIDC_ALGORITHMS or "").split(",")
        if part.strip()
    ]

    logger.info(
        "Enterprise SSO enabled | issuer=%s | client_id=%s | jwks_uri=%s",
        issuer, client_id, jwks_uri,
    )

    # JWT_ISSUER / JWT_AUDIENCE keep their ENG-6 override role: unset means
    # "expect what the IdP configuration implies" (the issuer itself, and
    # the client id as audience), which is the correct OIDC default.
    return JWTAuth(
        public_key="",
        issuer=settings.JWT_ISSUER or issuer,
        audience=settings.JWT_AUDIENCE or client_id,
        jwks_url=jwks_uri,
        algorithms=algorithms or None,
    )


# ---------------------------------------------------------------------------
# Phase E7 — GET /health payload (OBS-3/4, RAG-6)
# ---------------------------------------------------------------------------


def build_health_payload(container: "AppContainer") -> dict:
    """
    Build the ``GET /health`` response body.

    Extracted to a pure function (Phase E7) so it is directly unit-testable
    against a stub container — no live DB/Redis/Qdrant required — while the
    real route (below, in ``create_app``) calls this exact function, so
    there is exactly one implementation of the health-check logic.

    Args:
        container: The application's :class:`AppContainer` (or any object
                   exposing the same attributes — a test stub is fine).

    Returns:
        The full ``{"status": ..., "checks": {...}}`` dict. Callers decide
        the HTTP status code from ``result["status"]``.
    """
    status = {
        "status": "healthy",
        "checks": {
            "database": "unknown",
            "redis": "unknown",
            "queue": "unknown",
            # Phase E7 (OBS-3/4): supervision for the two autonomous
            # background workers, and freshness disclosure for the
            # lexical retrieval index (RAG-6).
            "monitor_agent": "unknown",
            "ingestion_worker": "unknown",
            "bm25_index": "unknown",
            # Hardening: Qdrant and the LLM were the two dependencies the
            # platform actually depends on for RAG and never reported, so the
            # console's StatusBar rendered a permanent "n/a" for both while
            # every other dependency showed a real light. An unreachable
            # Qdrant meant retrieval silently returned nothing; an expired LLM
            # key meant every investigation failed — and /health said
            # "healthy" through both. They are reported here, but
            # INFORMATIONALLY: both degrade investigation QUALITY, not
            # platform availability, so neither flips overall status (the same
            # contract bm25_index already follows).
            "qdrant": "unknown",
            "llm": "unknown",
        }
    }
    # Check database.
    #
    # Hardening: this check previously assigned "ok" inside a try whose body
    # was a dict assignment — it could not raise, so the handler was
    # unreachable and the value unconditional. A fully unreachable database
    # still reported "healthy", which defeated the orchestrator restart /
    # de-route decisions this endpoint exists to drive, and the console
    # StatusBar rendered the same false green. It now issues the cheapest
    # possible real round-trip through the EXISTING pooled client (no new
    # connection, no new client) so the answer is measured.
    db = getattr(container, "db", None)
    if db is None:
        status["status"] = "degraded"
        status["checks"]["database"] = "error: no database client is configured"
    else:
        try:
            db.fetch_one("SELECT 1 AS ok")
            status["checks"]["database"] = "ok"
        except Exception as e:  # noqa: BLE001
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

    # Phase E7: MonitorAgent / IngestionWorker supervision via
    # heartbeat age. A stale heartbeat means the thread stopped
    # updating it — either it died, or it is wedged — and DOES flip
    # overall status to "degraded", closing the "a dead thread is
    # discovered, not detected" audit gap. "disabled" (flag off) is
    # reported honestly and never counted against overall health.
    stale_after = container.settings.HEARTBEAT_STALE_SECONDS
    # Hardening: MonitorAgent beats ONCE PER CYCLE, so its heartbeat is
    # legitimately as old as one full interval. With the shipped defaults
    # (HEARTBEAT_STALE_SECONDS=120, MONITOR_INTERVAL_SECONDS=300) the raw
    # threshold declared a perfectly healthy agent stale for ~60% of every
    # cycle — and docker-compose.yml defaults ENABLE_MONITOR_AGENT=true, so
    # `docker compose up` produced a permanently 503 platform (a restart
    # loop wherever /health is a liveness probe). The interval is therefore
    # a FLOOR on this agent's threshold: the configured value still wins
    # whenever it is already generous enough, so an operator who tuned it
    # up keeps their value (COMPAT-1). The IngestionWorker below is
    # deliberately left on the raw setting — it polls every 2s, so the
    # configured threshold is already generous for it.
    monitor_interval = float(getattr(container.settings, "MONITOR_INTERVAL_SECONDS", 0) or 0)
    monitor_stale_after = max(float(stale_after), monitor_interval * 2.0 + 30.0)
    if getattr(container, "monitor_agent", None) is None:
        status["checks"]["monitor_agent"] = "disabled (ENABLE_MONITOR_AGENT=false)"
    else:
        age = heartbeat_tracker.age_seconds("monitor")
        if age is None:
            status["checks"]["monitor_agent"] = "starting (no heartbeat yet)"
        elif age > monitor_stale_after:
            status["status"] = "degraded"
            status["checks"]["monitor_agent"] = f"stale (last heartbeat {age:.0f}s ago)"
        else:
            status["checks"]["monitor_agent"] = f"ok (last heartbeat {age:.0f}s ago)"

    if getattr(container, "ingestion_worker", None) is None:
        status["checks"]["ingestion_worker"] = "not started"
    else:
        age = heartbeat_tracker.age_seconds("ingestion")
        if age is None:
            status["checks"]["ingestion_worker"] = "starting (no heartbeat yet)"
        elif age > stale_after:
            status["status"] = "degraded"
            status["checks"]["ingestion_worker"] = f"stale (last heartbeat {age:.0f}s ago)"
        else:
            status["checks"]["ingestion_worker"] = f"ok (last heartbeat {age:.0f}s ago)"

    # Phase F6: the two agents formalized this phase report their heartbeat
    # here so they are as observable as the workers above — but
    # INFORMATIONALLY ONLY, and never flipping overall `status`. Both are
    # request-scoped: planning beats once per finalized incident and the
    # supervisor once per oversight read, so an old heartbeat means "not used
    # recently", which on a quiet platform is correct behaviour rather than a
    # fault. Treating it as staleness would report a healthy idle system as
    # degraded — the exact fabrication OBS-4 forbids.
    for agent_key, enabled, label in (
        ("planning", container.settings.PLANNING_AGENT_ENABLED, "planning_agent"),
        ("supervisor", container.settings.SUPERVISOR_AGENT_ENABLED, "supervisor_agent"),
    ):
        if not enabled:
            status["checks"][label] = f"disabled ({label.upper()}_ENABLED=false)"
            continue
        agent_age = heartbeat_tracker.age_seconds(agent_key)
        status["checks"][label] = (
            "registered (no invocation yet)" if agent_age is None
            else f"ok (last invoked {agent_age:.0f}s ago)"
        )

    # Phase E7 (RAG-6): informational only — staleness here degrades
    # retrieval QUALITY, not platform availability, so it never flips
    # overall `status`.
    bm25_index = getattr(container, "bm25_index", None)
    if bm25_index is None:
        status["checks"]["bm25_index"] = "disabled (RAG_HYBRID_ENABLED=false or init failed)"
    else:
        bm25_age = bm25_index.age_seconds
        bm25_stale_after = container.settings.BM25_STALE_SECONDS
        if bm25_age is None:
            status["checks"]["bm25_index"] = "unbuilt"
        elif bm25_age > bm25_stale_after:
            status["checks"]["bm25_index"] = (
                f"stale (built {bm25_age:.0f}s ago, {bm25_index.size} docs)"
            )
        else:
            status["checks"]["bm25_index"] = (
                f"ok (built {bm25_age:.0f}s ago, {bm25_index.size} docs)"
            )

    # Qdrant (RAG corpus + Enterprise Memory). Informational: an unreachable
    # vector store degrades retrieval to nothing, but the platform still
    # serves, ingests, and investigates — so this is disclosed, never a 503.
    qdrant_client = getattr(container, "qdrant_client", None)
    if qdrant_client is None:
        status["checks"]["qdrant"] = "not configured"
    else:
        try:
            collections = qdrant_client.get_collections().collections
            names = sorted(getattr(c, "name", "?") for c in collections)
            status["checks"]["qdrant"] = f"ok ({len(names)} collection(s): {', '.join(names)})"
        except Exception as exc:  # noqa: BLE001
            status["checks"]["qdrant"] = f"unreachable: {exc}"

    # LLM posture. Deliberately does NOT make a provider call — a health probe
    # that spends tokens on every poll is its own defect. It reports the
    # configured posture, which is what an operator actually needs to see
    # (mock vs real, and whether a key exists at all).
    settings = container.settings
    if not getattr(settings, "LLM_ENABLED", False):
        status["checks"]["llm"] = "disabled (LLM_ENABLED=false)"
    elif getattr(settings, "USE_MOCK_LLM", True):
        status["checks"]["llm"] = "mock (USE_MOCK_LLM=true — no real provider call)"
    elif not str(getattr(settings, "LLM_API_KEY", "") or "").strip():
        status["checks"]["llm"] = "misconfigured: LLM_ENABLED=true but LLM_API_KEY is empty"
    else:
        provider = getattr(settings, "LLM_PROVIDER", "?")
        model = str(getattr(settings, "LLM_MODEL", "") or "").strip() or "llama-3.1-8b-instant"
        status["checks"]["llm"] = f"ok (provider={provider}, model={model}, key configured)"

    return status


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
    # Phase 8 / Phase E3: Security Middleware Registration
    # -------------------------------------------------
    # We must create the Redis client here, not use the container
    # because container is not yet attached at this point.
    settings = Settings()  # pyright: ignore[reportCallIssue]
    redis_client = RedisClient(redis_url=str(settings.REDIS_URL))
    # Exposed so the lifespan's shutdown hook can close this pool. It cannot
    # be the container's client (the container does not exist yet at
    # middleware-registration time), but it must not leak either.
    application.state.middleware_redis = redis_client

    # Phase E3 (SEC-1/SEC-4): resolve the RS256 public key from
    # SecretManager (env-first, settings-fallback). In non-development
    # environments a missing/placeholder key aborts startup loudly.
    # Development keeps working with the well-known placeholder so today's
    # local-dev bypass is byte-for-byte unchanged (COMPAT-1).
    jwt_auth = _build_jwt_auth(settings)

    rbac = RBAC()
    rate_limiter = RateLimiter(redis_client=redis_client)
    # Phase E3 (ARCH-7): file-sink path is now configurable; the durable
    # DB-backed sink is attached in the lifespan (once the DB client is
    # available on the container) via audit_logger.attach_database().
    audit_logger = AuditLogger(log_file=settings.AUDIT_LOG_FILE)
    # Exposed on app.state so the lifespan can upgrade it with a real DB
    # client once the container is built. The middleware holds the same
    # instance, so attach_database() takes effect for it too.
    application.state.audit_logger = audit_logger
    # Phase E10: exposed so the auth router can read ENVIRONMENT before the
    # lifespan-built AppContainer exists (the dev-token endpoint must be
    # gated from the very first request the app serves).
    application.state.settings = settings

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
    # CORS middleware for frontend (Phase E3, SEC-1)
    # -------------------------------------------------
    cors_origins = [
        o.strip()
        for o in (settings.CORS_ALLOWED_ORIGINS or "").split(",")
        if o.strip()
    ]
    application.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins or ["http://localhost:5173"],
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
    application.include_router(retrieval_debug_router)
    application.include_router(ingest_router)
    application.include_router(knowledge_router)
    application.include_router(data_center_router)
    application.include_router(observability_router)
    application.include_router(administration_router)
    application.include_router(review_router)
    application.include_router(auth_router)
    application.include_router(audit_router)
    application.include_router(learning_router)
    application.include_router(graph_router)
    application.include_router(replay_router)
    application.include_router(mesh_router)
    application.include_router(connectors_router)

    _register_routes(application)
    _mount_frontend_build(application)
    return application


# ---------------------------------------------------------------------------
# Phase E10 — production frontend serving (ARCH-1: single deployable)
# ---------------------------------------------------------------------------

# frontend/dist relative to the repository root (this file lives at
# aeam/main.py, so parents[1] is the repo root).
_FRONTEND_DIST: Path = Path(__file__).resolve().parents[1] / "frontend" / "dist"

# Prefixes that must NEVER fall through to the SPA index.html -- they are
# either real API/infra surfaces or already have their own explicit routes.
_NON_SPA_PREFIXES: tuple[str, ...] = (
    "api/", "health", "metrics", "docs", "redoc", "openapi.json", "internal/", "favicon.ico",
)


def _mount_frontend_build(app: FastAPI) -> None:
    """
    Serve the built React console from the same FastAPI process.

    Phase E10 (ARCH-1): the console ships as a static Vite build
    (``frontend/dist``), mounted by the monolith rather than requiring a
    separate Vite dev server or reverse proxy in production. Reused
    unchanged in local dev: if ``frontend/dist`` does not exist (nobody has
    run ``npm run build`` yet), this is a silent no-op and the existing
    ``GET /`` liveness JSON route continues to answer at ``/`` exactly as
    before (COMPAT-1) -- local dev keeps using the Vite dev server on 5173
    with its ``/api`` proxy, as documented in the frontend README.
    """
    if not _FRONTEND_DIST.is_dir():
        logger.info(
            "frontend/dist not found at %s -- skipping static frontend mount "
            "(expected in local dev; run `npm run build` to enable it).",
            _FRONTEND_DIST,
        )
        return

    assets_dir = _FRONTEND_DIST / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="frontend-assets")

    index_path = _FRONTEND_DIST / "index.html"

    @app.get("/{full_path:path}", include_in_schema=False)
    async def _serve_spa(full_path: str) -> Response:
        """SPA fallback: any GET not matched above and not an API/infra path
        returns the built index.html so client-side routing (react-router)
        can take over. API/infra prefixes are excluded so a genuinely
        missing API route still returns a normal 404, not HTML."""
        if full_path.startswith(_NON_SPA_PREFIXES):
            return JSONResponse(status_code=404, content={"detail": "Not found."})
        candidate = _FRONTEND_DIST / full_path
        if full_path and candidate.is_file():
            return Response(content=candidate.read_bytes(), media_type=_guess_media_type(candidate))
        return Response(content=index_path.read_bytes(), media_type="text/html")

    logger.info("Frontend production build mounted from %s", _FRONTEND_DIST)


def _guess_media_type(path: Path) -> str:
    suffix = path.suffix.lower()
    return {
        ".js": "application/javascript",
        ".css": "text/css",
        ".svg": "image/svg+xml",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".ico": "image/x-icon",
        ".json": "application/json",
        ".woff2": "font/woff2",
    }.get(suffix, "application/octet-stream")


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
        status = build_health_payload(container)
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
