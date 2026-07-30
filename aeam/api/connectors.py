"""
aeam/api/connectors.py

Enterprise Connector API (Phase F7).

The operator surface over the connector framework: what connectors exist, how
each one is doing, what it has ingested and where from, and the explicit
trigger that runs a synchronization.

Endpoints (all under ``/api/v1/connectors``):

- ``GET  /``                       — catalog + per-connector health.
- ``GET  /{source_id}``            — one connector's health.
- ``GET  /{source_id}/artifacts``  — provenance for what it has ingested.
- ``GET  /{source_id}/runs``       — its synchronization history.
- ``POST /sync``                   — run every enabled connector, isolated.
- ``POST /sync/{source_id}``       — run one synchronization.

Rules enforced (mirrors every other API module in this package):

- All state access via ``request.app.state.container``; no DB connections
  created here.
- **No connector logic lives here.** Listing, change detection, ingestion, and
  health computation belong to the framework, so the API and the console can
  never diverge from what a sync actually did.
- **No credential ever crosses this boundary.** Responses are built from
  ``describe()``/health output, which carries configuration KEYS and the
  secret's NAME — never a value (SEC-5). Sync errors are sanitised by the
  connector before they are persisted or returned.
- **Reads and writes share no prefix.** Both writes live under
  ``/api/v1/connectors/sync``, which ``_ENDPOINT_RBAC_MAP`` grades as
  ``admin:config``. This is why the per-connector trigger is
  ``POST /sync/{source_id}`` and not ``POST /{source_id}/sync``: that map
  matches on path alone, not method, so a write nested under a read prefix
  would be graded as a read — which is exactly how an analyst ends up able to
  fire a sync with organizational credentials.

**Why sync is a privileged write.** Running a sync fetches from an external
system with organizational credentials and enqueues ingestion work. It is
also the only way connector content ever enters the platform — there is no
timer and no autonomous poll — which makes it an operator action by design,
matching the repo's existing posture on autonomous work.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/connectors", tags=["Connectors"])

#: Bound on provenance/history page size (E6). A connector with 200k ingested
#: artifacts must not be able to return them all in one response.
MAX_PAGE_SIZE: int = 500


class SyncRequest(BaseModel):
    """Body for a synchronization trigger."""

    actor_id: str | None = Field(
        default=None,
        description=(
            "Acting principal. Ignored when the security middleware established "
            "a verified identity — a body can never override a verified JWT."
        ),
    )


def _principal(request: Request, supplied: str | None) -> tuple[str, str]:
    """Resolve the acting principal and say honestly where it came from.

    Same precedence as the E9 review router and the F2/F7 governance surfaces,
    deliberately: surfaces that attribute differently produce an audit trail an
    operator has to read several ways.
    """
    user_id = getattr(request.state, "user_id", None)
    if isinstance(user_id, str) and user_id.strip() and user_id != "anonymous":
        return user_id.strip(), "jwt"
    cleaned = (supplied or "").strip()
    return (cleaned, "request") if cleaned else ("unattributed", "unattributed")


def _health_reporter(request: Request) -> Any:
    """The container's connector health reporter.

    Raises:
        HTTPException: 503 when the framework was not wired at startup — the
                       honest answer for a surface whose entire job is
                       reporting connector state.
    """
    container = getattr(request.app.state, "container", None)
    reporter = getattr(container, "connector_health", None)
    if reporter is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "The connector framework is not wired (no database client, or "
                "startup did not construct it). No connector state is available."
            ),
        )
    return reporter


def _sync_engine(request: Request) -> Any:
    container = getattr(request.app.state, "container", None)
    engine = getattr(container, "connector_sync", None)
    if engine is None:
        raise HTTPException(
            status_code=503,
            detail="The connector sync engine is not wired; no synchronization can run.",
        )
    return engine


def _audit(request: Request, principal: str, action: str, detail: dict[str, Any]) -> None:
    """Record a sync against the acting principal.

    Failure is logged and swallowed — an audit-sink problem must never
    invalidate a sync that already happened (SEC-6).
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
        logger.warning("connectors | audit write failed | action=%s | %s", action, exc)


