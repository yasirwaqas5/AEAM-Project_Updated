"""
aeam/api/logs.py

Agent execution logs API for the AEAM system.

Exposes a read-only GET endpoint over the real, DB-backed ``action_logs``
table that :class:`~aeam.agents.action.action_agent.ActionAgent` writes on
every execution (the 50 most recent rows, with the execution metadata —
duration, retry count, failure reason, validation result — surfaced from
the JSON ``result`` column).

History note (Phase E1, DOC-2/ENG-8): this module originally served an
in-memory mock seeded at import time; the mock generator was dead code
after the endpoint became DB-backed and has been removed.

Rules enforced:
- No agent triggering.
- No orchestrator calls.
- No database writes.
- Read-only.
- Public endpoint — no authentication required.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/logs", tags=["Logs"])

# Phase E6: the pre-E6 endpoint always returned the 50 most recent rows.
# That constant becomes the DEFAULT limit so an unparameterised call is
# byte-identical to today; callers may page deeper with ?limit=&offset=.
_DEFAULT_LIMIT: int = 50


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.get("/agents", response_model=list[dict])
def list_agent_logs(
    request: Request,
    limit: int = Query(
        default=_DEFAULT_LIMIT,
        ge=1,
        le=500,
        description=(
            "Maximum number of agent-log rows to return, newest first. "
            "Defaults to 50 — the pre-E6 fixed page size — so an "
            "unparameterised call is unchanged."
        ),
    ),
    offset: int = Query(
        default=0,
        ge=0,
        description="Number of rows to skip (for pagination).",
    ),
):
    container = request.app.state.container
    try:
        db = container.db
        # Select the JSON `result` column so we can surface the execution
        # metadata (duration, retry count, failure reason, validation result)
        # that ActionAgent embeds there. Index-backed by
        # idx_action_logs_executed_at (Phase E5).
        query = """
        SELECT action_type as agent, incident_id, status,
               result, executed_at as timestamp
        FROM action_logs
        ORDER BY executed_at DESC
        LIMIT :limit OFFSET :offset
        """
        rows = _fetch_all(db, query, {"limit": limit, "offset": offset})
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
                # Granular payload validation errors (e.g. Slack invalid_blocks
                # detail list), when the failure originated from a structured
                # NonRetryableActionError.
                "validation_details": meta.get("details"),
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


def _fetch_all(db: Any, query: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    from sqlalchemy import text

    with db._engine.connect() as conn:
        result = conn.execute(text(query), params or {})
        rows = result.mappings().all()
        return [dict(row) for row in rows]
