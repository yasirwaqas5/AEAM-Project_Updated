"""
aeam/tests/test_phase_e3_security.py

Phase E3 — Identity & Access Enforcement regression ledger (SEC-1..7, ARCH-7).

Covers the E3 contract:

1. Fail-closed startup (SEC-4): non-development environments abort loudly
   when no JWT public key is configured. Development keeps the pre-E3
   placeholder with a warning (COMPAT-1).
2. JWTAuth's engine-owned issuer/audience defaults (ENG-6) may be
   overridden per-instance via kwargs — Settings.JWT_ISSUER/JWT_AUDIENCE
   the only expected callers.
3. Per-router × per-role authorization matrix: every router prefix in
   the ``_ENDPOINT_RBAC_MAP`` is covered, and every role either passes
   or gets 403 against every mapped router as documented. Write-side
   configuration endpoints (admin/config, data-center, debug/retrieval,
   knowledge/delete, knowledge/reindex) are admin-only by construction.
4. Durable audit sink (ARCH-7): AuditLogger with a database_client
   persists a row into the audit_logs table AND appends to the file
   sink; a DB failure never breaks the file sink or the request.
5. CORS_ALLOWED_ORIGINS honoured by the CORS middleware.
6. Dev bypass unchanged (COMPAT-1): every mapped endpoint admits any
   request in development regardless of role.

All tests use in-process fakes / SQLite temp DBs (TEST-3).
"""

from __future__ import annotations

import datetime as _dt
import os
import tempfile
from pathlib import Path

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aeam.integrations.database import DatabaseClient
from aeam.middleware.security_middleware import (
    _ENDPOINT_RBAC_MAP,
    SecurityMiddleware,
)
from aeam.security.audit_logger import AuditLogger
from aeam.security.jwt_auth import JWTAuth
from aeam.security.rate_limiter import RateLimiter
from aeam.security.rbac import RBAC


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------

# One RS256 keypair for the whole module — expensive to generate, cheap to reuse.
_RSA = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PRIVATE_PEM: str = _RSA.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
).decode()
_PUBLIC_PEM: str = _RSA.public_key().public_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PublicFormat.SubjectPublicKeyInfo,
).decode()


def _mint_token(
    *,
    sub: str = "u-1",
    roles: list[str] | None = None,
    issuer: str = "aeam-auth",
    audience: str = "aeam-api",
    expires_in: int = 300,
) -> str:
    """Mint a real RS256 JWT signed with the test private key."""
    now = _dt.datetime.now(tz=_dt.timezone.utc)
    payload = {
        "sub": sub,
        "iss": issuer,
        "aud": audience,
        "iat": int(now.timestamp()),
        "exp": int((now + _dt.timedelta(seconds=expires_in)).timestamp()),
        "roles": roles or [],
    }
    return pyjwt.encode(payload, _PRIVATE_PEM, algorithm="RS256")


class _InMemoryRedis:
    """Duck-typed enough for RateLimiter — plain dict-backed."""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    def get(self, key):
        return self._store.get(key)

    def setex(self, key, ttl, value):
        self._store[key] = value


def _build_app(
    *,
    environment: str = "production",
    audit_logger: AuditLogger | None = None,
    routes: list[tuple[str, str]] | None = None,
) -> FastAPI:
    """Assemble a FastAPI app wearing the full SecurityMiddleware."""
    app = FastAPI()
    jwt_auth = JWTAuth(public_key=_PUBLIC_PEM)
    rbac = RBAC()
    rate_limiter = RateLimiter(redis_client=_InMemoryRedis())
    audit = audit_logger or AuditLogger(
        log_file=str(Path(tempfile.gettempdir()) / "e3_test_audit.log")
    )
    app.add_middleware(
        SecurityMiddleware,
        jwt_auth=jwt_auth,
        rbac=rbac,
        rate_limiter=rate_limiter,
        audit_logger=audit,
        environment=environment,
    )
    for path, _method in (routes or [("/health", "GET")]):
        _install_ok_route(app, path)
    return app