def _observe_duration(outcome: dict[str, Any]) -> None:
    """Publish a completed run's duration onto the EXISTING histogram."""
    try:
        from aeam.monitoring.metrics import (
            connector_sync_artifacts_total,
            connector_sync_duration_seconds,
        )

        connector = str(outcome.get("connector") or "unknown")
        connector_sync_duration_seconds.labels(connector=connector).observe(
            float(outcome.get("duration_seconds") or 0.0)
        )
        for outcome_key, metric_label in (
            ("processed_count", "processed"),
            ("skipped_count", "skipped"),
            ("failed_count", "failed"),
        ):
            count = int(outcome.get(outcome_key) or 0)
            if count:
                connector_sync_artifacts_total.labels(
                    connector=connector, outcome=metric_label
                ).inc(count)
    except Exception as exc:  # noqa: BLE001
        logger.warning("connectors | metric publication failed: %s", exc)


def _artifact_to_dict(artifact: Any) -> dict[str, Any]:
    """One provenance row, credential-free by construction.

    Every field here came from the upstream system's own metadata or from the
    ingestion result. A field upstream did not expose is ``None`` rather than
    filled in, so a reader can tell what is actually known.
    """
    return {
        "artifact_id": artifact.artifact_id,
        "external_id": artifact.external_id,
        "connector": artifact.connector,
        "source_type": artifact.source_type,
        "title": artifact.title,
        "source_url": artifact.source_url,
        "source_timestamp": artifact.source_timestamp,
        "source_version": artifact.source_version,
        "semantic_type": artifact.semantic_type,
        "parent_type": artifact.parent_type,
        "parent_id": artifact.parent_id,
        "last_job_id": artifact.last_job_id,
        "first_synced_at": artifact.first_synced_at,
        "last_synced_at": artifact.last_synced_at,
        "skip_count": artifact.skip_count,
        "ingest_count": artifact.ingest_count,
    }


def _run_to_dict(run: Any) -> dict[str, Any]:
    return {
        "run_id": run.run_id,
        "connector": run.connector,
        "status": run.status,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "duration_seconds": run.duration_seconds,
        "listed_count": run.listed_count,
        "changed_count": run.changed_count,
        "processed_count": run.processed_count,
        "skipped_count": run.skipped_count,
        "failed_count": run.failed_count,
        "error": run.error,
        "cursor_from": run.cursor_from,
        "cursor_to": run.cursor_to,
        "triggered_by": run.triggered_by,
    }


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


@router.get("/health", summary="Connector catalog and per-connector health")
async def get_connector_health(request: Request) -> dict:
    """
    Every connector's honest state, plus the catalog of what is available.

    ``stale`` is ``null`` with a reason for a connector that has never synced —
    not ``false``. Unknown freshness is never reported as fresh (SEC-8), and the
    ``unknown`` bucket in the summary exists so a never-synced connector is not
    counted as healthy.
    """
    report = _health_reporter(request).report()
    logger.info(
        "connector health | enabled=%s | connectors=%d | summary=%s",
        report["framework_enabled"], len(report["connectors"]), report["summary"],
    )
    return report


@router.get("/", summary="Connector catalog and per-connector health (alias)")
async def list_connectors(request: Request) -> dict:
    """Same payload as ``/health`` — the collection root an operator reaches for."""
    return _health_reporter(request).report()


