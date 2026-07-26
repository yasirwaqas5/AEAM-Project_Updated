"""
aeam/api/review.py

Human Review API — the write path the Human Review workspace never had
(Phase E9).

Before this phase the console could offer Approve / Reject / Request Changes
/ Escalate, but there was nowhere to send them: the incidents table has no
verdict columns and no endpoint accepted one, so the workspace recorded
verdicts in browser session state and said so. This router is that missing
write path, and with it the workspace's disclaimer stops being true.

Endpoints (all under ``/api/v1/review``):

- ``GET  /queue``                        — incidents awaiting approval.
- ``GET  /verdicts``                     — verdict history.
- ``GET  /incidents/{incident_id}``      — one incident's approval + verdicts.
- ``POST /incidents/{incident_id}/approve``
- ``POST /incidents/{incident_id}/reject``
- ``POST /incidents/{incident_id}/verdict`` — the full verdict vocabulary,
  including ``changes_requested`` and ``escalated``.

Rules enforced (mirrors every other API module in this package):

- All state access via ``request.app.state.container``; no DB connections
  created here, no repositories instantiated per call beyond the thin
  registry classes the rest of the package already constructs this way.
- **No workflow logic lives here.** Chain ordering, idempotency, and
  execution release are all
  :class:`~aeam.governance.human_review.HumanReviewService`'s — this module
  translates HTTP to that service and back, so the API and any future caller
  cannot diverge on what "approved" means.
- **Authorisation is the middleware's**, not this router's. ``/api/v1/review``
  maps to ``actions:approve`` in ``_ENDPOINT_RBAC_MAP`` (SEC-3/SEC-7); the
  read-only queue and history map to ``incidents:view``. This module never
  re-implements a permission check.
- **Attribution is honest.** The acting principal comes from the verified JWT
  when the middleware established one; when it did not (only possible with
  ``ENVIRONMENT=development``, where the middleware bypasses everything), the
  verdict is still recorded but tagged with where the identity came from, so
  the audit trail never implies a cryptographically-established identity it
  does not have.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from aeam.governance.human_review import (
    ApprovalConflictError,
    ApprovalNotFoundError,
    HumanReviewService,
    InvalidVerdictError,
)
from aeam.registry.models import AttributionSource, Verdict

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/review", tags=["Human Review"])


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------

class VerdictRequest(BaseModel):
    """A reviewer's decision on one incident."""

    note: str | None = Field(
        default=None,
        description="Optional free-text reviewer note, recorded verbatim with the verdict.",
    )
    reviewer_id: str | None = Field(
        default=None,
        description=(
            "Fallback principal, used ONLY when no authenticated identity is "
            "available (i.e. ENVIRONMENT=development, where the security "
            "middleware bypasses authentication). Ignored whenever a verified "
            "JWT principal exists — a caller can never override the "
            "authenticated identity. Verdicts recorded this way are tagged "
            "attribution_source='request' so the audit trail stays honest."
        ),
    )