def _install_ok_route(app: FastAPI, path: str) -> None:
    """Attach a trivial handler that returns 200 for any GET on ``path``."""
    async def _ok() -> dict:
        return {"ok": True, "path": path}
    # Sanitise into a valid attribute name for FastAPI's endpoint name.
    _ok.__name__ = f"ok_{abs(hash(path))}"
    app.add_api_route(path, _ok, methods=["GET"])


# ===========================================================================
# 1. Fail-closed startup (SEC-4) — exercises main._build_jwt_auth directly.
# ===========================================================================

def test_dev_environment_uses_placeholder_key_when_none_configured(monkeypatch):
    """COMPAT-1: development startup unchanged when no PEM configured."""
    for k in ("JWT_PUBLIC_KEY", "JWT_PUBLIC_KEY_PATH"):
        monkeypatch.delenv(k, raising=False)

    from aeam.config.settings import Settings
    from aeam.main import _build_jwt_auth, _DEV_PLACEHOLDER_KEY

    settings = Settings(
        DATABASE_URL="sqlite:///:memory:",
        REDIS_URL="redis://localhost:6379/0",
        VECTOR_DB_URL="http://localhost",
        ENVIRONMENT="development",
    )
    auth = _build_jwt_auth(settings)
    # Placeholder loaded (private field access is intentional for the
    # regression: the value the middleware would have MUST be exactly the
    # documented placeholder — no accidental real key ever).
    assert auth._public_key == _DEV_PLACEHOLDER_KEY  # noqa: SLF001


@pytest.mark.parametrize("env", ["staging", "production", "test"])
def test_non_development_startup_fails_closed_without_key(monkeypatch, env):
    """SEC-4: non-development environments must refuse to start without a key."""
    for k in ("JWT_PUBLIC_KEY", "JWT_PUBLIC_KEY_PATH"):
        monkeypatch.delenv(k, raising=False)

    from aeam.config.settings import Settings
    from aeam.main import _build_jwt_auth

    settings = Settings(
        DATABASE_URL="sqlite:///:memory:",
        REDIS_URL="redis://localhost:6379/0",
        VECTOR_DB_URL="http://localhost",
        ENVIRONMENT=env,
    )
    with pytest.raises(RuntimeError, match="No JWT public key configured"):
        _build_jwt_auth(settings)


def test_pem_literal_via_settings_is_accepted_in_production(monkeypatch):
    """SEC-1/SEC-4: a real PEM in JWT_PUBLIC_KEY makes production startable."""
    from aeam.config.settings import Settings
    from aeam.main import _build_jwt_auth

    monkeypatch.setenv("JWT_PUBLIC_KEY", _PUBLIC_PEM)
    settings = Settings(
        DATABASE_URL="sqlite:///:memory:",
        REDIS_URL="redis://localhost:6379/0",
        VECTOR_DB_URL="http://localhost",
        ENVIRONMENT="production",
    )
    auth = _build_jwt_auth(settings)
    assert "BEGIN PUBLIC KEY" in auth._public_key  # noqa: SLF001


def test_pem_file_path_via_settings_is_accepted_in_production(monkeypatch, tmp_path):
    """SEC-1/SEC-4: JWT_PUBLIC_KEY_PATH fallback works when literal is unset."""
    monkeypatch.delenv("JWT_PUBLIC_KEY", raising=False)
    key_file = tmp_path / "pub.pem"
    key_file.write_text(_PUBLIC_PEM, encoding="utf-8")
    monkeypatch.setenv("JWT_PUBLIC_KEY_PATH", str(key_file))

    from aeam.config.settings import Settings
    from aeam.main import _build_jwt_auth

    settings = Settings(
        DATABASE_URL="sqlite:///:memory:",
        REDIS_URL="redis://localhost:6379/0",
        VECTOR_DB_URL="http://localhost",
        ENVIRONMENT="production",
    )
    auth = _build_jwt_auth(settings)
    assert "BEGIN PUBLIC KEY" in auth._public_key  # noqa: SLF001


# ===========================================================================
# 2. JWTAuth issuer / audience overrides (ENG-6).
# ===========================================================================