@router.get("/{source_id}/artifacts", summary="Provenance for a connector's ingested artifacts")
async def get_connector_artifacts(
    request: Request,
    source_id: str,
    limit: int = Query(default=50, ge=1, le=MAX_PAGE_SIZE, description="Maximum rows returned."),
    offset: int = Query(default=0, ge=0, description="Rows to skip."),
) -> dict:
    """
    What this connector has ingested, and where each artifact came from.

    This is the provenance surface: connector, upstream id and type, the URL an
    operator can open, when we synced it, when upstream last changed it, the
    declared semantic type, and which local Document/Dataset it became — plus
    the skip/ingest counters that show incremental sync working.
    """
    container = getattr(request.app.state, "container", None)
    db = getattr(container, "db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="No database client is configured.")

    from aeam.registry.repositories import ConnectorArtifactRepository, SourceRepository

    if SourceRepository(db).get(source_id) is None:
        raise HTTPException(status_code=404, detail=f"No source with id {source_id!r}.")

    repo = ConnectorArtifactRepository(db)
    artifacts = repo.list_by_source(source_id, limit=limit, offset=offset)
    total = repo.count_by_source(source_id)
    return {
        "source_id": source_id,
        "total": total,
        "limit": limit,
        "offset": offset,
        "truncated": offset + len(artifacts) < total,
        "artifacts": [_artifact_to_dict(a) for a in artifacts],
    }


@router.get("/{source_id}/runs", summary="A connector's synchronization history")
async def get_connector_runs(
    request: Request,
    source_id: str,
    limit: int = Query(default=20, ge=1, le=MAX_PAGE_SIZE, description="Maximum runs returned."),
) -> dict:
    """Recent runs, newest first, each with its measured counts and duration."""
    container = getattr(request.app.state, "container", None)
    db = getattr(container, "db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="No database client is configured.")

    from aeam.registry.repositories import ConnectorSyncRunRepository, SourceRepository

    if SourceRepository(db).get(source_id) is None:
        raise HTTPException(status_code=404, detail=f"No source with id {source_id!r}.")

    runs = ConnectorSyncRunRepository(db).list_by_source(source_id, limit=limit)
    return {"source_id": source_id, "count": len(runs), "runs": [_run_to_dict(r) for r in runs]}


@router.get("/{source_id}", summary="One connector's health")
async def get_connector(request: Request, source_id: str) -> dict:
    """
    One connector's state.

    Raises 404 for an unknown source, and for a source that is not a connector
    kind (``upload``, ``gsheet``) — those are real sources but they are not
    connectors, and reporting them here as unhealthy connectors would be
    misleading.
    """
    health = _health_reporter(request).health_for(source_id)
    if health is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No connector source with id {source_id!r}. (Sources of kind 'upload' "
                "or 'gsheet' are not connectors and are not reported here.)"
            ),
        )
    return health


# ---------------------------------------------------------------------------
# Write — the explicit sync trigger
# ---------------------------------------------------------------------------


@router.post("/sync", summary="Synchronize every enabled connector (isolated)")
async def sync_all_connectors(request: Request, body: SyncRequest | None = None) -> dict:
    """
    Run every enabled connector, each isolated from the others.

    A connector that fails produces a FAILED run record and nothing else: the
    loop continues, and the response carries every connector's outcome. One
    misconfigured connector never prevents the other seven from syncing.
    """
    body = body or SyncRequest()
    principal, attribution = _principal(request, body.actor_id)
    outcomes = _sync_engine(request).sync_all(triggered_by=principal)
    for outcome in outcomes:
        _observe_duration(outcome)

    _audit(request, principal, "connector.sync_all", {
        "connectors": len(outcomes),
        "attribution_source": attribution,
    })
    return {
        "actor": principal,
        "attribution_source": attribution,
        "connectors": len(outcomes),
        "outcomes": outcomes,
    }


@router.post("/sync/{source_id}", summary="Synchronize one connector")
async def sync_connector(
    request: Request, source_id: str, body: SyncRequest | None = None
) -> dict:
    """
    Run one connector's synchronization.

    Incremental: only artifacts upstream has actually changed are downloaded,
    and they enter the platform through the **existing** ingestion pipeline, so
    the resulting documents are indistinguishable from uploaded ones.

    Always returns ``200`` with an outcome, including when the sync failed — a
    connector failure is a recorded state, not a transport error, and returning
    a 5xx would make an isolated connector fault look like a platform fault.
    """
    body = body or SyncRequest()
    principal, attribution = _principal(request, body.actor_id)
    outcome = _sync_engine(request).sync_source(source_id, triggered_by=principal)
    _observe_duration(outcome)

    _audit(request, principal, "connector.sync", {
        "source_id": source_id,
        "status": outcome.get("status"),
        "processed_count": outcome.get("processed_count"),
        "skipped_count": outcome.get("skipped_count"),
        "attribution_source": attribution,
    })
    logger.info(
        "connector sync | principal=%s (%s) | source_id=%s | status=%s",
        principal, attribution, source_id, outcome.get("status"),
    )
    return {"actor": principal, "attribution_source": attribution, **outcome}
