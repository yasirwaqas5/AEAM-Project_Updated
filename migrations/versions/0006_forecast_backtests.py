"""F1 — forecast backtest results

Revision ID: 0006_forecast_backtests
Revises: 0005_knowledge_governance
Create Date: 2026-07-28

Phase F1 gives forecast models a quality gate (TECH-6): a newly trained
model is measured against a holdout it never saw before it is allowed to
serve, and refused when its error exceeds the configured ceiling. This
table is where those measurements live.

Why persist them at all — a gate that leaves no trace can only answer
"is this model good enough right now". The questions an operator actually
asks are longitudinal: *is this metric's forecast getting worse? did the
last retrain help? which metrics have never been measured?* None of those
are answerable from a log line that rotated away, and OBS-2 requires a
published quality number to state the window it was measured over.

Purely additive:

* One new table. Nothing existing is altered, and no code path requires it
  — ``ForecastAgent`` writes here only when a database client is wired AND
  ``FORECAST_BACKTEST_ENABLED`` is set. With the flag off (the default),
  this table stays empty and inert, which is the F1 rollback posture.
* The write is best-effort: a failure to record a measurement never breaks
  the forecast path being measured (same contract as the E3 audit sink).

The DDL is INLINED rather than imported from
``aeam.integrations.enterprise_schema`` for the same reason every prior
revision inlines it: a migration is a frozen historical artifact.
``test_phase_e5_migrations.py``'s drift guard keeps it in lock-step with
the startup DDL path.
"""

from __future__ import annotations

from alembic import op

revision: str = "0006_forecast_backtests"
down_revision: str | None = "0005_knowledge_governance"
branch_labels: str | None = None
depends_on: str | None = None


# TEXT primary key rather than an autoincrementing integer, matching every
# other table in this schema (incidents, policies, documents): identifiers
# are application-generated UUIDs so they are stable across a restore into
# a different database (see docs/DISASTER_RECOVERY.md).
_CREATE_TABLE: str = """
CREATE TABLE IF NOT EXISTS forecast_backtests (
    backtest_id      TEXT PRIMARY KEY,
    metric           TEXT NOT NULL,
    selected_model   TEXT,
    holdout_mape     DOUBLE PRECISION,
    holdout_mae      DOUBLE PRECISION,
    holdout_points   INTEGER,
    training_rows    INTEGER,
    refused          BOOLEAN DEFAULT FALSE,
    reason           TEXT,
    created_at       TIMESTAMP
);
"""

# SQLite has no DOUBLE PRECISION keyword but accepts it via type affinity
# (it resolves to REAL), so one DDL string serves both dialects — the same
# approach the baseline revision already relies on.

_INDEXES: tuple[tuple[str, str], ...] = (
    # The dominant query is "this metric's quality over time".
    (
        "idx_forecast_backtests_metric",
        "CREATE INDEX IF NOT EXISTS idx_forecast_backtests_metric "
        "ON forecast_backtests (metric, created_at);",
    ),
    # "Which models were refused?" — the review an operator runs after a
    # metric stops producing forecasts.
    (
        "idx_forecast_backtests_refused",
        "CREATE INDEX IF NOT EXISTS idx_forecast_backtests_refused "
        "ON forecast_backtests (refused);",
    ),
)


def upgrade() -> None:
    op.execute(_CREATE_TABLE)
    for _name, ddl in _INDEXES:
        op.execute(ddl)


def downgrade() -> None:
    for name, _ddl in _INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {name};")
    op.execute("DROP TABLE IF EXISTS forecast_backtests;")
