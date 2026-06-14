"""
aeam/api/incidents.py

Read-only incident listing API for AEAM.

Provides a single GET endpoint that returns all persisted incidents from
the database via the shared ``DatabaseClient`` held on
``request.app.state.container.db``.

Architecture constraints:
- No database connections created here.
- No agent calls.
- No orchestrator calls.
- No business logic.
- No authentication required (public endpoint).
- All data access via ``request.app.state.container.db``.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/incidents", tags=["incidents"])

# SQL — read-only, no writes.
_SELECT_ALL_INCIDENTS: str = """
    SELECT
        incident_id,
        event_type,
        severity,
        status,
        root_cause,
        confidence
    FROM incidents
    ORDER BY created_at DESC
"""


@router.get(
    "/",
    summary="List all incidents",
    response_description="List of persisted incident records",
)
def list_incidents(request: Request) -> list[dict[str, Any]]:
    """
    Return all persisted incidents ordered by creation date (newest first).

    Reads from the ``incidents`` table via the shared
    :class:`~aeam.integrations.database.DatabaseClient` attached to
    ``request.app.state.container.db``. No database connection is created
    here.

    Args:
        request: Incoming FastAPI request. Used to access the application
                 container via ``request.app.state.container``.

    Returns:
        List of incident dicts, each containing:

        - ``incident_id``  — unique incident identifier.
        - ``event_type``   — anomaly event type (e.g. ``"KPI_ANOMALY"``).
        - ``severity``     — severity level (e.g. ``"HIGH"``).
        - ``status``       — current lifecycle status.
        - ``root_cause``   — identified root cause string, or ``null``.
        - ``confidence``   — investigation confidence score (0–1).

        Returns an empty list if no incidents have been recorded yet.

    Raises:
        HTTPException 500: If the database query fails.

    Example response::

        [
            {
                "incident_id": "a1b2c3d4-...",
                "event_type":  "KPI_ANOMALY",
                "severity":    "HIGH",
                "status":      "COMPLETE",
                "root_cause":  "Runaway thread in payment service",
                "confidence":  0.91
            }
        ]
    """
    db = request.app.state.container.db

    try:
        rows: list[dict[str, Any]] = _fetch_all(db, _SELECT_ALL_INCIDENTS)
    except Exception as exc:
        logger.error("list_incidents | DB query failed: %s", exc)
        raise HTTPException(
            status_code=500,
            detail="Failed to retrieve incidents from the database.",
        ) from exc

    logger.info("list_incidents | returned %d records", len(rows))
    return rows


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _fetch_all(db: Any, query: str) -> list[dict[str, Any]]:
    """
    Execute a SELECT query and return all rows as a list of dicts.

    Uses the SQLAlchemy engine exposed by ``DatabaseClient._engine``
    directly, since :class:`~aeam.integrations.database.DatabaseClient`
    exposes only ``fetch_one`` for single-row reads.

    Args:
        db:    The ``DatabaseClient`` instance from the app container.
        query: Parameterised SQL SELECT string.

    Returns:
        List of row dicts (column name → value). Empty list if no rows.

    Raises:
        SQLAlchemyError: Propagated from the engine on query failure.
    """
    from sqlalchemy import text

    with db._engine.connect() as conn:
        result = conn.execute(text(query))
        rows = result.mappings().all()
        return [dict(row) for row in rows]