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

    def __init__(self, log_file: str = _DEFAULT_AUDIT_LOG_PATH) -> None:
        """
        Initialise the AuditLogger.

        Args:
            log_file: File path for the audit log. The file and its parent
                      directories are created automatically if absent.

        Raises:
            ValueError: If ``log_file`` is empty or whitespace-only.
        """
        if not log_file or not log_file.strip():
            raise ValueError("log_file must be a non-empty string.")

        self._log_file: Path = Path(log_file)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

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

    def __repr__(self) -> str:
        return f"AuditLogger(log_file={str(self._log_file)!r})"