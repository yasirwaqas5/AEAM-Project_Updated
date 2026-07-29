"""F4 — the business graph (nodes + edges)

Revision ID: 0009_business_graph
Revises: 0008_policy_rule_compilation
Create Date: 2026-07-29

Phase F4 turns C4's per-incident, pairwise correlation into a durable
relationship model. Before this revision the platform re-derived "what
relates to this metric?" from scratch on every investigation and threw the
answer away at finalize, so correlation could never compound across
incidents and an operator could not ask the question at all outside an
investigation. These two tables are where that structure lives.

``graph_nodes`` — one row per typed entity (metric, dataset, service,
policy, incident). ``node_key`` is the natural key (``metric:sales``) and
is UNIQUE: every edge references it, so two rows claiming the same key
would silently split the graph. ``node_id`` is a DETERMINISTIC UUID5 of
``node_key`` rather than a random UUID4 — a rebuild from unchanged
evidence produces byte-identical rows, and two builders racing on the same
node compute the same primary key so the uniqueness constraint resolves
the race instead of duplicating (ARCH-8).

``graph_edges`` — one row per typed, weighted, evidence-grounded
relationship. Four edge types only (``correlates_with``, ``governed_by``,
``derived_from``, ``co_occurred_in_incident``), each derivable from ONE
existing evidence source. ``confidence`` is measured (mean |Pearson r|
that C4 actually observed, or 1.0 for a structural fact recorded in the
registry) and ``evidence`` carries the pointers an operator follows to
check the claim — an edge nobody can falsify is a fabrication, which is
what the column exists to prevent.

``last_seen_at`` is indexed on both tables because it is the sweep key:
rows not re-confirmed by a build are retired, which is how an edge whose
grounding evidence disappeared leaves the graph. Sweeping on a timestamp
rather than a build id is what lets two concurrent builds coexist — a row
another builder just wrote is newer than this build's cutoff and survives.

Purely additive, and inert on arrival: both tables are empty until an
operator runs a build (``POST /api/v1/graph/build``), and the graph-aware
advisory finding is gated behind ``BUSINESS_GRAPH_ENABLED``, which
defaults to false. With the flag off, the deterministic decision path is
byte-identical to F3.

The DDL is INLINED rather than imported from
``aeam.integrations.enterprise_schema`` for the same reason every prior
revision inlines it: a migration is a frozen historical artifact.
``test_phase_e5_migrations.py``'s drift guard keeps it in lock-step with
the startup DDL path.
"""

from __future__ import annotations

from alembic import op

revision: str = "0009_business_graph"
down_revision: str | None = "0008_policy_rule_compilation"
branch_labels: str | None = None
depends_on: str | None = None


_CREATE_NODES: str = """
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

_CREATE_EDGES: str = """
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

_INDEXES: tuple[tuple[str, str], ...] = (
    (
        "idx_graph_nodes_key",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_graph_nodes_key "
        "ON graph_nodes (node_key);",
    ),
    (
        "idx_graph_nodes_type",
        "CREATE INDEX IF NOT EXISTS idx_graph_nodes_type ON graph_nodes (node_type);",
    ),
    (
        "idx_graph_edges_source",
        "CREATE INDEX IF NOT EXISTS idx_graph_edges_source "
        "ON graph_edges (source_key, confidence);",
    ),
    (
        "idx_graph_edges_target",
        "CREATE INDEX IF NOT EXISTS idx_graph_edges_target "
        "ON graph_edges (target_key, confidence);",
    ),
    (
        "idx_graph_edges_type",
        "CREATE INDEX IF NOT EXISTS idx_graph_edges_type ON graph_edges (edge_type);",
    ),
    (
        "idx_graph_edges_last_seen",
        "CREATE INDEX IF NOT EXISTS idx_graph_edges_last_seen "
        "ON graph_edges (last_seen_at);",
    ),
    (
        "idx_graph_nodes_last_seen",
        "CREATE INDEX IF NOT EXISTS idx_graph_nodes_last_seen "
        "ON graph_nodes (last_seen_at);",
    ),
)


def upgrade() -> None:
    op.execute(_CREATE_NODES)
    op.execute(_CREATE_EDGES)
    for _name, ddl in _INDEXES:
        op.execute(ddl)


def downgrade() -> None:
    for name, _ddl in _INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {name};")
    op.execute("DROP TABLE IF EXISTS graph_edges;")
    op.execute("DROP TABLE IF EXISTS graph_nodes;")
