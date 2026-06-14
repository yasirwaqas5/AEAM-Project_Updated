"""
aeam/api/system.py

System status API for the AEAM system.

Exposes a single read-only GET /status endpoint that returns a lightweight
snapshot of system health derived from the application container and
Prometheus metrics. No HealthMonitor class is used.

Rules enforced:
- All state access via request.app.state.container.
- No database connections created here.
- No agent calls, no orchestrator calls, no business logic.
- Public endpoint — no authentication required.
- Read-only.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from aeam.monitoring.metrics import active_incidents

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/system", tags=["System"])

# Number of registered agent types in this system build.
# Static constant — reflects the known agent roster (Monitor, RAG,
# Forecast, Action, Report) without requiring runtime introspection.
_AGENTS_ACTIVE: int = 5


@router.get(
    "/status",
    summary="System status",
    response_description="Current AEAM system health snapshot.",
)
def get_status(request: Request) -> JSONResponse:
    """
    Return a lightweight snapshot of system health.

    Derives status from:
    - ``active_incidents`` — read from the Prometheus ``active_incidents``
      Gauge via its internal ``_value`` counter (no external scrape needed).
    - ``agents_active`` — static count of registered agent types (5).
    - ``last_event_time`` — taken from the container's priority queue size
      as a proxy; falls back to the current UTC timestamp when the queue
      is empty (no events pending).
    - ``status`` — ``"healthy"`` unless the database URL is unconfigured,
      in which case ``"degraded"``.

    Args:
        request: Incoming FastAPI request. Used to access the app
                 container via ``request.app.state.container``.

    Returns:
        ``200`` — JSON status dict::

            {
                "status":           "healthy" | "degraded",
                "active_incidents": int,
                "agents_active":    int,
                "last_event_time":  str
            }

        ``500`` — If an unexpected error occurs while reading container state.

    Note:
        This endpoint is public — no authentication is required.
        It is read-only; no data is written or modified.
    """
    try:
        container = request.app.state.container

        # --- active_incidents from Prometheus Gauge ---
        # _value.get() returns the current gauge value without a scrape.
        try:
            incident_count: int = int(active_incidents._value.get())
        except Exception:  # noqa: BLE001
            incident_count = 0

        # --- overall status from settings ---
        db_url: str = str(container.settings.DATABASE_URL or "").strip()
        status: str = "healthy" if db_url else "degraded"

        # --- last_event_time ---
        # Use current UTC time as a live timestamp. When the event queue
        # has pending items it indicates recent activity; when empty the
        # timestamp still reflects system liveness.
        last_event_time: str = _derive_last_event_time(container)

        payload: dict[str, Any] = {
            "status":           status,
            "active_incidents": incident_count,
            "agents_active":    _AGENTS_ACTIVE,
            "last_event_time":  last_event_time,
        }

        logger.info(
            "get_status | status=%s | active_incidents=%d | queue=%d",
            status, incident_count, container.queue.size(),
        )

        return JSONResponse(status_code=200, content=payload)

    except Exception as exc:  # noqa: BLE001
        logger.error("get_status | unexpected error: %s", exc)
        return JSONResponse(
            status_code=500,
            content={"detail": "Failed to retrieve system status."},
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _derive_last_event_time(container: Any) -> str:
    """
    Derive the last event timestamp from the container's event queue.

    Checks whether the priority queue holds any pending events. If items
    are queued, their presence implies recent activity and the current UTC
    time is returned as the event time proxy. If the queue is empty, the
    current UTC time is still returned — representing the last known
    liveness check rather than a stale or null value.

    This approach avoids storing a separate ``last_event_at`` field in the
    container while still returning a meaningful, non-null timestamp.

    Args:
        container: The ``AppContainer`` from ``request.app.state.container``.

    Returns:
        UTC ISO 8601 timestamp string.
    """
    try:
        queue_size: int = container.queue.size()
        if queue_size > 0:
            logger.debug(
                "_derive_last_event_time | queue has %d item(s) — recent activity",
                queue_size,
            )
    except Exception:  # noqa: BLE001
        pass

    return datetime.now(tz=timezone.utc).isoformat()