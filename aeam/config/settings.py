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
        description="Feature flag to enable or disable the MonitorAgent.",
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
        default="gemini",
        description="Which LLM backend to use: 'gemini', 'openai', etc.",
    )

    LLM_API_KEY: str = Field(
        default="",
        description="API key for the LLM provider (loaded from .env).",
    )

    USE_MOCK_LLM: bool = Field(
        default=True,
        description="When True, LLM calls return mock responses for tests/offline use.",
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
            "Phase D4: caps how many most-recent incidents "
            "GET /api/v1/observability/ considers when building its summary. "
            "A read-time windowing cap only -- never deletes or alters any "
            "persisted incident row. None = unbounded (current behavior)."
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