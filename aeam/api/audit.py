"""
aeam/api/audit.py

Audit query API (Phase E11 — Platform Observability & Audit Surface).

Phase E3 made the audit trail durable: every request the security middleware
processes is appended to the ``audit_logs`` table alongside the file sink.
Durable, but unqueryable — compliance could not answer "what did this
principal do last Tuesday" without a DBA. This router is that missing read
surface, and nothing more.

Contract:

- **Read-only, always.** No endpoint here writes, mutates, or deletes an
  audit record. The trail is append-only by construction (see
  :class:`~aeam.security.audit_logger.AuditLogger`) and this module does not
  weaken that.
- **No second data store.** Reads the E3 ``audit_logs`` table through the
  same ``request.app.state.container.db`` access path every other API module
  uses. No new table, no cache, no export pipeline.
- **Bounded by construction (E6 semantics).** Every list response is paged
  with the same ``limit``/``offset`` + ``X-Total-Count`` contract the E6
  endpoints established, so an audit trail years deep can never produce an
  unbounded payload.
- **Authorisation is the middleware's.** ``/api/v1/audit`` maps to
  ``logs:view`` in ``_ENDPOINT_RBAC_MAP`` — the grant the ``auditor`` role
  holds, which is precisely what "an auditor-role user can query audit
  history" requires. This module never re-implements a permission check
  (ENG-6: one permission vocabulary, enforced in one place).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/audit", tags=["Audit"])

#: Hard ceiling on one page, mirroring the E6 list endpoints.
_MAX_LIMIT: int = 1000


def _iso(value: Any) -> str | None:
    """Normalise a driver-native timestamp to ISO-8601 (mirrors ingest.py/knowledge.py).

    PostgreSQL returns real ``datetime`` objects for TIMESTAMP columns while
    SQLite returns the ISO string as written; both must round-trip through
    JSON identically.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _row_to_entry(row: dict[str, Any]) -> dict[str, Any]:
    """Shape one ``audit_logs`` row for the API, decoding the JSON ``extra`` column."""
    extra = row.get("extra")
    if isinstance(extra, str) and extra.strip():
        try:
            extra = json.loads(extra)
        except (json.JSONDecodeError, TypeError):
            # Keep the raw text rather than dropping it — an unparseable
            # extra column is still evidence, and silently discarding audit
            # content would be exactly the wrong failure mode here.
            pass
    return {
        "entry_id": row.get("entry_id"),
        "timestamp": _iso(row.get("timestamp")),
        "user_id": row.get("user_id"),
        "action": row.get("action"),
        "endpoint": row.get("endpoint"),
        "status_code": row.get("status_code"),
        "hash": row.get("hash"),
        "extra": extra if extra else None,
    }


def _build_filters(
    principal: str | None,
    endpoint: str | None,
    status_code: int | None,
    since: str | None,
    until: str | None,
) -> tuple[str, dict[str, Any]]:
    """
    Build the parameterised WHERE clause shared by the list and count queries.

    Every value is bound, never interpolated — the column names are the only
    literals in the SQL, and they are fixed here rather than caller-supplied.
    """
    clauses: list[str] = []
    params: dict[str, Any] = {}

    if principal:
        clauses.append("user_id = :principal")
        params["principal"] = principal
    if endpoint:
        clauses.append("endpoint LIKE :endpoint")
        params["endpoint"] = f"{endpoint}%"
    if status_code is not None:
        clauses.append("status_code = :status_code")
        params["status_code"] = status_code
    if since:
        clauses.append("timestamp >= :since")
        params["since"] = since
    if until:
        clauses.append("timestamp <= :until")
        params["until"] = until

    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, params


def _validate_window(since: str | None, until: str | None) -> None:
    """Reject a malformed or inverted time window with a 422 rather than
    silently returning an empty page that looks like 'nothing happened'."""
    parsed: dict[str, datetime] = {}
    for name, value in (("since", since), ("until", until)):
        if not value:
            continue
        try:
            parsed[name] = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail=f"{name!r} must be an ISO-8601 timestamp (got {value!r}).",
            ) from exc

    if "since" in parsed and "until" in parsed and parsed["since"] > parsed["until"]:
        raise HTTPException(
            status_code=422,
            detail="'since' must not be later than 'until'.",
        )


