"""
scripts/dr_drill.py

Backup / restore rehearsal driver (Phase E13 — Enterprise Certification).

Article XVI requires a *declared* retention and backup/restore posture for
every store (MEM-6) and the E13 acceptance criteria require that a full
restore drill actually **succeeds and is documented** — not that a runbook
exists. This script is the executable half of that pair: it performs a
real backup, a real restore into a separate target, and a verification
pass that compares the two, then writes a machine-readable evidence record
the certification pack links to.

It is an operational artifact, not application code: nothing in ``aeam/``
imports it, and it adds no runtime surface (E13's "new modules: none in the
application"). It reuses the platform's own clients — ``DatabaseClient``,
the ``BlobStore`` factory, ``qdrant-client`` — so the drill exercises the
same access paths production uses rather than a parallel implementation.

Per-store posture (see docs/DISASTER_RECOVERY.md for the full runbook):

* **PostgreSQL** — authoritative store for incidents, decisions, audit
  logs, approvals, datasets, policies. Backed up here as a logical,
  dialect-portable JSON export so the drill runs identically against the
  CI SQLite instance and a production PostgreSQL. For production *volume*,
  the runbook prescribes ``pg_dump``/PITR; this exporter is the drill's
  verifier, and it reads through the same client the application uses.
* **Object store (blobs)** — content-addressed; every key is the SHA-256 of
  its bytes, so a restore is verifiable by recomputing hashes rather than
  by trusting the copy.
* **Qdrant** — vector collections are *derived* state: they can be rebuilt
  by re-ingesting from the blob store and re-embedding. Snapshots are
  still taken because rebuilding costs embedding time; the drill records
  which collections were captured, and honestly records "unreachable" when
  Qdrant is not running rather than reporting a snapshot it did not take.
* **Redis** — cache, dedup, idempotency, rate-limit and ingestion-queue
  state. Declared **not backed up**: every key is either reconstructible or
  intentionally short-lived, and restoring a stale dedup window would be
  worse than an empty one. The drill records the declaration so the posture
  is stated (MEM-6) rather than silently absent.

Usage::

    # Rehearse against the configured deployment, writing evidence to disk
    python scripts/dr_drill.py --backup-dir ./dr-backup --evidence dr-evidence.json

    # Backup only / restore only
    python scripts/dr_drill.py --backup-dir ./dr-backup --stage backup
    python scripts/dr_drill.py --backup-dir ./dr-backup --stage restore \\
        --restore-database-url postgresql://.../aeam_restore_test

A non-zero exit status means the drill FAILED — that is the point: a
rehearsal that cannot fail proves nothing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# The script lives outside the package; make the repository root importable
# so `python scripts/dr_drill.py` works from a clean checkout.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aeam.integrations.database import DatabaseClient  # noqa: E402

logger = logging.getLogger("aeam.dr_drill")

# ---------------------------------------------------------------------------
# What the drill covers
# ---------------------------------------------------------------------------
#
# The authoritative tables. Derived/rebuildable tables are deliberately
# absent — a restore drill proves the *irreplaceable* data comes back.
# Tables that do not exist in a given deployment are skipped and recorded
# as skipped, never silently treated as empty (OBS-3).

BACKED_UP_TABLES: tuple[str, ...] = (
    "incidents",
    "decisions",
    "action_logs",
    "audit_logs",
    "metrics",
    "documents",
    "datasets",
    "schemas",
    "sources",
    "versions",
    "policies",
    "ingestion_jobs",
    "incident_approvals",   # E9 approval chains
    "review_verdicts",      # E9 reviewer verdicts
    "forecast_backtests",   # F1 forecast model quality history
    "calibration_models",   # F2 versioned calibration state (the rollback ledger)
    "learning_proposals",   # F2 governance decisions on threshold changes
    "compiled_rules",       # F3 rule compilation proposals and their approvals
)

# Redis posture, stated rather than implemented (MEM-6).
REDIS_POSTURE: dict[str, str] = {
    "backed_up": "no",
    "rationale": (
        "Every Redis key is either reconstructible (BM25 index warm state, "
        "cached retrievals) or intentionally short-lived (dedup windows, "
        "idempotency markers, rate-limit counters). Restoring a stale dedup "
        "window would suppress real incidents — an empty cache is the safer "
        "recovery state."
    ),
    "recovery": (
        "Start an empty Redis. The platform repopulates on demand; the first "
        "monitor cycle after recovery re-warms dedup state."
    ),
}


@dataclass
class DrillResult:
    """Outcome of one drill stage, with enough detail to be evidence."""

    name: str
    status: str  # "ok" | "skipped" | "failed"
    detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status, "detail": self.detail, **({"data": self.data} if self.data else {})}


# ---------------------------------------------------------------------------
# Database backup / restore
# ---------------------------------------------------------------------------


def database_identity(db: DatabaseClient) -> str:
    """Return a comparable identity string for ``db``'s target database.

    ``DatabaseClient`` exposes no public accessor for its URL, and adding
    one would edit a frozen-core component for a drill script's benefit
    (MOD-1: wrapping is the default). Reading the SQLAlchemy engine's URL
    here keeps the change inside this operational artifact. Falls back to
    object identity if the internals ever move, which still catches the
    dangerous case (the same client object passed as both source and
    target).
    """
    engine = getattr(db, "_engine", None)
    url = getattr(engine, "url", None)
    return str(url) if url is not None else f"<engine:{id(db)}>"


def _table_exists(db: DatabaseClient, table: str) -> bool:
    """True when ``table`` can be read from ``db``.

    Probed by query rather than by dialect-specific catalog lookup so the
    same code path works on SQLite (CI) and PostgreSQL (production). The
    probe is issued on its own connection scope, so a failed probe on
    PostgreSQL cannot leave a poisoned transaction behind for the next
    statement.
    """
    try:
        db.fetch_all(f"SELECT * FROM {table} LIMIT 1")
        return True
    except Exception:  # noqa: BLE001
        # An absent table is an expected outcome, not an error: deployments
        # legitimately differ in which optional tables exist. The caller
        # records it as `skipped`.
        return False


def _json_safe(value: Any) -> Any:
    """Coerce a DB value into something ``json.dump`` accepts *and* the
    driver can insert back.

    - datetimes → ISO-8601 strings.
    - bytes → hex.
    - dict/list → a canonical JSON string. PostgreSQL returns ``json``/
      ``jsonb`` columns as native Python objects, which psycopg2 cannot
      adapt back into an INSERT ("can't adapt type 'dict'"). Serialising
      with ``sort_keys`` makes the form canonical, so a value exported,
      restored and re-exported produces a byte-identical string and the
      verification digest still matches. PostgreSQL parses the string back
      into jsonb on insert; SQLite stores the same TEXT the application
      already writes there.
    """
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, default=str)
    return value


def backup_database(db: DatabaseClient, backup_dir: Path) -> DrillResult:
    """Export every authoritative table to ``backup_dir/database.json``.

    Returns a result carrying per-table row counts and a content digest —
    the digest is what the verification stage compares, so a restore that
    loses or mangles a row cannot pass unnoticed.
    """
    backup_dir.mkdir(parents=True, exist_ok=True)
    tables: dict[str, list[dict[str, Any]]] = {}
    skipped: list[str] = []

    for table in BACKED_UP_TABLES:
        if not _table_exists(db, table):
            skipped.append(table)
            continue
        rows = db.fetch_all(f"SELECT * FROM {table}")
        tables[table] = [{k: _json_safe(v) for k, v in row.items()} for row in rows]

    payload = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "tables": tables,
        "skipped_tables": skipped,
    }
    target = backup_dir / "database.json"
    target.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")

    counts = {name: len(rows) for name, rows in tables.items()}
    return DrillResult(
        name="database.backup",
        status="ok",
        detail=f"{sum(counts.values())} rows across {len(counts)} tables → {target}",
        data={
            "path": str(target),
            "row_counts": counts,
            "skipped_tables": skipped,
            "digest": digest_of_tables(tables),
        },
    )


def digest_of_tables(tables: dict[str, list[dict[str, Any]]]) -> str:
    """Return a stable SHA-256 over the exported table contents.

    Row order is normalised by sorting each table's rows by their canonical
    JSON form, so a restore that returns the same rows in a different
    physical order still verifies — order is not a property the application
    depends on, and asserting it would make the drill fail for a reason
    that is not a data-loss reason.
    """
    hasher = hashlib.sha256()
    for table in sorted(tables):
        hasher.update(table.encode("utf-8"))
        rows = sorted(
            json.dumps(row, sort_keys=True, default=str) for row in tables[table]
        )
        for row in rows:
            hasher.update(row.encode("utf-8"))
    return hasher.hexdigest()


def restore_database(target_db: DatabaseClient, backup_dir: Path) -> DrillResult:
    """Restore ``backup_dir/database.json`` into ``target_db``.

    The target's schema must already exist (created by ``alembic upgrade
    head`` — schema recovery is a migration concern, per E5, not a backup
    concern). Rows are inserted table by table; any table missing from the
    target is reported, never skipped silently.
    """
    source = backup_dir / "database.json"
    if not source.exists():
        return DrillResult(
            name="database.restore",
            status="failed",
            detail=f"No backup found at {source}",
        )

    payload = json.loads(source.read_text(encoding="utf-8"))
    tables: dict[str, list[dict[str, Any]]] = payload.get("tables", {})

    restored: dict[str, int] = {}
    failures: list[str] = []

    for table, rows in tables.items():
        if not _table_exists(target_db, table):
            failures.append(f"{table}: absent in restore target")
            continue
        # Start from a known-empty table so the drill measures the restore,
        # not the union of a restore and whatever was already there.
        target_db.execute(f"DELETE FROM {table}")
        for row in rows:
            columns = ", ".join(row.keys())
            placeholders = ", ".join(f":{key}" for key in row)
            target_db.execute(
                f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",
                params=row,
            )
        restored[table] = len(rows)

    if failures:
        return DrillResult(
            name="database.restore",
            status="failed",
            detail="; ".join(failures),
            data={"restored_row_counts": restored},
        )

    return DrillResult(
        name="database.restore",
        status="ok",
        detail=f"{sum(restored.values())} rows restored across {len(restored)} tables",
        data={"restored_row_counts": restored},
    )


def verify_database(target_db: DatabaseClient, backup_dir: Path) -> DrillResult:
    """Re-export the restored target and compare digests with the backup.

    This is the assertion that makes the rehearsal a test: identical
    digests mean every backed-up row came back with every field intact.
    """
    payload = json.loads((backup_dir / "database.json").read_text(encoding="utf-8"))
    expected_tables: dict[str, list[dict[str, Any]]] = payload.get("tables", {})
    expected_digest = digest_of_tables(expected_tables)

    actual_tables: dict[str, list[dict[str, Any]]] = {}
    for table in expected_tables:
        rows = target_db.fetch_all(f"SELECT * FROM {table}")
        actual_tables[table] = [{k: _json_safe(v) for k, v in row.items()} for row in rows]
    actual_digest = digest_of_tables(actual_tables)

    if actual_digest != expected_digest:
        mismatches = {
            table: {"expected": len(expected_tables[table]), "actual": len(actual_tables.get(table, []))}
            for table in expected_tables
            if len(expected_tables[table]) != len(actual_tables.get(table, []))
        }
        return DrillResult(
            name="database.verify",
            status="failed",
            detail=(
                f"Digest mismatch — expected {expected_digest[:12]}…, "
                f"got {actual_digest[:12]}…"
            ),
            data={"row_count_mismatches": mismatches},
        )

    return DrillResult(
        name="database.verify",
        status="ok",
        detail=f"Restored content matches backup (digest {expected_digest[:12]}…)",
        data={"digest": expected_digest},
    )


# ---------------------------------------------------------------------------
# Blob store backup / restore
# ---------------------------------------------------------------------------


def backup_blobs(blob_store: Any, content_hashes: list[str], backup_dir: Path) -> DrillResult:
    """Copy the named blobs out of the store into ``backup_dir/blobs``.

    ``content_hashes`` comes from the ``documents`` table — the blob store
    is content-addressed and has no listing contract in the ``BlobStore``
    interface, so the database is the authoritative index of what exists.
    A hash the store cannot produce is recorded as missing, which is itself
    a finding worth failing on.
    """
    blob_dir = backup_dir / "blobs"
    blob_dir.mkdir(parents=True, exist_ok=True)

    copied: list[str] = []
    missing: list[str] = []

    for content_hash in content_hashes:
        try:
            data = blob_store.get(content_hash)
        except Exception as exc:  # noqa: BLE001
            logger.warning("dr_drill | blob unreadable | hash=%s | %s", content_hash, exc)
            missing.append(content_hash)
            continue
        (blob_dir / content_hash).write_bytes(data)
        copied.append(content_hash)

    status = "failed" if missing else "ok"
    return DrillResult(
        name="blobs.backup",
        status=status,
        detail=f"{len(copied)} blobs copied, {len(missing)} unreadable",
        data={"copied": len(copied), "missing": missing, "path": str(blob_dir)},
    )


def restore_blobs(blob_store: Any, backup_dir: Path) -> DrillResult:
    """Write every backed-up blob into ``blob_store`` and verify its hash.

    Content addressing makes verification exact: a blob whose restored
    bytes hash to a different value than its key was corrupted in transit,
    and the drill fails rather than reporting a successful restore.
    """
    blob_dir = backup_dir / "blobs"
    if not blob_dir.exists():
        return DrillResult(name="blobs.restore", status="skipped", detail="No blobs in backup.")

    restored: list[str] = []
    corrupted: list[str] = []

    for path in sorted(blob_dir.iterdir()):
        if not path.is_file():
            continue
        data = path.read_bytes()
        actual = hashlib.sha256(data).hexdigest()
        if actual != path.name:
            corrupted.append(path.name)
            continue
        blob_store.put(data)
        restored.append(path.name)

    status = "failed" if corrupted else "ok"
    return DrillResult(
        name="blobs.restore",
        status=status,
        detail=f"{len(restored)} blobs restored, {len(corrupted)} failed hash verification",
        data={"restored": len(restored), "corrupted": corrupted},
    )


# ---------------------------------------------------------------------------
# Qdrant snapshot
# ---------------------------------------------------------------------------


def snapshot_qdrant(vector_db_url: str, timeout: float = 10.0) -> DrillResult:
    """Ask Qdrant to snapshot every collection.

    Vector collections are derived state — a total loss is recoverable by
    re-ingesting from the blob store and re-embedding — so an unreachable
    Qdrant is reported as ``skipped`` with the real reason rather than
    failing the drill. Losing the snapshot costs embedding time, not data.
    """
    if not vector_db_url:
        return DrillResult(
            name="qdrant.snapshot", status="skipped", detail="VECTOR_DB_URL is not configured."
        )

    try:
        from qdrant_client import QdrantClient  # imported lazily (CODE-6)

        client = QdrantClient(url=vector_db_url, timeout=timeout)
        collections = [c.name for c in client.get_collections().collections]
        snapshots: dict[str, str] = {}
        for name in collections:
            description = client.create_snapshot(collection_name=name)
            snapshots[name] = getattr(description, "name", "") or str(description)
    except Exception as exc:  # noqa: BLE001
        # Declared boundary (CODE-5): derived state, honest skip.
        return DrillResult(
            name="qdrant.snapshot",
            status="skipped",
            detail=f"Qdrant unreachable at {vector_db_url}: {exc}",
            data={
                "recovery": (
                    "Re-create collections and re-ingest from the blob store; "
                    "vectors are derived, not authoritative."
                )
            },
        )

    return DrillResult(
        name="qdrant.snapshot",
        status="ok",
        detail=f"{len(snapshots)} collection snapshots created",
        data={"snapshots": snapshots},
    )


def declare_redis_posture() -> DrillResult:
    """Record the Redis backup posture (MEM-6 requires it be *stated*)."""
    return DrillResult(
        name="redis.posture",
        status="ok",
        detail="Declared not-backed-up; recovery is an empty instance.",
        data=dict(REDIS_POSTURE),
    )


# ---------------------------------------------------------------------------
# Drill orchestration
# ---------------------------------------------------------------------------


def run_drill(
    *,
    source_db: DatabaseClient,
    restore_db: DatabaseClient,
    backup_dir: Path,
    source_blob_store: Any = None,
    restore_blob_store: Any = None,
    vector_db_url: str = "",
) -> dict[str, Any]:
    """Run backup → restore → verify across every store and return evidence.

    Args:
        source_db:          Client for the deployment being backed up.
        restore_db:         Client for the restore target. MUST NOT be the
                            source: the restore stage truncates the tables
                            it restores, and pointing both at one database
                            would destroy the data the drill claims to
                            protect. Enforced below.
        backup_dir:         Directory the backup is written to and read from.
        source_blob_store:  Blob store to back up. None skips the blob stage.
        restore_blob_store: Blob store to restore into. None skips restore.
        vector_db_url:      Qdrant URL, or "" to skip the snapshot stage.

    Returns:
        An evidence dict: ``{"started_at", "finished_at", "passed",
        "results": [...]}``.

    Raises:
        ValueError: If ``source_db`` and ``restore_db`` address the same
                    database.
    """
    if database_identity(source_db) == database_identity(restore_db):
        raise ValueError(
            "The restore target must be a different database from the source — "
            "the restore stage truncates the tables it restores."
        )

    started = datetime.now(timezone.utc)
    results: list[DrillResult] = []

    results.append(backup_database(source_db, backup_dir))

    if source_blob_store is not None:
        hashes: list[str] = []
        if _table_exists(source_db, "documents"):
            rows = source_db.fetch_all("SELECT content_hash FROM documents")
            hashes = [str(r["content_hash"]) for r in rows if r.get("content_hash")]
        results.append(backup_blobs(source_blob_store, hashes, backup_dir))

    results.append(snapshot_qdrant(vector_db_url))
    results.append(declare_redis_posture())

    results.append(restore_database(restore_db, backup_dir))
    results.append(verify_database(restore_db, backup_dir))

    if restore_blob_store is not None:
        results.append(restore_blobs(restore_blob_store, backup_dir))

    finished = datetime.now(timezone.utc)
    passed = all(r.status != "failed" for r in results)

    return {
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "duration_seconds": round((finished - started).total_seconds(), 3),
        "passed": passed,
        "results": [r.to_dict() for r in results],
    }


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns a process exit status (0 = drill passed)."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")

    parser = argparse.ArgumentParser(description="AEAM backup/restore rehearsal (Phase E13).")
    parser.add_argument("--backup-dir", required=True, help="Directory for the backup artifacts.")
    parser.add_argument(
        "--restore-database-url",
        default="",
        help=(
            "DSN of the restore TARGET (must differ from DATABASE_URL). "
            "Required for the restore/verify stages."
        ),
    )
    parser.add_argument(
        "--evidence",
        default="",
        help="Path to write the JSON evidence record to. Omit to print it.",
    )
    args = parser.parse_args(argv)

    from aeam.config.settings import Settings
    from aeam.storage.factory import build_blob_store

    settings = Settings()
    if not args.restore_database_url:
        parser.error(
            "--restore-database-url is required: a drill that restores over its "
            "own source proves nothing and destroys data."
        )

    source_db = DatabaseClient(database_url=settings.DATABASE_URL)
    restore_db = DatabaseClient(database_url=args.restore_database_url)
    try:
        evidence = run_drill(
            source_db=source_db,
            restore_db=restore_db,
            backup_dir=Path(args.backup_dir),
            source_blob_store=build_blob_store(settings),
            restore_blob_store=None,
            vector_db_url=str(getattr(settings, "VECTOR_DB_URL", "") or ""),
        )
    finally:
        source_db.dispose()
        restore_db.dispose()

    rendered = json.dumps(evidence, indent=2)
    if args.evidence:
        Path(args.evidence).write_text(rendered, encoding="utf-8")
        logger.info("Evidence written to %s", args.evidence)
    else:
        print(rendered)

    for result in evidence["results"]:
        logger.info("%-20s %-8s %s", result["name"], result["status"], result["detail"])

    return 0 if evidence["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
