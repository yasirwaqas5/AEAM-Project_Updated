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
_PUBLIC_PATHS: frozenset[str] = frozenset({"/health", "/docs", "/openapi.json", "/redoc", "/favicon.ico"})

# ---------------------------------------------------------------------------
# Endpoint → (resource, action) mapping for RBAC.
# Keys are path prefixes, matched by ``startswith``, longest prefix first.
# ---------------------------------------------------------------------------
_ENDPOINT_RBAC_MAP: list[tuple[str, str, str]] = [
    # (path_prefix, resource, action)  — ordered longest-prefix first.
    ("/api/v1/actions/approve",  "actions",   "approve"),
    ("/api/v1/actions",          "actions",   "execute"),
    ("/api/v1/incidents/resolve","incidents", "resolve"),
    ("/api/v1/incidents",        "incidents", "view"),
    ("/api/v1/documents/ingest", "documents", "ingest"),
    ("/api/v1/documents",        "documents", "search"),
    ("/api/v1/kpis/trigger",     "kpis",      "trigger"),
    ("/api/v1/kpis",             "kpis",      "read"),
    ("/api/v1/logs",             "logs",      "view"),
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
    1. Bypass if path starts with ``/internal`` or is in ``_PUBLIC_PATHS``.
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

    Example::

        app.add_middleware(
            SecurityMiddleware,
            jwt_auth=JWTAuth(public_key=public_key_pem),
            rbac=RBAC(),
            rate_limiter=RateLimiter(redis_client=redis),
            audit_logger=AuditLogger(),
        )
    """

    def __init__(
        self,
        app: ASGIApp,
        jwt_auth: JWTAuth,
        rbac: RBAC,
        rate_limiter: RateLimiter,
        audit_logger: AuditLogger,
    ) -> None:
        """
        Initialise SecurityMiddleware with injected security dependencies.

        Args:
            app:          Wrapped ASGI application.
            jwt_auth:     JWT verifier instance.
            rbac:         RBAC enforcer instance.
            rate_limiter: Rate limiter instance.
            audit_logger: Audit logger instance.

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
        """
        path: str = request.url.path
        method: str = request.method

        # Step 1: bypass internal routes and public paths.
        if self._is_bypassed(path):
            logger.debug("SecurityMiddleware | BYPASS | %s %s", method, path)
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

    @staticmethod
    def _extract_bearer_token(request: Request) -> str | None:
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
            f"{_RATE_LIMIT_WINDOW_SECONDS}s)"
        )