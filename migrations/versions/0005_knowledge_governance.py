"""E12 — knowledge, policy & memory governance

Revision ID: 0005_knowledge_governance
Revises: 0004_human_review
Create Date: 2026-07-26

Phase E12 makes the platform's knowledge stores curated, correctable, and
quality-measured. Two of its four objectives need schema:

* ``policies.status`` (+ attribution columns) — extracted policies had no
  lifecycle: every row matched investigations forever until someone deleted
  it from the database by hand. This adds the ``active`` / ``pending_review``
  / ``retired`` lifecycle ``PolicyRegistry`` now honours, plus who changed it,
  when, and why (SEC-7: curation is privileged AND attributable).

  COMPAT-6 is satisfied by the DEFAULT: every existing row becomes ``active``,
  which is exactly the behaviour it already had, so no investigation's policy
  matching changes as a result of this migration.

* ``documents.semantic_type`` — the upload path stored the FORMAT category
  ("markdown", "pdf") in ``doc_type``, which is what the retrieval-time
  business-relevance bonus reads. The consequence was a MOD-4 contract defect:
  an uploaded runbook could never earn the authoritative-source bonus, because
  its ``doc_type`` said "markdown", not "runbook". This column carries the
  DECLARED semantic type, separate from the format, and retrieval prefers it
  when present.

  COMPAT-1 is satisfied by NULLability: a document that never declared a
  semantic type falls back to ``doc_type`` exactly as before.

Both changes are purely additive — no existing column changes type, no
existing row loses data, and both are individually reversible.

The DDL is INLINED rather than imported from
``aeam.integrations.enterprise_schema`` for the same reason every prior
revision inlines it: a migration is a frozen historical artifact.
``test_phase_e5_migrations.py``'s drift guard is what keeps this in lock-step
with the startup DDL path.
"""

from __future__ import annotations

from alembic import op

revision: str = "0005_knowledge_governance"
down_revision: str | None = "0004_human_review"
branch_labels: str | None = None
depends_on: str | None = None


# (table, column, DDL type) — applied with ADD COLUMN IF NOT EXISTS semantics
# where the dialect supports it, and guarded by an inspector check where it
# does not (SQLite), so a re-run is always safe.
_ADDITIVE_COLUMNS: tuple[tuple[str, str, str], ...] = (
    # Phase E12: policy lifecycle. DEFAULT 'active' preserves today's
    # matching behaviour for every existing row (COMPAT-6).
    ("policies", "status", "TEXT DEFAULT 'active'"),
    ("policies", "status_changed_at", "TIMESTAMP"),
    ("policies", "status_changed_by", "TEXT"),
    ("policies", "status_reason", "TEXT"),
    # Phase E12: declared semantic document type, separate from format.
    ("documents", "semantic_type", "TEXT"),
)

_INDEXES: tuple[tuple[str, str], ...] = (
    # PolicyRegistry now filters to status='active' on every match call.
    (
        "idx_policies_status",
        "CREATE INDEX IF NOT EXISTS idx_policies_status ON policies (status);",
    ),
    # Knowledge Center lists/filters documents by declared semantic type.
    (
        "idx_documents_semantic_type",
        "CREATE INDEX IF NOT EXISTS idx_documents_semantic_type "
        "ON documents (semantic_type);",
    ),
)


def _existing_columns(table: str) -> set[str]:
    from sqlalchemy import inspect

    inspector = inspect(op.get_bind())
    return {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    for table, column, ddl_type in _ADDITIVE_COLUMNS:
        if column in _existing_columns(table):
            continue
        op.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type};")

    # Backfill existing rows explicitly. ADD COLUMN ... DEFAULT populates new
    # rows on every dialect but does NOT retroactively fill existing ones on
    # some, and "policy with a NULL status" must never exist — PolicyRegistry
    # would then have to guess whether it is active.
    op.execute("UPDATE policies SET status = 'active' WHERE status IS NULL;")

    for _name, ddl in _INDEXES:
        op.execute(ddl)


def downgrade() -> None:
    for name, _ddl in _INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {name};")

    # Reverse order of upgrade so a partially-applied upgrade unwinds cleanly.
    for table, column, _ddl_type in reversed(_ADDITIVE_COLUMNS):
        if column not in _existing_columns(table):
            continue
        op.execute(f"ALTER TABLE {table} DROP COLUMN {column};")
