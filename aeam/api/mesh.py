"""
aeam/api/mesh.py

Agent Mesh oversight API (Phase F6).

The whole-mesh view that previously existed only in an operator's head
across four dashboards: which agents this process constructed, whether each
is alive and participating, and what the Supervisor Agent observes to be
behaving oddly — each observation carrying the metrics behind it.

Endpoints (all under ``/api/v1/mesh``):

- ``GET /health``  — the Supervisor's mesh-health and anomaly report.
- ``GET /roster``  — the E1 roster with each agent's observed state.
- ``GET /issues``  — the anomaly list alone, filterable by severity.

**Every endpoint is a GET, and that is structural.** This router exposes the
Supervisor's *observations*; there is no endpoint to act on one. Acting is an
operator decision taken through the existing gated surfaces — an oversight
API with a "restart this agent" button would give the Supervisor the
coordination authority ARCH-1 reserves for the Orchestrator.

Rules enforced (mirrors every other API module in this package):

- All state access via ``request.app.state.container``; no DB connections
  created here, and in fact no database access at all — the Supervisor reads
  telemetry, not tables.
- **No oversight logic lives here.** Health computation, anomaly detection,
  and evidence disclosure are all
  :class:`~aeam.agents.supervisor.supervisor_agent.SupervisorAgent`'s, so the
  API and the console can never diverge on what "healthy" means.
- **Authorisation is the observability tier's.** Mesh oversight is
  operational telemetry, the same material ``/api/v1/observability`` and the
  audit log expose, so it maps to ``logs:view`` — reachable by the auditor
  role without granting anything actionable.
- **Disabled is reported, not hidden.** With
  ``SUPERVISOR_AGENT_ENABLED=false`` the endpoints return
  ``supervisor_enabled: false`` with the reason rather than 404 or an empty
  report: "oversight is off" and "the mesh is healthy" must never look alike.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/mesh", tags=["Agent Mesh"])

_VALID_SEVERITIES: frozenset[str] = frozenset({"critical", "warning", "info"})


def _disabled_payload(reason: str) -> dict[str, Any]:
    """The honest shape returned when no Supervisor is wired.

    Deliberately the SAME key set a live report uses, with empty collections
    and ``supervisor_enabled: false``, so a console renders one code path and
    cannot mistake "oversight disabled" for "nothing wrong".
    """
    return {
        "supervisor_enabled": False,
        "reason": reason,
        "roster": [],
        "agents": [],
        "issues": [],
        "recommended_escalations": [],
        "mesh_health": {
            "available": False,
            "score": None,
            "components": {},
            "formula": None,
            "reason": reason,
        },
    }


def _supervisor(request: Request) -> Any | None:
    """The container's SupervisorAgent, or ``None`` when oversight is off.

    Returns ``None`` rather than raising: a disabled Supervisor is a
    configuration state to report, not an error to surface as a 5xx.
    """
    container = getattr(request.app.state, "container", None)
    return getattr(container, "supervisor_agent", None)


def _report(request: Request) -> dict[str, Any]:
    """Ask the Supervisor to observe, or return the disabled payload."""
    supervisor = _supervisor(request)
    if supervisor is None:
        return _disabled_payload(
            "SupervisorAgent is not wired (SUPERVISOR_AGENT_ENABLED=false). No mesh "
            "oversight is being performed, which is distinct from a healthy mesh."
        )
    try:
        report = supervisor.observe()
    except Exception as exc:  # noqa: BLE001
        logger.error("mesh | supervisor observation failed | %s", exc, exc_info=True)
        raise HTTPException(
            status_code=500, detail=f"Mesh observation failed: {exc}"
        ) from exc
    return {"supervisor_enabled": True, "reason": None, **report}


# ---------------------------------------------------------------------------
# Read — the entire surface
# ---------------------------------------------------------------------------


@router.get("/health", summary="Mesh health and behaviour anomalies (advisory)")
async def get_mesh_health(request: Request) -> dict:
    """
    The Supervisor's report: per-agent observed state, detected anomalies,
    and the mesh-health score with its formula.

    Every figure is read from telemetry that already existed — E7
    heartbeats, E11 metrics, the E1 roster, and the D3/E11 observability
    summary. A health score appears only when at least one component is
    genuinely computable; otherwise ``mesh_health.available`` is ``false``
    with the reason, never a placeholder number.

    Advisory only: the report recommends, and nothing here acts.
    """
    report = _report(request)
    logger.info(
        "mesh health | enabled=%s | agents=%d | issues=%d",
        report["supervisor_enabled"], len(report["agents"]), len(report["issues"]),
    )
    return report


@router.get("/roster", summary="The agents this process constructed, with observed state")
async def get_mesh_roster(request: Request) -> dict:
    """
    The E1 ``agent_roster`` plus each member's heartbeat, execution count,
    and measured participation.

    The roster itself is also on ``GET /api/v1/system/status``; this view
    adds the observed state per agent, which that endpoint deliberately does
    not carry (it answers "how many agents are active", not "how is each
    one behaving").
    """
    report = _report(request)
    return {
        "supervisor_enabled": report["supervisor_enabled"],
        "reason": report["reason"],
        "roster": report["roster"],
        "roster_source": report.get("roster_source"),
        "agents": report["agents"],
    }


@router.get("/issues", summary="Detected mesh behaviour anomalies (advisory)")
async def get_mesh_issues(
    request: Request,
    severity: str | None = Query(
        default=None,
        description="Restrict to one severity: critical, warning, or info.",
    ),
) -> dict:
    """
    The anomaly list alone, worst-first.

    Each entry names the affected ``agent``, the ``kind`` of anomaly, a plain
    ``reason``, the ``evidence`` (the observed metrics and their source), and
    the ``recommended_escalation``. No anomaly is reported without the
    numbers that produced it.
    """
    if severity is not None and severity not in _VALID_SEVERITIES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown severity {severity!r}. Valid values: {sorted(_VALID_SEVERITIES)}.",
        )
    report = _report(request)
    issues = report["issues"]
    if severity is not None:
        issues = [issue for issue in issues if issue.get("severity") == severity]
    return {
        "supervisor_enabled": report["supervisor_enabled"],
        "reason": report["reason"],
        "severity": severity,
        "count": len(issues),
        "issues": issues,
        "recommended_escalations": report["recommended_escalations"],
    }
