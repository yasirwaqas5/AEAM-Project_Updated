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
    # Phase E9 — Human-in-the-Loop Enforcement.
    "incident_approvals",
    "review_verdicts",
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
    doc_type         TEXT,               -- FORMAT category as detected at upload ('markdown'|'pdf'|...)
    semantic_type    TEXT,               -- Phase E12 (MOD-4/RAG-7): DECLARED semantic type
                                         -- ('runbook'|'incident_report'|'post_mortem'|...), separate
                                         -- from the format above. NULL until declared; retrieval
                                         -- falls back to doc_type so pre-E12 rows are unchanged.
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
    extracted_at      TIMESTAMP,
    embedding         JSONB,              -- Phase E6: stored policy embedding (list[float]); NULL until computed
    embedding_model   TEXT,               -- Phase E6: model id the embedding was produced with (TECH-6 invalidation)
    -- Phase E12 (COMPAT-6): lifecycle status. 'active'|'pending_review'|'retired'.
    -- PolicyRegistry matches ACTIVE policies only; the 'active' default means
    -- every pre-E12 row keeps matching exactly as it did before this phase.
    status            TEXT DEFAULT 'active',
    status_changed_at TIMESTAMP,          -- when status last transitioned
    status_changed_by TEXT,               -- acting principal (SEC-7: curation is attributable)
    status_reason     TEXT                -- why, recorded verbatim
);
"""

# --- Phase E9: Human-in-the-Loop Enforcement -------------------------------
# Two additive tables. Neither alters an existing table, and an incident
# with no approval requirement never gets a row in either — incidents that
# predate this phase therefore read back exactly as they always did
# (COMPAT-1). MEM-2: verdicts are new records ABOUT an incident, never a
# mutation of the incident row.

_INCIDENT_APPROVALS = """
CREATE TABLE IF NOT EXISTS incident_approvals (
    approval_id      TEXT PRIMARY KEY,
    incident_id      TEXT,               -- -> incidents.incident_id (unenforced)
    investigation_id TEXT,               -- Orchestrator's per-investigation id
    event_type       TEXT,
    metric           TEXT,
    severity         TEXT,
    status           TEXT,               -- 'pending'|'approved'|'rejected'
    required_tiers   JSONB,              -- ordered chain, e.g. ["analyst","manager"]
    current_tier     INTEGER,            -- index of the tier whose approval is awaited
    pending_actions  JSONB,              -- [{"step":...,"params":{...}}] withheld, in runbook order
    executed_actions JSONB,              -- step names that returned SUCCESS on approval
    skipped_actions  JSONB,              -- [{"action":...,"reason":...}]
    created_at       TIMESTAMP,
    updated_at       TIMESTAMP
);
"""

_REVIEW_VERDICTS = """
CREATE TABLE IF NOT EXISTS review_verdicts (
    verdict_id         TEXT PRIMARY KEY,
    approval_id        TEXT,             -- -> incident_approvals.approval_id (unenforced)
    incident_id        TEXT,             -- -> incidents.incident_id (unenforced)
    tier               INTEGER,          -- 0-based index into required_tiers
    tier_label         TEXT,             -- the tier's name at the time of the verdict
    verdict            TEXT,             -- 'approved'|'rejected'|'changes_requested'|'escalated'
    reviewer_id        TEXT,             -- acting principal (E3 identity)
    reviewer_roles     JSONB,            -- roles that principal held
    attribution_source TEXT,             -- 'jwt'|'request'|'unattributed'
    note               TEXT,
    created_at         TIMESTAMP
);
"""

_DDL: tuple[str, ...] = (
    _SOURCES, _DOCUMENTS, _DATASETS, _SCHEMAS, _VERSIONS, _INGESTION_JOBS, _POLICIES,
    _INCIDENT_APPROVALS, _REVIEW_VERDICTS,
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
    # Phase E9 — the review queue reads by status, everything else by incident.
    "CREATE INDEX IF NOT EXISTS idx_approvals_incident ON incident_approvals (incident_id);",
    "CREATE INDEX IF NOT EXISTS idx_approvals_status ON incident_approvals (status);",
    "CREATE INDEX IF NOT EXISTS idx_verdicts_incident ON review_verdicts (incident_id);",
    "CREATE INDEX IF NOT EXISTS idx_verdicts_approval ON review_verdicts (approval_id);",
)


def _ensure_policy_embedding_columns(conn) -> None:
    """
    Dev-only idempotent add of the Phase E6 ``embedding`` /
    ``embedding_model`` columns to an EXISTING ``policies`` table.

    A fresh database gets these columns straight from ``_POLICIES``'s
    ``CREATE TABLE`` above. But a developer whose ``policies`` table was
    created before E6 needs them added in place — the production path for
    that is migration ``0003_policy_embedding``; this keeps the dev-only
    startup path in lock-step (COMPAT-2). Never drops or alters existing
    columns; a failure here is logged and swallowed so a locked/edge-case
    DB never blocks startup (the migration remains the authoritative path).
    """
    existing = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(policies)")} \
        if conn.dialect.name == "sqlite" else None

    for column, coltype in (("embedding", "JSONB"), ("embedding_model", "TEXT")):
        try:
            if conn.dialect.name == "sqlite":
                if existing is not None and column not in existing:
                    conn.exec_driver_sql(f"ALTER TABLE policies ADD COLUMN {column} {coltype}")
            else:
                # PostgreSQL supports IF NOT EXISTS natively.
                conn.exec_driver_sql(
                    f"ALTER TABLE policies ADD COLUMN IF NOT EXISTS {column} {coltype}"
                )
        except SQLAlchemyError as exc:
            logger.warning(
                "policies.%s add skipped (already present or unsupported): %s",
                column, exc,
            )


def create_enterprise_tables(engine: Engine) -> None:
    """
    Create the Enterprise Data Layer registry tables and indexes if absent.

    Idempotent and additive: safe to call on every startup, creates nothing
    that already exists, and never touches the pre-existing AEAM tables.

    **Phase E5:** this is a dev-only convenience path kept in lock-step
    with the Alembic baseline (migration ``0001_baseline``); production
    schema is managed by ``alembic upgrade head``.

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
            # Phase E6: backfill the embedding columns onto an existing
            # policies table (fresh tables already have them from _POLICIES).
            _ensure_policy_embedding_columns(conn)
        logger.info(
            "Enterprise Data Layer schema verified/created | tables=%d | indexes=%d",
            len(ENTERPRISE_TABLES), len(_INDEXES),
        )
    except SQLAlchemyError as exc:
        logger.error("Enterprise table creation failed: %s", exc)
        raise
