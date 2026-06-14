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

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

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

@router.get(
    "/agents",
    summary="List agent execution logs",
    response_description="List of agent execution log entries.",
)
def list_agent_logs(request: Request) -> JSONResponse:
    """
    Return agent execution log entries from the in-memory log store.

    No persistent log storage exists yet. This endpoint returns
    deterministic mock data seeded at module load time. The response
    shape matches the intended production schema so consumers can
    integrate against it immediately.

    When real log persistence is implemented, replace ``_LOG_STORE``
    with a query against the ``action_logs`` table via
    ``request.app.state.container.db``.

    Args:
        request: Incoming FastAPI request. Available for future use
                 when ``container.db`` is used for real log retrieval.

    Returns:
        ``200`` — JSON array of log entry objects::

            [
                {
                    "agent":             "rag",
                    "incident_id":       "inc-mock-0002",
                    "status":            "SUCCESS",
                    "execution_time_ms": 1340,
                    "timestamp":         "2024-01-15T14:32:00.000000+00:00"
                },
                ...
            ]

        ``500`` — If an unexpected error occurs reading the log store.

    Note:
        This endpoint is public — no authentication is required.
        No agents are triggered. No data is written.
    """
    try:
        logs = list(_LOG_STORE)
        logger.info("list_agent_logs | returned %d entries", len(logs))
        return JSONResponse(status_code=200, content=logs)

    except Exception as exc:  # noqa: BLE001
        logger.error("list_agent_logs | unexpected error: %s", exc)
        return JSONResponse(
            status_code=500,
            content={"detail": "Failed to retrieve agent logs."},
        )