"""
aeam/api/auth.py

Console session-bootstrap API (Phase E10 — Enterprise Console; Phase E13 —
enterprise SSO federation).

AEAM validates enterprise-issued tokens; it does not act as an identity
provider. Phase E13 adds the *relying-party* half of that posture: the
console can redirect an operator to the organization's IdP and exchange
the returned authorization code for the IdP's own token, which the
unchanged E3 verification path then validates via JWKS. AEAM still never
mints an enterprise credential.

This router exposes:

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
- ``GET  /api/v1/auth/sso/config``   — (E13) the public parameters the
  browser needs to start an OIDC authorization-code + PKCE redirect.
  Returns ``{"enabled": false, "reason": ...}`` when SSO is not
  configured, so the console can honestly show why the button is absent
  instead of rendering a control that cannot work (EXPL-3/EXPL-5).
- ``POST /api/v1/auth/sso/callback`` — (E13) exchanges the authorization
  code for the IdP's tokens. The exchange runs server-side so a
  confidential-client secret (when the IdP requires one) never reaches
  the browser; with the recommended public-client posture no secret is
  configured at all and PKCE alone protects the exchange.

No RBAC-mapped resource exists for ``/api/v1/auth`` in
``_ENDPOINT_RBAC_MAP``, so ``/session`` requires a verified bearer token in
any non-development environment (authentication still runs) but no specific
permission grant. ``/dev-token`` and both ``/sso/*`` endpoints are listed in
``_PUBLIC_PATHS`` because they are how a caller obtains a token in the first
place — they are reachable *before* any token exists, by necessity, and
neither reads nor writes platform state.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlencode

import jwt as pyjwt
import requests
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from aeam.security.dev_token_issuer import DevTokenIssuer

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/auth", tags=["Auth"])

# One issuer per process — tokens it mints are only ever consumed within
# the same process's lifetime (frontend dev session), so there is no need
# to persist or share the ephemeral keypair anywhere.
_dev_issuer = DevTokenIssuer()

# ---------------------------------------------------------------------------
# Phase E13 — OIDC discovery
# ---------------------------------------------------------------------------
#
# The discovery document is fetched at most once per process and cached:
# it is static configuration published by the IdP, and re-fetching it on
# every sign-in would make the login path depend on IdP latency twice.
# Explicit endpoint overrides in Settings bypass discovery entirely, which
# is what the tests (and IdPs without a discovery document) use.

_DISCOVERY_SUFFIX: str = "/.well-known/openid-configuration"
_discovery_cache: dict[str, dict[str, Any]] = {}


def reset_discovery_cache() -> None:
    """Clear the cached OIDC discovery documents.

    Exposed for tests and for an operator-driven process restart-free
    reconfiguration; discovery results are IdP-published configuration, not
    platform state, so dropping the cache is always safe.
    """
    _discovery_cache.clear()


def _fetch_discovery(issuer: str, timeout: float) -> dict[str, Any]:
    """Fetch (and cache) the IdP's openid-configuration document.

    Raises:
        HTTPException: 502 if the IdP is unreachable or returns something
                       that is not a JSON object — an honest "the IdP did
                       not answer", never a silent fallback to a partially
                       configured flow.
    """
    key = issuer.rstrip("/")
    cached = _discovery_cache.get(key)
    if cached is not None:
        return cached

    url = f"{key}{_DISCOVERY_SUFFIX}"
    try:
        response = requests.get(url, timeout=timeout)
        response.raise_for_status()
        document = response.json()
    except Exception as exc:  # noqa: BLE001
        # Declared boundary (CODE-5): every transport/parse failure means
        # the same thing to the caller — SSO cannot be started right now.
        logger.warning("oidc.discovery | FAILED | url=%s | detail=%s", url, exc)
        raise HTTPException(
            status_code=502,
            detail=f"OIDC discovery failed for issuer {key!r}: {exc}",
        ) from exc

    if not isinstance(document, dict):
        raise HTTPException(
            status_code=502,
            detail=f"OIDC discovery document at {url} was not a JSON object.",
        )

    _discovery_cache[key] = document
    logger.info("oidc.discovery | OK | issuer=%s", key)
    return document


def resolve_oidc_endpoints(settings: Any) -> dict[str, str]:
    """Resolve the IdP's authorization/token/JWKS endpoints.

    Explicit Settings overrides win; anything left empty is read from the
    issuer's discovery document. Discovery is attempted only when at least
    one endpoint is actually missing, so a fully-pinned configuration never
    touches the network.

    Args:
        settings: The application :class:`~aeam.config.settings.Settings`
                  (or any object exposing the same OIDC_* attributes).

    Returns:
        ``{"authorization_endpoint": ..., "token_endpoint": ...,
        "jwks_uri": ...}`` — values may be empty strings when the IdP
        publishes none, which callers must treat as "unavailable".

    Raises:
        HTTPException: 502 when discovery was required and failed.
    """
    authorization = str(getattr(settings, "OIDC_AUTHORIZATION_ENDPOINT", "") or "").strip()
    token = str(getattr(settings, "OIDC_TOKEN_ENDPOINT", "") or "").strip()
    jwks = str(getattr(settings, "OIDC_JWKS_URL", "") or "").strip()

    if not (authorization and token and jwks):
        issuer = str(getattr(settings, "OIDC_ISSUER", "") or "").strip()
        if issuer:
            timeout = float(getattr(settings, "OIDC_DISCOVERY_TIMEOUT_SECONDS", 5.0) or 5.0)
            document = _fetch_discovery(issuer, timeout)
            authorization = authorization or str(document.get("authorization_endpoint", "") or "")
            token = token or str(document.get("token_endpoint", "") or "")
            jwks = jwks or str(document.get("jwks_uri", "") or "")

    return {
        "authorization_endpoint": authorization,
        "token_endpoint": token,
        "jwks_uri": jwks,
    }


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


# ---------------------------------------------------------------------------
# Phase E13 — Enterprise SSO (OIDC authorization code + PKCE)
# ---------------------------------------------------------------------------


class SsoCallbackRequest(BaseModel):
    """Authorization-code exchange payload posted by the console callback."""

    code: str = Field(description="Authorization code returned by the IdP.")
    code_verifier: str = Field(
        default="",
        description=(
            "PKCE code verifier matching the challenge sent to the "
            "authorization endpoint. Required by any IdP configured for "
            "PKCE (the recommended public-client posture)."
        ),
    )
    redirect_uri: str = Field(
        default="",
        description=(
            "Redirect URI used in the authorization request. Must match it "
            "exactly. Empty falls back to the server-configured "
            "OIDC_REDIRECT_URI."
        ),
    )


def _sso_disabled_reason(settings: Any) -> str | None:
    """Return why SSO cannot be offered, or None when it is fully configured.

    Kept as one function so ``/sso/config`` and ``/sso/callback`` can never
    disagree about whether SSO is available.
    """
    if not bool(getattr(settings, "OIDC_ENABLED", False)):
        return "OIDC_ENABLED is false for this deployment."
    if not str(getattr(settings, "OIDC_ISSUER", "") or "").strip():
        return "OIDC_ISSUER is not configured."
    if not str(getattr(settings, "OIDC_CLIENT_ID", "") or "").strip():
        return "OIDC_CLIENT_ID is not configured."
    return None


@router.get("/sso/config", summary="Public OIDC parameters for the console sign-in redirect")
async def get_sso_config(request: Request) -> dict:
    """
    Return the parameters the console needs to start an SSO redirect.

    Response shape::

        {
          "enabled": true,
          "issuer": "https://idp.example.com/",
          "client_id": "aeam-console",
          "authorization_endpoint": "https://idp.example.com/authorize",
          "redirect_uri": "https://aeam.example.com/auth/callback",
          "scopes": "openid profile email",
          "response_type": "code",
          "code_challenge_method": "S256"
        }

    or, when SSO is not available::

        {"enabled": false, "reason": "OIDC_ENABLED is false for this deployment."}

    Every field here is public by definition — a client id, a redirect URI
    and an authorization URL all travel in the browser's address bar during
    a normal sign-in. The client *secret* is never included (SEC-5).

    Returns:
        The dict described above. ``502`` if SSO is enabled but the IdP's
        discovery document could not be fetched — an honest failure rather
        than an ``enabled: true`` response the browser cannot act on.
    """
    settings = getattr(request.app.state, "settings", None)

    reason = _sso_disabled_reason(settings)
    if reason is not None:
        return {"enabled": False, "reason": reason}

    endpoints = resolve_oidc_endpoints(settings)
    if not endpoints["authorization_endpoint"]:
        return {
            "enabled": False,
            "reason": (
                "The IdP published no authorization_endpoint and none is "
                "configured via OIDC_AUTHORIZATION_ENDPOINT."
            ),
        }

    return {
        "enabled": True,
        "issuer": str(settings.OIDC_ISSUER).strip(),
        "client_id": str(settings.OIDC_CLIENT_ID).strip(),
        "authorization_endpoint": endpoints["authorization_endpoint"],
        "redirect_uri": str(getattr(settings, "OIDC_REDIRECT_URI", "") or "").strip(),
        "scopes": str(getattr(settings, "OIDC_SCOPES", "") or "openid profile email").strip(),
        "response_type": "code",
        "code_challenge_method": "S256",
    }


@router.post("/sso/callback", summary="Exchange an OIDC authorization code for tokens")
async def sso_callback(request: Request, body: SsoCallbackRequest) -> dict:
    """
    Exchange the IdP's authorization code for the tokens it issued.

    The exchange runs server-side for two reasons: a confidential-client
    secret (when the IdP demands one) must never reach the browser, and the
    IdP's token endpoint frequently does not send CORS headers a browser
    would need. The response body is passed through essentially unchanged —
    AEAM adds nothing to the token and signs nothing itself.

    The returned ``access_token`` is what the console then presents on every
    API call, and it is verified on arrival by the *unchanged* E3
    ``SecurityMiddleware``/``JWTAuth`` path (against the IdP's JWKS keys).
    A token this endpoint returns therefore still has to pass issuer,
    audience, expiry and signature checks like any other — this endpoint
    grants no access by itself.

    Returns:
        The IdP's token response (``access_token``, ``token_type``,
        ``expires_in``, and ``id_token`` when the IdP issues one).

    Raises:
        HTTPException: ``404`` when SSO is not enabled (the endpoint does
                       not exist for that deployment), ``400`` when the IdP
                       rejects the code, ``502`` when the IdP is
                       unreachable or answers with a non-JSON body.
    """
    settings = getattr(request.app.state, "settings", None)

    reason = _sso_disabled_reason(settings)
    if reason is not None:
        raise HTTPException(status_code=404, detail=f"SSO is not enabled: {reason}")

    endpoints = resolve_oidc_endpoints(settings)
    token_endpoint = endpoints["token_endpoint"]
    if not token_endpoint:
        raise HTTPException(
            status_code=502,
            detail=(
                "The IdP published no token_endpoint and none is configured "
                "via OIDC_TOKEN_ENDPOINT."
            ),
        )

    redirect_uri = (
        body.redirect_uri.strip()
        or str(getattr(settings, "OIDC_REDIRECT_URI", "") or "").strip()
    )
    if not redirect_uri:
        raise HTTPException(
            status_code=400,
            detail=(
                "No redirect_uri supplied and OIDC_REDIRECT_URI is not "
                "configured; the IdP requires an exact match."
            ),
        )

    form: dict[str, str] = {
        "grant_type": "authorization_code",
        "code": body.code,
        "redirect_uri": redirect_uri,
        "client_id": str(settings.OIDC_CLIENT_ID).strip(),
    }
    if body.code_verifier.strip():
        form["code_verifier"] = body.code_verifier.strip()

    client_secret = str(getattr(settings, "OIDC_CLIENT_SECRET", "") or "").strip()
    if client_secret:
        form["client_secret"] = client_secret

    timeout = float(getattr(settings, "OIDC_DISCOVERY_TIMEOUT_SECONDS", 5.0) or 5.0)
    try:
        response = requests.post(
            token_endpoint,
            data=urlencode(form),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001
        # Declared boundary (CODE-5). Never log `form` — it carries the
        # authorization code and, for confidential clients, the secret.
        logger.warning("oidc.callback | token endpoint unreachable | detail=%s", exc)
        raise HTTPException(
            status_code=502, detail=f"OIDC token endpoint unreachable: {exc}"
        ) from exc

    try:
        payload = response.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "oidc.callback | non-JSON token response | status=%s", response.status_code
        )
        raise HTTPException(
            status_code=502,
            detail=f"OIDC token endpoint returned a non-JSON body (HTTP {response.status_code}).",
        ) from exc

    if response.status_code >= 400 or not isinstance(payload, dict) or "access_token" not in payload:
        # The IdP's own error code/description is the most useful thing an
        # operator can see here, and neither field ever carries a secret.
        error = ""
        if isinstance(payload, dict):
            error = str(payload.get("error_description") or payload.get("error") or "")
        logger.warning(
            "oidc.callback | exchange rejected | status=%s | error=%s",
            response.status_code, error,
        )
        raise HTTPException(
            status_code=400,
            detail=f"Authorization code exchange rejected by the IdP. {error}".strip(),
        )

    logger.info("oidc.callback | SUCCESS | token issued by IdP for the console session")
    result: dict[str, Any] = {
        "access_token": payload["access_token"],
        "token_type": str(payload.get("token_type", "bearer")),
    }
    if "expires_in" in payload:
        result["expires_in"] = payload["expires_in"]
    if "id_token" in payload:
        result["id_token"] = payload["id_token"]
    return result
