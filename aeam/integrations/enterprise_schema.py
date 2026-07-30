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
    # Phase F4 — Correlation Intelligence & Business Graph.
    "graph_nodes",
    "graph_edges",
    # Phase F7 — Enterprise Connector Framework.
    "connector_artifacts",
    "connector_sync_runs",
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

_FORECAST_BACKTESTS = """
CREATE TABLE IF NOT EXISTS forecast_backtests (
    backtest_id      TEXT PRIMARY KEY,
    metric           TEXT NOT NULL,
    selected_model   TEXT,             -- winning candidate name
    holdout_mape     DOUBLE PRECISION, -- percent; NULL when unmeasurable
    holdout_mae      DOUBLE PRECISION, -- metric's own units
    holdout_points   INTEGER,          -- points actually scored
    training_rows    INTEGER,
    refused          BOOLEAN DEFAULT FALSE,  -- TECH-6: model denied the right to serve
    reason           TEXT,
    created_at       TIMESTAMP
);
"""

_CALIBRATION_MODELS = """
CREATE TABLE IF NOT EXISTS calibration_models (
    calibration_id    TEXT PRIMARY KEY,
    version           INTEGER NOT NULL,
    status            TEXT DEFAULT 'active',   -- 'active' | 'superseded'
    knots             JSONB,                   -- [[x, y], ...] isotonic mapping
    training_samples  INTEGER,
    holdout_samples   INTEGER,
    ece_before        DOUBLE PRECISION,        -- held-out, pre-calibration
    ece_after         DOUBLE PRECISION,        -- held-out, post-calibration
    brier_before      DOUBLE PRECISION,
    brier_after       DOUBLE PRECISION,
    curve_before      JSONB,                   -- reliability diagram buckets
    curve_after       JSONB,
    skipped_counts    JSONB,                   -- why records were excluded
    source_window     TEXT,
    created_by        TEXT,
    reason            TEXT,
    created_at        TIMESTAMP,
    superseded_at     TIMESTAMP
);
"""

_LEARNING_PROPOSALS = """
CREATE TABLE IF NOT EXISTS learning_proposals (
    proposal_id        TEXT PRIMARY KEY,
    proposal_type      TEXT,   -- e.g. 'automation_threshold'
    subject            TEXT,   -- the setting the proposal concerns
    current_value      TEXT,
    proposed_value     TEXT,
    rationale          TEXT,
    evidence           JSONB,  -- the measurement that motivated it
    status             TEXT DEFAULT 'pending',  -- 'pending'|'approved'|'rejected'
    reviewer_id        TEXT,   -- acting principal (E3 identity)
    reviewer_roles     JSONB,
    attribution_source TEXT,   -- 'jwt'|'request'|'unattributed'
    note               TEXT,
    created_at         TIMESTAMP,
    decided_at         TIMESTAMP
);
"""

_COMPILED_RULES = """
CREATE TABLE IF NOT EXISTS compiled_rules (
    rule_id             TEXT PRIMARY KEY,
    policy_id           TEXT,
    domain              TEXT NOT NULL,
    rule_key            TEXT NOT NULL,
    comparison          TEXT,
    value               DOUBLE PRECISION,
    rationale           TEXT,
    status              TEXT DEFAULT 'proposed',  -- 'proposed'|'approved'|'rejected'|'retired'
    created_at          TIMESTAMP,
    created_by          TEXT,
    reviewer_id         TEXT,
    reviewer_roles      JSONB,
    attribution_source  TEXT,
    note                TEXT,
    decided_at          TIMESTAMP,
    retired_at          TIMESTAMP,
    retired_by          TEXT,
    retired_reason      TEXT
);
"""

