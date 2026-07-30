"""
aeam/api/replay.py

Investigation & Timeline Replay API (Phase F5).

The durable data source the Replay workspace never had. Before this phase
the console derived its own narrative client-side from an incident's summary
fields; there was no backend that reconstructed an investigation as an
ordered, navigable sequence, so two readers could disagree about what
happened and neither could cite the record.

Endpoints (all under ``/api/v1/replay``):

- ``GET /{incident_id}``           — the full reconstruction: ordered stages,
  honest gaps, and the timeline in one payload. Bounded.
- ``GET /{incident_id}/stages``    — the stage sequence alone, paginated,
  with ``X-Total-Count`` for a paged client.
- ``GET /{incident_id}/timeline``  — the timeline alone.

**Every endpoint is a GET, and that is structural, not stylistic.** There is
no POST, PUT, PATCH, or DELETE in this module, no write helper, and no
import of anything that could execute an investigation stage. Replay
reconstructs history; it never re-executes it and never records that it
looked (MEM-2). A thousand replays leave the database bit-identical.

What this module cannot reach
-----------------------------
``RuleEngine``, ``StatisticalDetector``, ``KPIAgent``, ``ForecastAgent``,
the business graph, ``PolicyAgent``, ``ActionAgent``, and every LLM client.
None is imported here or by :mod:`aeam.intelligence.replay`, so the "no
re-execution" guarantee is enforced by the import graph rather than by
reviewer vigilance — and a regression test asserts it stays that way.

Rules enforced (mirrors every other API module in this package):

- All state access via ``request.app.state.container``; no DB connections
  created here.
- **No replay logic lives here.** Stage ordering, gap honesty, and duration
  attribution are all :mod:`aeam.intelligence.replay`'s, so the API and the
  console can never diverge on what an investigation did.
- **Authorisation is the audit tier's** (SEC-6). Replay reconstructs an
  incident's complete decision trail — the same material the audit log
  exposes — so it maps to ``logs:view``, reachable by the auditor role, and
  is guarded by that grant for every path under the prefix.
- **Every read is bounded** (E6). One incident is fetched by primary key,
  and its stages are paged with a ceiling the caller cannot exceed.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, Response

from aeam.intelligence.replay import (
    MAX_STAGE_LIMIT,
    STAGE_CATALOG,
    InvestigationReplayBuilder,
    TimelineBuilder,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/replay", tags=["Replay"])

# The ONE query this module issues. Parameterised, primary-key bounded, and
# read-only — there is no second data path and no write path at all.
_SELECT_INCIDENT: str = "SELECT * FROM incidents WHERE incident_id = :incident_id"

_replay_builder = InvestigationReplayBuilder()
_timeline_builder = TimelineBuilder()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _incident_row(request: Request, incident_id: str) -> dict[str, Any]:
    """
    Fetch one persisted incident by id.

    Raises:
        HTTPException: 503 when no database client is wired, 404 when the
                       incident does not exist. A missing incident is a 404
                       and not an empty replay: "we have no record of this"
                       and "this investigation did nothing" are different
                       answers and an auditor must not have to guess which
                       one they received.
    """
    container = getattr(request.app.state, "container", None)
    db = getattr(container, "db", None)
    if db is None:
        raise HTTPException(
            status_code=503,
            detail="No database client is configured; replay is unavailable.",
        )
    try:
        row = db.fetch_one(_SELECT_INCIDENT, {"incident_id": incident_id})
    except Exception as exc:  # noqa: BLE001
        logger.error("replay | incident read failed | incident_id=%s | %s", incident_id, exc)
        raise HTTPException(status_code=500, detail=f"Incident read failed: {exc}") from exc

    if not row:
        raise HTTPException(
            status_code=404,
            detail=f"No incident with id {incident_id!r} is recorded.",
        )
    return dict(row)


# ---------------------------------------------------------------------------
# Read — the entire surface
# ---------------------------------------------------------------------------


@router.get("/catalog", summary="The canonical investigation stage sequence")
async def get_stage_catalog() -> dict:
    """
    The stage vocabulary replay reconstructs against, and the phase each
    stage arrived in.

    Exposed so a console can render a gap with the same context the backend
    used to identify it, rather than hardcoding a second copy of the stage
    list that could drift from this one.
    """
    return {
        "stages": [
            {
                "key": spec.key,
                "label": spec.label,
                "category": spec.category,
                "introduced_in": spec.introduced_in,
                # False means absence is expected rather than a gap: the
                # stage is conditional by design (an escalation, an LLM
                # parse failure, an approval gate).
                "absence_is_a_gap": spec.expected,
            }
            for spec in STAGE_CATALOG
        ],
        "max_stage_limit": MAX_STAGE_LIMIT,
    }


@router.get("/{incident_id}", summary="Reconstruct one investigation, stage by stage")
async def get_replay(
    request: Request,
    incident_id: str,
    offset: int = Query(default=0, ge=0, description="Stages to skip."),
    limit: int | None = Query(
        default=None, ge=1, le=MAX_STAGE_LIMIT,
        description=f"Maximum stages returned (ceiling {MAX_STAGE_LIMIT}).",
    ),
    include_timeline: bool = Query(
        default=True,
        description="Include the timeline projection alongside the stages.",
    ),
) -> dict:
    """
    Rebuild ``incident_id``'s investigation from its persisted record.

    Stages come back in the order they were recorded — never re-sorted into
    a canonical pipeline order — and a stage the record does not contain is
    reported as an explicit gap rather than reconstructed.

    Nothing is executed and nothing is written; the response is a projection
    of one already-persisted row.
    """
    incident = _incident_row(request, incident_id)
    payload = _replay_builder.reconstruct(incident, offset=offset, limit=limit)
    if include_timeline:
        payload["timeline"] = _timeline_builder.build(incident)
    logger.info(
        "replay | incident_id=%s | stages=%d | returned=%d | gaps=%d",
        incident_id, payload["total_stages"], len(payload["stages"]), len(payload["gaps"]),
    )
    return payload


@router.get("/{incident_id}/stages", summary="The recorded stage sequence, paginated")
async def get_replay_stages(
    request: Request,
    response: Response,
    incident_id: str,
    offset: int = Query(default=0, ge=0, description="Stages to skip."),
    limit: int | None = Query(
        default=None, ge=1, le=MAX_STAGE_LIMIT,
        description=f"Maximum stages returned (ceiling {MAX_STAGE_LIMIT}).",
    ),
) -> dict:
    """
    The stage sequence alone — the streaming/pagination path for an
    investigation whose findings array is large.

    ``X-Total-Count`` carries the full stage count so a paged client can
    compute pages without a second request (the same header contract
    ``/api/v1/incidents`` already uses, reused rather than reinvented).
    """
    incident = _incident_row(request, incident_id)
    payload = _replay_builder.reconstruct(incident, offset=offset, limit=limit)
    response.headers["X-Total-Count"] = str(payload["total_stages"])
    return {
        "incident_id": payload["incident_id"],
        "total_stages": payload["total_stages"],
        "offset": payload["offset"],
        "limit": payload["limit"],
        "truncated": payload["truncated"],
        "stages": payload["stages"],
        "gaps": payload["gaps"],
    }


@router.get("/{incident_id}/timeline", summary="The investigation timeline (measured time only)")
async def get_replay_timeline(request: Request, incident_id: str) -> dict:
    """
    Place ``incident_id``'s recorded stages against measured time.

    Every figure is a persisted measurement. No stage is given a wall-clock
    position (per-stage start times were never persisted), no unmeasured
    stage is filled with zero, and time the instrumentation did not cover is
    disclosed as unattributed rather than distributed across stages.
    """
    return _timeline_builder.build(_incident_row(request, incident_id))
