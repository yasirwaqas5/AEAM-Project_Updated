"""
aeam/integrations/enterprise_schema.py

Additive DDL for the Enterprise Data Layer (Phase B1.1 — Storage Foundation).

Declares the six new registry tables that later B1 ingestion phases will
reuse. This module ONLY defines schema — no business logic, no ingestion, no
ORM. It never references or alters the existing incidents / decisions /
metrics / action_logs tables.

All statements are idempotent (``CREATE TABLE IF NOT EXISTS`` /
``CREATE INDEX IF NOT EXISTS``), matching the existing
``DatabaseClient._create_tables_if_not_exist()`` convention. Column types are
chosen to work on both PostgreSQL (production) and SQLite (tests): ``JSONB`` /
``TIMESTAMP`` / ``DATE`` / ``BIGINT`` degrade to type affinity under SQLite,
exactly like the existing ``action_logs`` table.

Foreign-key relationships are documented in comments rather than enforced as
constraints — this mirrors the existing schema style (``decisions.incident_id``
carries no FK constraint) and keeps the DDL portable and insertion-order-free.
"""

from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

logger = logging.getLogger(__name__)

# Public list of the tables this module owns (for diagnostics / verification).
ENTERPRISE_TABLES: tuple[str, ...] = (
    "sources",
    "documents",
    "datasets",
    "schemas",
    "versions",
    "ingestion_jobs",
    "policies",
)

# ---------------------------------------------------------------------------
# Table DDL — one CREATE per registry. Columns mirror the approved B1 blueprint
# (Task 7). Each column is annotated inline.
# ---------------------------------------------------------------------------

_SOURCES = """
CREATE TABLE IF NOT EXISTS sources (
    source_id        TEXT PRIMARY KEY,   -- uuid
    name             TEXT,               -- operator-facing label
    kind             TEXT,               -- 'upload'|'confluence'|'sharepoint'|'s3'|'azure_blob'|'database'|'rest'|'gsheet'
    config           JSONB,              -- non-secret connection params
    secret_ref       TEXT,               -- pointer to a secret; NEVER the secret itself
    status           TEXT,               -- 'active'|'error'|'disabled'
    sync_schedule    TEXT,               -- cron/interval, or NULL for manual
    last_synced_at   TIMESTAMP,
    created_at       TIMESTAMP,
    created_by       TEXT
);
"""

_DOCUMENTS = """
CREATE TABLE IF NOT EXISTS documents (
    doc_id           TEXT PRIMARY KEY,
    source_id        TEXT,               -- -> sources.source_id (unenforced)
    title            TEXT,
    origin_path      TEXT,               -- filename / page URL of origin
    doc_type         TEXT,               -- 'runbook'|'incident_report'|'wiki'|'api_doc'|...
    current_version  INTEGER,            -- -> versions.version (active)
    content_hash     TEXT,               -- hash of active version bytes (idempotent re-ingest)
    chunk_count      INTEGER,            -- chunks currently in Qdrant for active version
    status           TEXT,               -- 'pending'|'processing'|'indexed'|'stale'|'archived'|'deleted'|'error'
    review_by        DATE,               -- freshness / staleness driver
    language         TEXT,
    created_at       TIMESTAMP,
    updated_at       TIMESTAMP
);
"""

_DATASETS = """
CREATE TABLE IF NOT EXISTS datasets (
    dataset_id       TEXT PRIMARY KEY,
    source_id        TEXT,               -- -> sources.source_id (unenforced)
    name             TEXT,
    schema_id        TEXT,               -- -> schemas.schema_id (unenforced)
    row_count        BIGINT,
    metric_columns   JSONB,              -- column names flagged as monitored metrics
    refresh_schedule TEXT,
    status           TEXT,               -- same lifecycle vocab as documents
    last_ingested_at TIMESTAMP,
    created_at       TIMESTAMP
);
"""

_SCHEMAS = """
CREATE TABLE IF NOT EXISTS schemas (
    schema_id        TEXT PRIMARY KEY,
    source_id        TEXT,               -- -> sources.source_id (unenforced)
    object_name      TEXT,               -- table / sheet / file name
    columns          JSONB,              -- [{name,type,nullable,is_metric,role}]
    relationships    JSONB,              -- inferred FK / join hints
    discovered_at    TIMESTAMP
);
"""

