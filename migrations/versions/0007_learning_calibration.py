"""F2 — confidence calibration state and learning proposals

Revision ID: 0007_learning_calibration
Revises: 0006_forecast_backtests
Create Date: 2026-07-29

Phase F2 closes the feedback loop: E9 human verdicts and resolved-incident
outcomes become labeled signal, a calibration mapping is fitted from them,
and stated confidence is post-processed so a "0.8" means what it says.
Two things need schema.

``calibration_models`` — the versioned calibration state.

  Versioning is not bookkeeping here, it is the rollback mechanism the
  phase's rollback strategy names: "calibration state is versioned, so any
  prior calibration can be restored". Exactly one row is ``active`` at a
  time; superseding a calibration never deletes the one it replaced, so
  the mapping that produced a historical incident's calibrated confidence
  remains inspectable forever (COMPAT-7 — evidence provenance is
  permanent).

  Every row carries the numbers that justified adopting it — held-out ECE
  and Brier before and after, sample counts, the training window — because
  PHIL-1 requires calibration to be measured rather than asserted, and an
  operator approving one needs to see what it was measured on.

``learning_proposals`` — threshold changes the Learning Agent proposes.

  AGENT-5: the Learning Agent is advisory. It may propose that an
  automation threshold move; it may never move one. A proposal sits
  ``pending`` until a human records a verdict, and the verdict carries the
  same attribution shape E9 verdicts carry (who, what roles, how
  attributed, why) so the two are auditable the same way.

  The E9 ``review_verdicts`` table is deliberately NOT reused: every column
  in it is incident-scoped (``incident_id``, ``approval_id``, ``tier``),
  and a calibration threshold belongs to no incident. Forcing one in would
  have made both tables lie about what they contain. What is reused is the
  contract — the vocabulary, the attribution fields, and the rule that
  nothing takes effect without a recorded human verdict.

Both tables are purely additive. Nothing existing is altered, and with
``LEARNING_CALIBRATION_ENABLED`` false (the default) both stay empty and
inert — the F2 rollback posture.

The DDL is INLINED rather than imported from
``aeam.integrations.enterprise_schema`` for the same reason every prior
revision inlines it: a migration is a frozen historical artifact.
``test_phase_e5_migrations.py``'s drift guard keeps it in lock-step with
the startup DDL path.
"""

from __future__ import annotations

from alembic import op

revision: str = "0007_learning_calibration"
down_revision: str | None = "0006_forecast_backtests"
branch_labels: str | None = None
depends_on: str | None = None


_CALIBRATION_MODELS: str = """
CREATE TABLE IF NOT EXISTS calibration_models (
    calibration_id    TEXT PRIMARY KEY,
    version           INTEGER NOT NULL,
    status            TEXT DEFAULT 'active',
    knots             JSONB,
    training_samples  INTEGER,
    holdout_samples   INTEGER,
    ece_before        DOUBLE PRECISION,
    ece_after         DOUBLE PRECISION,
    brier_before      DOUBLE PRECISION,
    brier_after       DOUBLE PRECISION,
    curve_before      JSONB,
    curve_after       JSONB,
    skipped_counts    JSONB,
    source_window     TEXT,
    created_by        TEXT,
    reason            TEXT,
    created_at        TIMESTAMP,
    superseded_at     TIMESTAMP
);
"""

_LEARNING_PROPOSALS: str = """
CREATE TABLE IF NOT EXISTS learning_proposals (
    proposal_id        TEXT PRIMARY KEY,
    proposal_type      TEXT,
    subject            TEXT,
    current_value      TEXT,
    proposed_value     TEXT,
    rationale          TEXT,
    evidence           JSONB,
    status             TEXT DEFAULT 'pending',
    reviewer_id        TEXT,
    reviewer_roles     JSONB,
    attribution_source TEXT,
    note               TEXT,
    created_at         TIMESTAMP,
    decided_at         TIMESTAMP
);
"""

_INDEXES: tuple[tuple[str, str], ...] = (
    # The hot read: "what calibration is live right now?" — executed on
    # every finalize when calibration is enabled.
    (
        "idx_calibration_models_status",
        "CREATE INDEX IF NOT EXISTS idx_calibration_models_status "
        "ON calibration_models (status, version);",
    ),
    # The rollback read: "show me the version history for this metric".
    (
        "idx_calibration_models_version",
        "CREATE INDEX IF NOT EXISTS idx_calibration_models_version "
        "ON calibration_models (version);",
    ),
    # The governance read: "what is waiting on a human?"
    (
        "idx_learning_proposals_status",
        "CREATE INDEX IF NOT EXISTS idx_learning_proposals_status "
        "ON learning_proposals (status, created_at);",
    ),
)


def upgrade() -> None:
    op.execute(_CALIBRATION_MODELS)
    op.execute(_LEARNING_PROPOSALS)
    for _name, ddl in _INDEXES:
        op.execute(ddl)


def downgrade() -> None:
    for name, _ddl in _INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {name};")
    op.execute("DROP TABLE IF EXISTS learning_proposals;")
    op.execute("DROP TABLE IF EXISTS calibration_models;")
