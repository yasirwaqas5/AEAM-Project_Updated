"""
aeam/config/settings.py

Centralized configuration for the AEAM modular monolith.

This module defines all application-level settings using Pydantic's BaseSettings,
which automatically loads values from environment variables or a .env file.

No secrets are hardcoded. Required fields will raise a ValidationError at startup
if not provided via the environment.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, field_validator


class Settings(BaseSettings):
    """
    Application settings loaded from environment variables.

    Required environment variables:
        - DATABASE_URL
        - REDIS_URL
        - VECTOR_DB_URL
        - ENVIRONMENT

    Optional environment variables (with defaults):
        - MONITOR_INTERVAL_SECONDS (default: 300)
        - MAX_INVESTIGATION_DEPTH (default: 5)
        - LLM_ENABLED (default: False)
        - USE_MOCK_LLM (default: True)

        # --- Forecast configuration (Phase 5) ---
        - FORECAST_WINDOW_DAYS (default: 7)
        - FORECAST_MIN_HISTORY_DAYS (default: 30)
        - FORECAST_RETRAIN_DAYS (default: 7)
        - FORECAST_DEVIATION_THRESHOLD_PERCENT (default: 20.0)
        - FORECAST_CONFIDENCE_INTERVAL (default: 0.95)
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="forbid",
    )

    # --- Required settings ---

    DATABASE_URL: str = Field(
        ...,
        description="Connection string for relational database (e.g. PostgreSQL or SQLite).",
    )

    REDIS_URL: str = Field(
        ...,
        description="Connection string for Redis instance.",
    )

    VECTOR_DB_URL: str = Field(
        ...,
        description="Connection string or endpoint for vector database.",
    )

    ENVIRONMENT: str = Field(
        ...,
        description="Deployment environment: development, staging, production, or test.",
    )

    # --- Optional settings with defaults ---

    MONITOR_INTERVAL_SECONDS: int = Field(
        default=300,
        ge=1,
        description="Polling interval (seconds) for monitor loop.",
    )

    MAX_INVESTIGATION_DEPTH: int = Field(
        default=5,
        ge=1,
        description="Maximum recursive depth for investigation chain.",
    )

    LLM_ENABLED: bool = Field(
        default=False,
        description="Feature flag to enable or disable LLM-powered components.",
    )

    ENABLE_MONITOR_AGENT: bool = Field(
        default=False,
        description=(
            "Phase E7 (SEC-8/PHIL-1): sole, authoritative gate for the "
            "autonomous MonitorAgent polling loop. No environment backdoor "
            "in either direction -- this flag decides in every environment, "
            "including 'development' and 'test'. Deployment artifacts "
            "(docker-compose.yml, deploy/cloudrun.yaml) set this explicitly "
            "per posture; see docs/autonomous_operations.md for the full "
            "environment posture matrix."
        ),
    )

    # --- Detection & forecast intelligence uplift (Phase F1) ---
    #
    # Every detection change in F1 is flag-gated with today's behaviour as
    # the default (COMPAT-2), so an unset deployment runs the exact Phase-5
    # detection pipeline byte-for-byte. The KPI Agent is the one exception
    # and it defaults ON: it replaces a placeholder that F1 *deletes*, so
    # defaulting it off would leave the investigation loop with no KPI pass
    # at all — strictly worse than the honestly-labelled stand-in it
    # retires. Its flag exists for the documented rollback, not as a
    # posture anyone should run.

    KPI_AGENT_ENABLED: bool = Field(
        default=True,
        description=(
            "Phase F1 (PHIL-1): enables the real KPIAgent investigation "
            "pass. True (the default) is the intended posture. False "
            "disables the KPI pass entirely — it does NOT restore the "
            "pre-F1 placeholder, which was deleted rather than bypassed; "
            "reinstating that requires a version-control revert (see the "
            "F1 rollback strategy in ROADMAP.md)."
        ),
    )

    KPI_AGENT_HISTORY_LIMIT: int = Field(
        default=90,
        ge=2,
        description=(
            "Phase F1: maximum historical observations the KPIAgent fetches "
            "per investigation. Bounds the query the same way E6 bounds "
            "every other read path; 90 daily points covers a quarter."
        ),
    )

    DETECTION_CHANGEPOINT_ENABLED: bool = Field(
        default=False,
        description=(
            "Phase F1 (COMPAT-2): enables ChangepointDetector in the "
            "MonitorAgent detection pipeline. False (the default) leaves "
            "the pipeline's signals, severity and event metadata "
            "byte-identical to Phase 5."
        ),
    )

    DETECTION_CHANGEPOINT_THRESHOLD: float = Field(
        default=3.0,
        gt=0,
        description=(
            "Phase F1 (ENG-6): robust-sigma score above which a level shift "
            "is reported. Overrides advanced_detectors.py's engine-owned "
            "default; matches the z-score threshold so both speak the same "
            "units."
        ),
    )

    DETECTION_CHANGEPOINT_MIN_SEGMENT: int = Field(
        default=4,
        ge=2,
        description=(
            "Phase F1 (ENG-6): minimum observations either side of a "
            "candidate changepoint. Smaller segments make 'level' "
            "meaningless."
        ),
    )

    DETECTION_SEASONAL_HYBRID_ENABLED: bool = Field(
        default=False,
        description=(
            "Phase F1 (COMPAT-2): enables SeasonalHybridDetector in the "
            "MonitorAgent detection pipeline. False (the default) leaves "
            "Phase-5 behaviour unchanged."
        ),
    )

    DETECTION_SEASONAL_PERIOD: int = Field(
        default=7,
        ge=2,
        description=(
            "Phase F1 (ENG-6): seasonal cycle length in observations "
            "(7 = weekly on daily data)."
        ),
    )

    DETECTION_SEASONAL_MIN_CYCLES: int = Field(
        default=3,
        ge=2,
        description=(
            "Phase F1 (ENG-6): complete cycles required before the seasonal "
            "detector emits any score. Below this it honestly reports "
            "insufficient data rather than guessing a seasonal shape."
        ),
    )

    DETECTION_SEASONAL_THRESHOLD: float = Field(
        default=3.0,
        gt=0,
        description=(
            "Phase F1 (ENG-6): robust-sigma score above which a seasonal "
            "residual is an anomaly."
        ),
    )

    FORECAST_BACKTEST_ENABLED: bool = Field(
        default=False,
        description=(
            "Phase F1 (TECH-6): when true, a newly trained forecast model "
            "is backtested on a holdout before it is allowed to serve, and "
            "its holdout MAPE is recorded. False (the default) preserves "
            "the Phase-5 train-and-serve behaviour exactly."
        ),
    )

    FORECAST_BACKTEST_HOLDOUT: int = Field(
        default=7,
        ge=1,
        description=(
            "Phase F1: number of trailing observations withheld from "
            "training and used as the backtest horizon."
        ),
    )

    FORECAST_MAX_HOLDOUT_MAPE: float = Field(
        default=0.0,
        ge=0,
        description=(
            "Phase F1 (TECH-6): holdout MAPE (percent) above which a newly "
            "trained model is REFUSED and the metric reports insufficient "
            "data rather than serving predictions nobody validated. 0.0 "
            "(the default) means 'record the score but never refuse' — "
            "unset must mean exactly the behaviour before this setting "
            "existed (COMPAT-3)."
        ),
    )

    FORECAST_MODEL_SELECTION_ENABLED: bool = Field(
        default=False,
        description=(
            "Phase F1: when true (and FORECAST_BACKTEST_ENABLED is also "
            "true), candidate models are backtested and the lowest-MAPE "
            "candidate serves. False (the default) trains the single "
            "Phase-5 configuration."
        ),
    )

    # --- Adaptive learning & confidence recalibration (Phase F2) ---
    #
    # Everything here defaults OFF. With the flag off the platform reports
    # raw confidence exactly as it did through F1 — the phase's stated
    # rollback posture — and the calibration tables stay empty and inert.
    # Turning it on changes what `confidence` means on a finalized
    # incident, which is a decision an operator makes deliberately after
    # reviewing a recalibration's measured improvement, never a default.

    LEARNING_CALIBRATION_ENABLED: bool = Field(
        default=False,
        description=(
            "Phase F2: when true, an active calibration mapping is applied "
            "to confidence at finalize and BOTH values are disclosed "
            "(raw retained in audit_summary.calibration.confidence_raw). "
            "False (the default) reports raw confidence exactly as F1 did."
        ),
    )

    LEARNING_HISTORY_LIMIT: int = Field(
        default=5000,
        ge=2,
        description=(
            "Phase F2: maximum incidents the Learning Agent reads per "
            "recalibration run. Bounds the query the same way E6 bounds "
            "every other read path."
        ),
    )

    LEARNING_MIN_TRAINING_SAMPLES: int = Field(
        default=60,
        ge=2,
        description=(
            "Phase F2 (ENG-6): labeled samples required before a "
            "calibration is fitted at all. Below this, isotonic regression "
            "reproduces the training set and generalises to nothing, so "
            "the engine refuses rather than shipping a confident-looking "
            "bad mapping. Overrides calibration.py's engine-owned default."
        ),
    )

    LEARNING_HOLDOUT_FRACTION: float = Field(
        default=0.3,
        gt=0.0,
        lt=1.0,
        description=(
            "Phase F2: fraction of labeled samples withheld from the fit "
            "and used to measure improvement. Every reported ECE gain is "
            "measured on data the mapping never saw."
        ),
    )

    LEARNING_MIN_ECE_IMPROVEMENT: float = Field(
        default=0.01,
        ge=0.0,
        description=(
            "Phase F2 (PHIL-1): minimum held-out ECE reduction that counts "
            "as learning rather than noise. A fit below this is reported "
            "as not improved and is NOT adopted."
        ),
    )

    # --- Correlation Intelligence & Business Graph (Phase F4) ---
    #
    # The graph is an ADVISORY evidence source and ships off. With the flag
    # false, no graph finding is appended, CrossDatasetAnalyzer runs its
    # exact Phase C4 pairwise path, and the graph tables sit empty and
    # inert — the F4 rollback posture, and the reason turning this on is a
    # deliberate operator act rather than a default.

    BUSINESS_GRAPH_ENABLED: bool = Field(
        default=False,
        description=(
            "Phase F4: when true, the Orchestrator appends a 'graph' "
            "advisory finding from the persisted business graph and "
            "CrossDatasetAnalyzer consults known relationships. False (the "
            "default) restores pairwise Phase C4 correlation exactly. The "
            "graph never overrides RuleEngine/StatisticalDetector/KPIAgent/"
            "ForecastAgent in either state."
        ),
    )

    GRAPH_MAX_DEPTH: int = Field(
        default=2,
        ge=1,
        le=5,
        description=(
            "Phase F4 (E6): maximum traversal hops from the incident's "
            "metric. Two is the default because one hop is what C4 already "
            "sees; the second is the compounding this phase adds. The "
            "ceiling is enforced in business_graph.py as well, so this "
            "setting can lower the bound but never remove it."
        ),
    )

    GRAPH_MAX_NODES: int = Field(
        default=100,
        ge=1,
        le=1000,
        description=(
            "Phase F4 (E6): maximum distinct nodes one traversal may visit. "
            "Reaching it truncates the answer and the finding says so."
        ),
    )

    GRAPH_MAX_EDGES: int = Field(
        default=300,
        ge=1,
        le=5000,
        description=(
            "Phase F4 (E6): maximum edges one traversal may read, across "
            "all hops. This is the cost bound that keeps a hub node with "
            "tens of thousands of edges from turning an investigation into "
            "a full table scan."
        ),
    )

    GRAPH_MIN_EDGE_CONFIDENCE: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description=(
            "Phase F4: edges below this confidence are not traversed. Zero "
            "(the default) traverses every persisted edge — every edge is "
            "already evidence-grounded, so filtering is an operator's "
            "signal-to-noise preference, not a correctness requirement."
        ),
    )

    GRAPH_BUILD_INCIDENT_LIMIT: int = Field(
        default=5000,
        ge=1,
        description=(
            "Phase F4 (E6): maximum incidents one graph build reads, most "
            "recent first. Bounds the build the same way "
            "LEARNING_HISTORY_LIMIT bounds a recalibration run."
        ),
    )

    GRAPH_MIN_CORRELATION: float = Field(
        default=0.7,
        gt=0.0,
        le=1.0,
        description=(
            "Phase F4: minimum |Pearson r| a persisted cross-dataset "
            "observation must show before it becomes a 'correlates_with' "
            "edge. Defaults to C4's own reporting threshold — the graph "
            "does not relax the bar the measurement was made against."
        ),
    )

    # --- Autonomous operations supervision (Phase E7, OBS-3/4) ---

    HEARTBEAT_STALE_SECONDS: int = Field(
        default=120,
        ge=1,
        description=(
            "Phase E7: a background worker (MonitorAgent, IngestionWorker) "
            "whose last heartbeat is older than this many seconds is "
            "reported as 'stale' by GET /health (and the console StatusBar), "
            "and flips overall health to 'degraded'. IngestionWorker's "
            "default poll interval (2s) makes a 120s threshold generous "
            "for it; a deliberately killed thread is still caught within "
            "one interval either way -- MonitorAgent operators tuning "
            "MONITOR_INTERVAL_SECONDS above this value should raise it "
            "accordingly so a normal sleep is never mistaken for a dead "
            "thread."
        ),
    )

    BM25_STALE_SECONDS: int = Field(
        default=3600,
        ge=1,
        description=(
            "Phase E7 (RAG-6): informational staleness threshold for the "
            "BM25 lexical index disclosed by GET /health. Does not affect "
            "overall health status (a stale lexical index degrades "
            "retrieval quality, not platform availability) -- it is a "
            "disclosed freshness signal, never a fabricated 'fresh'."
        ),
    )

    RAG_HYBRID_ENABLED: bool = Field(
        default=True,
        description=(
            "Phase 7.1: when True, RAG retrieval fuses dense (Qdrant) and BM25 "
            "lexical results via Reciprocal Rank Fusion. When False, falls back "
            "to the original dense-only RetrievalPipeline unchanged."
        ),
    )

    POLICY_EXTRACTION_ENABLED: bool = Field(
        default=True,
        description=(
            "Phase C2: when True, DocumentIngestJobProcessor runs an additional "
            "LLM extraction pass after a document is chunked/embedded/indexed, "
            "looking for explicit business rules (conditions/actions/thresholds/"
            "escalation/approval/department/role/time constraints/priority/"
            "related metrics) and persisting any found to the 'policies' table. "
            "Never blocks ingestion on failure. When False, ingestion behaves "
            "exactly as before this phase — no LLM call, no policies table writes."
        ),
    )

    RAG_RERANK_ENABLED: bool = Field(
        default=True,
        description=(
            "Phase 7.2: when True, a cross-encoder reranks the fused candidate "
            "list before it reaches the LLM. If the reranker model cannot "
            "initialize, retrieval falls back to hybrid automatically."
        ),
    )

    RAG_RERANK_TOP_N: int = Field(
        default=20,
        ge=1,
        description=(
            "Phase 7.2: number of fused candidates fetched and re-scored by the "
            "cross-encoder before returning the caller's top_k."
        ),
    )

    RAG_RERANK_MODEL: str = Field(
        default="cross-encoder/ms-marco-MiniLM-L-6-v2",
        description="Phase 7.2: SentenceTransformers cross-encoder model id for reranking.",
    )

    RAG_MULTI_QUERY_ENABLED: bool = Field(
        default=True,
        description=(
            "Phase 7.3: when True, the original query is expanded into several "
            "diverse variants (LLM-generated) before hybrid retrieval, and the "
            "per-variant results are merged via Reciprocal Rank Fusion. If "
            "query expansion fails, retrieval falls back to the original query only."
        ),
    )

    RAG_MULTI_QUERY_COUNT: int = Field(
        default=4,
        ge=1,
        description=(
            "Phase 7.3: total number of queries used for retrieval, INCLUDING "
            "the original — up to (count - 1) additional variants are "
            "LLM-generated. 1 disables expansion (original query only)."
        ),
    )

    # --- Enterprise Data Layer (Phase B1.1) ---

    BLOB_STORAGE_DIR: str = Field(
        default="data/blobs",
        description=(
            "Phase B1.1: root directory for the content-addressable BlobStore "
            "that holds original ingested files. Local-disk backend; swappable "
            "for S3/Azure later without changing callers."
        ),
    )

    # --- Durable BlobStore backend (Phase E4, ARCH-7 / SEC-5 / TECH-5) ---
    #
    # Selects which concrete BlobStore implementation the platform runs.
    # 'local' preserves today's LocalDiskBlobStore behavior byte-for-byte
    # (COMPAT-1: unchanged when nothing is configured). 's3' targets any
    # S3-compatible endpoint (AWS S3, MinIO, R2, GCS-via-HMAC) and is the
    # correct posture for ephemeral compute like Cloud Run — where the
    # audit found today's local-disk backend evaporates on instance
    # recycle. All S3 credential/endpoint values are resolved through
    # SecretManager (SEC-5); nothing here is a hardcoded credential.

    BLOB_STORAGE_BACKEND: str = Field(
        default="local",
        description=(
            "Phase E4 (ARCH-7): 'local' for LocalDiskBlobStore, 's3' for "
            "the S3-compatible backend. Default preserves today's local "
            "behavior; production posture on ephemeral compute must set "
            "this to 's3' (see deploy/cloudrun.yaml)."
        ),
    )

    BLOB_S3_BUCKET: str = Field(
        default="",
        description=(
            "Phase E4: S3 bucket name for the durable BlobStore. Required "
            "when BLOB_STORAGE_BACKEND=s3; ignored otherwise."
        ),
    )

    BLOB_S3_ENDPOINT_URL: str = Field(
        default="",
        description=(
            "Phase E4: custom S3 endpoint URL for non-AWS backends "
            "(MinIO, Cloudflare R2, GCS HMAC). Empty routes to the AWS "
            "regional endpoint via boto3's default provider chain."
        ),
    )

    BLOB_S3_REGION: str = Field(
        default="",
        description=(
            "Phase E4: AWS region for endpoint resolution. Optional for "
            "non-AWS endpoints."
        ),
    )

    BLOB_S3_ACCESS_KEY_ID: str = Field(
        default="",
        description=(
            "Phase E4 (SEC-5): S3 access key ID resolved through "
            "SecretManager (never hardcoded in tracked deployment "
            "artifacts). Empty falls back to boto3's default provider "
            "chain (IMDS, IAM role, shared credentials, env)."
        ),
    )

    BLOB_S3_SECRET_ACCESS_KEY: str = Field(
        default="",
        description=(
            "Phase E4 (SEC-5): S3 secret access key resolved through "
            "SecretManager. Empty falls back to boto3's default provider "
            "chain — matching the paired access key ID field."
        ),
    )

    BLOB_S3_PREFIX: str = Field(
        default="",
        description=(
            "Phase E4: key prefix inside BLOB_S3_BUCKET. Empty by default; "
            "set this when a single bucket is shared across environments."
        ),
    )

    INGEST_WORKER_POLL_SECONDS: float = Field(
        default=2.0,
        gt=0,
        description=(
            "Phase B1.2: seconds between IngestionWorker polls of the "
            "ingestion_jobs queue when idle."
        ),
    )

    ACTIVATED_DATASET_IDS: str = Field(
        default="",
        description=(
            "Phase B1.5.3: comma-separated list of registered dataset ids "
            "explicitly approved for autonomous monitoring. Registering a "
            "dataset (upload -> B1.4 processing) only makes it eligible — it "
            "becomes a live KPI feed for MonitorAgent only once its id "
            "appears here. Empty by default: no dataset is auto-monitored. "
            "This is the interim (config-driven) DatasetActivation source; a "
            "future admin-UI/DB-backed implementation can replace it without "
            "changing any consumer."
        ),
    )

    RAG_DIVERSITY_ENABLED: bool = Field(
        default=True,
        description=(
            "Phase 7.4: when True, an evidence diversity filter runs after "
            "cross-encoder reranking — removing near-duplicate chunks and "
            "capping how many chunks may come from the same source document."
        ),
    )

    RAG_MAX_CHUNKS_PER_DOCUMENT: int = Field(
        default=2,
        ge=1,
        description=(
            "Phase 7.4: maximum chunks kept from the same source document "
            "(metadata['source']) in the final evidence set. A preference, "
            "not a hard cap — backfilled if too few documents are available."
        ),
    )

    RAG_SIMILARITY_THRESHOLD: float = Field(
        default=0.8,
        gt=0.0,
        le=1.0,
        description=(
            "Phase 7.4: Jaccard token-overlap threshold at/above which two "
            "chunks are treated as near-duplicates and the lower-ranked one "
            "is dropped."
        ),
    )

    RAG_ADVANCED_RETRIEVAL_ENABLED: bool = Field(
        default=True,
        description=(
            "Phase C6: when True, wraps the fully-composed existing retrieval "
            "pipeline with entity extraction (from event.metadata), "
            "metadata-aware filtering (with automatic relaxation if the "
            "filter matches nothing), and business-relevance ranking with "
            "explainable ranking_reasons/retrieval_confidence. When False, "
            "retrieval behaves exactly as it did before this phase."
        ),
    )

    LLM_PROVIDER: str = Field(
        default="groq",
        description=(
            "Phase E8 (AI-1, coherent defaults): which LLM backend to use. "
            "The default is 'groq' -- the ONLY provider LLMService actually "
            "implements today. Configuring any other value while "
            "LLM_ENABLED=true and USE_MOCK_LLM=false aborts startup with an "
            "explicit message (LLMService's provider-truth check) rather "
            "than silently promising a vendor the platform cannot reach. "
            "See docs/ai_governance.md for the provider support statement."
        ),
    )

    LLM_API_KEY: str = Field(
        default="",
        description="API key for the LLM provider (loaded from .env).",
    )

    USE_MOCK_LLM: bool = Field(
        default=True,
        description=(
            "When True, LLM calls return a mock response instead of "
            "reaching a real provider. Phase E8: the safe default "
            "everywhere except a deliberately configured production/staging "
            "posture -- see docs/ai_governance.md's 'Environment posture' "
            "table for what each environment should set. Every real call "
            "made when this is False is metered (aeam.monitoring.metrics: "
            "llm_calls_total{status='mock'} vs status='success'/'failure'), "
            "so an operator can always tell from /metrics whether AEAM is "
            "actually calling a real model."
        ),
    )

    # --- AI Governance (Phase E8, AI-1..AI-7) ---

    LLM_TIMEOUT_SECONDS: float = Field(
        default=30.0,
        gt=0.0,
        description=(
            "Phase E8 (AI-3): per-call timeout enforced at the LLM client "
            "level for every call site (RAGAgent, QueryExpansionAgent, "
            "PolicyExtractor, DecisionEngine, Orchestrator LLM reasoning, "
            "ReportAgent) -- they all share one LLMService instance, so one "
            "setting governs all six. See docs/ai_governance.md's call-site "
            "register."
        ),
    )

    LLM_COST_PER_1K_PROMPT_TOKENS_USD: float = Field(
        default=0.0,
        ge=0.0,
        description=(
            "Phase E8 (AI-6): USD price per 1,000 prompt tokens, used to "
            "compute the llm_cost_usd_total Prometheus counter. Default 0.0 "
            "means the metric is published with declared, honest semantics "
            "(OBS-2) but reports zero cost until an operator configures the "
            "real negotiated rate for their provider account -- never a "
            "guessed number presented as fact."
        ),
    )

    LLM_COST_PER_1K_COMPLETION_TOKENS_USD: float = Field(
        default=0.0,
        ge=0.0,
        description=(
            "Phase E8 (AI-6): USD price per 1,000 completion tokens. Same "
            "honesty contract as LLM_COST_PER_1K_PROMPT_TOKENS_USD."
        ),
    )

    # --- Distributed tracing (Phase E11, OBS-1/OBS-6) ---
    #
    # OpenTelemetry spans are a COMPLEMENTARY signal to the Prometheus
    # metrics pipeline, never a replacement for it (OBS-1: metrics remain
    # the single metrics pipeline). Tracing is OFF by default so every
    # existing deployment behaves exactly as it did before this phase
    # (COMPAT-1); turning it on requires both the flag and a real endpoint.

    OTEL_TRACING_ENABLED: bool = Field(
        default=False,
        description=(
            "Phase E11 (OBS-6): emit OpenTelemetry spans across the "
            "investigation path (decision -> evidence stages -> planning -> "
            "action), correlated by incident id. False (the default) makes "
            "every tracing call site a no-op. Requires the optional "
            "opentelemetry-sdk / opentelemetry-exporter-otlp-proto-http "
            "packages; when they are absent, tracing stays off with a loud "
            "warning rather than failing startup."
        ),
    )

    OTEL_EXPORTER_OTLP_ENDPOINT: str = Field(
        default="",
        description=(
            "Phase E11: base OTLP/HTTP endpoint of the enterprise tracing "
            "backend (e.g. 'http://otel-collector:4318'). '/v1/traces' is "
            "appended by the exporter. Empty with tracing enabled keeps "
            "tracing OFF rather than silently dropping spans."
        ),
    )

    OTEL_SERVICE_NAME: str = Field(
        default="aeam",
        description=(
            "Phase E11: 'service.name' resource attribute on every emitted "
            "span, so AEAM is identifiable in a shared tracing backend."
        ),
    )

    # --- Knowledge, policy & memory governance (Phase E12) ---

    KNOWLEDGE_CURATION_ENABLED: bool = Field(
        default=True,
        description=(
            "Phase E12 (SEC-7): master switch for the privileged curation "
            "write endpoints -- policy status transitions and Enterprise "
            "Memory expunge/correct. False returns 503 from those endpoints "
            "while every read path stays available, which is the phase's "
            "documented rollback posture. Authorisation is still the "
            "middleware's job (admin:config); this flag only decides whether "
            "the capability exists at all in this deployment."
        ),
    )

    # --- Forecast configuration (Phase 5) ---

    FORECAST_WINDOW_DAYS: int = Field(
        default=7,
        ge=1,
        description="Forecast horizon (number of future periods).",
    )

    FORECAST_MIN_HISTORY_DAYS: int = Field(
        default=30,
        ge=7,
        description="Minimum historical window required to train forecast model.",
    )

    FORECAST_RETRAIN_DAYS: int = Field(
        default=7,
        ge=1,
        description="Days after which the forecast model must retrain.",
    )

    FORECAST_DEVIATION_THRESHOLD_PERCENT: float = Field(
        default=20.0,
        ge=1.0,
        description="Deviation threshold percentage beyond forecast confidence interval.",
    )

    FORECAST_CONFIDENCE_INTERVAL: float = Field(
        default=0.95,
        gt=0.5,
        lt=1.0,
        description="Confidence interval width for Prophet forecasts.",
    )

    FORECAST_MODEL_DIR: str = Field(
        default="",
        description=(
            "Phase E4 (ARCH-7): filesystem path where Prophet model artifacts "
            "are persisted. Empty (the default) keeps ForecastAgent's own "
            "engine-owned default ('models/forecasting') exactly as it was "
            "before E4 (COMPAT-1). On ephemeral compute (Cloud Run) this "
            "MUST point at a durable mount so re-training is not required "
            "on every instance recycle — see deploy/cloudrun.yaml."
        ),
    )

    # --- Google Sheets configuration ---

    GOOGLE_SHEETS_SA_CREDENTIALS: str = Field(
        default="",
        description="Minified one-line Google service account JSON credentials.",
    )

    SHEET_ID: str = Field(
        default="",
        description="Google Sheets spreadsheet ID.",
    )

    SHEET_RANGE: str = Field(
        default="Sheet1!A2:C10",
        description="Google Sheets range for KPI data.",
    )

    # --- Slack configuration ---

    SLACK_BOT_TOKEN: str = Field(
        default="",
        description="Slack Bot User OAuth token.",
    )

    SLACK_CHANNEL: str = Field(
        default="#aeam-alerts",
        description="Default Slack channel for alerts.",
    )

    # --- Jira configuration ---

    JIRA_URL: str = Field(
        default="",
        description="Jira Cloud instance URL",
    )

    JIRA_API_TOKEN: str = Field(
        default="",
        description="Jira API token",
    )

    JIRA_USER_EMAIL: str = Field(
        default="",
        description="Jira user email for authentication",
    )

    JIRA_PROJECT_KEY: str = Field(
        default="", 
        description="Jira project key (e.g., 'OPS')"
    )

    JIRA_ISSUE_TYPE: str = Field(
        default="",
        description="Jira issue type name or ID (e.g., '10004' for Task)"
    )

    # --- Enterprise Configuration Engine (Phase D4) ---
    #
    # Centralizes tunable thresholds/weights/limits for the intelligence
    # engines (C1/C3/C4/C5/C6/C7/D1/D2/D3) that previously only lived as
    # hardcoded module constants. Every field here is Optional and defaults
    # to None -- "unconfigured" -- so the literal default value is NEVER
    # duplicated here; it continues to live exactly once, in the owning
    # engine's own module, and is used whenever the corresponding Settings
    # field is None (no env var set). Setting the env var overrides it.
    # Historical, already-persisted incident findings are plain JSON and are
    # never touched by any of these values -- they are only ever read at
    # engine construction/call time for NEW investigations.

    MEMORY_SIMILARITY_THRESHOLD: float | None = Field(
        default=None,
        gt=0.0,
        le=1.0,
        description=(
            "Phase D4: additional similarity floor EnterpriseMemoryEngine "
            "applies on top of whatever RetrievalPipeline.search() already "
            "returns, when recalling similar resolved incidents. None = no "
            "extra filtering (current behavior -- unchanged)."
        ),
    )

    POLICY_SIMILARITY_THRESHOLD: float | None = Field(
        default=None,
        gt=0.0,
        le=1.0,
        description="Phase D4: overrides PolicyRegistry's semantic-match threshold (default 0.4).",
    )

    CROSS_DATASET_CORRELATION_THRESHOLD: float | None = Field(
        default=None,
        gt=0.0,
        le=1.0,
        description="Phase D4: overrides CrossDatasetAnalyzer's correlation threshold (default 0.7).",
    )

    ADAPTIVE_MIN_BASELINE_POINTS: int | None = Field(
        default=None,
        ge=1,
        description="Phase D4: overrides AdaptiveDetectionEngine's minimum baseline history points (default 10).",
    )

    ADAPTIVE_MIN_SEASONALITY_POINTS: int | None = Field(
        default=None,
        ge=1,
        description="Phase D4: overrides AdaptiveDetectionEngine's minimum seasonality history points (default 14).",
    )

    ADAPTIVE_SEASONALITY_STRENGTH_THRESHOLD: float | None = Field(
        default=None,
        gt=0.0,
        description="Phase D4: overrides AdaptiveDetectionEngine's seasonality strength ratio (default 0.5).",
    )

    ADAPTIVE_WINDOW_SIZE: int | None = Field(
        default=None,
        ge=1,
        description="Phase D4: overrides AdaptiveDetectionEngine's longer-horizon window size (default 30).",
    )

    RETRIEVAL_ENTITY_BONUS_PER_MATCH: float | None = Field(
        default=None,
        ge=0.0,
        description="Phase D4: overrides BusinessRelevanceScorer's per-entity-match bonus (default 0.15).",
    )

    RETRIEVAL_MAX_ENTITY_BONUS: float | None = Field(
        default=None,
        ge=0.0,
        description="Phase D4: overrides BusinessRelevanceScorer's cap on total entity bonus (default 0.45).",
    )

    RETRIEVAL_DOC_TYPE_BONUS: float | None = Field(
        default=None,
        ge=0.0,
        description="Phase D4: overrides BusinessRelevanceScorer's actionable-doc-type bonus (default 0.05).",
    )

    RETRIEVAL_RECENCY_BONUS: float | None = Field(
        default=None,
        ge=0.0,
        description="Phase D4: overrides BusinessRelevanceScorer's recency bonus (default 0.05).",
    )

    RETRIEVAL_RECENCY_WINDOW_DAYS: int | None = Field(
        default=None,
        ge=1,
        description="Phase D4: overrides BusinessRelevanceScorer's recency window in days (default 30).",
    )

    EXECUTION_PLAN_AMBIGUOUS_CAUSE_GAP: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Phase D4: overrides ExecutionPlanningEngine's ambiguous-cause confidence gap (default 0.15).",
    )

    EXECUTION_PLAN_CONFLICT_CONFIDENCE_CAP: float | None = Field(
        default=None,
        gt=0.0,
        le=1.0,
        description="Phase D4: overrides ExecutionPlanningEngine's confidence cap applied when evidence conflicts (default 0.5).",
    )

    HUMAN_APPROVAL_QUALITY_LEVELS: str | None = Field(
        default=None,
        description=(
            "Phase D4: comma-separated evidence_quality levels that force "
            "human_approval_required=True in ExecutionPlanningEngine (default "
            "'insufficient,low'). Same comma-separated-string convention as "
            "ACTIVATED_DATASET_IDS."
        ),
    )

    AI_EVAL_STRENGTH_THRESHOLD: float | None = Field(
        default=None,
        gt=0.0,
        le=1.0,
        description="Phase D4: overrides AIEvaluationEngine's strength-signal threshold (default 0.7).",
    )

    AI_EVAL_WEAKNESS_THRESHOLD: float | None = Field(
        default=None,
        gt=0.0,
        le=1.0,
        description="Phase D4: overrides AIEvaluationEngine's weakness-signal threshold (default 0.4).",
    )

    AI_EVAL_CONFLICT_PENALTY_WEIGHT: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Phase D4: overrides AIEvaluationEngine's evidence-conflict penalty weight (default 0.2).",
    )

    AI_EVAL_MEMORY_MIXED_OUTCOME_PENALTY: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Phase D4: overrides AIEvaluationEngine's mixed-outcomes memory-quality penalty (default 0.15).",
    )

    OBSERVABILITY_TREND_WINDOW: int | None = Field(
        default=None,
        ge=1,
        description="Phase D4: overrides ObservabilityEngine's recent-values trend cap (default 20).",
    )

    OBSERVABILITY_RETENTION_LIMIT: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Phase D4/E6: caps how many most-recent incidents "
            "GET /api/v1/observability/ considers when building its summary. "
            "A read-time windowing cap only -- never deletes or alters any "
            "persisted incident row. None means the E6 sane default window "
            "applies (disclosed in the response's 'retention' block); set an "
            "explicit value to override it in either direction."
        ),
    )

    # --- Resource management (Phase E6, ENG-6 / PHIL-5) ---
    #
    # Explicit, configured bounds on the shared resource pools the platform
    # holds, so a load spike degrades gracefully (bounded pools) instead of
    # exhausting connections or threads. Defaults reproduce today's
    # hard-coded values exactly (COMPAT-1) — this only makes them tunable.

    DB_POOL_SIZE: int = Field(
        default=5,
        ge=1,
        description=(
            "Phase E6: persistent SQLAlchemy connection-pool size for the "
            "DatabaseClient. Default 5 matches the pre-E6 hard-coded value."
        ),
    )

    DB_MAX_OVERFLOW: int = Field(
        default=10,
        ge=0,
        description=(
            "Phase E6: additional transient DB connections allowed beyond "
            "DB_POOL_SIZE under load. Default 10 matches the pre-E6 value."
        ),
    )

    DB_POOL_TIMEOUT_SECONDS: int = Field(
        default=30,
        ge=1,
        description=(
            "Phase E6: seconds a caller waits for a pooled DB connection "
            "before failing fast (bounded back-pressure) instead of blocking "
            "unboundedly. Default 30 is SQLAlchemy's own default."
        ),
    )

    API_MAX_PAGE_SIZE: int = Field(
        default=1000,
        ge=1,
        description=(
            "Phase E6: hard upper bound on the `limit` any paginated list "
            "endpoint will honour, so no single request can pull an "
            "unbounded payload regardless of the requested size."
        ),
    )

    # --- Human-in-the-Loop Enforcement (Phase E9, AGENT-5 / COMPAT-6) ---
    #
    # C7 has computed `human_approval_required` since Phase C7, but nothing
    # enforced it — the runbook executed regardless, which AGENT-5 permits
    # only if the flag is documented as advisory. E9 makes it a real gate at
    # the execution boundary. The three fields below are the whole control
    # surface: whether the gate is enforced at all, and which ordered chain
    # of approval tiers an incident must clear before its gated runbook
    # steps run.
    #
    # Deliberately NOT registered in aeam/config/config_registry.py: the D5
    # admin API can edit every field it lists, and an approval gate that a
    # single API call can switch off is not a governance control. Changing
    # these is a deployment-time act (env var / cloudrun.yaml), auditable in
    # the deployment record rather than silently at runtime.

    HUMAN_APPROVAL_ENFORCED: bool = Field(
        default=True,
        description=(
            "Phase E9 (AGENT-5): when true, an incident whose execution plan "
            "sets human_approval_required=True has its GATED runbook steps "
            "recorded as pending instead of executed; only an authorized "
            "approval through the review API executes them. Notifications "
            "(Slack/Jira/email) are never gated — informing humans is not an "
            "action that needs approving. Set false to restore the pre-E9 "
            "advisory behaviour exactly (the tables stay, inert): this is "
            "the phase's documented rollback switch."
        ),
    )

    APPROVAL_TIER_CHAIN: str = Field(
        default="reviewer",
        description=(
            "Phase E9: the DEFAULT ordered approval chain, comma-separated "
            "(same convention as ACTIVATED_DATASET_IDS). One entry — the "
            "default — is a single-tier requirement and behaves exactly like "
            "a one-step approval (COMPAT-1/6). Several entries (e.g. "
            "'analyst,manager,risk') require every tier to approve IN ORDER "
            "before any gated step executes."
        ),
    )

    APPROVAL_TIER_CHAIN_OVERRIDES: str | None = Field(
        default=None,
        description=(
            "Phase E9: optional per-severity chain overrides, formatted "
            "'SEVERITY:tier1,tier2;SEVERITY2:tier1' (e.g. "
            "'CRITICAL:analyst,manager,risk;HIGH:analyst,manager'). Severity "
            "match is case-insensitive. A severity with no override uses "
            "APPROVAL_TIER_CHAIN. A matched policy that names responsible "
            "roles takes precedence over both (see docs/human_in_the_loop.md)."
        ),
    )

    # --- Identity & Access Enforcement (Phase E3) ---
    #
    # Key material for the RS256 JWT verifier and CORS origins move out of
    # main.py hardcoded literals and into configuration/SecretManager. All
    # new fields default to empty/None so a `development` posture keeps
    # working with the placeholder key exactly as it does today (COMPAT-1);
    # non-development startup applies the SEC-4 fail-closed contract in
    # main.create_app if key material is placeholder/absent. Engine-owned
    # defaults (issuer/audience) live in aeam/security/jwt_auth.py -- these
    # two Settings fields override them only when a value is actually set.

    JWT_PUBLIC_KEY: str = Field(
        default="",
        description=(
            "Phase E3 (SEC-1/SEC-4): PEM-encoded RSA public key for RS256 JWT "
            "verification, resolved via SecretManager (env var wins). Highest "
            "precedence key source. Empty = try JWT_PUBLIC_KEY_PATH next; if "
            "both are empty and ENVIRONMENT is not 'development', startup "
            "aborts (fail-closed, SEC-4)."
        ),
    )

    JWT_PUBLIC_KEY_PATH: str = Field(
        default="",
        description=(
            "Phase E3 (SEC-1/SEC-4): filesystem path to a PEM-encoded RSA "
            "public key. Fallback used only when JWT_PUBLIC_KEY is empty. "
            "Same fail-closed contract as JWT_PUBLIC_KEY."
        ),
    )

    JWT_ISSUER: str | None = Field(
        default=None,
        description=(
            "Phase E3 (SEC-2, ENG-6): overrides jwt_auth.py's engine-owned "
            "default issuer claim ('aeam-auth'). None keeps the default -- "
            "the value continues to live once, in its owning module."
        ),
    )

    JWT_AUDIENCE: str | None = Field(
        default=None,
        description=(
            "Phase E3 (SEC-2, ENG-6): overrides jwt_auth.py's engine-owned "
            "default audience claim ('aeam-api'). None keeps the default."
        ),
    )

    CORS_ALLOWED_ORIGINS: str = Field(
        default="http://localhost:5173",
        description=(
            "Phase E3 (SEC-1): comma-separated list of origins the CORS "
            "middleware admits. Default preserves today's dev frontend "
            "origin exactly (COMPAT-1). Same comma-separated-string "
            "convention as ACTIVATED_DATASET_IDS."
        ),
    )

    AUDIT_LOG_FILE: str = Field(
        default="/tmp/audit.log",
        description=(
            "Phase E3 (ARCH-7): filesystem path for the AuditLogger's file "
            "sink. Kept for local-dev convenience; the durable sink is the "
            "audit_logs table (created by DatabaseClient._init_schema, "
            "written whenever AuditLogger is constructed with a database "
            "client). Default preserves today's behaviour exactly."
        ),
    )

    # --- Enterprise SSO / OIDC federation (Phase E13, SEC-1/SEC-4) ---
    #
    # E13 federates identity onto the *same* E3 verification path rather
    # than building a second one: when OIDC is enabled, JWTAuth resolves
    # signing keys from the IdP's JWKS document instead of a static PEM,
    # and everything downstream (RBAC, rate limiting, audit) is untouched.
    # AEAM remains a relying party — it validates tokens the enterprise
    # IdP issued and never mints enterprise credentials itself.
    #
    # Every field defaults to disabled/empty, so an unset deployment keeps
    # the exact E3 static-key posture (COMPAT-2/COMPAT-3). Turning OIDC on
    # without the fields it requires aborts startup (SEC-4 fail-closed,
    # enforced in main._build_jwt_auth) rather than silently falling back
    # to a posture the operator did not ask for.

    OIDC_ENABLED: bool = Field(
        default=False,
        description=(
            "Phase E13 (SEC-1): master switch for enterprise SSO. False "
            "(the default) keeps the E3 static-PEM verification posture "
            "byte-for-byte. True requires OIDC_ISSUER and OIDC_CLIENT_ID; "
            "startup aborts if either is missing (SEC-4)."
        ),
    )

    OIDC_ISSUER: str = Field(
        default="",
        description=(
            "Phase E13: the IdP's issuer URL (e.g. "
            "'https://login.microsoftonline.com/<tenant>/v2.0'). Used both "
            "for OIDC discovery (<issuer>/.well-known/openid-configuration) "
            "and as the expected 'iss' claim when JWT_ISSUER is unset."
        ),
    )

    OIDC_CLIENT_ID: str = Field(
        default="",
        description=(
            "Phase E13: the client id the console is registered as at the "
            "IdP. Also the expected 'aud' claim when JWT_AUDIENCE is unset "
            "— an access token minted for a different application must not "
            "be accepted here."
        ),
    )

    OIDC_CLIENT_SECRET: str = Field(
        default="",
        description=(
            "Phase E13 (SEC-5): client secret for the authorization-code "
            "exchange, resolved via SecretManager. LEAVE EMPTY for the "
            "recommended public-client + PKCE posture — the console is a "
            "browser app and PKCE is the flow it should use. The value is "
            "never logged and never returned by any endpoint."
        ),
    )

    OIDC_REDIRECT_URI: str = Field(
        default="",
        description=(
            "Phase E13: the console callback URL registered at the IdP "
            "(e.g. 'https://aeam.example.com/auth/callback'). Empty means "
            "the console derives it from its own origin; set it explicitly "
            "when the console is served behind a proxy whose public origin "
            "differs from the browser's."
        ),
    )

    OIDC_SCOPES: str = Field(
        default="openid profile email",
        description=(
            "Phase E13: space-separated scopes requested at the "
            "authorization endpoint. The default is the minimum an OIDC "
            "sign-in needs; add the IdP-specific scope that carries role "
            "claims if yours requires one."
        ),
    )

    OIDC_ROLES_CLAIM: str = Field(
        default="roles",
        description=(
            "Phase E13: the token claim carrying the caller's AEAM roles. "
            "Default matches the E3/E10 token shape ('roles'); enterprise "
            "IdPs often use 'groups' or a namespaced claim. The claim's "
            "values must be members of aeam.security.rbac's vocabulary — "
            "unknown roles simply grant nothing (deny by default, SEC-1)."
        ),
    )

    OIDC_ALGORITHMS: str = Field(
        default="",
        description=(
            "Phase E13 (ENG-6): comma-separated signature-algorithm "
            "allow-list overriding jwt_auth.py's engine-owned default "
            "('RS256'). Empty keeps the default. Same comma-separated "
            "convention as CORS_ALLOWED_ORIGINS."
        ),
    )

    OIDC_JWKS_URL: str = Field(
        default="",
        description=(
            "Phase E13: explicit JWKS endpoint, bypassing discovery. Empty "
            "(the default) means the URL is read from the IdP's "
            "openid-configuration document. Set this for IdPs that do not "
            "publish discovery, or to pin the endpoint."
        ),
    )

    OIDC_AUTHORIZATION_ENDPOINT: str = Field(
        default="",
        description=(
            "Phase E13: explicit authorization endpoint, bypassing "
            "discovery. Empty = read from the discovery document."
        ),
    )

    OIDC_TOKEN_ENDPOINT: str = Field(
        default="",
        description=(
            "Phase E13: explicit token endpoint, bypassing discovery. "
            "Empty = read from the discovery document."
        ),
    )

    OIDC_DISCOVERY_TIMEOUT_SECONDS: float = Field(
        default=5.0,
        description=(
            "Phase E13 (PHIL-5): timeout for the discovery and token-"
            "exchange HTTP calls to the IdP. A slow IdP must degrade "
            "sign-in, never hang a worker."
        ),
    )

    # --- Tenancy & data classification declaration (Phase E13, Article XVI) ---
    #
    # Article XVI requires both positions to be *stated*, not necessarily
    # to take a particular value: "single-tenant by declaration" is an
    # honest, acceptable answer (PHIL-1). These fields exist so the
    # deployment's answer is machine-readable and surfaced by the platform
    # itself rather than living only in a document that can drift.

    TENANCY_MODEL: str = Field(
        default="single-tenant",
        description=(
            "Phase E13 (Article XVI): this deployment's tenancy position. "
            "'single-tenant' — one organization per deployment; no "
            "tenant discriminator exists in any table, collection, or "
            "cache key, and isolation is achieved by deploying separately. "
            "'multi-tenant' must not be declared unless every store "
            "carries a tenant discriminator (AEAM does not implement one "
            "today, so declaring it would be dishonest)."
        ),
    )

    DATA_CLASSIFICATION: str = Field(
        default="internal",
        description=(
            "Phase E13 (Article XVI): the highest data classification this "
            "deployment's stores are approved to hold — e.g. 'public', "
            "'internal', 'confidential', 'restricted'. Surfaced by "
            "GET /api/v1/system/compliance so an operator can see the "
            "declared posture rather than infer it. See "
            "docs/ENTERPRISE_CERTIFICATION.md for the per-store statement."
        ),
    )

    PII_POSTURE: str = Field(
        default="not-expected",
        description=(
            "Phase E13 (Article XVI): the PII posture for incidents, "
            "documents and memory. 'not-expected' — the platform is fed "
            "operational metrics and runbooks, and no field is designated "
            "to hold personal data; free-text fields (evidence, reviewer "
            "reasons) may incidentally contain it, which is why the "
            "retention posture in docs/persistence_and_retention.md "
            "applies to them. 'contains-pii' declares the opposite and "
            "obliges the operator to the handling described in the "
            "certification pack."
        ),
    )

    # --- Configuration persistence disclosure (Phase E4, PHIL-1 / PHIL-5) ---
    #
    # The Enterprise Configuration Engine (D5) writes to the project .env
    # file. On a durable single-node deployment that survives instance
    # recycle exactly as an operator would expect. On ephemeral compute
    # (Cloud Run), the .env file lives inside the container's writable
    # layer and evaporates on every recycle — the write itself succeeds,
    # but the value is lost. Silently succeeding without disclosing that
    # is dishonest; these two fields let the admin API surface the true
    # posture in every response.

    CONFIG_PERSISTENCE_MODE: str = Field(
        default="durable",
        description=(
            "Phase E4 (PHIL-1): 'durable' when writes to the D5 config "
            ".env file survive instance recycle (single-node deployments; "
            "any environment with a persistent volume mounted at the .env "
            "path). 'ephemeral' when they do not (Cloud Run's writable "
            "layer). The admin API surfaces this so the UI never claims "
            "durability the platform cannot deliver."
        ),
    )

    CONFIG_PERSISTENCE_NOTE: str = Field(
        default="",
        description=(
            "Phase E4 (PHIL-1): optional operator-facing message shown by "
            "the admin API alongside CONFIG_PERSISTENCE_MODE. Free text — "
            "e.g. 'writes evaporate on instance recycle; use env-var "
            "overrides in cloudrun.yaml for durable production changes'."
        ),
    )

    # --- Validators ---

    @field_validator("ENVIRONMENT")
    @classmethod
    def validate_environment(cls, v: str) -> str:
        allowed = {"development", "staging", "production", "test"}
        value = v.lower()
        if value not in allowed:
            raise ValueError(f"ENVIRONMENT must be one of {allowed}. Got: '{v}'")
        return value

    @field_validator("TENANCY_MODEL")
    @classmethod
    def validate_tenancy_model(cls, v: str) -> str:
        """Phase E13 (Article XVI): a tenancy declaration must be one of the
        two positions the platform can actually honour. A free-text answer
        would let a deployment claim isolation it does not implement."""
        allowed = {"single-tenant", "multi-tenant"}
        value = (v or "").strip().lower()
        if value not in allowed:
            raise ValueError(f"TENANCY_MODEL must be one of {allowed}. Got: '{v}'")
        return value