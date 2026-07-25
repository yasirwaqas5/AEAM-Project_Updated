"""
aeam/api/incidents.py

Read-only incident listing API for AEAM.

Provides a single GET endpoint that returns persisted incidents from the
database via the shared ``DatabaseClient`` held on
``request.app.state.container.db``.

Phase E6 (Scale Contracts) — bounded consumption:
- New OPTIONAL query parameters (``limit``, ``offset``, ``severity``,
  ``event_type``, ``requires_human``) bound both the payload and the work
  the database does.
- **Defaults preserve today's exact behaviour byte-for-byte** (COMPAT-2/4):
  a parameter-less ``GET /api/v1/incidents/`` still returns the full list
  of incidents as a bare JSON array, ordered newest-first. Every consumer
  that does ``fetch("/api/v1/incidents/")`` is unaffected.
- When a caller paginates/filters, the response is still a bare JSON array
  (the shape never changes); the total matching count is returned in the
  ``X-Total-Count`` response header so a paged client can compute pages
  without a second endpoint.

Architecture constraints (unchanged):
- No database connections created here.
- No agent / orchestrator / business logic.
- All data access via ``request.app.state.container.db``.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, Response

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/incidents", tags=["incidents"])

# Base read — read-only, newest-first (index-backed by idx_incidents_timestamp,
# Phase E5). Filters and paging are appended as parameterised clauses.
_SELECT_BASE: str = "SELECT * FROM incidents"
_ORDER: str = " ORDER BY timestamp DESC"

# Backwards-compatible full-scan query + helper, imported by
# aeam/api/observability.py (which summarizes across every incident and
# therefore genuinely wants the unbounded read). Kept verbatim so that
# import site is unaffected by the E6 pagination work.
_SELECT_ALL_INCIDENTS: str = _SELECT_BASE + _ORDER


def _fetch_all(db: Any, query: str) -> list[dict[str, Any]]:
    """Execute a SELECT and return all rows as dicts (no params variant).

    Retained for :mod:`aeam.api.observability`, which reuses this exact
    helper + query string to avoid a second incident-reading code path.
    """
    from sqlalchemy import text

    with db._engine.connect() as conn:
        result = conn.execute(text(query))
        return [dict(row) for row in result.mappings().all()]

# Whitelisted equality filter columns → the query param that drives them.
# Kept explicit (never interpolate a user string as a column name).
_EQ_FILTERS: dict[str, str] = {
    "severity": "severity",
    "event_type": "event_type",
}


@router.get(
    "/",
    summary="List incidents (newest first; optionally paginated and filtered)",
    response_description=(
        "A JSON array of incident records. Parameter-less calls return every "
        "incident (unchanged). With limit/offset/filters the array is bounded; "
        "the total matching count is in the X-Total-Count header."
    ),
)
def list_incidents(
    request: Request,
    response: Response,
    limit: int | None = Query(
        default=None,
        ge=1,
        le=1000,
        description=(
            "Maximum number of incidents to return. Omit for the full list "
            "(today's default behaviour). Max 1000 per page."
        ),
    ),
    offset: int = Query(
        default=0,
        ge=0,
        description="Number of incidents to skip (for pagination). Requires limit to be meaningful.",
    ),
    severity: str | None = Query(
        default=None,
        description="Optional exact-match filter on severity (e.g. HIGH, CRITICAL).",
    ),
    event_type: str | None = Query(
        default=None,
        description="Optional exact-match filter on event_type (e.g. kpi_anomaly).",
    ),
    requires_human: bool | None = Query(
        default=None,
        description="Optional filter: only incidents flagged (or not) for human review.",
    ),
) -> list[dict[str, Any]]:
    """
    Return persisted incidents ordered by timestamp (newest first).

    A parameter-less call is byte-compatible with the pre-E6 endpoint:
    the entire ``incidents`` table as a bare JSON array. Supplying
    ``limit``/``offset``/filters bounds the result and populates the
    ``X-Total-Count`` header with the total number of matching rows.
    """
    db = request.app.state.container.db

    # Phase E6: clamp the requested page size to the configured hard bound
    # so no single request can pull an unbounded payload, even if the
    # static Query ceiling is ever raised.
    settings = getattr(request.app.state.container, "settings", None)
    max_page = int(getattr(settings, "API_MAX_PAGE_SIZE", 1000)) if settings else 1000
    if limit is not None:
        limit = min(limit, max_page)

    where_clauses: list[str] = []
    params: dict[str, Any] = {}

    if severity:
        where_clauses.append("severity = :severity")
        params["severity"] = severity
    if event_type:
        where_clauses.append("event_type = :event_type")
        params["event_type"] = event_type
    if requires_human is not None:
        where_clauses.append("requires_human = :requires_human")
        params["requires_human"] = requires_human

    where_sql = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    try:
        # Only compute the total count when the caller is paginating or
        # filtering — a plain full-list call pays no extra query, staying
        # identical to before (COMPAT-2).
        paginating = limit is not None or offset > 0 or bool(where_clauses)
        if paginating:
            total_row = db.fetch_one(
                f"SELECT COUNT(*) AS n FROM incidents{where_sql}", params
            )
            total = int(total_row["n"]) if total_row and total_row.get("n") is not None else 0
            response.headers["X-Total-Count"] = str(total)

        query = _SELECT_BASE + where_sql + _ORDER
        page_params = dict(params)
        if limit is not None:
            query += " LIMIT :limit OFFSET :offset"
            page_params["limit"] = limit
            page_params["offset"] = offset

        rows: list[dict[str, Any]] = db.fetch_all(query, page_params)
    except Exception as exc:  # noqa: BLE001
        logger.error("list_incidents | DB query failed: %s", exc)
        raise HTTPException(
            status_code=500,
            detail="Failed to retrieve incidents from the database.",
        ) from exc

    logger.info(
        "list_incidents | returned %d records | limit=%s offset=%d filters=%s",
        len(rows), limit, offset, where_clauses or "none",
    )
    return rows
