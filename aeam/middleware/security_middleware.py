"""
aeam/middleware/security_middleware.py

Security middleware for the AEAM FastAPI application.

Applies JWT authentication, RBAC enforcement, and rate limiting to every
inbound API request. Internal agent routes (paths starting with
``/internal``) bypass all checks so that intra-system communication is
never blocked.

All dependencies are injected at construction time so the middleware is
fully testable in isolation.

Phase constraints:
- Must not block internal agent communication.
- Must not crash the system on unexpected errors.
- Logging at every step.
- 401 → invalid/missing token.
- 403 → RBAC denial.
- 429 → rate limit exceeded.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from aeam.security.audit_logger import AuditLogger
from aeam.security.jwt_auth import JWTAuth
from aeam.security.rate_limiter import RateLimiter
from aeam.security.rbac import RBAC

logger = logging.getLogger(__name__)

# Routes that bypass all security checks.
_INTERNAL_PREFIX: str = "/internal"

# Routes that never require authentication (e.g. health check).
# Phase E10: /api/v1/auth/dev-token is how a caller obtains a token in the
# first place (dev posture only -- the route itself 404s outside
# development, see aeam/api/auth.py), so it must be reachable pre-auth.
# /api/v1/auth/session deliberately is NOT listed here: it still requires a
# verified bearer token in any non-development environment (no RBAC
# resource mapping is needed since it grants no permission beyond "prove
# who you are").
#
# Phase E13: the two SSO endpoints are pre-auth by necessity -- they are the
# path by which a caller acquires a token from the enterprise IdP, so
# requiring a token to reach them would be circular. Neither is a security
# hole under SEC-1's deny-by-default rule: /sso/config returns only values
# that travel in the browser's address bar during a normal sign-in (client
# id, redirect URI, authorization URL) and never the client secret, and
# /sso/callback only forwards an authorization code to the IdP -- it grants
# nothing on its own, because whatever token it returns must still pass the
# full JWTAuth signature/issuer/audience/expiry check on the caller's next
# request. Both endpoints read and write no platform state.
_PUBLIC_PATHS: frozenset[str] = frozenset({
    "/", "/health", "/docs", "/openapi.json", "/redoc", "/favicon.ico",
    "/api/v1/auth/dev-token",
    "/api/v1/auth/sso/config",
    "/api/v1/auth/sso/callback",
})

# ---------------------------------------------------------------------------
# Endpoint → (resource, action) mapping for RBAC.
# Keys are path prefixes, matched by ``startswith``, longest prefix first.
# ---------------------------------------------------------------------------
_ENDPOINT_RBAC_MAP: list[tuple[str, str, str]] = [
    # (path_prefix, resource, action) — ordered LONGEST-PREFIX FIRST.
    # Every entry with a longer, more-specific prefix MUST come before
    # any entry that would otherwise swallow it (e.g. an admin sub-path
    # must precede its broader parent). _resolve_rbac iterates in order
    # and returns the first startswith() hit.

    # --- Strictest tier: configuration-writing endpoints (Phase E3,
    # SEC-3 / SEC-7). All map to admin:config, which only the 'admin'
    # role holds — no other role can reach these paths.
    ("/api/v1/admin/config",      "admin",     "config"),
    ("/api/v1/debug/retrieval",   "admin",     "config"),

    # --- Learning & calibration (Phase F2, SEC-3 / SEC-7).
    #
    # Recalibrating changes what every subsequent incident's confidence
    # MEANS, and deciding a proposal is a governance act — both are
    # configuration writes and sit in the strictest tier.
    #
    # The router's paths are deliberately shaped so read and write surfaces
    # never share a prefix. This map matches on path alone, not method, so
    # a read endpoint nested under a write prefix (or the reverse) would be
    # graded by whichever entry happened to come first — which is how an
    # auditor ends up able to approve a proposal. The three read paths are
    # listed first and named distinctly; everything else under /learning
    # falls through to admin:config, so a write surface added later is
    # guarded by default rather than by remembering to map it (SEC-1).
    ("/api/v1/learning/state",     "logs",  "view"),
    ("/api/v1/learning/history",   "logs",  "view"),
    ("/api/v1/learning/proposals", "logs",  "view"),
    ("/api/v1/learning",           "admin", "config"),

    # --- Business graph (Phase F4, SEC-3 / SEC-7).
    #
    # Rebuilding the graph changes what every subsequent investigation's
    # advisory graph finding says — platform-wide state, so it sits in the
    # strictest tier alongside the other configuration writes. The three
    # read surfaces map to documents:search: the graph is derived from the
    # same governed knowledge (datasets, policies, incident history) that
    # grant already covers, so an analyst who may search the corpus may
    # also ask what relates to what.
    #
    # Same shape as /learning above and for the same reason: reads are
    # listed individually with longer, distinct prefixes, and everything
    # else under /graph falls through to admin:config, so a write surface
    # added later is guarded by default (SEC-1).
    ("/api/v1/graph/stats",        "documents", "search"),
    ("/api/v1/graph/nodes",        "documents", "search"),
    ("/api/v1/graph/neighborhood", "documents", "search"),
    ("/api/v1/graph",              "admin",     "config"),

    # --- Actions (approve is stricter than execute).
    ("/api/v1/actions/approve",   "actions",   "approve"),
    ("/api/v1/actions",           "actions",   "execute"),

    # --- Human Review (Phase E9, SEC-3 / SEC-7). Casting a verdict RELEASES
    # withheld action execution, so it is guarded by the strictest action
    # grant — actions:approve — exactly like /actions/approve above. The two
    # read-only surfaces (queue depth and verdict history) are incident
    # reading, so they map to incidents:view and stay reachable by the
    # auditor/analyst roles that must be able to see governance state
    # without being able to release anything. Both read prefixes are longer
    # and therefore precede the broader /api/v1/review entry.
    ("/api/v1/review/queue",      "incidents", "view"),
    ("/api/v1/review/verdicts",   "incidents", "view"),
    ("/api/v1/review",            "actions",   "approve"),

    # --- Incidents (resolve is stricter than view).
    ("/api/v1/incidents/resolve", "incidents", "resolve"),
    ("/api/v1/incidents",         "incidents", "view"),

    # --- Documents: the legacy /documents/ingest route was retained by
    # the pre-E3 map even though the real router lives at /api/v1/ingest;
    # both are mapped so callers using either path see the same guard.
    ("/api/v1/documents/ingest",  "documents", "ingest"),
    ("/api/v1/documents",         "documents", "search"),
    ("/api/v1/ingest",            "documents", "ingest"),

    # --- Knowledge: read via GET on /knowledge; write endpoints under
    # /knowledge (delete/reindex) require admin:config — added before
    # the broader /knowledge entry so they resolve first.
    ("/api/v1/knowledge/delete",  "admin",     "config"),
    ("/api/v1/knowledge/reindex", "admin",     "config"),
    # Phase E12 (SEC-7): knowledge CURATION is privileged. Policy lifecycle
    # transitions, Enterprise Memory expunge/correct, and semantic-type
    # declarations all live under this one namespace precisely so a single
    # longest-prefix-first entry guards every one of them — a curation
    # endpoint added later cannot accidentally land outside the guard by
    # being registered at a path nobody remembered to map. Reads
    # (GET /knowledge/policies) stay on documents:search below.
    ("/api/v1/knowledge/curate",  "admin",     "config"),
    ("/api/v1/knowledge",         "documents", "search"),

    # --- Data Center: activating/deactivating datasets is configuration.
    ("/api/v1/data-center",       "admin",     "config"),

    # --- KPIs (trigger is stricter than read).
    ("/api/v1/kpis/trigger",      "kpis",      "trigger"),
    ("/api/v1/kpis",              "kpis",      "read"),

    # --- Manual trigger endpoint (the actual router prefix — the
    # pre-E3 map only covered /kpis/trigger, missing this path).
    ("/api/v1/trigger",           "kpis",      "trigger"),

    # --- Read-only observability and status: modelled as logs:view
    # so the auditor role reaches them by construction.
    ("/api/v1/logs",              "logs",      "view"),
    ("/api/v1/observability",     "logs",      "view"),
    ("/api/v1/system",            "logs",      "view"),

    # --- Audit query surface (Phase E11, SEC-6). Read-only queries over the
    # durable E3 audit_logs table. Mapped to logs:view because that is the
    # grant the 'auditor' role holds — the role the phase requires to be able
    # to query audit history by principal and time window. No new permission
    # verb is invented for it (ENG-6: one permission vocabulary); the
    # existing read-only-observability tier is exactly the right tier, and
    # the router itself can only ever read.
    ("/api/v1/audit",             "logs",      "view"),

    # --- Investigation & Timeline Replay (Phase F5, SEC-6).
    #
    # Replay reconstructs an incident's complete decision trail — the same
    # material the audit log exposes — so it is graded by the audit tier
    # rather than by incidents:view: an auditor must be able to walk an
    # investigation, and nobody should reach the full trail with only the
    # incident-list grant.
    #
    # ONE entry covers the whole prefix because the router has no write
    # surface at all (every endpoint is a GET, and the module imports
    # nothing that could execute or persist anything). Should that ever
    # change, the write would inherit logs:view rather than a stricter
    # tier — which is why the F5 test suite asserts the absence of any
    # non-GET route under this prefix.
    ("/api/v1/replay",            "logs",      "view"),
]

# Rate limit configuration applied to all authenticated requests.
_RATE_LIMIT_REQUESTS: int = 100
_RATE_LIMIT_WINDOW_SECONDS: int = 60


class SecurityMiddleware(BaseHTTPMiddleware):
    """
    FastAPI/Starlette middleware enforcing JWT auth, RBAC, and rate limiting.

    Applied to every request before it reaches a route handler. Internal
    routes (``/internal/*``) and a small set of public paths bypass all
    security checks.

    Per-request steps:
    1. Bypass if path starts with ``/internal`` or is in ``_PUBLIC_PATHS``,
       or if the environment is ``"development"``.
    2. Extract ``Authorization: Bearer <token>`` header.
    3. Verify the JWT via :class:`~aeam.security.jwt_auth.JWTAuth`.
    4. Extract ``user_id`` and ``roles`` from the payload.
    5. Map the endpoint path to an ``(resource, action)`` pair and perform
       RBAC check via :class:`~aeam.security.rbac.RBAC`.
    6. Check rate limit via :class:`~aeam.security.rate_limiter.RateLimiter`
       keyed on ``user_id + endpoint``.
    7. Forward the request to the next handler.
    8. Append an audit log entry via :class:`~aeam.security.audit_logger.AuditLogger`.

    Args:
        app:           The ASGI application to wrap.
        jwt_auth:      Configured JWT verifier.
        rbac:          RBAC policy enforcer.
        rate_limiter:  Redis-backed rate limiter.
        audit_logger:  Append-only audit recorder.
        environment:   Deployment environment ("development", "staging", "production").
                       In development, all security checks are bypassed.

    Example::

        app.add_middleware(
            SecurityMiddleware,
            jwt_auth=JWTAuth(public_key=public_key_pem),
            rbac=RBAC(),
            rate_limiter=RateLimiter(redis_client=redis),
            audit_logger=AuditLogger(),
            environment="development",
        )
    """

    def __init__(
        self,
        app: ASGIApp,
        jwt_auth: JWTAuth,
        rbac: RBAC,
        rate_limiter: RateLimiter,
        audit_logger: AuditLogger,
        environment: str = "production",
    ) -> None:
        """
        Initialise SecurityMiddleware with injected security dependencies.

        Args:
            app:          Wrapped ASGI application.
            jwt_auth:     JWT verifier instance.
            rbac:         RBAC enforcer instance.
            rate_limiter: Rate limiter instance.
            audit_logger: Audit logger instance.
            environment:  Deployment environment (default: "production").

        Raises:
            ValueError: If any required dependency is None.
        """
        super().__init__(app)

        if jwt_auth is None:
            raise ValueError("jwt_auth must not be None.")
        if rbac is None:
            raise ValueError("rbac must not be None.")
        if rate_limiter is None:
            raise ValueError("rate_limiter must not be None.")
        if audit_logger is None:
            raise ValueError("audit_logger must not be None.")

        self._jwt_auth: JWTAuth = jwt_auth
        self._rbac: RBAC = rbac
        self._rate_limiter: RateLimiter = rate_limiter
        self._audit: AuditLogger = audit_logger
        self._environment: str = environment

    # ------------------------------------------------------------------
    # Middleware dispatch
    # ------------------------------------------------------------------

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        """
        Process an incoming request through the security pipeline.

        Args:
            request:   Incoming :class:`fastapi.Request`.
            call_next: ASGI callable to forward the request downstream.

        Returns:
            :class:`fastapi.responses.JSONResponse` with an error status on
            security failure, or the downstream :class:`starlette.responses.Response`
            on success.
        logger.debug("SecurityMiddleware | ENTER DISPATCH | %s %s", method, path)
        """
        path: str = request.url.path
        method: str = request.method

        # Step 0: bypass ALL authentication in development, or if forced.
        if self._environment == "development":  # temporarily bypass all auth
            logger.debug("SecurityMiddleware | BYPASS (all auth disabled) | %s %s", method, path)
            return await call_next(request)

        # Step 1: bypass internal routes and public paths.
        if self._is_bypassed(path):
            logger.debug(
                "SecurityMiddleware | BYPASS | %s %s | env=%s",
                method, path, self._environment,
            )
            return await call_next(request)

        user_id: str = "anonymous"
        status_code: int = 200

        try:
            # Step 2: extract Authorization header.
            token = self._extract_bearer_token(request)
            if token is None:
                logger.warning(
                    "SecurityMiddleware | 401 | missing token | %s %s",
                    method, path,
                )
                return self._error_response(
                    status_code=401,
                    detail="Missing or malformed Authorization header. "
                           "Expected: 'Bearer <token>'.",
                )

            # Step 3: verify JWT.
            try:
                payload: dict[str, Any] = self._jwt_auth.verify(token)
            except Exception as exc:
                logger.warning(
                    "SecurityMiddleware | 401 | token invalid | %s %s | %s",
                    method, path, exc,
                )
                return self._error_response(
                    status_code=401,
                    detail=f"Invalid or expired token: {exc}",
                )

            # Step 4: extract user_id and roles.
            user_id = str(payload.get("sub", "unknown"))
            roles: list[str] = self._extract_roles(payload)

            # Phase E9: publish the verified principal on request.state so
            # route handlers can ATTRIBUTE an action to it (the Human Review
            # verdict endpoints record who approved what). This is
            # attribution only — authorisation still happens here, in step 5;
            # no handler re-checks permissions from these values.
            request.state.user_id = user_id
            request.state.roles = roles

            logger.info(
                "SecurityMiddleware | authenticated | user_id=%s | roles=%s | "
                "%s %s",
                user_id, roles, method, path,
            )

            # Step 5: RBAC check.
            resource, action = self._resolve_rbac(path)

            if resource and action:
                if not self._rbac.check_permission(roles, resource, action):
                    logger.warning(
                        "SecurityMiddleware | 403 | RBAC denied | user_id=%s | "
                        "resource=%s | action=%s | %s %s",
                        user_id, resource, action, method, path,
                    )
                    status_code = 403
                    return self._error_response(
                        status_code=403,
                        detail=f"Access denied: role(s) {roles} cannot perform "
                               f"'{action}' on '{resource}'.",
                    )
            else:
                logger.debug(
                    "SecurityMiddleware | no RBAC mapping for %s — skipping check.",
                    path,
                )

            # Step 6: rate limiting.
            rate_key: str = f"rate:{user_id}:{path}"
            if not self._rate_limiter.allow(
                key=rate_key,
                limit=_RATE_LIMIT_REQUESTS,
                window_seconds=_RATE_LIMIT_WINDOW_SECONDS,
            ):
                logger.warning(
                    "SecurityMiddleware | 429 | rate limited | user_id=%s | %s %s",
                    user_id, method, path,
                )
                status_code = 429
                return self._error_response(
                    status_code=429,
                    detail=f"Rate limit exceeded: max {_RATE_LIMIT_REQUESTS} "
                           f"requests per {_RATE_LIMIT_WINDOW_SECONDS}s.",
                )

            # Step 7: forward request.
            response: Response = await call_next(request)
            status_code = response.status_code

            logger.info(
                "SecurityMiddleware | forwarded | user_id=%s | %s %s | status=%d",
                user_id, method, path, status_code,
            )
            return response

        except Exception as exc:  # noqa: BLE001
            # Never crash the system — log and return 500.
            logger.error(
                "SecurityMiddleware | unexpected error | %s %s | error=%s",
                method, path, exc,
            )
            status_code = 500
            return self._error_response(
                status_code=500,
                detail="Internal security middleware error.",
            )

        finally:
            # Step 8: always write an audit log entry.
            self._write_audit(
                user_id=user_id,
                endpoint=path,
                status_code=status_code,
                method=method,
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_bypassed(path: str) -> bool:
        """
        Return ``True`` if ``path`` should bypass all security checks.

        Bypassed paths:
        - Any path starting with ``/internal``.
        - Paths in the ``_PUBLIC_PATHS`` whitelist (e.g. ``/health``).

        Args:
            path: The URL path of the incoming request.

        Returns:
            ``True`` if security checks should be skipped.
        """
        return path.startswith(_INTERNAL_PREFIX) or path in _PUBLIC_PATHS

    def _extract_bearer_token(self, request: Request) -> str | None:
        """
        Extract the Bearer token from the ``Authorization`` header.

        Args:
            request: Incoming FastAPI request.

        Returns:
            Token string, or ``None`` if the header is absent or
            does not follow the ``"Bearer <token>"`` format.
        """
        auth_header: str | None = request.headers.get("Authorization")
        if not auth_header:
            return None
        parts = auth_header.strip().split(" ", 1)
        if len(parts) != 2 or parts[0].lower() != "bearer":
            return None
        token = parts[1].strip()
        return token if token else None

    @staticmethod
    def _extract_roles(payload: dict[str, Any]) -> list[str]:
        """
        Extract role strings from the JWT payload.

        Looks for a ``"roles"`` claim (list) or a ``"role"`` claim (string).
        Returns an empty list if neither is present.

        Args:
            payload: Decoded JWT payload dict.

        Returns:
            List of role name strings.
        """
        roles = payload.get("roles")
        if isinstance(roles, list):
            return [str(r) for r in roles if r]

        role = payload.get("role")
        if isinstance(role, str) and role.strip():
            return [role.strip()]

        return []

    @staticmethod
    def _resolve_rbac(path: str) -> tuple[str, str]:
        """
        Map a URL path to an ``(resource, action)`` pair for RBAC.

        Iterates ``_ENDPOINT_RBAC_MAP`` in order (longest prefix first).
        Returns ``("", "")`` when no mapping matches — the RBAC check is
        then skipped for that path.

        Args:
            path: The URL path of the incoming request.

        Returns:
            Tuple of (resource, action) strings, or (``""``, ``""``) if
            no mapping is found.
        """
        for prefix, resource, action in _ENDPOINT_RBAC_MAP:
            if path.startswith(prefix):
                return resource, action
        return "", ""

    @staticmethod
    def _error_response(status_code: int, detail: str) -> JSONResponse:
        """
        Build a JSON error response.

        Args:
            status_code: HTTP status code (401, 403, 429, 500).
            detail:      Human-readable error description.

        Returns:
            :class:`fastapi.responses.JSONResponse` with the given status
            and a ``{"detail": "..."}`` body.
        """
        return JSONResponse(
            status_code=status_code,
            content={"detail": detail},
        )

    def _write_audit(
        self,
        user_id: str,
        endpoint: str,
        status_code: int,
        method: str,
    ) -> None:
        """
        Append an audit log entry for the completed request.

        Failures are caught and logged at ERROR level — an audit write
        failure must never crash the middleware or affect the response.

        Args:
            user_id:     Authenticated user or ``"anonymous"``.
            endpoint:    Request URL path.
            status_code: Final HTTP status code of the response.
            method:      HTTP method (GET, POST, etc.).
        """
        try:
            self._audit.log({
                "user_id":     user_id,
                "action":      method,
                "endpoint":    endpoint,
                "status_code": status_code,
            })
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "SecurityMiddleware._write_audit | FAILED | user_id=%s | "
                "endpoint=%s | error=%s",
                user_id, endpoint, exc,
            )

    def __repr__(self) -> str:
        return (
            f"SecurityMiddleware("
            f"rate_limit={_RATE_LIMIT_REQUESTS}req/"
            f"{_RATE_LIMIT_WINDOW_SECONDS}s, "
            f"env={self._environment})"
        )