class FullVerdictRequest(VerdictRequest):
    """A verdict from the full vocabulary, not just approve/reject."""

    verdict: str = Field(
        description=(
            "One of: approved, rejected, changes_requested, escalated. Only "
            "'approved' advances the approval chain; only 'rejected' halts it."
        ),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _service(request: Request) -> HumanReviewService:
    """
    The shared :class:`HumanReviewService` from the container.

    ``503`` (never a silent no-op) if the platform was built without it —
    the same "this endpoint never pretends" convention the Data Center's
    activation endpoints already use.
    """
    service = getattr(request.app.state.container, "human_review_service", None)
    if service is None:
        raise HTTPException(
            status_code=503,
            detail="Human review is not available in this deployment "
                   "(no HumanReviewService configured).",
        )
    return service


def _principal(request: Request, body: VerdictRequest | None) -> tuple[str, list[str], str]:
    """
    Resolve the acting principal and say honestly where it came from.

    Precedence:
    1. The verified-JWT identity the security middleware put on
       ``request.state`` (E3). A request body can never override it.
    2. ``reviewer_id`` from the body — reachable only when the middleware
       bypassed authentication (``ENVIRONMENT=development``).
    3. ``"unattributed"``.

    Returns:
        ``(reviewer_id, roles, attribution_source)``.
    """
    user_id = getattr(request.state, "user_id", None)
    if isinstance(user_id, str) and user_id.strip() and user_id != "anonymous":
        roles = getattr(request.state, "roles", None)
        return user_id.strip(), list(roles or []), AttributionSource.JWT

    supplied = (body.reviewer_id or "").strip() if body and body.reviewer_id else ""
    if supplied:
        return supplied, [], AttributionSource.REQUEST

    return "unattributed", [], AttributionSource.UNATTRIBUTED


def _audit(request: Request, principal: str, outcome: dict[str, Any], source: str) -> None:
    """
    Append the verdict to the audit trail with the acting principal.

    The security middleware already audits that the request happened; this
    adds WHAT was decided — tier, chain position, resulting status, and any
    actions that execution released — so the audit trail answers "who
    approved what" without joining back to the review tables. A failure here
    is logged and swallowed: an audit-sink problem must never invalidate a
    verdict that is already persisted.
    """
    audit_logger = getattr(request.app.state, "audit_logger", None)
    if audit_logger is None:
        return
    try:
        audit_logger.log({
            "user_id": principal,
            "action": f"review_verdict:{outcome.get('verdict')}",
            "endpoint": request.url.path,
            "status_code": 200,
            "incident_id": outcome.get("incident_id"),
            "approval_id": outcome.get("approval_id"),
            "approval_status": outcome.get("status"),
            "tier": outcome.get("tier"),
            "tier_label": outcome.get("tier_label"),
            "required_tiers": outcome.get("required_tiers"),
            "executed_actions": outcome.get("executed_actions"),
            "attribution_source": source,
            "idempotent": outcome.get("idempotent"),
        })
    except Exception as exc:  # noqa: BLE001
        logger.error("review | audit write failed (verdict already persisted): %s", exc)


def _iso(value: Any) -> str | None:
    """
    Normalise a timestamp field to an ISO-8601 string for JSON responses
    (mirrors the identical helper in ingest.py / knowledge.py).

    Needed because the two dialects disagree: PostgreSQL returns a real
    ``datetime`` for a TIMESTAMP column while SQLite returns the stored
    string. Serialising the raw value works on one and 500s on the other.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _approval_dict(approval: Any) -> dict[str, Any]:
    """Serialise an IncidentApproval for the console."""
    tiers = list(approval.required_tiers or [])
    current = int(approval.current_tier or 0)
    return {
        "approval_id": approval.approval_id,
        "incident_id": approval.incident_id,
        "investigation_id": approval.investigation_id,
        "event_type": approval.event_type,
        "metric": approval.metric,
        "severity": approval.severity,
        "status": approval.status,
        "required_tiers": tiers,
        "current_tier": current,
        "awaiting_tier": tiers[current] if current < len(tiers) else None,
        "remaining_tiers": tiers[current:] if current < len(tiers) else [],
        "pending_actions": [
            entry.get("step") for entry in (approval.pending_actions or [])
            if isinstance(entry, dict)
        ],
        "executed_actions": list(approval.executed_actions or []),
        "skipped_actions": list(approval.skipped_actions or []),
        "created_at": _iso(approval.created_at),
        "updated_at": _iso(approval.updated_at),
    }


def _verdict_dict(verdict: Any) -> dict[str, Any]:
    """Serialise a ReviewVerdict for the console."""
    return {
        "verdict_id": verdict.verdict_id,
        "approval_id": verdict.approval_id,
        "incident_id": verdict.incident_id,
        "tier": verdict.tier,
        "tier_label": verdict.tier_label,
        "verdict": verdict.verdict,
        "reviewer_id": verdict.reviewer_id,
        "reviewer_roles": list(verdict.reviewer_roles or []),
        "attribution_source": verdict.attribution_source,
        "note": verdict.note,
        "created_at": _iso(verdict.created_at),
    }


def _submit(request: Request, incident_id: str, verdict: str, body: VerdictRequest) -> JSONResponse:
    """Shared path for every verdict endpoint — one place, one behaviour."""
    service = _service(request)
    principal, roles, source = _principal(request, body)

    try:
        outcome = service.submit_verdict(
            incident_id=incident_id,
            verdict=verdict,
            reviewer_id=principal,
            reviewer_roles=roles,
            attribution_source=source,
            note=body.note if body else None,
        )
    except InvalidVerdictError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ApprovalNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ApprovalConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    _audit(request, principal, outcome, source)
    logger.info(
        "review | verdict=%s | incident_id=%s | reviewer=%s (%s) | status=%s | "
        "executed=%s | idempotent=%s",
        verdict, incident_id, principal, source, outcome.get("status"),
        outcome.get("executed_actions"), outcome.get("idempotent"),
    )
    return JSONResponse(status_code=200, content={
        **outcome,
        "reviewer_id": principal,
        "attribution_source": source,
    })


# ---------------------------------------------------------------------------
# Read endpoints
# ---------------------------------------------------------------------------

@router.get("/queue", summary="Incidents awaiting human approval")
def get_queue(
    request: Request,
    limit: int | None = Query(
        default=None, ge=1, le=1000,
        description="Maximum queue entries to return. Omit for the whole queue.",
    ),
    offset: int = Query(default=0, ge=0, description="Entries to skip (pagination)."),
) -> JSONResponse:
    """
    The review queue: every incident whose gated runbook steps are held
    pending, oldest first (the order a reviewer should work them).

    Total queue depth is returned in ``X-Total-Count`` — the same convention
    the E6 paginated list endpoints use.
    """
    service = _service(request)

    settings = getattr(request.app.state.container, "settings", None)
    max_page = int(getattr(settings, "API_MAX_PAGE_SIZE", 1000)) if settings else 1000
    if limit is not None:
        limit = min(limit, max_page)

    approvals, total = service.list_queue(limit, offset)
    return JSONResponse(
        status_code=200,
        content={
            "enforced": service.enforced,
            "total": total,
            "queue": [_approval_dict(a) for a in approvals],
        },
        headers={"X-Total-Count": str(total)},
    )


@router.get("/verdicts", summary="Persisted review verdict history")
def get_verdicts(
    request: Request,
    limit: int | None = Query(
        default=None, ge=1, le=1000,
        description="Maximum verdicts to return, newest first. Omit for all.",
    ),
    offset: int = Query(default=0, ge=0, description="Verdicts to skip (pagination)."),
    incident_id: str | None = Query(
        default=None, description="Optional filter to one incident's verdicts.",
    ),
) -> JSONResponse:
    """
    Verdict history, newest first. Every entry carries its reviewer, tier,
    and how that reviewer's identity was established.
    """
    service = _service(request)

    settings = getattr(request.app.state.container, "settings", None)
    max_page = int(getattr(settings, "API_MAX_PAGE_SIZE", 1000)) if settings else 1000
    if limit is not None:
        limit = min(limit, max_page)

    verdicts, total = service.list_verdicts(limit, offset, incident_id)
    return JSONResponse(
        status_code=200,
        content={"total": total, "verdicts": [_verdict_dict(v) for v in verdicts]},
        headers={"X-Total-Count": str(total)},
    )


@router.get("/incidents/{incident_id}", summary="Approval state and verdicts for one incident")
def get_incident_review(request: Request, incident_id: str) -> JSONResponse:
    """
    One incident's approval record and its full verdict chain.

    ``200`` with ``"approval": null`` when the incident never required
    approval — including every incident that predates this phase. That is
    the honest answer ("no gate"), not a 404, which would suggest the
    incident itself is unknown.
    """
    service = _service(request)
    approval = service.get_approval(incident_id)
    if approval is None:
        return JSONResponse(status_code=200, content={
            "incident_id": incident_id,
            "approval": None,
            "verdicts": [],
            "reason": "This incident has no approval requirement on record.",
        })
    return JSONResponse(status_code=200, content={
        "incident_id": incident_id,
        "approval": _approval_dict(approval),
        "verdicts": [_verdict_dict(v) for v in service.verdicts_for(approval.approval_id)],
    })


# ---------------------------------------------------------------------------
# Write endpoints
# ---------------------------------------------------------------------------

@router.post("/incidents/{incident_id}/approve", summary="Approve at the awaited tier")
def approve_incident(
    request: Request, incident_id: str, body: VerdictRequest | None = None,
) -> JSONResponse:
    """
    Record an approval at the tier the chain is currently awaiting.

    When that tier is the last one in the chain, the incident's recorded
    pending steps execute — exactly those steps, in the recorded order,
    through the unchanged ActionAgent. Otherwise the chain simply advances
    and nothing executes yet.

    Repeating this call as the same principal is idempotent: the response
    reports ``idempotent: true`` and no action runs a second time.
    """
    return _submit(request, incident_id, Verdict.APPROVED, body or VerdictRequest())


@router.post("/incidents/{incident_id}/reject", summary="Reject and halt the approval chain")
def reject_incident(
    request: Request, incident_id: str, body: VerdictRequest | None = None,
) -> JSONResponse:
    """
    Record a rejection at the awaited tier.

    The chain halts permanently: no gated step ever executes for this
    incident, and each withheld step is recorded as skipped naming the tier
    and principal that rejected it.
    """
    return _submit(request, incident_id, Verdict.REJECTED, body or VerdictRequest())


@router.post("/incidents/{incident_id}/verdict", summary="Record any verdict from the full vocabulary")
def record_verdict(
    request: Request, incident_id: str, body: FullVerdictRequest,
) -> JSONResponse:
    """
    Record any of the four verdicts the review workspace offers.

    ``changes_requested`` and ``escalated`` are persisted with full
    attribution but leave the chain exactly where it was — they mean "not
    yet / not by me", and are never silently converted into an approval or
    a rejection.
    """
    return _submit(request, incident_id, body.verdict, body)
