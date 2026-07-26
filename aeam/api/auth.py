"""
aeam/api/auth.py

Console session-bootstrap API (Phase E10 — Enterprise Console).

AEAM validates enterprise-issued tokens; it does not act as an identity
provider (real OIDC/SSO federation is Phase E13's job, landing on this same
session layer). This router exposes exactly two endpoints:

- ``POST /api/v1/auth/dev-token`` — dev-only convenience token minting
  (see :mod:`aeam.security.dev_token_issuer`). Disabled (404) outside a
  development posture so it can never reach staging/production.
- ``GET  /api/v1/auth/session``   — "who am I" for the console session
  layer. Reads the verified principal that ``SecurityMiddleware`` publishes
  on ``request.state`` when it performed real JWT verification. In a
  development posture (where the middleware bypasses verification
  entirely) it falls back to decoding the caller's bearer token WITHOUT
  signature verification, purely for UX display -- this is safe only
  because development already trusts every request unconditionally.

No RBAC-mapped resource exists for ``/api/v1/auth`` in
``_ENDPOINT_RBAC_MAP``, so ``/session`` requires a verified bearer token in
any non-development environment (authentication still runs) but no specific
permission grant. ``/dev-token`` is listed in ``_PUBLIC_PATHS`` so it can be
called *before* the caller has a token at all.
"""

from __future__ import annotations

import logging

import jwt as pyjwt
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from aeam.security.dev_token_issuer import DevTokenIssuer

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/auth", tags=["Auth"])

# One issuer per process — tokens it mints are only ever consumed within
# the same process's lifetime (frontend dev session), so there is no need
# to persist or share the ephemeral keypair anywhere.
_dev_issuer = DevTokenIssuer()


class DevTokenRequest(BaseModel):
    """Request body for the dev-only token-minting endpoint."""

    sub: str = Field(default="dev-user", description="Subject/principal to embed in the token.")
    roles: list[str] = Field(
        default_factory=lambda: ["admin"],
        description="Roles to embed (must match aeam.security.rbac's vocabulary).",
    )
    ttl_seconds: int = Field(default=3600, ge=60, le=86400, description="Token lifetime in seconds.")


@router.post("/dev-token", summary="Mint a dev-only session token (development posture only)")
async def mint_dev_token(request: Request, body: DevTokenRequest) -> dict:
    """
    Issue a short-lived, self-signed JWT for local/demo console testing.

    Returns 404 in any environment other than ``development`` -- this
    endpoint must never be reachable in staging or production, matching the
    same fail-closed posture every other dev-only surface in this codebase
    uses (e.g. the SecurityMiddleware placeholder-key fallback).
    """
    settings = getattr(request.app.state, "settings", None)
    environment = str(getattr(settings, "ENVIRONMENT", "") or "").strip().lower()
    if environment != "development":
        raise HTTPException(status_code=404, detail="Not found.")

    return _dev_issuer.mint(sub=body.sub, roles=body.roles, ttl_seconds=body.ttl_seconds)


@router.get("/session", summary="Return the calling principal's identity and roles")
async def get_session(request: Request) -> dict:
    """
    Return ``{authenticated, sub, roles, source}`` for the current caller.

    ``source`` is one of:
    - ``"verified"``   — SecurityMiddleware verified the JWT signature
                         (any non-development environment).
    - ``"unverified"`` — the environment bypasses verification
                         (development only); claims are decoded for
                         display purposes without cryptographic proof.
    - ``"none"``        — no usable token was presented.
    """
    user_id = getattr(request.state, "user_id", None)
    roles = getattr(request.state, "roles", None)
    if user_id and user_id != "anonymous":
        return {"authenticated": True, "sub": user_id, "roles": roles or [], "source": "verified"}

    auth_header = request.headers.get("Authorization") or ""
    parts = auth_header.strip().split(" ", 1)
    if len(parts) == 2 and parts[0].lower() == "bearer" and parts[1].strip():
        try:
            payload = pyjwt.decode(parts[1].strip(), options={"verify_signature": False})
            raw_roles = payload.get("roles")
            if not isinstance(raw_roles, list):
                role = payload.get("role")
                raw_roles = [role] if isinstance(role, str) and role else []
            return {
                "authenticated": True,
                "sub": str(payload.get("sub", "unknown")),
                "roles": [str(r) for r in raw_roles if r],
                "source": "unverified",
            }
        except Exception as exc:  # noqa: BLE001
            logger.debug("auth.session | undecodable token | %s", exc)

    return {"authenticated": False, "sub": None, "roles": [], "source": "none"}