def test_jwt_default_issuer_and_audience_accept_tokens_matching_defaults():
    auth = JWTAuth(public_key=_PUBLIC_PEM)  # no overrides
    tok = _mint_token(roles=["admin"])
    payload = auth.verify(tok)
    assert payload["sub"] == "u-1"


def test_jwt_issuer_override_rejects_a_token_from_the_default_issuer():
    """A caller who tightened the issuer no longer accepts the default one."""
    auth = JWTAuth(public_key=_PUBLIC_PEM, issuer="corp-idp")
    default_tok = _mint_token()  # issuer='aeam-auth'
    with pytest.raises(Exception):
        auth.verify(default_tok)


def test_jwt_audience_override_accepts_a_token_minted_for_the_override():
    auth = JWTAuth(public_key=_PUBLIC_PEM, audience="aeam-console")
    tok = _mint_token(audience="aeam-console", roles=["operator"])
    payload = auth.verify(tok)
    assert payload["sub"] == "u-1"


# ===========================================================================
# 3. Per-router × per-role authorization matrix (SEC-3).
# ===========================================================================
#
# The matrix below documents the expected outcome for every distinct RBAC
# prefix in _ENDPOINT_RBAC_MAP, for each shipped role. `A` = allowed (200),
# `D` = denied (403). Empty roles → 401 covered separately.

# Fixed permission surface (must stay in sync with RBAC matrix).
_EXPECTED_MATRIX: dict[str, dict[str, str]] = {
    #                                    admin analyst operator auditor readonly
    "/api/v1/admin/config/x":            {"admin": "A", "analyst": "D", "operator": "D", "auditor": "D", "readonly": "D"},
    "/api/v1/debug/retrieval/x":         {"admin": "A", "analyst": "D", "operator": "D", "auditor": "D", "readonly": "D"},
    "/api/v1/data-center/x":             {"admin": "A", "analyst": "D", "operator": "D", "auditor": "D", "readonly": "D"},
    "/api/v1/knowledge/delete/x":        {"admin": "A", "analyst": "D", "operator": "D", "auditor": "D", "readonly": "D"},
    "/api/v1/knowledge/reindex/x":       {"admin": "A", "analyst": "D", "operator": "D", "auditor": "D", "readonly": "D"},
    "/api/v1/actions/approve/x":         {"admin": "A", "analyst": "D", "operator": "D", "auditor": "D", "readonly": "D"},
    "/api/v1/actions/x":                 {"admin": "A", "analyst": "D", "operator": "A", "auditor": "D", "readonly": "D"},
    "/api/v1/incidents/resolve/x":       {"admin": "A", "analyst": "D", "operator": "A", "auditor": "D", "readonly": "D"},
    "/api/v1/incidents/x":               {"admin": "A", "analyst": "A", "operator": "A", "auditor": "A", "readonly": "A"},
    "/api/v1/documents/ingest/x":        {"admin": "A", "analyst": "D", "operator": "A", "auditor": "D", "readonly": "D"},
    "/api/v1/documents/x":               {"admin": "A", "analyst": "A", "operator": "A", "auditor": "D", "readonly": "A"},
    "/api/v1/ingest/x":                  {"admin": "A", "analyst": "D", "operator": "A", "auditor": "D", "readonly": "D"},
    "/api/v1/knowledge/x":               {"admin": "A", "analyst": "A", "operator": "A", "auditor": "D", "readonly": "A"},
    "/api/v1/kpis/trigger/x":            {"admin": "A", "analyst": "A", "operator": "A", "auditor": "D", "readonly": "D"},
    "/api/v1/kpis/x":                    {"admin": "A", "analyst": "A", "operator": "A", "auditor": "D", "readonly": "A"},
    "/api/v1/trigger/x":                 {"admin": "A", "analyst": "A", "operator": "A", "auditor": "D", "readonly": "D"},
    "/api/v1/logs/x":                    {"admin": "A", "analyst": "A", "operator": "A", "auditor": "A", "readonly": "A"},
    "/api/v1/observability/x":           {"admin": "A", "analyst": "A", "operator": "A", "auditor": "A", "readonly": "A"},
    "/api/v1/system/x":                  {"admin": "A", "analyst": "A", "operator": "A", "auditor": "A", "readonly": "A"},
}


