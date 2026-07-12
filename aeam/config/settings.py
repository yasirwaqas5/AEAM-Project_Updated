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

    # --- Validators ---

    @field_validator("ENVIRONMENT")
    @classmethod
    def validate_environment(cls, v: str) -> str:
        allowed = {"development", "staging", "production", "test"}
        value = v.lower()
        if value not in allowed:
            raise ValueError(f"ENVIRONMENT must be one of {allowed}. Got: '{v}'")
        return value