_VERSIONS = """
CREATE TABLE IF NOT EXISTS versions (
    version_id       TEXT PRIMARY KEY,
    parent_type      TEXT,               -- 'document'|'dataset'
    parent_id        TEXT,               -- doc_id or dataset_id (unenforced)
    version          INTEGER,            -- monotonic per parent
    content_hash     TEXT,               -- bytes hash -> dedup / supersede-and-delete key
    blob_ref         TEXT,               -- BlobStore URI of the original
    chunk_ids        JSONB,              -- chunk_ids in Qdrant for this version (clean delete)
    created_at       TIMESTAMP,
    created_by       TEXT,
    is_active        BOOLEAN             -- exactly one active version per parent
);
"""

_INGESTION_JOBS = """
CREATE TABLE IF NOT EXISTS ingestion_jobs (
    job_id           TEXT PRIMARY KEY,
    source_id        TEXT,               -- -> sources.source_id (unenforced)
    parent_type      TEXT,               -- 'document'|'dataset'|'source_sync'
    parent_id        TEXT,               -- nullable until registered
    job_type         TEXT,               -- 'ingest'|'reindex'|'delete'|'sync'
    status           TEXT,               -- 'queued'|'validating'|'extracting'|'indexing'|'done'|'failed'
    progress         INTEGER,            -- 0-100
    stage            TEXT,               -- current pipeline stage name
    error            TEXT,               -- structured failure reason (NULL on success)
    content_hash     TEXT,               -- idempotency: skip if already indexed
    created_at       TIMESTAMP,
    updated_at       TIMESTAMP
);
"""

_POLICIES = """
CREATE TABLE IF NOT EXISTS policies (
    policy_id         TEXT PRIMARY KEY,
    doc_id            TEXT,               -- -> documents.doc_id (unenforced)
    source_document   TEXT,               -- human-readable title/origin_path
    source_chunk      TEXT,               -- chunk_id within the document, if attributable
    raw_text          TEXT,               -- verbatim source sentence(s) this policy is based on
    business_rule     TEXT,               -- short human-readable summary
    condition         TEXT,
    threshold         TEXT,
    actions           JSONB,              -- list of action strings
    escalation_rule   TEXT,
    approval_required BOOLEAN,
    department        TEXT,
    role              TEXT,
    time_constraint   TEXT,
    priority          TEXT,
    related_metrics   JSONB,              -- list of metric name strings
    extracted_at      TIMESTAMP
);
"""

_DDL: tuple[str, ...] = (
    _SOURCES, _DOCUMENTS, _DATASETS, _SCHEMAS, _VERSIONS, _INGESTION_JOBS, _POLICIES,
)

# Helpful lookup indexes for the query patterns later phases rely on.
_INDEXES: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_documents_content_hash ON documents (content_hash);",
    "CREATE INDEX IF NOT EXISTS idx_documents_source ON documents (source_id);",
    "CREATE INDEX IF NOT EXISTS idx_documents_status ON documents (status);",
    "CREATE INDEX IF NOT EXISTS idx_datasets_source ON datasets (source_id);",
    "CREATE INDEX IF NOT EXISTS idx_schemas_source ON schemas (source_id);",
    "CREATE INDEX IF NOT EXISTS idx_versions_parent ON versions (parent_type, parent_id);",
    "CREATE INDEX IF NOT EXISTS idx_versions_content_hash ON versions (content_hash);",
    "CREATE INDEX IF NOT EXISTS idx_jobs_status ON ingestion_jobs (status);",
    "CREATE INDEX IF NOT EXISTS idx_jobs_content_hash ON ingestion_jobs (content_hash);",
    "CREATE INDEX IF NOT EXISTS idx_policies_doc ON policies (doc_id);",
)


def create_enterprise_tables(engine: Engine) -> None:
    """
    Create the Enterprise Data Layer registry tables and indexes if absent.

    Idempotent and additive: safe to call on every startup, creates nothing
    that already exists, and never touches the pre-existing AEAM tables.

    Args:
        engine: The shared SQLAlchemy engine from
                :class:`~aeam.integrations.database.DatabaseClient`.

    Raises:
        SQLAlchemyError: If DDL execution fails (propagated so a broken schema
                         surfaces at startup rather than silently).
    """
    try:
        with engine.begin() as conn:
            for ddl in _DDL:
                conn.execute(text(ddl))
            for idx in _INDEXES:
                conn.execute(text(idx))
        logger.info(
            "Enterprise Data Layer schema verified/created | tables=%d | indexes=%d",
            len(ENTERPRISE_TABLES), len(_INDEXES),
        )
    except SQLAlchemyError as exc:
        logger.error("Enterprise table creation failed: %s", exc)
        raise
