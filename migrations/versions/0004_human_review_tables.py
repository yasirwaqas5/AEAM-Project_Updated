"""E9 — human-in-the-loop review tables

Revision ID: 0004_human_review
Revises: 0003_policy_embedding
Create Date: 2026-07-26

Phase E9 turns C7's computed ``human_approval_required`` flag into a real
gate at the execution boundary. Enforcing it requires somewhere to record
(a) which gated runbook steps were withheld from an incident, and (b) each
human verdict, attributed to the principal who cast it. This revision adds
those two tables:

* ``incident_approvals`` — one row per incident that actually required
  approval. Carries the ORDERED approval chain (``required_tiers``), how far
  through it the incident is (``current_tier``), and the exact withheld
  ActionAgent calls (``pending_actions``) so an approval later executes
  precisely what was held back rather than a re-derived set.
* ``review_verdicts`` — append-only verdict records. One row per (tier,
  principal), so a three-tier chain leaves three attributable rows and a
  rejection names the tier and principal that halted it.

Both tables are purely additive and inert when
``Settings.HUMAN_APPROVAL_ENFORCED`` is false: no existing table is altered,
no existing column changes type, and an incident that never required
approval simply has no row in either (COMPAT-1/5 — incidents predating this
phase render exactly as before, because absence of a row means "no gate").

MEM-2 is satisfied by construction: a verdict is a NEW record about an
incident, never a mutation of the incident row.

The DDL is INLINED (not imported from ``aeam.integrations.enterprise_schema``)
for the same reason revision 0001 inlines the baseline: a migration is a
frozen historical artifact. ``test_phase_e5_migrations.py``'s drift test is
what keeps this in lock-step with the startup DDL path going forward.
"""

from __future__ import annotations

from alembic import op

revision: str = "0004_human_review"
down_revision: str | None = "0003_policy_embedding"
branch_labels: str | None = None
depends_on: str | None = None


_TABLES: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS incident_approvals (
        approval_id      TEXT PRIMARY KEY,
        incident_id      TEXT,
        investigation_id TEXT,
        event_type       TEXT,
        metric           TEXT,
        severity         TEXT,
        status           TEXT,
        required_tiers   JSONB,
        current_tier     INTEGER,
        pending_actions  JSONB,
        executed_actions JSONB,
        skipped_actions  JSONB,
        created_at       TIMESTAMP,
        updated_at       TIMESTAMP
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS review_verdicts (
        verdict_id         TEXT PRIMARY KEY,
        approval_id        TEXT,
        incident_id        TEXT,
        tier               INTEGER,
        tier_label         TEXT,
        verdict            TEXT,
        reviewer_id        TEXT,
        reviewer_roles     JSONB,
        attribution_source TEXT,
        note               TEXT,
        created_at         TIMESTAMP
    );
    """,
)

_INDEXES: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_approvals_incident ON incident_approvals (incident_id);",
    "CREATE INDEX IF NOT EXISTS idx_approvals_status ON incident_approvals (status);",
    "CREATE INDEX IF NOT EXISTS idx_verdicts_incident ON review_verdicts (incident_id);",
    "CREATE INDEX IF NOT EXISTS idx_verdicts_approval ON review_verdicts (approval_id);",
)


def upgrade() -> None:
    for ddl in _TABLES:
        op.execute(ddl)
    for idx in _INDEXES:
        op.execute(idx)


def downgrade() -> None:
    # Indexes go with their tables on drop; dropping explicitly first keeps
    # the statement order symmetric with upgrade() on dialects that do not
    # cascade index drops.
    for name in (
        "idx_verdicts_approval", "idx_verdicts_incident",
        "idx_approvals_status", "idx_approvals_incident",
    ):
        op.execute(f"DROP INDEX IF EXISTS {name};")
    op.execute("DROP TABLE IF EXISTS review_verdicts;")
    op.execute("DROP TABLE IF EXISTS incident_approvals;")
