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
from aeam.storage.blob_store import BlobStore, LocalDiskBlobStore
from aeam.registry.repositories import (
    IngestionJobRepository,
    DatasetRepository,
    SchemaRepository,
    VersionRepository,
    PolicyRepository,
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
from aeam.intelligence.cross_dataset_analyzer import CrossDatasetAnalyzer
from aeam.intelligence.adaptive_detection import AdaptiveDetectionEngine
from aeam.agents.rag.hybrid_retrieval import BM25Index, HybridRetrievalPipeline
from aeam.agents.rag.query_expansion import QueryExpansionAgent
from aeam.agents.rag.multi_query_retrieval import MultiQueryRetrievalPipeline
from aeam.agents.rag.reranker import CrossEncoderReranker, RerankingRetrievalPipeline
from aeam.agents.rag.evidence_diversity import EvidenceDiversityFilter, EvidenceDiversityPipeline
from aeam.agents.rag.retrieval_debug import RetrievalDebugTracer
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
from aeam.api.retrieval_debug import router as retrieval_debug_router
from aeam.api.ingest import router as ingest_router
from aeam.api.knowledge import router as knowledge_router
from aeam.api.data_center import router as data_center_router

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

    # Enterprise Data Layer (Phase B1.1) — content-addressable blob store for
    # original ingested files. Local-disk backend; the registry tables were
    # created during DatabaseClient construction above.
    logger.info("Initialising BlobStore …")
    blob_store = LocalDiskBlobStore(settings.BLOB_STORAGE_DIR)

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
    container.qdrant_client = qdrant_client
    print("=== AFTER Qdrant ===")

    print("=== BEFORE IngestionPipeline ===")
    ingestion_pipeline = IngestionPipeline(
        embedding_service=embedding_service,
        qdrant_client=qdrant_client,
    )
    container.ingestion_pipeline = ingestion_pipeline
    _ingest_startup_documents(ingestion_pipeline)
    print("=== AFTER IngestionPipeline ===")

    print("=== BEFORE RetrievalPipeline ===")
    retrieval_pipeline = RetrievalPipeline(
        embedding_service=embedding_service,
        qdrant_client=qdrant_client,
    )
    print("=== AFTER RetrievalPipeline ===")

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
    )
    logger.info("Retrieval debug tracer initialised.")

    print("=== BEFORE RAGAgent ===")
    validator = RAGResponseValidator()
    rag_agent = RAGAgent(
        retrieval_pipeline=rag_retrieval,
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
    composite_rule_engine = CompositeRuleEngine(base=RuleEngine())
    composite_rule_engine.add_domain_provider(
        "datasets",
        lambda: dataset_intelligence.list_monitorable_metric_names(
            dataset_activation.list_activated_dataset_ids()
        ),
    )
    container.rule_engine = composite_rule_engine

    # --- Monitor Agent (Phase 2) ---
    monitor_agent = None
    if settings.ENABLE_MONITOR_AGENT or settings.ENVIRONMENT != "production":
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
        logger.info("MonitorAgent disabled by configuration.")

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
    )

    # --- Cross-Dataset Intelligence (Phase C4) ---
    # Reuses the EXACT SAME dataset_activation/dataset_intelligence/
    # dataset_kpi_source instances MonitorAgent's own CompositeKPISource
    # already depends on (constructed above) -- no second dataset reader,
    # no second profiler, no second activation store. StatisticalDetector
    # is constructed fresh inside CrossDatasetAnalyzer with the SAME
    # window_size=7 MonitorAgent itself uses (same class, not a second
    # detector implementation).
    cross_dataset_analyzer = CrossDatasetAnalyzer(
        dataset_activation=dataset_activation,
        intelligence=dataset_intelligence,
        kpi_source=dataset_kpi_source,
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
    )

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
        memory_engine=enterprise_memory,
        policy_registry=policy_registry,
        cross_dataset_analyzer=cross_dataset_analyzer,
        adaptive_detection_engine=adaptive_detection_engine,
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
    if getattr(container, "ingestion_worker", None) is not None:
        container.ingestion_worker.stop()
        logger.info("IngestionWorker stop signalled.")
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
    application.include_router(retrieval_debug_router)
    application.include_router(ingest_router)
    application.include_router(knowledge_router)
    application.include_router(data_center_router)

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