@router.get(
    "/entries",
    summary="Query the durable audit trail by principal and time window",
    response_description="A bounded page of audit entries, newest first.",
)
def list_audit_entries(
    request: Request,
    principal: str | None = Query(
        default=None,
        description="Exact acting principal (audit_logs.user_id) to filter by.",
    ),
    endpoint: str | None = Query(
        default=None,
        description="Endpoint path PREFIX to filter by, e.g. '/api/v1/review'.",
    ),
    status_code: int | None = Query(
        default=None, description="Exact HTTP status code to filter by."
    ),
    since: str | None = Query(
        default=None, description="Inclusive window start, ISO-8601."
    ),
    until: str | None = Query(
        default=None, description="Inclusive window end, ISO-8601."
    ),
    limit: int = Query(default=100, ge=1, le=_MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> JSONResponse:
    """
    Return a bounded, newest-first page of audit entries.

    All filters are optional and compose with AND. ``X-Total-Count`` carries
    the number of entries matching the filters BEFORE paging, so a caller can
    page without a second count request — the same contract the Phase E6 list
    endpoints established.
    """
    _validate_window(since, until)

    container = request.app.state.container
    db = container.db

    where, params = _build_filters(principal, endpoint, status_code, since, until)

    try:
        total_row = db.fetch_one(f"SELECT COUNT(*) AS n FROM audit_logs{where}", params)
        total = int(total_row["n"]) if total_row and total_row.get("n") is not None else 0

        rows = db.fetch_all(
            f"SELECT * FROM audit_logs{where} "
            "ORDER BY timestamp DESC LIMIT :limit OFFSET :offset",
            {**params, "limit": limit, "offset": offset},
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("list_audit_entries | query failed: %s", exc)
        raise HTTPException(
            status_code=500, detail="Failed to query the audit trail."
        ) from exc

    entries = [_row_to_entry(dict(r)) for r in rows]

    logger.info(
        "list_audit_entries | principal=%s | since=%s | until=%s | "
        "returned=%d | total=%d",
        principal, since, until, len(entries), total,
    )

    return JSONResponse(
        status_code=200,
        content=entries,
        headers={"X-Total-Count": str(total)},
    )


@router.get(
    "/principals",
    summary="Distinct acting principals present in the audit trail",
    response_description="Principals with their entry counts and activity window.",
)
def list_audit_principals(
    request: Request,
    since: str | None = Query(default=None, description="Inclusive window start, ISO-8601."),
    until: str | None = Query(default=None, description="Inclusive window end, ISO-8601."),
    limit: int = Query(default=100, ge=1, le=_MAX_LIMIT),
) -> JSONResponse:
    """
    Return the distinct principals in the trail with per-principal counts.

    The "who has been active" question an auditor asks before drilling into
    one principal's history. Aggregated in SQL rather than by pulling every
    row into Python, so it stays bounded at any trail size.
    """
    _validate_window(since, until)

    container = request.app.state.container
    db = container.db
    where, params = _build_filters(None, None, None, since, until)

    try:
        rows = db.fetch_all(
            "SELECT user_id, COUNT(*) AS entry_count, "
            "MIN(timestamp) AS first_seen, MAX(timestamp) AS last_seen "
            f"FROM audit_logs{where} "
            "GROUP BY user_id ORDER BY entry_count DESC LIMIT :limit",
            {**params, "limit": limit},
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("list_audit_principals | query failed: %s", exc)
        raise HTTPException(
            status_code=500, detail="Failed to query the audit trail."
        ) from exc

    principals = [
        {
            "principal": r.get("user_id"),
            "entry_count": int(r.get("entry_count") or 0),
            "first_seen": _iso(r.get("first_seen")),
            "last_seen": _iso(r.get("last_seen")),
        }
        for r in (dict(row) for row in rows)
    ]

    return JSONResponse(
        status_code=200,
        content={
            "principals": principals,
            "window": {
                "since": since,
                "until": until,
                # EXPL-5: state plainly when no window was applied, so an
                # empty/short list is never mistaken for a filtered view.
                "applied": bool(since or until),
            },
        },
    )
