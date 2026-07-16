"""
aeam/api/observability.py

Enterprise Observability API (Phase D3).

Exposes a single read-only endpoint summarizing how AEAM itself is
performing across every completed investigation. Reuses the EXACT SAME
``incidents`` table read `aeam/api/incidents.py` already performs (same SQL,
same ``DatabaseClient`` access path) -- this is not a second data-access
mechanism, and this module writes nothing.

Architecture constraints (same as incidents.py):
- No database connections created here.
- No agent/Orchestrator calls.
- No business logic beyond parsing the persisted ``findings`` JSON text and
  delegating to :class:`~aeam.intelligence.observability.ObservabilityEngine`.
- No authentication required (public, read-only endpoint).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from aeam.api.incidents import _SELECT_ALL_INCIDENTS, _fetch_all
from aeam.intelligence.observability import ObservabilityEngine

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/observability", tags=["observability"])

_engine = ObservabilityEngine()


@router.get(
    "/",
    summary="Summarize AEAM's own investigation quality across all completed incidents",
    response_description="Cross-incident observability summary (hit rates, trends, overall AI health).",
)
def get_observability_summary(request: Request) -> JSONResponse:
    """
    Return the Enterprise Observability summary across every persisted incident.

    Reuses the identical ``SELECT * FROM incidents`` read
    ``aeam.api.incidents.list_incidents`` already performs (same SQL string,
    same ``_fetch_all`` helper) -- no second query, no second table. Each
    row's ``findings`` column is stored as JSON-encoded text (see
    ``DatabaseClient.insert``); this endpoint parses it back to a list
    before handing it to :class:`~aeam.intelligence.observability.ObservabilityEngine`,
    exactly mirroring what the frontend's own ``parseMaybeJSON``/``getFindings``
    helpers already do client-side.

    Args:
        request: Incoming FastAPI request. Used to access the app container.

    Returns:
        ``200`` — the observability summary (see
        ``ObservabilityEngine.summarize`` for the full field list).
        ``500`` — Unexpected failure (DB error or summarization error).
    """
    db = request.app.state.container.db

    try:
        rows: list[dict[str, Any]] = _fetch_all(db, _SELECT_ALL_INCIDENTS)
    except Exception as exc:  # noqa: BLE001
        logger.error("get_observability_summary | DB query failed: %s", exc)
        raise HTTPException(
            status_code=500,
            detail="Failed to retrieve incidents from the database.",
        ) from exc

    incidents: list[dict[str, Any]] = []
    for row in rows:
        incident = dict(row)
        findings = incident.get("findings")
        if isinstance(findings, str):
            try:
                incident["findings"] = json.loads(findings) if findings else []
            except (json.JSONDecodeError, TypeError):
                incident["findings"] = []
        incidents.append(incident)

    try:
        summary = _engine.summarize(incidents)
    except Exception as exc:  # noqa: BLE001
        logger.error("get_observability_summary | summarization failed: %s", exc)
        raise HTTPException(
            status_code=500,
            detail=f"Observability summarization failed: {exc}",
        ) from exc

    logger.info(
        "get_observability_summary | total_investigations=%d | overall_ai_health=%s",
        summary.get("total_investigations"),
        (summary.get("overall_ai_health") or {}).get("score"),
    )
    return JSONResponse(status_code=200, content=summary)
