"""
aeam/security/audit_logger.py

Immutable audit logging for the AEAM system.

Appends structured audit records to ``/tmp/audit.log`` in append-only mode.
Each entry is timestamped, integrity-hashed with SHA-256, and written as
a single JSON line. The file is created automatically if it does not exist.

No entry is ever overwritten or deleted — append-only by design.

Dependencies: stdlib only (hashlib, json, pathlib, logging).
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Default audit log file path — now using /tmp for container compatibility.
# Callers may override by passing a different path to the constructor.
_DEFAULT_AUDIT_LOG_PATH: str = "/tmp/audit.log"

# Required fields every audit entry must contain.
_REQUIRED_FIELDS: frozenset[str] = frozenset(
    {"user_id", "action", "endpoint", "status_code"}
)


class AuditLogger:
    """
    Immutable, append-only audit logger.

    Writes one JSON record per line to the configured audit log file.
    Each record includes:

    - All fields from the caller-supplied ``entry`` dict.
    - A ``timestamp`` field (UTC ISO 8601) added automatically.
    - An ``entry_hash`` field — a SHA-256 hex digest of the serialised
      entry (including the timestamp) for tamper-evidence.

    The log file is opened in append mode (``"a"``) on every
    :meth:`log` call, so it is safe to use across threads and processes.
    The file is created automatically if it does not exist.

    Args:
        log_file: Path to the audit log file. Defaults to ``"/tmp/audit.log"``
                  (a writable location in containerised environments).

    Raises:
        ValueError: If ``log_file`` is empty or whitespace-only.

    Example::

        audit = AuditLogger()
        audit.log({
            "user_id":     "user_123",
            "action":      "execute_action",
            "endpoint":    "/api/v1/actions/jira",
            "status_code": 200,
        })
    """

    def __init__(
        self,
        log_file: str = _DEFAULT_AUDIT_LOG_PATH,
        database_client: Any | None = None,
    ) -> None:
        """
        Initialise the AuditLogger.

        Args:
            log_file:        File path for the audit log. The file and its
                             parent directories are created automatically
                             if absent.
            database_client: Optional :class:`aeam.integrations.database.DatabaseClient`
                             (or duck-typed equivalent exposing ``insert(table,
                             data, returning_column)``). When provided, every
                             logged entry is ALSO persisted into the
                             ``audit_logs`` table alongside the file sink
                             (Phase E3, ARCH-7). ``None`` (the default)
                             preserves pre-E3 file-only behaviour exactly
                             (COMPAT-1). A DB insert failure is caught and
                             logged; it never breaks the file write, and
                             never propagates to the request pipeline
                             (same resilience contract the middleware relies
                             on today).

        Raises:
            ValueError: If ``log_file`` is empty or whitespace-only.
        """
        if not log_file or not log_file.strip():
            raise ValueError("log_file must be a non-empty string.")

        self._log_file: Path = Path(log_file)
        self._db: Any | None = database_client

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def attach_database(self, database_client: Any) -> None:
        """
        Attach (or replace) the durable DB sink used alongside the file
        sink (Phase E3, ARCH-7).

        Provided so callers that construct the AuditLogger before their
        database client is available (e.g. FastAPI's ``create_app`` runs
        BEFORE the lifespan builds the container) can still upgrade it
        once the client exists. Passing ``None`` disables the DB sink
        cleanly.

        Args:
            database_client: A :class:`aeam.integrations.database.DatabaseClient`
                             (or duck-typed equivalent exposing ``insert``),
                             or ``None`` to disable the sink.
        """
        self._db = database_client

    def log(self, entry: dict[str, Any]) -> None:
        """
        Validate, enrich, and append an audit record to the log file.

        Steps:
        1. Validate that all required fields are present in ``entry``
           (``user_id``, ``action``, ``endpoint``, ``status_code``).
        2. Build the full record by copying ``entry`` and adding a UTC
           ISO 8601 ``timestamp``.
        3. Generate a SHA-256 ``entry_hash`` of the canonical JSON
           serialisation of the record (sorted keys, no whitespace).
        4. Append the full record (including the hash) as a single JSON
           line to the audit log file (file created if absent).
        5. Log success or failure via the Python ``logging`` module.

        Args:
            entry: Dict containing at minimum:

                - ``"user_id"``     — identifier of the acting user or
                  service account.
                - ``"action"``      — the operation performed
                  (e.g. ``"execute_action"``).
                - ``"endpoint"``    — the API endpoint or resource
                  accessed (e.g. ``"/api/v1/actions/jira"``).
                - ``"status_code"`` — HTTP or logical status code of the
                  outcome (e.g. ``200``, ``403``).

                Additional fields are preserved verbatim.

        Raises:
            ValueError: If any required field is missing from ``entry``.
            OSError:    If the audit log file cannot be written.

        Note:
            The caller's original ``entry`` dict is never mutated.

        Example::

            audit.log({
                "user_id":     "svc-orchestrator",
                "action":      "jira_ticket_created",
                "endpoint":    "/api/v1/actions/jira",
                "status_code": 201,
                "incident_id": "INC-42",
            })
        """
        # Step 1: validate required fields.
        missing = _REQUIRED_FIELDS - set(entry.keys())
        if missing:
            raise ValueError(
                f"Audit entry is missing required fields: {sorted(missing)}. "
                f"Received keys: {sorted(entry.keys())}."
            )

        # Step 2: build record with timestamp.
        record: dict[str, Any] = {
            **entry,
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        }

        # Step 3: generate SHA-256 integrity hash.
        try:
            canonical: str = json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)
        except TypeError as exc:
            raise ValueError(
                f"Audit entry contains non-JSON-serialisable values: {exc}"
            ) from exc

        entry_hash: str = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        record["hash"] = entry_hash

        # Step 4: append to audit log file (append-only, auto-create).
        try:
            self._log_file.parent.mkdir(parents=True, exist_ok=True)

            with self._log_file.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, default=str))
                fh.write("\n")

            # Step 5: log success.
            logger.info(
                "AuditLogger.log | SUCCESS | user_id=%s | action=%s | "
                "endpoint=%s | status_code=%s | hash=%s",
                entry.get("user_id"),
                entry.get("action"),
                entry.get("endpoint"),
                entry.get("status_code"),
                entry_hash[:12],  # abbreviated for log readability
            )

        except OSError as exc:
            logger.error(
                "AuditLogger.log | FAILED | user_id=%s | action=%s | error=%s",
                entry.get("user_id"),
                entry.get("action"),
                exc,
            )
            raise

        # Step 6 (Phase E3, ARCH-7): durable DB sink alongside the file.
        # A missing db client is the pre-E3 default and yields file-only
        # behaviour. A DB insert failure is caught + logged; it never
        # breaks the file write (already succeeded above) and never
        # propagates to the request pipeline. The persisted shape mirrors
        # the file record 1:1 for the four required fields + timestamp +
        # hash; any additional caller-supplied fields land in the JSONB
        # ``extra`` column so the schema does not need to grow to
        # accommodate them (MOD-4: what is honestly extra stays extra).
        if self._db is not None:
            try:
                required = {"user_id", "action", "endpoint", "status_code"}
                extra_fields = {
                    k: v for k, v in entry.items() if k not in required
                }
                row: dict[str, Any] = {
                    "entry_id":    str(uuid.uuid4()),
                    "timestamp":   record["timestamp"],
                    "user_id":     entry["user_id"],
                    "action":      entry["action"],
                    "endpoint":    entry["endpoint"],
                    "status_code": entry["status_code"],
                    "hash":        entry_hash,
                    "extra":       json.dumps(extra_fields, default=str) if extra_fields else None,
                }
                self._db.insert(
                    table="audit_logs",
                    data=row,
                    returning_column="entry_id",
                )
            except Exception as db_exc:  # noqa: BLE001
                logger.error(
                    "AuditLogger.log | durable-sink write FAILED | user_id=%s | "
                    "action=%s | error=%s (file record already persisted)",
                    entry.get("user_id"),
                    entry.get("action"),
                    db_exc,
                )

    def __repr__(self) -> str:
        return f"AuditLogger(log_file={str(self._log_file)!r})"