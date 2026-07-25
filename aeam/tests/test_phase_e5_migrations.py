"""
aeam/tests/test_phase_e5_migrations.py

Phase E5 — Persistence Evolution & Data Lifecycle (COMPAT-5, MEM-6, TECH-1/2).

Acceptance criteria under test:

1. A fresh database built purely from migrations is schema-identical to
   the one the legacy startup DDL produces (the drift guard).
2. An existing populated database upgrades in place without data loss.
3. Downgrade of the baseline-adjacent revisions works.
4. The audited hot-path indexes exist after ``upgrade head`` and are
   actually used by the query plans they were created for.
5. The retention declaration document exists (MEM-6).

Infrastructure: SQLite temp files driven through Alembic's own Python API
(``alembic.command``) — no live services, no subprocess (TEST-3). SQLite
is the same portability target the migrations already support via
``render_as_batch``; the production dialect (PostgreSQL) is exercised by
the same revisions in gating CI.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from aeam.integrations.database import DatabaseClient

_REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _alembic_config(db_url: str) -> Config:
    cfg = Config(str(_REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_REPO_ROOT / "migrations"))
    # env.py reads -x db_url first; set it via the command-line x-arg dict.
    cfg.cmd_opts = type("O", (), {"x": [f"db_url={db_url}"]})()  # minimal shim
    return cfg


def _schema_snapshot(engine) -> dict[str, dict]:
    """Return {table: {columns: set, indexes: set}} for structural comparison.

    Alembic's own bookkeeping table is excluded — it exists only on the
    migration-built DB and is not part of the application schema.
    """
    insp = inspect(engine)
    snapshot: dict[str, dict] = {}
    for table in insp.get_table_names():
        if table == "alembic_version":
            continue
        columns = {c["name"] for c in insp.get_columns(table)}
        indexes = set()
        for idx in insp.get_indexes(table):
            # Compare on the indexed-column tuple, not the name, so the
            # comparison is about STRUCTURE not incidental naming.
            indexes.add((tuple(idx["column_names"]),))
        snapshot[table] = {"columns": columns, "indexes": indexes}
    return snapshot


def _sqlite_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / ('e5-' + uuid.uuid4().hex + '.db')}"


# ---------------------------------------------------------------------------
# 1. Migration mechanism: upgrade to head builds the full schema
# ---------------------------------------------------------------------------

def test_upgrade_head_creates_all_tables(tmp_path):
    url = _sqlite_url(tmp_path)
    command.upgrade(_alembic_config(url), "head")

    engine = create_engine(url)
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    expected = {
        "incidents", "decisions", "metrics", "action_logs", "audit_logs",
        "sources", "documents", "datasets", "schemas", "versions",
        "ingestion_jobs", "policies",
    }
    assert expected.issubset(tables), f"missing tables: {expected - tables}"


# ---------------------------------------------------------------------------
# 2. Schema-identity drift guard: migrations == startup DDL
# ---------------------------------------------------------------------------

def test_migrated_schema_matches_startup_ddl(tmp_path):
    # (a) migration-built database
    mig_url = _sqlite_url(tmp_path)
    command.upgrade(_alembic_config(mig_url), "head")
    mig_engine = create_engine(mig_url)

    # (b) startup-DDL-built database (the dev-only convenience path)
    ddl_url = _sqlite_url(tmp_path)
    ddl_client = DatabaseClient(database_url=ddl_url)  # runs _create_tables + enterprise

    try:
        mig_snapshot = _schema_snapshot(mig_engine)
        ddl_snapshot = _schema_snapshot(ddl_client._engine)
    finally:
        mig_engine.dispose()
        ddl_client.dispose()

    # Same set of tables.
    assert set(mig_snapshot) == set(ddl_snapshot), (
        f"table set drift: migrations={set(mig_snapshot)} "
        f"startup={set(ddl_snapshot)}"
    )

    # Same columns per table.
    for table in mig_snapshot:
        assert mig_snapshot[table]["columns"] == ddl_snapshot[table]["columns"], (
            f"column drift in {table!r}: "
            f"migrations={mig_snapshot[table]['columns']} "
            f"startup={ddl_snapshot[table]['columns']}"
        )


def test_policies_table_has_e6_embedding_columns_in_both_paths(tmp_path):
    mig_url = _sqlite_url(tmp_path)
    command.upgrade(_alembic_config(mig_url), "head")
    mig_engine = create_engine(mig_url)

    ddl_client = DatabaseClient(database_url=_sqlite_url(tmp_path))
    try:
        mig_cols = {c["name"] for c in inspect(mig_engine).get_columns("policies")}
        ddl_cols = {c["name"] for c in inspect(ddl_client._engine).get_columns("policies")}
    finally:
        mig_engine.dispose()
        ddl_client.dispose()

    for col in ("embedding", "embedding_model"):
        assert col in mig_cols, f"migration path missing policies.{col}"
        assert col in ddl_cols, f"startup path missing policies.{col}"


# ---------------------------------------------------------------------------
# 3. Existing populated database upgrades in place without data loss
# ---------------------------------------------------------------------------

def test_existing_data_survives_upgrade(tmp_path):
    url = _sqlite_url(tmp_path)

    # Start at the baseline only, insert a row, then upgrade the rest.
    command.upgrade(_alembic_config(url), "0001_baseline")

    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO policies (policy_id, doc_id, raw_text) "
                "VALUES ('p-1', 'd-1', 'retain sales above threshold')"
            ))

        # Upgrade to head (adds the E6 embedding columns via 0003).
        command.upgrade(_alembic_config(url), "head")

        with engine.connect() as conn:
            row = conn.execute(text(
                "SELECT policy_id, raw_text, embedding FROM policies WHERE policy_id='p-1'"
            )).mappings().one()
        assert row["policy_id"] == "p-1"
        assert row["raw_text"] == "retain sales above threshold"
        assert row["embedding"] is None  # new column, NULL for the pre-existing row
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# 4. Downgrade works
# ---------------------------------------------------------------------------

def test_downgrade_removes_embedding_columns_and_indexes(tmp_path):
    url = _sqlite_url(tmp_path)
    command.upgrade(_alembic_config(url), "head")
    command.downgrade(_alembic_config(url), "0001_baseline")

    engine = create_engine(url)
    try:
        insp = inspect(engine)
        policy_cols = {c["name"] for c in insp.get_columns("policies")}
        incident_indexes = {tuple(i["column_names"]) for i in insp.get_indexes("incidents")}
    finally:
        engine.dispose()

    assert "embedding" not in policy_cols
    assert "embedding_model" not in policy_cols
    # The hot-path incidents-by-timestamp index came from 0002; gone now.
    assert ("timestamp",) not in incident_indexes


def test_full_downgrade_to_base_empties_schema(tmp_path):
    url = _sqlite_url(tmp_path)
    command.upgrade(_alembic_config(url), "head")
    command.downgrade(_alembic_config(url), "base")

    engine = create_engine(url)
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    # Only Alembic's own bookkeeping remains.
    assert tables <= {"alembic_version"}, f"unexpected tables after base downgrade: {tables}"


# ---------------------------------------------------------------------------
# 5. Hot-path indexes exist and are used
# ---------------------------------------------------------------------------

def test_hot_path_indexes_exist_after_upgrade(tmp_path):
    url = _sqlite_url(tmp_path)
    command.upgrade(_alembic_config(url), "head")

    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            names = {
                r[0] for r in conn.execute(text(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                ))
            }
    finally:
        engine.dispose()

    for expected in (
        "idx_incidents_timestamp",
        "idx_metrics_metric_timestamp",
        "idx_audit_logs_timestamp",
        "idx_audit_logs_user_id",
        "idx_action_logs_executed_at",
    ):
        assert expected in names, f"missing hot-path index {expected!r}"


def test_incident_timestamp_query_plan_uses_index(tmp_path):
    """The query plan for the incidents hot read must reference the index,
    not a full scan + sort."""
    url = _sqlite_url(tmp_path)
    command.upgrade(_alembic_config(url), "head")

    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            # Seed enough rows that the planner would prefer the index.
            for i in range(200):
                conn.execute(text(
                    "INSERT INTO incidents (incident_id, timestamp) "
                    "VALUES (:id, :ts)"
                ), {"id": f"i-{i}", "ts": f"2026-07-{(i % 28) + 1:02d}T00:00:00Z"})

        with engine.connect() as conn:
            plan = conn.execute(text(
                "EXPLAIN QUERY PLAN SELECT * FROM incidents ORDER BY timestamp DESC LIMIT 25"
            )).fetchall()
        plan_text = " ".join(str(r) for r in plan).lower()
    finally:
        engine.dispose()

    assert "idx_incidents_timestamp" in plan_text, (
        f"incidents-by-timestamp query did not use the index; plan={plan_text}"
    )


def test_metric_history_query_plan_uses_index(tmp_path):
    url = _sqlite_url(tmp_path)
    command.upgrade(_alembic_config(url), "head")

    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            for i in range(200):
                conn.execute(text(
                    "INSERT INTO metrics (metric, value, timestamp) "
                    "VALUES (:m, :v, :ts)"
                ), {"m": "sales" if i % 2 else "latency", "v": float(i),
                    "ts": f"2026-07-{(i % 28) + 1:02d}T00:00:00Z"})

        with engine.connect() as conn:
            plan = conn.execute(text(
                "EXPLAIN QUERY PLAN SELECT timestamp, value FROM metrics "
                "WHERE metric = 'sales' ORDER BY timestamp DESC LIMIT 30"
            )).fetchall()
        plan_text = " ".join(str(r) for r in plan).lower()
    finally:
        engine.dispose()

    assert "idx_metrics_metric_timestamp" in plan_text, (
        f"metric-history query did not use the index; plan={plan_text}"
    )


# ---------------------------------------------------------------------------
# 6. Retention declaration exists (MEM-6)
# ---------------------------------------------------------------------------

def test_retention_declaration_document_exists():
    doc = _REPO_ROOT / "docs" / "persistence_and_retention.md"
    assert doc.exists(), "MEM-6 requires a retention declaration document."
    text_content = doc.read_text(encoding="utf-8").lower()
    # It must actually declare owner + posture for the core stores.
    for store in ("incidents", "metrics", "audit_logs", "policies", "redis", "blobstore"):
        assert store in text_content, f"retention doc does not cover {store!r}"
    for word in ("owner", "retention", "posture"):
        assert word in text_content
