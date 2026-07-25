"""E6 — stored policy embedding column

Revision ID: 0003_policy_embedding
Revises: 0002_hot_path_indexes
Create Date: 2026-07-25

The audit found Phase C3's PolicyRegistry re-embedding the ENTIRE policy
corpus on every incident that falls through to the semantic tier — an
O(policies) cost curve that grows forever. E6 fixes this by computing a
policy's embedding ONCE, at extraction time, and storing it, so matching
reads the stored vector instead of recomputing it.

This revision adds the additive columns that hold that stored vector:

* ``embedding``       — JSON-encoded ``list[float]`` (the SentenceTransformer
                        output). JSONB on PostgreSQL, text affinity on SQLite.
* ``embedding_model`` — the model id the vector was produced with, so a
                        future model change can invalidate stale vectors
                        (TECH-6) rather than silently mixing embedding spaces.

Both are NULLABLE. Policies extracted before this phase (and any row whose
embedding has not yet been backfilled) simply carry ``NULL``, and
PolicyRegistry falls back to on-the-fly embedding for those exactly as it
did before E6 — so behaviour is byte-identical until a policy is
re-extracted or backfilled (COMPAT-2/6).
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision: str = "0003_policy_embedding"
down_revision: str | None = "0002_hot_path_indexes"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    with op.batch_alter_table("policies") as batch:
        batch.add_column(sa.Column("embedding", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("embedding_model", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("policies") as batch:
        batch.drop_column("embedding_model")
        batch.drop_column("embedding")
