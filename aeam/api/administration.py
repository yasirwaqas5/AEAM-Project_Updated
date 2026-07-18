"""
aeam/api/administration.py

Enterprise Administration & Settings UI (Phase D5) — backend API for the
Phase D4 Enterprise Configuration Engine.

Architecture Gate conclusion: Phase D4 already built the ONE configuration
mechanism this system has — the Pydantic ``Settings`` class
(``aeam/config/settings.py``), read from process environment variables and
the project ``.env`` file, with every intelligence engine constructed ONCE
at startup (``aeam/main.py``) from whatever ``Settings()`` resolved to at
that time. This module does not introduce a second configuration store, a
live-reload mechanism, or any change to how engines are constructed —doing
so would redesign the Configuration Engine and the intelligence engines,
both explicitly forbidden by this phase's mission. Instead, this module is
purely a read/write surface over the SAME ``.env`` file ``Settings`` already
reads, using ``python-dotenv`` (already a project dependency, since
pydantic-settings itself is built on it) — the same "Settings model" the
mission says to reuse.

Because engines are constructed once at startup, a ``.env`` edit here does
NOT retroactively change the currently-running investigation pipeline —
this is disclosed honestly via ``effective_value``/``configured_value``/
``restart_required`` on every field, never silently implied to be live.
This is the honest form of "configuration changes affect only future
investigations": today, "future" concretely means "investigations
processed after the next restart" — the exact same granularity Phase D4's
own wiring already has. Historical investigations are unaffected regardless
(their ``findings`` are already-persisted JSON this module never touches).

Validation reuses Pydantic's own ``Field(gt=..., le=..., ...)`` constraints
declared on ``Settings`` in Phase D4 (via a full ``Settings()`` reconstruction
with the candidate value applied) — no second validation system.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any

from dotenv import set_key, unset_key
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from aeam.config.config_registry import (
    CONFIG_FIELDS,
    CONFIG_FIELDS_BY_KEY,
    SECTION_ORDER,
    field_constraints,
    field_description,
    field_type,
)
from aeam.config.settings import Settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin/config", tags=["administration"])

_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"

# Guards the temporary os.environ mutate/restore window in _validate_candidates
# so two concurrent requests (FastAPI runs sync path operations in a thread
# pool) can never interleave and observe each other's in-flight candidate.
_env_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _current_settings() -> Settings:
    """
    Fresh read of the persisted configuration (process env + the SAME
    ``.env`` file this module reads/writes via ``_ENV_PATH``), exactly what
    the next application restart would load. Deliberately NOT the cached
    ``container.settings`` (which reflects only what was true at the LAST
    startup) — this is what "configured" means throughout this API.

    Passes ``_env_file=_ENV_PATH`` explicitly (pydantic-settings' own
    supported per-instantiation override) rather than relying on
    ``Settings.model_config``'s default ``env_file=".env"`` resolving
    against the current working directory to coincidentally match
    ``_ENV_PATH`` — keeps the read and write paths provably the same file.
    """
    return Settings(_env_file=_ENV_PATH)  # pyright: ignore[reportCallIssue]


def _validate_candidates(overrides: dict[str, Any]) -> dict[str, str]:
    """
    Validate ``overrides`` (``{key: candidate_value_or_None}``) by
    temporarily applying them to ``os.environ`` (highest-priority source for
    ``Settings``) and reconstructing ``Settings()`` — real Pydantic
    validation, not reimplemented bounds-checking. Restores the prior
    environment unconditionally. ``None`` means "candidate: unset".

    Returns:
        ``{key: error_message}`` for every INVALID key. Keys not present in
        the return value are valid. Unrecognised keys are reported as errors
        without ever reaching ``Settings()``.
    """
    unknown = {k: "Not a recognised configuration field." for k in overrides if k not in CONFIG_FIELDS_BY_KEY}
    known_overrides = {k: v for k, v in overrides.items() if k in CONFIG_FIELDS_BY_KEY}

    errors: dict[str, str] = dict(unknown)
    if not known_overrides:
        return errors

    with _env_lock:
        saved: dict[str, str | None] = {}
        try:
            for key, value in known_overrides.items():
                saved[key] = os.environ.get(key)
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = str(value)
            try:
                _current_settings()
            except ValidationError as exc:
                for err in exc.errors():
                    loc = err["loc"][0] if err["loc"] else None
                    if loc in known_overrides:
                        errors[loc] = err["msg"]
        finally:
            for key, prior in saved.items():
                if prior is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = prior

    return errors


def _serialize_field(key: str, configured: Settings, effective: Any) -> dict[str, Any]:
    spec = CONFIG_FIELDS_BY_KEY[key]
    configured_value = getattr(configured, key)
    effective_value = getattr(effective, key, None) if effective is not None else None
    payload: dict[str, Any] = {
        "key": key,
        "section": spec.section,
        "label": spec.label,
        "description": field_description(key),
        "type": field_type(key),
        "default": spec.default,
        "configured_value": configured_value,
        "effective_value": effective_value,
        "is_overridden": configured_value is not None,
        "restart_required": configured_value != effective_value,
    }
    if spec.default_note:
        payload["default_note"] = spec.default_note
    if spec.choices:
        payload["choices"] = list(spec.choices)
    constraints = field_constraints(key)
    if constraints:
        payload["constraints"] = constraints
    return payload


def _all_fields_payload(container: Any) -> dict[str, Any]:
    configured = _current_settings()
    effective = getattr(container, "settings", None)
    fields = [_serialize_field(f.key, configured, effective) for f in CONFIG_FIELDS]
    return {
        "sections": list(SECTION_ORDER),
        "fields": fields,
        "restart_required": any(f["restart_required"] for f in fields),
        "env_file": str(_ENV_PATH),
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get(
    "/",
    summary="Read the Enterprise Configuration Engine's current settings",
    response_description="Every configurable field, grouped by section, with default/configured/effective values.",
)
def get_configuration(request: Request) -> JSONResponse:
    """
    Return every Phase D4 configuration field: its section/label/description
    (reused from ``Settings`` itself), real engine default, currently
    PERSISTED value (fresh from process env + ``.env``), currently EFFECTIVE
    value (what the running process's engines were actually constructed
    with), and whether a restart is required to reconcile the two.
    """
    container = request.app.state.container
    try:
        payload = _all_fields_payload(container)
    except Exception as exc:  # noqa: BLE001
        logger.error("get_configuration | failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Failed to read configuration: {exc}") from exc
    return JSONResponse(status_code=200, content=payload)


@router.post(
    "/validate",
    summary="Validate candidate configuration values without persisting them",
    response_description="Per-field validation result — nothing is written.",
)
def validate_configuration(request: Request, body: dict[str, Any]) -> JSONResponse:
    """
    Dry-run validation for ``{"values": {key: candidate_value, ...}}``.
    Reuses the exact same Pydantic ``Field`` constraints Phase D4 declared —
    never a second validation system. Never writes to ``.env``.
    """
    values = body.get("values")
    if not isinstance(values, dict) or not values:
        raise HTTPException(status_code=422, detail="Request body must include a non-empty 'values' object.")

    errors = _validate_candidates(values)
    results = {
        key: {"valid": key not in errors, "error": errors.get(key)}
        for key in values
    }
    return JSONResponse(status_code=200, content={"results": results, "all_valid": not errors})


@router.put(
    "/",
    summary="Update configuration values (persisted to .env; applies on next restart)",
    response_description="Updated field list, or per-field validation errors if any value was invalid.",
)
def update_configuration(request: Request, body: dict[str, Any]) -> JSONResponse:
    """
    Persist ``{"values": {key: value_or_null, ...}}`` to the project
    ``.env`` file. ``null`` for a key means "restore that field to its
    default" (equivalent to one entry of :func:`reset_configuration`).

    Atomic: if ANY field fails validation, NOTHING is written and a ``422``
    with every field's error is returned — never a partial write.

    Never alters historical investigation data: this only ever changes
    where FUTURE ``Settings()`` reads resolve to; already-persisted incident
    ``findings`` rows are untouched.
    """
    values = body.get("values")
    if not isinstance(values, dict) or not values:
        raise HTTPException(status_code=422, detail="Request body must include a non-empty 'values' object.")

    errors = _validate_candidates(values)
    if errors:
        raise HTTPException(status_code=422, detail={"errors": errors})

    for key, value in values.items():
        if value is None:
            unset_key(_ENV_PATH, key)
        else:
            set_key(_ENV_PATH, key, str(value))

    logger.info("update_configuration | updated=%s", sorted(values.keys()))

    container = request.app.state.container
    payload = _all_fields_payload(container)
    return JSONResponse(status_code=200, content=payload)


@router.post(
    "/reset",
    summary="Restore one or more configuration fields to their engine defaults",
    response_description="Updated field list after the reset.",
)
def reset_configuration(request: Request, body: dict[str, Any] | None = None) -> JSONResponse:
    """
    Restore fields to their engine defaults by removing them from ``.env``.
    ``{"all": true}`` resets every known field; ``{"keys": [...]}`` resets
    only the named ones. Unsetting an already-unset key is a harmless no-op
    (idempotent) — this endpoint never fails because a field was already at
    its default.
    """
    body = body or {}
    if body.get("all"):
        keys = list(CONFIG_FIELDS_BY_KEY.keys())
    else:
        keys = body.get("keys") or []
        unknown = [k for k in keys if k not in CONFIG_FIELDS_BY_KEY]
        if unknown:
            raise HTTPException(status_code=422, detail=f"Not recognised configuration field(s): {unknown}")

    for key in keys:
        unset_key(_ENV_PATH, key)

    logger.info("reset_configuration | reset=%s", sorted(keys))

    container = request.app.state.container
    payload = _all_fields_payload(container)
    return JSONResponse(status_code=200, content=payload)
