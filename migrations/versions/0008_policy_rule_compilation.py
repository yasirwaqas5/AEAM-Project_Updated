"""F3 — compiled rule proposals

Revision ID: 0008_policy_rule_compilation
Revises: 0007_learning_calibration
Create Date: 2026-07-30

Phase F3 bridges the gap between "a document says X" and "the engine
enforces X". C2/C3 extract policies and match them as advisory evidence;
E12 governs their lifecycle — but nothing before this phase could turn a
policy into an actually-enforced ``RuleEngine`` threshold without an
operator hand-editing ``detection_rules.yaml``. This table is where that
bridge's state lives.

``compiled_rules`` — one row per compilation attempt a human chose to
persist (via the Policy Agent's ``propose`` action), carrying:

* the RuleEngine-shaped candidate (``domain``, ``rule_key``, ``value``)
  the deterministic :mod:`aeam.intelligence.rule_compiler` produced;
* its lifecycle (``proposed`` → ``approved``/``rejected``, with
  ``approved`` further revocable to ``retired``) — AGENT-5: a rule is
  never enforced by its mere existence, only by an explicit, attributed
  human decision;
* the same attribution shape (``reviewer_id``/``reviewer_roles``/
  ``attribution_source``) the E9 ``review_verdicts`` table already uses,
  reused for consistency rather than inventing a second governance
  vocabulary.

Purely additive. Nothing existing is altered, and the table is inert until
an operator actually proposes and approves a rule — with zero rows
approved (every fresh deployment's starting state), ``RuleEngine`` is
constructed with an empty ``overrides`` dict, which is defined to be
byte-identical to not passing ``overrides`` at all (the F3 rollback
posture, and the acceptance criterion this migration exists to support:
"the deterministic decision path is byte-identical for any policy that has
not been adopted as a rule").

The DDL is INLINED rather than imported from
``aeam.integrations.enterprise_schema`` for the same reason every prior
revision inlines it: a migration is a frozen historical artifact.
``test_phase_e5_migrations.py``'s drift guard keeps it in lock-step with
the startup DDL path.
"""

from __future__ import annotations

from alembic import op

revision: str = "0008_policy_rule_compilation"
down_revision: str | None = "0007_learning_calibration"
branch_labels: str | None = None
depends_on: str | None = None


_CREATE_TABLE: str = """
CREATE TABLE IF NOT EXISTS compiled_rules (
    rule_id             TEXT PRIMARY KEY,
    policy_id           TEXT,             -- -> policies.policy_id (unenforced)
    domain              TEXT NOT NULL,    -- RuleEngine domain, e.g. 'sales'
    rule_key            TEXT NOT NULL,    -- e.g. 'daily_drop_percent'
    comparison          TEXT,             -- documentation only
    value               DOUBLE PRECISION,
    rationale           TEXT,             -- the compiler's own explanation
    status              TEXT DEFAULT 'proposed',
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

_INDEXES: tuple[tuple[str, str], ...] = (
    # The startup read: "which rules are adopted?" — executed once per
    # process start when building RuleEngine's overrides.
    (
        "idx_compiled_rules_status",
        "CREATE INDEX IF NOT EXISTS idx_compiled_rules_status "
        "ON compiled_rules (status);",
    ),
    # The Knowledge Center read: "every rule proposed from this policy".
    (
        "idx_compiled_rules_policy",
        "CREATE INDEX IF NOT EXISTS idx_compiled_rules_policy "
        "ON compiled_rules (policy_id);",
    ),
    # The collision check: "is any other rule already targeting this
    # domain+key?" — read by the Policy Agent before proposing a new one,
    # so a colliding proposal is caught at propose-time, not only by the
    # corpus-wide validator report.
    (
        "idx_compiled_rules_domain_key",
        "CREATE INDEX IF NOT EXISTS idx_compiled_rules_domain_key "
        "ON compiled_rules (domain, rule_key, status);",
    ),
)


def upgrade() -> None:
    op.execute(_CREATE_TABLE)
    for _name, ddl in _INDEXES:
        op.execute(ddl)


def downgrade() -> None:
    for name, _ddl in _INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {name};")
    op.execute("DROP TABLE IF EXISTS compiled_rules;")