_GRAPH_NODES = """
CREATE TABLE IF NOT EXISTS graph_nodes (
    node_id          TEXT PRIMARY KEY,   -- deterministic UUID5 of node_key
    node_key         TEXT NOT NULL,      -- natural key, e.g. 'metric:sales'
    node_type        TEXT NOT NULL,      -- 'metric'|'dataset'|'service'|'policy'|'incident'
    label            TEXT,
    attributes       JSONB,              -- descriptive only; never derives an edge
    evidence_source  TEXT,               -- which registry/record produced this node
    first_seen_at    TIMESTAMP,
    last_seen_at     TIMESTAMP,
    build_id         TEXT
);
"""

_GRAPH_EDGES = """
CREATE TABLE IF NOT EXISTS graph_edges (
    edge_id            TEXT PRIMARY KEY, -- deterministic UUID5 of the natural key
    source_key         TEXT NOT NULL,    -- -> graph_nodes.node_key
    target_key         TEXT NOT NULL,    -- -> graph_nodes.node_key
    edge_type          TEXT NOT NULL,    -- 'correlates_with'|'governed_by'|'derived_from'|'co_occurred_in_incident'
    confidence         DOUBLE PRECISION,
    observation_count  INTEGER,
    evidence           JSONB,            -- pointers an operator follows to check the claim
    evidence_source    TEXT,
    first_seen_at      TIMESTAMP,
    last_seen_at       TIMESTAMP,
    build_id           TEXT
);
"""

_CONNECTOR_ARTIFACTS = """
CREATE TABLE IF NOT EXISTS connector_artifacts (
    artifact_id          TEXT PRIMARY KEY,
    source_id            TEXT NOT NULL,   -- -> sources.source_id
    external_id          TEXT NOT NULL,   -- the upstream system's own id
    connector            TEXT,            -- denormalised sources.kind
    source_type          TEXT,            -- upstream's own type, verbatim
    title                TEXT,
    source_url           TEXT,
    source_timestamp     TIMESTAMP,       -- upstream last-modified, when exposed
    source_version       TEXT,            -- upstream revision, when exposed
    source_content_hash  TEXT,            -- change signal for incremental sync
    semantic_type        TEXT,            -- E12 declared doc type, when known
    parent_type          TEXT,            -- 'document' | 'dataset'
    parent_id            TEXT,
    last_job_id          TEXT,            -- -> ingestion_jobs.job_id
    first_synced_at      TIMESTAMP,
    last_synced_at       TIMESTAMP,
    skip_count           INTEGER DEFAULT 0,
    ingest_count         INTEGER DEFAULT 0,
    extra                JSONB
);
"""

_CONNECTOR_SYNC_RUNS = """
CREATE TABLE IF NOT EXISTS connector_sync_runs (
    run_id            TEXT PRIMARY KEY,
    source_id         TEXT NOT NULL,      -- -> sources.source_id
    connector         TEXT,
    status            TEXT DEFAULT 'running',  -- 'running'|'succeeded'|'partial'|'failed'
    started_at        TIMESTAMP,
    finished_at       TIMESTAMP,
    duration_seconds  DOUBLE PRECISION,
    listed_count      INTEGER DEFAULT 0,
    changed_count     INTEGER DEFAULT 0,
    processed_count   INTEGER DEFAULT 0,
    skipped_count     INTEGER DEFAULT 0,
    failed_count      INTEGER DEFAULT 0,
    error             TEXT,               -- sanitised; never carries a secret
    cursor_from       TIMESTAMP,
    cursor_to         TIMESTAMP,
    triggered_by      TEXT,
    extra             JSONB
);
"""

