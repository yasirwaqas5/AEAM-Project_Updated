"""F7 — connector sync state, provenance, and run history

Revision ID: 0010_connector_framework
Revises: 0009_business_graph
Create Date: 2026-07-30

Phase F7 lets organizational knowledge and metrics flow in from the systems
an enterprise already runs, instead of requiring manual export and upload.
Two additive tables carry the state that makes that safe and honest.

``connector_artifacts`` — one row per upstream artifact per connector, keyed
on ``(source_id, external_id)`` with a UNIQUE index. It serves two needs
that are really one set of facts viewed twice:

* **incremental sync** — ``source_content_hash`` / ``source_timestamp`` /
  ``source_version`` are compared against what the connector reports now, so
  an unchanged artifact is skipped before anything is downloaded, chunked,
  embedded, or indexed;
* **provenance** — which connector, which upstream id, what upstream calls
  its type, the URL an operator can open, when we synced it, when upstream
  last changed it, and which local Document/Dataset it became.

The UNIQUE index is load-bearing rather than tidy: two rows for one upstream
artifact would split its sync state, and a repeated sync would then
re-ingest content it already had — the exact duplicate this table exists to
prevent.

``connector_sync_runs`` — one row per synchronization run, with the four
counts that make connector health honest (listed / changed / processed /
skipped / failed) plus the measured duration and a sanitised error reason.

Deliberately a separate ledger from ``ingestion_jobs`` rather than a new
``job_type`` row in it. ``IngestionWorker`` claims every QUEUED job it
finds, so a sync recorded as a job would run connector work on the ingestion
worker's thread — and a connector hanging on a slow upstream call would then
block document ingestion for everything else. Failure isolation is the
requirement; a separate ledger is what buys it.

Both tables are purely additive and inert on arrival: a deployment with no
connectors configured has no rows in either, and the upload + Google Sheets
posture is unchanged (COMPAT-4). No credential is ever stored here —
connector credentials are resolved through ``SecretManager`` at sync time and
``sources.secret_ref`` holds only a pointer (SEC-5).

The DDL is INLINED rather than imported from
``aeam.integrations.enterprise_schema`` for the same reason every prior
revision inlines it: a migration is a frozen historical artifact.
``test_phase_e5_migrations.py``'s drift guard keeps it in lock-step with the
startup DDL path.
"""

from __future__ import annotations

from alembic import op

revision: str = "0010_connector_framework"
down_revision: str | None = "0009_business_graph"
branch_labels: str | None = None
depends_on: str | None = None


_CREATE_ARTIFACTS: str = """
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

_CREATE_RUNS: str = """
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

_INDEXES: tuple[tuple[str, str], ...] = (
    (
        "idx_connector_artifacts_external",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_connector_artifacts_external "
        "ON connector_artifacts (source_id, external_id);",
    ),
    (
        "idx_connector_artifacts_parent",
        "CREATE INDEX IF NOT EXISTS idx_connector_artifacts_parent "
        "ON connector_artifacts (parent_type, parent_id);",
    ),
    (
        "idx_connector_sync_runs_source",
        "CREATE INDEX IF NOT EXISTS idx_connector_sync_runs_source "
        "ON connector_sync_runs (source_id, started_at);",
    ),
    (
        "idx_connector_sync_runs_status",
        "CREATE INDEX IF NOT EXISTS idx_connector_sync_runs_status "
        "ON connector_sync_runs (source_id, status, started_at);",
    ),
)


def upgrade() -> None:
    op.execute(_CREATE_ARTIFACTS)
    op.execute(_CREATE_RUNS)
    for _name, ddl in _INDEXES:
        op.execute(ddl)


def downgrade() -> None:
    for name, _ddl in _INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {name};")
    op.execute("DROP TABLE IF EXISTS connector_sync_runs;")
    op.execute("DROP TABLE IF EXISTS connector_artifacts;")