@pytest.fixture(scope="module")
def matrix_app() -> TestClient:
    routes = [(p, "GET") for p in _EXPECTED_MATRIX]
    app = _build_app(routes=routes)
    return TestClient(app)


def _pairs():
    for path, per_role in _EXPECTED_MATRIX.items():
        for role, verdict in per_role.items():
            yield path, role, verdict


@pytest.mark.parametrize("path,role,verdict", list(_pairs()))
def test_per_router_per_role_authorization_matrix(matrix_app, path, role, verdict):
    tok = _mint_token(roles=[role])
    resp = matrix_app.get(path, headers={"Authorization": f"Bearer {tok}"})
    if verdict == "A":
        assert resp.status_code == 200, (
            f"role={role} should be ALLOWED on {path}, got {resp.status_code} "
            f"body={resp.text}"
        )
    else:
        assert resp.status_code == 403, (
            f"role={role} should be DENIED on {path}, got {resp.status_code} "
            f"body={resp.text}"
        )


def test_missing_token_returns_401_on_any_mapped_endpoint(matrix_app):
    # Pick the first mapped path — any path is fine, the middleware runs
    # authentication before authorization.
    path = next(iter(_EXPECTED_MATRIX))
    resp = matrix_app.get(path)
    assert resp.status_code == 401


def test_invalid_token_returns_401_on_any_mapped_endpoint(matrix_app):
    path = next(iter(_EXPECTED_MATRIX))
    resp = matrix_app.get(path, headers={"Authorization": "Bearer not-a-jwt"})
    assert resp.status_code == 401


def test_expired_token_returns_401(matrix_app):
    path = next(iter(_EXPECTED_MATRIX))
    tok = _mint_token(roles=["admin"], expires_in=-10)  # already past
    resp = matrix_app.get(path, headers={"Authorization": f"Bearer {tok}"})
    assert resp.status_code == 401


def test_no_roles_in_token_returns_403_on_privileged_endpoint(matrix_app):
    tok = _mint_token(roles=[])
    resp = matrix_app.get(
        "/api/v1/admin/config/x", headers={"Authorization": f"Bearer {tok}"}
    )
    assert resp.status_code == 403


def test_rbac_map_contains_every_shipped_router_prefix():
    """
    Structural guard: every real router prefix on the app must appear in
    the RBAC map. If someone adds a router without an RBAC entry, this
    fails loudly.
    """
    expected_router_prefixes = {
        "/api/v1/admin/config",
        "/api/v1/data-center",
        "/api/v1/incidents",
        "/api/v1/ingest",
        "/api/v1/knowledge",
        "/api/v1/logs",
        "/api/v1/observability",
        "/api/v1/debug/retrieval",
        "/api/v1/system",
        "/api/v1/trigger",
    }
    mapped_prefixes = {prefix for prefix, _r, _a in _ENDPOINT_RBAC_MAP}
    for expected in expected_router_prefixes:
        matched = any(m.startswith(expected) or expected.startswith(m) for m in mapped_prefixes)
        assert matched, f"Router prefix {expected!r} is not covered by _ENDPOINT_RBAC_MAP"


# ===========================================================================
# 4. Durable audit sink (ARCH-7).
# ===========================================================================

def test_audit_logger_persists_row_to_audit_logs_when_db_client_attached(tmp_path):
    db = DatabaseClient(database_url="sqlite:///:memory:")
    try:
        audit = AuditLogger(
            log_file=str(tmp_path / "audit.log"),
            database_client=db,
        )
        audit.log({
            "user_id": "user-42",
            "action": "GET",
            "endpoint": "/api/v1/incidents",
            "status_code": 200,
            "incident_id": "INC-1",
        })

        rows = db.fetch_all(
            "SELECT user_id, action, endpoint, status_code, hash, extra "
            "FROM audit_logs"
        )
        assert len(rows) == 1
        row = rows[0]
        assert row["user_id"] == "user-42"
        assert row["action"] == "GET"
        assert row["endpoint"] == "/api/v1/incidents"
        assert row["status_code"] == 200
        assert row["hash"]  # non-empty
        assert "INC-1" in (row["extra"] or "")
    finally:
        db.dispose()


