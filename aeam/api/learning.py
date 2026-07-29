"""
aeam/api/learning.py

Adaptive Learning API (Phase F2).

The operator-facing surface over the feedback loop: what the platform's
confidence calibration currently is, what a recalibration would do, and the
threshold proposals the Learning Agent has put forward for human decision.

Endpoints (all under ``/api/v1/learning``):

- ``GET  /state``                  — the active calibration, its measured
  improvement, and its reliability curve. Read-only.
- ``GET  /history``                — the version ledger. Read-only.
- ``GET  /proposals``              — threshold proposals, filterable by status.
- ``POST /recalibrate``            — fit from history. Supports ``dry_run``
  so an operator can see what a recalibration would do before committing.
- ``POST /restore``                — re-activate a previous version. The
  named rollback path.
- ``POST /decisions/{proposal_id}`` — record a human decision.

Read and write paths deliberately share no prefix. ``_ENDPOINT_RBAC_MAP``
grades on path alone, not method, so nesting a read under a write prefix
(or the reverse) would let one be graded as the other — which is exactly
how an auditor ends up able to approve a proposal.

Rules enforced (mirrors every other API module in this package):

- All state access via ``request.app.state.container``; no DB connections
  created here.
- **No learning logic lives here.** Fitting, versioning, and the advisory
  boundary are all
  :class:`~aeam.agents.learning.learning_agent.LearningAgent`'s; this module
  translates HTTP to that agent and back, so the API and any future caller
  cannot diverge on what "adopted" means.
- **Authorisation is the middleware's.** ``/state``, ``/history`` and
  ``/proposals`` map to ``logs:view`` so an auditor can inspect calibration
  state and the proposal ledger; everything else under ``/api/v1/learning``
  falls through to ``admin:config`` (SEC-7), so a write surface added later
  is guarded by default rather than by remembering to map it.
- **Attribution is honest.** The acting principal comes from the verified
  JWT when the middleware established one; when it did not (only possible
  under ``ENVIRONMENT=development``), the action is still recorded but
  tagged with where the identity came from.

**What this API cannot do:** apply an approved proposal. Approving records
that a human agrees a threshold should move; moving it is a deployment-
configuration act. That separation is AGENT-5 — an advisory agent whose
recommendation self-applies on approval is not advisory, it is an agent
with a confirmation dialog.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from aeam.agents.learning.learning_agent import (
    InvalidVerdictError,
    LearningAgent,
    LearningError,
    ProposalConflictError,
    ProposalNotFoundError,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/learning", tags=["Learning"])


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class RecalibrateRequest(BaseModel):
    """Body for a recalibration run."""

    dry_run: bool = Field(
        default=False,
        description=(
            "Measure and report without persisting. Lets an operator see the "
            "held-out improvement a recalibration would deliver before adopting it."
        ),
    )
    actor_id: str | None = Field(
        default=None,
        description=(
            "Acting principal. Ignored when the security middleware established "
            "a verified identity — a body can never override a verified JWT."
        ),
    )


class RestoreRequest(BaseModel):
    """Body for restoring a previous calibration version."""

    version: int = Field(description="The calibration version to re-activate.")
    actor_id: str | None = Field(default=None, description="See RecalibrateRequest.actor_id.")


class ProposalVerdictRequest(BaseModel):
    """Body for deciding a learning proposal."""

    verdict: str = Field(description="'approved' or 'rejected'.")
    note: str = Field(default="", description="Free-text rationale for the decision.")
    reviewer_id: str | None = Field(default=None, description="See RecalibrateRequest.actor_id.")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _principal(request: Request, supplied: str | None) -> tuple[str, list[str], str]:
    """Resolve the acting principal and say honestly where it came from.

    Identical precedence to the E9 review router, deliberately: two
    governance surfaces that attribute differently would produce an audit
    trail an operator has to read two ways.
    """
    user_id = getattr(request.state, "user_id", None)
    if isinstance(user_id, str) and user_id.strip() and user_id != "anonymous":
        roles = getattr(request.state, "roles", None)
        return user_id.strip(), list(roles or []), "jwt"

    cleaned = (supplied or "").strip()
    if cleaned:
        return cleaned, [], "request"

    return "unattributed", [], "unattributed"


def _agent(request: Request) -> LearningAgent:
    """Build a LearningAgent over the container's database client.

    Constructed per request rather than held on the container because it is
    stateless over the database and per-request construction keeps the
    composition root from growing another long-lived object (ARCH-1). The
    Orchestrator holds its own instance for the finalize path, which is a
    different lifetime with a different reason.

    Raises:
        HTTPException: 503 when no database client is wired — the honest
                       answer for a surface whose entire job is reading
                       persisted outcomes.
    """
    container = getattr(request.app.state, "container", None)
    db = getattr(container, "db", None)
    if db is None:
        raise HTTPException(
            status_code=503,
            detail="No database client is configured; learning state is unavailable.",
        )

    settings = getattr(container, "settings", None)
    from aeam.intelligence.calibration import CalibrationEngine

    return LearningAgent(
        database_client=db,
        engine=CalibrationEngine(
            min_training_samples=getattr(settings, "LEARNING_MIN_TRAINING_SAMPLES", 60),
            holdout_fraction=getattr(settings, "LEARNING_HOLDOUT_FRACTION", 0.3),
            min_improvement=getattr(settings, "LEARNING_MIN_ECE_IMPROVEMENT", 0.01),
        ),
        history_limit=getattr(settings, "LEARNING_HISTORY_LIMIT", 5000),
    )


def _audit(request: Request, principal: str, action: str, detail: dict[str, Any]) -> None:
    """Record a governance action against the acting principal.

    The middleware already audits that the request happened; this adds WHAT
    was decided, so the trail answers "who recalibrated confidence, and to
    what" without joining back to the calibration tables. Failure is logged
    and swallowed — an audit-sink problem must never invalidate a decision
    that already happened (SEC-6).
    """
    container = getattr(request.app.state, "container", None)
    audit_logger = getattr(container, "audit_logger", None)
    if audit_logger is None:
        return
    try:
        audit_logger.log({
            "user_id": principal,
            "action": action,
            "endpoint": request.url.path,
            "status_code": 200,
            **detail,
        })
    except Exception as exc:  # noqa: BLE001
        logger.warning("learning | audit write failed | action=%s | %s", action, exc)


def _emit_calibration_metrics(active: dict[str, Any] | None) -> None:
    """Publish the E11 calibration gauges from an active calibration row.

    Version 0 with no ECE published is the honest representation of "no
    calibration is active" — distinct from a calibration whose error
    happens to be zero.
    """
    try:
        from aeam.monitoring.metrics import (
            calibration_ece,
            calibration_samples,
            calibration_version,
        )

        if not active:
            calibration_version.set(0)
            return

        calibration_version.set(int(active.get("version") or 0))
        if active.get("ece_before") is not None:
            calibration_ece.labels(stage="raw").set(float(active["ece_before"]))
        if active.get("ece_after") is not None:
            calibration_ece.labels(stage="calibrated").set(float(active["ece_after"]))
        if active.get("training_samples") is not None:
            calibration_samples.labels(split="training").set(int(active["training_samples"]))
        if active.get("holdout_samples") is not None:
            calibration_samples.labels(split="holdout").set(int(active["holdout_samples"]))
    except Exception as exc:  # noqa: BLE001
        logger.warning("learning | calibration metrics not published | %s", exc)


# ---------------------------------------------------------------------------
# Calibration — read
# ---------------------------------------------------------------------------


@router.get("/state", summary="The active confidence calibration and its measured quality")
async def get_calibration(request: Request) -> dict:
    """
    Return the live calibration, or an honest statement that none is active.

    Returns:
        ``{"active": false, "reason": ..., "enabled": bool}`` when no
        calibration is in force, or the active version with the held-out
        ECE/Brier before and after and the reliability curve behind it.

        A deployment with calibration disabled reports ``enabled: false``
        even if a calibration row exists, because the row is not being
        applied and reporting it as active would misdescribe what every
        incident's confidence currently means (EXPL-5).
    """
    container = getattr(request.app.state, "container", None)
    settings = getattr(container, "settings", None)
    enabled = getattr(settings, "LEARNING_CALIBRATION_ENABLED", False) is True

    agent = _agent(request)
    active = agent.active_calibration()
    _emit_calibration_metrics(active)

    if not active:
        return {
            "enabled": enabled,
            "active": False,
            "reason": (
                "No calibration has been adopted; confidence is reported raw."
            ),
        }

    return {
        "enabled": enabled,
        "active": True,
        "applied_to_new_incidents": enabled,
        "version": active.get("version"),
        "calibration_id": active.get("calibration_id"),
        "training_samples": active.get("training_samples"),
        "holdout_samples": active.get("holdout_samples"),
        "ece_before": active.get("ece_before"),
        "ece_after": active.get("ece_after"),
        "brier_before": active.get("brier_before"),
        "brier_after": active.get("brier_after"),
        "curve_before": _maybe_json(active.get("curve_before")),
        "curve_after": _maybe_json(active.get("curve_after")),
        "skipped_counts": _maybe_json(active.get("skipped_counts")),
        "source_window": active.get("source_window"),
        "created_by": active.get("created_by"),
        "created_at": str(active.get("created_at")) if active.get("created_at") else None,
        "reason": active.get("reason"),
        "note": (
            None if enabled else
            "A calibration is stored but LEARNING_CALIBRATION_ENABLED is false, "
            "so incidents are finalized with raw confidence."
        ),
    }


@router.get("/history", summary="Calibration version ledger")
async def get_calibration_history(
    request: Request,
    limit: int = Query(default=20, ge=1, le=200),
) -> dict:
    """
    Return calibration versions newest-first — the rollback ledger.

    Every version ever adopted remains here; superseding never deletes.
    That is what makes the rollback path in ``POST /restore``
    safe, and what keeps the mapping that produced a historical incident's
    calibrated confidence inspectable (COMPAT-7).
    """
    agent = _agent(request)
    versions = agent.calibration_history(limit=limit)
    return {
        "versions": [
            {
                **row,
                "created_at": str(row.get("created_at")) if row.get("created_at") else None,
                "superseded_at": (
                    str(row.get("superseded_at")) if row.get("superseded_at") else None
                ),
            }
            for row in versions
        ],
        "count": len(versions),
    }


# ---------------------------------------------------------------------------
# Calibration — write (privileged)
# ---------------------------------------------------------------------------


@router.post("/recalibrate", summary="Fit a calibration from resolved history")
async def recalibrate(request: Request, body: RecalibrateRequest) -> dict:
    """
    Run the feedback loop and adopt the result if it measurably helps.

    Reads E9 verdicts and finalized incident outcomes, fits an isotonic
    mapping on a training split, and measures it on a held-out split the
    fit never saw. Adopted only when held-out ECE improves by at least
    ``LEARNING_MIN_ECE_IMPROVEMENT`` — a calibration that does not
    demonstrably help is reported, not shipped (PHIL-1).

    **Mutates no historical record.** Every read is a SELECT; the only
    writes are the new ``calibration_models`` row and the status flag on the
    version it supersedes (MEM-2).

    Returns:
        The run's measurements, ``adopted``, and the new ``version`` when
        one was persisted. A refusal is a 200 with ``adopted: false`` and a
        reason — it is a normal outcome, not an error.
    """
    principal, roles, source = _principal(request, body.actor_id)
    agent = _agent(request)

    try:
        result = agent.recalibrate(created_by=principal, dry_run=body.dry_run)
    except Exception as exc:  # noqa: BLE001
        logger.error("learning | recalibration failed | %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Recalibration failed: {exc}") from exc

    if result.get("adopted"):
        _emit_calibration_metrics(agent.active_calibration())
        _audit(request, principal, "learning.recalibrate", {
            "version": result.get("version"),
            "calibration_id": result.get("calibration_id"),
            "ece_before": result.get("ece_before"),
            "ece_after": result.get("ece_after"),
            "training_samples": result.get("training_samples"),
            "holdout_samples": result.get("holdout_samples"),
            "attribution_source": source,
            "roles": roles,
        })

    return result


@router.post("/restore", summary="Re-activate a previous calibration version")
async def restore_calibration(request: Request, body: RestoreRequest) -> dict:
    """
    Roll back to a previously adopted calibration.

    Creates no mapping and re-measures nothing — it re-points ``active`` at
    a version whose measurements are already on record, which is exactly
    what makes rollback safe to perform under pressure.

    Raises:
        HTTPException: ``404`` when the version does not exist.
    """
    principal, roles, source = _principal(request, body.actor_id)
    agent = _agent(request)

    try:
        result = agent.restore_calibration(version=body.version, restored_by=principal)
    except LearningError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    _emit_calibration_metrics(agent.active_calibration())
    _audit(request, principal, "learning.restore_calibration", {
        "restored_version": body.version,
        "attribution_source": source,
        "roles": roles,
    })
    return result


# ---------------------------------------------------------------------------
# Proposals
# ---------------------------------------------------------------------------


@router.get("/proposals", summary="Learning proposals awaiting or holding a human decision")
async def list_proposals(
    request: Request,
    status: str | None = Query(default=None, description="'pending' | 'approved' | 'rejected'"),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    """Return proposals, newest first."""
    agent = _agent(request)
    proposals = agent.list_proposals(status=status, limit=limit)
    return {
        "proposals": [
            {
                **row,
                "evidence": _maybe_json(row.get("evidence")),
                "reviewer_roles": _maybe_json(row.get("reviewer_roles")),
                "created_at": str(row.get("created_at")) if row.get("created_at") else None,
                "decided_at": str(row.get("decided_at")) if row.get("decided_at") else None,
            }
            for row in proposals
        ],
        "count": len(proposals),
        "filter": status,
    }


@router.post("/decisions/{proposal_id}", summary="Record a human decision on a proposal")
async def decide_proposal(
    request: Request,
    proposal_id: str,
    body: ProposalVerdictRequest,
) -> dict:
    """
    Approve or reject a threshold proposal.

    **Approval does not apply the change.** The response says so explicitly
    in its ``applied`` and ``note`` fields. Approving records that an
    authorized human agrees the threshold should move; moving it is a
    deployment-configuration act, and the record is the authorization for
    it. An agent that could enact its own recommendation on approval would
    not be advisory (AGENT-5).

    Raises:
        HTTPException: ``400`` for an unrecognised verdict or missing
                       attribution, ``404`` when the proposal does not
                       exist, ``409`` when it has already been decided.
    """
    reviewer_id, roles, source = _principal(request, body.reviewer_id)
    agent = _agent(request)

    try:
        result = agent.decide_proposal(
            proposal_id=proposal_id,
            verdict=body.verdict,
            reviewer_id=reviewer_id,
            reviewer_roles=roles,
            attribution_source=source,
            note=body.note,
        )
    except InvalidVerdictError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ProposalNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ProposalConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    _audit(request, reviewer_id, "learning.decide_proposal", {
        "proposal_id": proposal_id,
        "verdict": result["status"],
        "subject": result.get("subject"),
        "proposed_value": result.get("proposed_value"),
        "applied": False,
        "attribution_source": source,
        "roles": roles,
    })
    return result


def _maybe_json(value: Any) -> Any:
    """Decode a JSON column that may arrive as text (SQLite) or native (PG)."""
    if isinstance(value, str):
        import json

        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return value
    return value
