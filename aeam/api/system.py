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
    - ``agents_active`` / ``agents`` — the roster of agents the lifespan
      ACTUALLY constructed this startup (``container.agent_roster``, Phase
      E1) — monitor and action are conditional on configuration, so this
      figure varies honestly by environment instead of being hardcoded.
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
                "agents_active":    int,        # len(agents)
                "agents":           list[str],  # constructed-agent names (additive, Phase E1)
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
        last_event_time: str | None = _derive_last_event_time(container)

        # Roster of agents the lifespan actually constructed (Phase E1).
        # Empty-list fallback keeps minimal test apps (no roster attached)
        # working while still reporting an honest zero, never a made-up count.
        roster: list[str] = list(getattr(container, "agent_roster", []) or [])

        payload: dict[str, Any] = {
            "status":           status,
            "active_incidents": incident_count,
            "agents_active":    len(roster),
            "agents":           roster,
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


@router.get(
    "/compliance",
    summary="Declared enterprise posture (tenancy, data classification, identity, retention)",
    response_description="The postures this deployment declares, as configured.",
)
def get_compliance_posture(request: Request) -> JSONResponse:
    """
    Return this deployment's declared enterprise posture.

    Phase E13 (Article XVI). Three of the checklist's governance items are
    *declarations*: the multi-tenancy position, the data-classification /
    PII posture, and the identity posture. A declaration that lives only in
    a document drifts from what the deployment actually runs, so this
    endpoint reports the configured values — the same fields the
    certification pack quotes — straight from Settings.

    It states positions; it does not enforce them, and it says so. The
    enforcement for each item lives where the roadmap put it: single-tenant
    isolation is achieved by deploying separately (no tenant discriminator
    exists in any store), retention is the posture documented in
    ``docs/persistence_and_retention.md``, and identity is enforced by
    ``SecurityMiddleware``.

    Returns:
        ``200`` — JSON posture dict::

            {
              "tenancy": {"model": "single-tenant", "enforced_by": "...", "note": "..."},
              "data_classification": {"level": "internal", "pii_posture": "not-expected"},
              "identity": {"mode": "static-key" | "enterprise-sso",
                           "issuer": str|null, "is_identity_provider": false},
              "retention": {"declared_in": "docs/persistence_and_retention.md",
                            "backup_runbook": "docs/DISASTER_RECOVERY.md"},
              "evidence_pack": "docs/ENTERPRISE_CERTIFICATION.md"
            }

        ``500`` — If settings cannot be read from the container.

    Note:
        Read-only. Mapped to ``logs:view`` through the ``/api/v1/system``
        RBAC prefix, so the auditor role reaches it by construction.
    """
    try:
        settings = request.app.state.container.settings

        oidc_enabled = bool(getattr(settings, "OIDC_ENABLED", False))
        tenancy_model = str(getattr(settings, "TENANCY_MODEL", "single-tenant"))

        payload: dict[str, Any] = {
            "tenancy": {
                "model": tenancy_model,
                "enforced_by": (
                    "deployment separation — no tenant discriminator exists in any "
                    "table, Qdrant collection, or Redis key namespace"
                    if tenancy_model == "single-tenant"
                    else "per-store tenant discriminator"
                ),
                "note": (
                    "AEAM implements no tenant partitioning; one deployment serves "
                    "one organization. This is a declaration, not an enforcement "
                    "mechanism."
                ),
            },
            "data_classification": {
                "level": str(getattr(settings, "DATA_CLASSIFICATION", "internal")),
                "pii_posture": str(getattr(settings, "PII_POSTURE", "not-expected")),
                "scope": ["incidents", "documents", "enterprise_memory", "audit_logs"],
                "declared_in": "docs/ENTERPRISE_CERTIFICATION.md",
            },
            "identity": {
                "mode": "enterprise-sso" if oidc_enabled else "static-key",
                "issuer": (
                    str(getattr(settings, "OIDC_ISSUER", "") or "").strip() or None
                    if oidc_enabled
                    else (getattr(settings, "JWT_ISSUER", None) or None)
                ),
                "is_identity_provider": False,
                "note": (
                    "AEAM validates tokens issued by the organization's identity "
                    "provider. It never issues enterprise credentials."
                ),
            },
            "retention": {
                "declared_in": "docs/persistence_and_retention.md",
                "backup_runbook": "docs/DISASTER_RECOVERY.md",
            },
            "evidence_pack": "docs/ENTERPRISE_CERTIFICATION.md",
        }

        return JSONResponse(status_code=200, content=payload)

    except Exception as exc:  # noqa: BLE001
        logger.error("get_compliance_posture | unexpected error: %s", exc)
        return JSONResponse(
            status_code=500,
            content={"detail": "Failed to read the declared compliance posture."},
        )


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


def _derive_last_event_time(container: Any) -> str | None:
    """
    The timestamp of the most recent event AEAM actually processed.

    Hardening. This used to be derived from ``container.queue.size()`` and
    then returned ``datetime.now()`` on BOTH branches, so the field reported
    "just now" on every single call — verified at runtime, where two
    consecutive requests 1.7s apart returned two timestamps 1.7s apart. It
    was a liveness indicator structurally incapable of indicating anything,
    and indistinguishable from a working one.

    The queue it read has had no producer since Phase E1 (MonitorAgent's
    push was removed) and no consumer, so it is permanently empty; that
    retention is deliberate (COMPAT-4 keeps the ``queue`` field in this
    payload and in ``/health``), but deriving a TIMESTAMP from a
    structurally-zero gauge is not something compatibility justifies.

    The real answer is the newest persisted incident, which is exactly "the
    last event AEAM processed". The read is index-backed by
    ``idx_incidents_timestamp`` (Phase E5), so it costs one indexed lookup.

    Returns:
        UTC ISO 8601 timestamp of the newest incident, or ``None`` when no
        event has ever been processed. ``None`` is the honest answer for a
        fresh deployment — a fabricated "now" claimed activity that never
        happened.
    """
    db = getattr(container, "db", None)
    if db is None:
        return None
    try:
        row = db.fetch_one(
            "SELECT timestamp FROM incidents ORDER BY timestamp DESC LIMIT 1"
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("_derive_last_event_time | incident read failed: %s", exc)
        return None

    if not row:
        return None
    value = row.get("timestamp")
    if value is None:
        return None
    return value.isoformat() if isinstance(value, datetime) else str(value)