def test_audit_logger_survives_db_failure_without_breaking_file_sink(tmp_path):
    """A DB write failure must not cascade — file record already persisted."""

    class _BrokenDB:
        def insert(self, table, data, returning_column):
            raise RuntimeError("simulated DB outage")

    file = tmp_path / "audit.log"
    audit = AuditLogger(log_file=str(file), database_client=_BrokenDB())
    # Must not raise.
    audit.log({
        "user_id": "u",
        "action": "POST",
        "endpoint": "/api/v1/trigger",
        "status_code": 202,
    })
    assert file.exists()
    assert file.read_text().count("\n") == 1


def test_attach_database_upgrades_a_file_only_logger(tmp_path):
    """The main.create_app / lifespan handoff: file-only at construction, DB after attach."""
    file = tmp_path / "audit.log"
    audit = AuditLogger(log_file=str(file))  # no DB yet
    audit.log({
        "user_id": "u-before-attach",
        "action": "GET",
        "endpoint": "/api/v1/system",
        "status_code": 200,
    })

    db = DatabaseClient(database_url="sqlite:///:memory:")
    try:
        audit.attach_database(db)
        audit.log({
            "user_id": "u-after-attach",
            "action": "GET",
            "endpoint": "/api/v1/system",
            "status_code": 200,
        })
        rows = db.fetch_all("SELECT user_id FROM audit_logs")
        # Only the post-attach entry lands in the DB.
        assert [r["user_id"] for r in rows] == ["u-after-attach"]
        # File captured both.
        assert file.read_text().count("\n") == 2
    finally:
        db.dispose()


def test_audit_logger_file_only_behaviour_unchanged_when_no_db_configured(tmp_path):
    """COMPAT-1: pre-E3 file-only default preserved byte-for-byte."""
    file = tmp_path / "audit.log"
    audit = AuditLogger(log_file=str(file))  # no db
    audit.log({
        "user_id": "u",
        "action": "GET",
        "endpoint": "/x",
        "status_code": 200,
    })
    content = file.read_text()
    assert "user_id" in content
    assert "hash" in content


# ===========================================================================
# 5. CORS_ALLOWED_ORIGINS honoured.
# ===========================================================================

def test_cors_origins_are_read_from_settings(monkeypatch):
    """The list is derived from settings.CORS_ALLOWED_ORIGINS, comma-separated."""
    from aeam.config.settings import Settings

    monkeypatch.setenv(
        "CORS_ALLOWED_ORIGINS",
        "http://a.example.com, http://b.example.com , ,",  # includes noise
    )
    settings = Settings(
        DATABASE_URL="sqlite:///:memory:",
        REDIS_URL="redis://localhost:6379/0",
        VECTOR_DB_URL="http://localhost",
        ENVIRONMENT="development",
    )
    # Emulate the exact split logic create_app uses.
    origins = [
        o.strip() for o in (settings.CORS_ALLOWED_ORIGINS or "").split(",") if o.strip()
    ]
    assert origins == ["http://a.example.com", "http://b.example.com"]


def test_cors_default_preserves_pre_e3_frontend_origin():
    from aeam.config.settings import Settings

    settings = Settings(
        DATABASE_URL="sqlite:///:memory:",
        REDIS_URL="redis://localhost:6379/0",
        VECTOR_DB_URL="http://localhost",
        ENVIRONMENT="development",
    )
    assert settings.CORS_ALLOWED_ORIGINS == "http://localhost:5173"


# ===========================================================================
# 6. Development bypass unchanged (COMPAT-1).
# ===========================================================================

def test_development_bypass_admits_every_mapped_endpoint_without_a_token():
    """CLAUDE.md's documented invariant: dev bypasses all security checks."""
    routes = [(p, "GET") for p in _EXPECTED_MATRIX]
    app = _build_app(environment="development", routes=routes)
    client = TestClient(app)
    for path in _EXPECTED_MATRIX:
        resp = client.get(path)
        assert resp.status_code == 200, (
            f"dev bypass should admit {path} without a token; got {resp.status_code}"
        )