_DDL: tuple[str, ...] = (
    _SOURCES, _DOCUMENTS, _DATASETS, _SCHEMAS, _VERSIONS, _INGESTION_JOBS, _POLICIES,
    _INCIDENT_APPROVALS, _REVIEW_VERDICTS,
    # Phase F1 — forecast model quality tracking (TECH-6, OBS-2).
    _FORECAST_BACKTESTS,
    # Phase F2 — versioned calibration state and advisory learning
    # proposals (AGENT-5: proposals never self-apply).
    _CALIBRATION_MODELS, _LEARNING_PROPOSALS,
    # Phase F3 — compiled rule proposals (AGENT-5/SEC-7: never enforced
    # without a recorded human approval).
    _COMPILED_RULES,
    # Phase F4 — the business graph (advisory evidence source; inert until
    # an operator runs a build).
    _GRAPH_NODES, _GRAPH_EDGES,
    # Phase F7 — connector sync state/provenance and run history. Inert with
    # no connectors configured (COMPAT-4).
    _CONNECTOR_ARTIFACTS, _CONNECTOR_SYNC_RUNS,
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
    # Phase F1 — "this metric's forecast quality over time", and "which
    # models were refused" (the review after a metric stops forecasting).
    "CREATE INDEX IF NOT EXISTS idx_forecast_backtests_metric ON forecast_backtests (metric, created_at);",
    "CREATE INDEX IF NOT EXISTS idx_forecast_backtests_refused ON forecast_backtests (refused);",
    # Phase F2 — "which calibration is live?" runs on every finalize when
    # calibration is enabled; the other two serve rollback and governance.
    "CREATE INDEX IF NOT EXISTS idx_calibration_models_status ON calibration_models (status, version);",
    "CREATE INDEX IF NOT EXISTS idx_calibration_models_version ON calibration_models (version);",
    "CREATE INDEX IF NOT EXISTS idx_learning_proposals_status ON learning_proposals (status, created_at);",
    # Phase F3 — "which rules are adopted?" (startup override load), "every
    # rule proposed from this policy", and "is this domain+key already
    # colliding?" (checked at propose-time, before the corpus-wide report).
    "CREATE INDEX IF NOT EXISTS idx_compiled_rules_status ON compiled_rules (status);",
    "CREATE INDEX IF NOT EXISTS idx_compiled_rules_policy ON compiled_rules (policy_id);",
    "CREATE INDEX IF NOT EXISTS idx_compiled_rules_domain_key ON compiled_rules (domain, rule_key, status);",
    # Phase F4 — the business graph. node_key is UNIQUE because it is the
    # natural key every edge references: two rows claiming to be
    # 'metric:sales' would silently split the graph in half.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_graph_nodes_key ON graph_nodes (node_key);",
    "CREATE INDEX IF NOT EXISTS idx_graph_nodes_type ON graph_nodes (node_type);",
    # The two hot traversal reads: "edges out of this frontier" and "edges
    # into it". Confidence is in the index so the ORDER BY that makes a
    # truncated traversal deterministic does not force a full sort.
    "CREATE INDEX IF NOT EXISTS idx_graph_edges_source ON graph_edges (source_key, confidence);",
    "CREATE INDEX IF NOT EXISTS idx_graph_edges_target ON graph_edges (target_key, confidence);",
    "CREATE INDEX IF NOT EXISTS idx_graph_edges_type ON graph_edges (edge_type);",
    # The mark-and-sweep that retires edges whose evidence disappeared.
    "CREATE INDEX IF NOT EXISTS idx_graph_edges_last_seen ON graph_edges (last_seen_at);",
    "CREATE INDEX IF NOT EXISTS idx_graph_nodes_last_seen ON graph_nodes (last_seen_at);",
    # Phase F7 — the hot incremental-sync read: "have we seen this upstream
    # artifact before?", executed once per listed artifact per sync. UNIQUE
    # because two rows for one upstream artifact would split its sync state
    # and let a repeated sync re-ingest content it already had.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_connector_artifacts_external "
    "ON connector_artifacts (source_id, external_id);",
    "CREATE INDEX IF NOT EXISTS idx_connector_artifacts_parent "
    "ON connector_artifacts (parent_type, parent_id);",
    # Connector health reads the most recent run, and the most recent run in a
    # given status ("last successful" vs "last failed" are separate facts).
    "CREATE INDEX IF NOT EXISTS idx_connector_sync_runs_source "
    "ON connector_sync_runs (source_id, started_at);",
    "CREATE INDEX IF NOT EXISTS idx_connector_sync_runs_status "
    "ON connector_sync_runs (source_id, status, started_at);",
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
