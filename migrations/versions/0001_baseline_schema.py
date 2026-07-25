"""E5 baseline — snapshot of the pre-migration AEAM schema

Revision ID: 0001_baseline
Revises:
Create Date: 2026-07-25

This revision is the migration-era baseline: it reproduces EXACTLY the
schema that ``DatabaseClient._create_tables_if_not_exist()`` plus
``create_enterprise_tables()`` produced before Phase E5 existed. A fresh
database built purely from migrations is therefore schema-identical to
one built by the legacy startup DDL path — asserted by
``aeam/tests/test_phase_e5_migrations.py::test_migrated_schema_matches_startup_ddl``.

The DDL is INLINED here rather than imported from the application
modules on purpose. A migration is a frozen historical artifact: if it
imported live application code, editing that code later would
retroactively change what this revision does, destroying the very
guarantee migrations exist to provide. The drift test above is what
keeps the two in sync going forward.

Every statement is ``IF NOT EXISTS``, so this revision can be stamped
onto an EXISTING populated database (``alembic stamp 0001_baseline``)
or applied to an empty one, with the same result either way.
"""

from __future__ import annotations

from alembic import op

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


# --- Core AEAM tables (aeam/integrations/database.py) ----------------------

_CORE_TABLES: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS incidents (
        incident_id TEXT PRIMARY KEY,
        event_id TEXT,
        event_type TEXT,
        metric TEXT,
        severity TEXT,
        current_value REAL,
        expected_value REAL,
        detection_methods TEXT,
        timestamp TEXT,
        investigation_depth INTEGER,
        root_cause TEXT,
        confidence REAL,
        action_taken BOOLEAN,
        requires_human BOOLEAN,
        findings TEXT,
        llm_response TEXT
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS decisions (
        incident_id TEXT,
        decision TEXT,
        confidence REAL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS metrics (
        metric TEXT,
        value REAL,
        timestamp TEXT
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS action_logs (
        action_id TEXT PRIMARY KEY,
        incident_id TEXT,
        action_type TEXT,
        parameters JSONB,
        status TEXT,
        result JSONB,
        executed_at TIMESTAMP
    );
    """,
    # Phase E3 durable audit sink.
    """
    CREATE TABLE IF NOT EXISTS audit_logs (
        entry_id TEXT PRIMARY KEY,
        timestamp TIMESTAMP,
        user_id TEXT,
        action TEXT,
        endpoint TEXT,
        status_code INTEGER,
        hash TEXT,
        extra JSONB
    );
    """,
)

# --- Enterprise Data Layer tables (aeam/integrations/enterprise_schema.py) --

_ENTERPRISE_TABLES: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS sources (
        source_id        TEXT PRIMARY KEY,
        name             TEXT,
        kind             TEXT,
        config           JSONB,
        secret_ref       TEXT,
        status           TEXT,
        sync_schedule    TEXT,
        last_synced_at   TIMESTAMP,
        created_at       TIMESTAMP,
        created_by       TEXT
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS documents (
        doc_id           TEXT PRIMARY KEY,
        source_id        TEXT,
        title            TEXT,
        origin_path      TEXT,
        doc_type         TEXT,
        current_version  INTEGER,
        content_hash     TEXT,
        chunk_count      INTEGER,
        status           TEXT,
        review_by        DATE,
        language         TEXT,
        created_at       TIMESTAMP,
        updated_at       TIMESTAMP
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS datasets (
        dataset_id       TEXT PRIMARY KEY,
        source_id        TEXT,
        name             TEXT,
        schema_id        TEXT,
        row_count        BIGINT,
        metric_columns   JSONB,
        refresh_schedule TEXT,
        status           TEXT,
        last_ingested_at TIMESTAMP,
        created_at       TIMESTAMP
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS schemas (
        schema_id        TEXT PRIMARY KEY,
        source_id        TEXT,
        object_name      TEXT,
        columns          JSONB,
        relationships    JSONB,
        discovered_at    TIMESTAMP
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS versions (
        version_id       TEXT PRIMARY KEY,
        parent_type      TEXT,
        parent_id        TEXT,
        version          INTEGER,
        content_hash     TEXT,
        blob_ref         TEXT,
        chunk_ids        JSONB,
        created_at       TIMESTAMP,
        created_by       TEXT,
        is_active        BOOLEAN
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS ingestion_jobs (
        job_id           TEXT PRIMARY KEY,
        source_id        TEXT,
        parent_type      TEXT,
        parent_id        TEXT,
        job_type         TEXT,
        status           TEXT,
        progress         INTEGER,
        stage            TEXT,
        error            TEXT,
        content_hash     TEXT,
        created_at       TIMESTAMP,
        updated_at       TIMESTAMP
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS policies (
        policy_id         TEXT PRIMARY KEY,
        doc_id            TEXT,
        source_document   TEXT,
        source_chunk      TEXT,
        raw_text          TEXT,
        business_rule     TEXT,
        condition         TEXT,
        threshold         TEXT,
        actions           JSONB,
        escalation_rule   TEXT,
        approval_required BOOLEAN,
        department        TEXT,
        role              TEXT,
        time_constraint   TEXT,
        priority          TEXT,
        related_metrics   JSONB,
        extracted_at      TIMESTAMP
    );
    """,
)

_ENTERPRISE_INDEXES: tuple[str, ...] = (
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

# Tables this baseline owns, newest-dependency-last so downgrade can
# drop them in reverse without tripping over (unenforced) references.
_ALL_TABLE_NAMES: tuple[str, ...] = (
    "incidents", "decisions", "metrics", "action_logs", "audit_logs",
    "sources", "documents", "datasets", "schemas", "versions",
    "ingestion_jobs", "policies",
)

_ALL_INDEX_NAMES: tuple[str, ...] = (
    "idx_documents_content_hash", "idx_documents_source", "idx_documents_status",
    "idx_datasets_source", "idx_schemas_source", "idx_versions_parent",
    "idx_versions_content_hash", "idx_jobs_status", "idx_jobs_content_hash",
    "idx_policies_doc",
)


def upgrade() -> None:
    for ddl in _CORE_TABLES:
        op.execute(ddl)
    for ddl in _ENTERPRISE_TABLES:
        op.execute(ddl)
    for idx in _ENTERPRISE_INDEXES:
        op.execute(idx)


def downgrade() -> None:
    """
    Drop everything this baseline created.

    Destructive by definition — this is the bottom of the revision graph,
    so downgrading past it empties the schema. Guarded with IF EXISTS so
    a partially-applied state downgrades cleanly.
    """
    for index_name in _ALL_INDEX_NAMES:
        op.execute(f"DROP INDEX IF EXISTS {index_name};")
    for table_name in reversed(_ALL_TABLE_NAMES):
        op.execute(f"DROP TABLE IF EXISTS {table_name};")
