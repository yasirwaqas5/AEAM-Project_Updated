"""E5 — indexes for the audited hot query paths

Revision ID: 0002_hot_path_indexes
Revises: 0001_baseline
Create Date: 2026-07-25

The 2026-07 Engineering Audit flagged two unindexed hot paths that every
console page and the autonomous loop depend on:

1. ``incidents ORDER BY timestamp DESC`` — the read behind
   ``GET /api/v1/incidents/`` (and therefore the Dashboard, Incidents
   page, Investigation picker, Analytics, and the Observability summary,
   all of which consume that same list).

2. ``metrics WHERE metric = ? ORDER BY timestamp DESC`` — the read behind
   ``DatabaseClient.fetch_metric_history()``, which ForecastAgent and the
   Adaptive Detection Engine call per investigation.

Both grow without bound as history accumulates; without an index each
becomes a full table scan plus a sort. These composite/ordered indexes
let both queries be answered from the index directly.

Additive and non-destructive: creating an index changes no row and no
query result — only the plan used to produce it.
"""

from __future__ import annotations

from alembic import op

revision: str = "0002_hot_path_indexes"
down_revision: str | None = "0001_baseline"
branch_labels: str | None = None
depends_on: str | None = None


_INDEXES: tuple[tuple[str, str], ...] = (
    (
        "idx_incidents_timestamp",
        "CREATE INDEX IF NOT EXISTS idx_incidents_timestamp "
        "ON incidents (timestamp DESC);",
    ),
    (
        "idx_metrics_metric_timestamp",
        "CREATE INDEX IF NOT EXISTS idx_metrics_metric_timestamp "
        "ON metrics (metric, timestamp DESC);",
    ),
    # Supports the audit-history query surface (E3 durable sink): auditors
    # filter by principal and time window.
    (
        "idx_audit_logs_timestamp",
        "CREATE INDEX IF NOT EXISTS idx_audit_logs_timestamp "
        "ON audit_logs (timestamp DESC);",
    ),
    (
        "idx_audit_logs_user_id",
        "CREATE INDEX IF NOT EXISTS idx_audit_logs_user_id "
        "ON audit_logs (user_id);",
    ),
    # Supports GET /api/v1/logs/agents (ORDER BY executed_at DESC).
    (
        "idx_action_logs_executed_at",
        "CREATE INDEX IF NOT EXISTS idx_action_logs_executed_at "
        "ON action_logs (executed_at DESC);",
    ),
)


def upgrade() -> None:
    for _name, ddl in _INDEXES:
        op.execute(ddl)


def downgrade() -> None:
    for name, _ddl in reversed(_INDEXES):
        op.execute(f"DROP INDEX IF EXISTS {name};")
