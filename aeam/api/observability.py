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

from aeam.api.incidents import _SELECT_ALL_INCIDENTS, _fetch_all  # noqa: F401 (compat re-export)
from aeam.intelligence.observability import ObservabilityEngine

logger = logging.getLogger(__name__)


def _fetch_all_params(db: Any, query: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Parameterised sibling of incidents._fetch_all — used for the bounded
    (LIMIT :limit) observability window read (Phase E6)."""
    from sqlalchemy import text

    with db._engine.connect() as conn:
        result = conn.execute(text(query), params)
        return [dict(row) for row in result.mappings().all()]

router = APIRouter(prefix="/api/v1/observability", tags=["observability"])

_engine = ObservabilityEngine()

# Phase E6 (OBS-2): the observability summary is an O(history) aggregate —
# without a cap it re-parses every incident's findings JSON on every poll.
# When OBSERVABILITY_RETENTION_LIMIT is not explicitly configured, this
# sane default bounds the read to the N most-recent incidents. The window
# that was actually applied is DISCLOSED in the response (``retention``
# block) so the number is never silently windowed.
_DEFAULT_RETENTION_WINDOW: int = 1000


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
    container = request.app.state.container
    db = container.db
    # Phase D4 Enterprise Configuration Engine: read-only override lookup.
    # `settings` may be absent (e.g. a minimal test app) -- falls back to
    # the module-level, all-defaults engine/unbounded read exactly as
    # before this phase.
    settings = getattr(container, "settings", None)
    trend_window = getattr(settings, "OBSERVABILITY_TREND_WINDOW", None) if settings else None
    configured_retention = getattr(settings, "OBSERVABILITY_RETENTION_LIMIT", None) if settings else None
    engine = ObservabilityEngine(trend_window=trend_window) if trend_window is not None else _engine

    # Phase E6 (OBS-2): apply a sane default window when the operator has
    # not configured one, so this O(history) aggregate stays bounded at any
    # volume. An explicit setting (including a deliberately large one) still
    # wins. The effective window is disclosed in the response.
    effective_window: int = int(configured_retention) if configured_retention is not None else _DEFAULT_RETENTION_WINDOW

    # Total available (for honest disclosure of whether windowing dropped rows).
    try:
        total_row = db.fetch_one("SELECT COUNT(*) AS n FROM incidents")
        total_available = int(total_row["n"]) if total_row and total_row.get("n") is not None else 0
    except Exception:  # noqa: BLE001
        total_available = 0

    # Bound the read at the DB (index-backed by idx_incidents_timestamp,
    # Phase E5) instead of fetching everything and slicing in Python.
    windowed_query = _SELECT_ALL_INCIDENTS + " LIMIT :limit"
    try:
        rows: list[dict[str, Any]] = _fetch_all_params(db, windowed_query, {"limit": effective_window})
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
        summary = engine.summarize(incidents)
    except Exception as exc:  # noqa: BLE001
        logger.error("get_observability_summary | summarization failed: %s", exc)
        raise HTTPException(
            status_code=500,
            detail=f"Observability summarization failed: {exc}",
        ) from exc

    # Phase E6 (OBS-2 / EXPL-5): disclose the window that was actually
    # applied, so a consumer never mistakes a windowed summary for a
    # whole-history one. Additive field (COMPAT-4) — existing readers that
    # ignore it are unaffected.
    if isinstance(summary, dict):
        summary["retention"] = {
            "window": effective_window,
            "source": "configured" if configured_retention is not None else "default",
            "incidents_considered": len(incidents),
            "incidents_available": total_available,
            "windowed": total_available > len(incidents),
        }

    logger.info(
        "get_observability_summary | considered=%d | available=%d | window=%d | overall_ai_health=%s",
        len(incidents), total_available, effective_window,
        (summary.get("overall_ai_health") or {}).get("score"),
    )
    return JSONResponse(status_code=200, content=summary)
