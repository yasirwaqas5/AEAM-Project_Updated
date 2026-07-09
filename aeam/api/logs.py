"""
aeam/api/logs.py

Agent execution logs API for the AEAM system.

Exposes a read-only GET endpoint that returns agent execution log entries.
No persistent log storage exists yet — this module uses an in-memory list
seeded with deterministic mock data to satisfy the API contract until a
real log store is wired in.

Rules enforced:
- No agent triggering.
- No orchestrator calls.
- No database writes.
- Read-only.
- Public endpoint — no authentication required.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/logs", tags=["Logs"])


# ---------------------------------------------------------------------------
# In-memory log store
# Seeded with mock data at module load time.
# Replace _LOG_STORE with a real DB-backed query when log persistence
# is implemented (aeam/integrations/database.py insert to action_logs table).
# ---------------------------------------------------------------------------

def _generate_mock_logs() -> list[dict[str, Any]]:
    """
    Generate a deterministic set of mock agent execution log entries.

    Produces one entry per agent type across two simulated incidents,
    providing realistic sample data for development and integration
    testing without requiring a live log store.

    Returns:
        List of log entry dicts, ordered newest first. Each entry contains:

        - ``agent``              — agent name string.
        - ``incident_id``        — simulated incident UUID string.
        - ``status``             — ``"SUCCESS"`` or ``"FAILED"``.
        - ``execution_time_ms``  — simulated execution duration in milliseconds.
        - ``timestamp``          — UTC ISO 8601 string.
    """
    now = datetime.now(tz=timezone.utc)

    # Simulated incident IDs.
    inc_1 = "inc-mock-0001"
    inc_2 = "inc-mock-0002"

    entries: list[dict[str, Any]] = [
        # --- Incident 2 (most recent) ---
        {
            "agent":             "monitor",
            "incident_id":       inc_2,
            "status":            "SUCCESS",
            "execution_time_ms": 42,
            "timestamp":         (now - timedelta(minutes=5)).isoformat(),
        },
        {
            "agent":             "rag",
            "incident_id":       inc_2,
            "status":            "SUCCESS",
            "execution_time_ms": 1340,
            "timestamp":         (now - timedelta(minutes=4, seconds=50)).isoformat(),
        },
        {
            "agent":             "forecast",
            "incident_id":       inc_2,
            "status":            "SUCCESS",
            "execution_time_ms": 870,
            "timestamp":         (now - timedelta(minutes=4, seconds=30)).isoformat(),
        },
        {
            "agent":             "report",
            "incident_id":       inc_2,
            "status":            "SUCCESS",
            "execution_time_ms": 210,
            "timestamp":         (now - timedelta(minutes=4)).isoformat(),
        },
        {
            "agent":             "action",
            "incident_id":       inc_2,
            "status":            "SUCCESS",
            "execution_time_ms": 530,
            "timestamp":         (now - timedelta(minutes=3, seconds=45)).isoformat(),
        },
        # --- Incident 1 (older) ---
        {
            "agent":             "monitor",
            "incident_id":       inc_1,
            "status":            "SUCCESS",
            "execution_time_ms": 38,
            "timestamp":         (now - timedelta(hours=1)).isoformat(),
        },
        {
            "agent":             "rag",
            "incident_id":       inc_1,
            "status":            "FAILED",
            "execution_time_ms": 5001,
            "timestamp":         (now - timedelta(minutes=59, seconds=50)).isoformat(),
        },
        {
            "agent":             "forecast",
            "incident_id":       inc_1,
            "status":            "SUCCESS",
            "execution_time_ms": 910,
            "timestamp":         (now - timedelta(minutes=59, seconds=30)).isoformat(),
        },
        {
            "agent":             "report",
            "incident_id":       inc_1,
            "status":            "SUCCESS",
            "execution_time_ms": 195,
            "timestamp":         (now - timedelta(minutes=59)).isoformat(),
        },
        {
            "agent":             "action",
            "incident_id":       inc_1,
            "status":            "SUCCESS",
            "execution_time_ms": 480,
            "timestamp":         (now - timedelta(minutes=58, seconds=30)).isoformat(),
        },
    ]

    return entries


# Module-level in-memory store — seeded once at import time.
_LOG_STORE: list[dict[str, Any]] = _generate_mock_logs()


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.get("/agents", response_model=list[dict])
def list_agent_logs(request: Request):
    container = request.app.state.container
    try:
        db = container.db
        # Select the JSON `result` column so we can surface the execution
        # metadata (duration, retry count, failure reason, validation result)
        # that ActionAgent embeds there.
        query = """
        SELECT action_type as agent, incident_id, status,
               result, executed_at as timestamp
        FROM action_logs
        ORDER BY executed_at DESC
        LIMIT 50
        """
        rows = _fetch_all(db, query)
        logs = []
        for row in rows:
            meta = _parse_result(row.get("result"))
            logs.append({
                "agent": row["agent"] or "action",
                "incident_id": row["incident_id"],
                "status": row["status"],
                # Existing key preserved; now populated from the real duration.
                "execution_time_ms": meta.get("execution_duration_ms", 0),
                # New, additive fields (null on legacy rows that predate them).
                "retry_count": meta.get("retry_count"),
                "failure_reason": meta.get("failure_reason"),
                "validation_result": meta.get("validation_result"),
                "timestamp": (
                    row["timestamp"].isoformat()
                    if hasattr(row["timestamp"], "isoformat")
                    else row["timestamp"]
                ),
            })
        return logs
    except Exception as e:
        logger.error("Failed to fetch agent logs: %s", e)
        raise HTTPException(
            status_code=500,
            detail="Failed to retrieve agent logs from the database.",
        ) from e


def _parse_result(result: Any) -> dict[str, Any]:
    """
    Normalise the ``action_logs.result`` column into a dict.

    The column may come back as a dict (Postgres JSONB), a JSON string
    (SQLite / text storage), or ``None``. Any parse failure degrades to an
    empty dict so a malformed legacy row never breaks the endpoint.
    """
    if isinstance(result, dict):
        return result
    if isinstance(result, str) and result.strip():
        import json
        try:
            parsed = json.loads(result)
            return parsed if isinstance(parsed, dict) else {}
        except (ValueError, TypeError):
            return {}
    return {}


def _fetch_all(db: Any, query: str) -> list[dict[str, Any]]:
    from sqlalchemy import text

    with db._engine.connect() as conn:
        result = conn.execute(text(query))
        rows = result.mappings().all()
        return [dict(row) for row in rows]
