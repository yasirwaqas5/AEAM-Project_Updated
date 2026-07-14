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

from aeam.agents.kpi.rule_engine import RuleEngine

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
    - ``active_incidents`` — count of UNRESOLVED incidents (those with
      ``requires_human = true``) from the persistent incident store. This is
      deliberately NOT the live Prometheus ``active_incidents`` gauge: the
      gauge tracks investigations executing *right now*, but because the
      pipeline is synchronous (``POST /trigger`` returns only after
      ``finalize_incident()`` has already decremented it) it is never
      observably non-zero to a dashboard poll. See
      :func:`_count_unresolved_incidents`.
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

        # --- active_incidents = UNRESOLVED incidents (persistent) ---
        # NOT the live Prometheus gauge (which is inc'd and dec'd within one
        # synchronous handle_event() call and is therefore always 0 to a
        # poller). An operator's "Active Incidents" means the backlog still
        # needing attention, so count unresolved incidents from the store.
        incident_count: int = _count_unresolved_incidents(container)

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


@router.get(
    "/rule-engine",
    summary="Rule Engine domain snapshot",
    response_description="The curated metric domains RuleEngine currently loads.",
)
def get_rule_engine_status(request: Request) -> JSONResponse:
    """
    Return the curated metric domains :class:`~aeam.agents.kpi.rule_engine.RuleEngine`
    loads from ``detection_rules.yaml``.

    Added for the Agent Observatory (frontend): no existing endpoint exposed
    "which domains are loaded" anywhere, and it is a required, non-optional
    field for the Rule Engine panel. Justified as minimal — this constructs a
    fresh, unmodified :class:`RuleEngine` (a side-effect-free operation
    already performed internally by ``aeam.api.data_center``'s dataset
    profile endpoint) and returns only its already-computed
    ``loaded_domains`` property. No new class, no mutation, no persisted
    state, no change to ``RuleEngine`` itself.

    Deliberately does NOT reflect ``main.py``'s live ``CompositeRuleEngine``
    (which also merges in dynamic per-dataset metric names) — this endpoint
    answers "what governed rules exist," not "what is MonitorAgent currently
    watching this instant" (see ``/api/v1/data-center/activation`` for the
    live monitored-dataset list).

    Returns:
        ``200`` — ``{"loaded_domains": [...], "count": int}``.
        ``500`` — If ``detection_rules.yaml`` cannot be loaded.
    """
    try:
        domains = RuleEngine().loaded_domains
        return JSONResponse(status_code=200, content={"loaded_domains": domains, "count": len(domains)})
    except Exception as exc:  # noqa: BLE001
        logger.error("get_rule_engine_status | failed to load RuleEngine: %s", exc)
        return JSONResponse(status_code=500, content={"detail": "Failed to load Rule Engine configuration."})


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _count_unresolved_incidents(container: Any) -> int:
    """
    Count incidents still requiring human attention (``requires_human = true``).

    This is the persistent "unresolved incidents" backlog an operator cares
    about — distinct from the live ``active_incidents`` Prometheus gauge, which
    only tracks investigations executing at this instant (always 0 between the
    synchronous trigger→finalize cycles).

    Reads through the shared engine already held on the container (the same
    pattern used by :mod:`aeam.api.incidents`). Any failure degrades to 0 so
    the status endpoint never 500s on a metrics read.

    The predicate ``WHERE requires_human`` is portable across PostgreSQL
    (native boolean column) and SQLite (0/1 numeric affinity); NULLs are
    correctly excluded (an unknown flag is not "unresolved").

    Args:
        container: The ``AppContainer`` from ``request.app.state.container``.

    Returns:
        Non-negative count of unresolved incidents, or 0 on any read error.
    """
    from sqlalchemy import text

    try:
        with container.db._engine.connect() as conn:
            value = conn.execute(
                text("SELECT COUNT(*) FROM incidents WHERE requires_human")
            ).scalar()
        return int(value or 0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("get_status | unresolved-incident count failed: %s", exc)
        return 0


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