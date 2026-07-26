"""
aeam/tests/test_phase_e10_console.py

Phase E10 — Enterprise Console regression ledger (SEC-1, EXPL-5, ENG-7,
ARCH-1).

Covers the backend half of the E10 contract:

1. ``POST /api/v1/auth/dev-token`` is reachable only in a development
   posture; it 404s everywhere else so it can never leak into staging or
   production (mirrors the SecurityMiddleware placeholder-key precedent).
2. ``GET /api/v1/auth/session`` reports the verified principal when
   SecurityMiddleware has authenticated the request, falls back to an
   honestly-labelled unverified decode in development, and reports
   ``authenticated: False`` when no usable token is present.
3. The production static-frontend mount (``_mount_frontend_build``):
   a no-op (and unchanged ``GET /`` liveness JSON, COMPAT-1) when
   ``frontend/dist`` does not exist; SPA fallback to ``index.html`` for
   client-side routes when it does; API/infra prefixes never fall through
   to the SPA shell.

All tests use in-process FastAPI apps -- no live DB/Redis/Qdrant required
(TEST-3).
"""

from __future__ import annotations

import time

import jwt as pyjwt
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from aeam.api.auth import router as auth_router
import aeam.main as aeam_main


class _FakeSettings:
    def __init__(self, environment: str) -> None:
        self.ENVIRONMENT = environment


def _build_app(environment: str) -> FastAPI:
    app = FastAPI()
    app.state.settings = _FakeSettings(environment)
    app.include_router(auth_router)
    return app


def _build_app_with_state(user_id: str | None, roles: list[str] | None) -> FastAPI:
    """An app that simulates SecurityMiddleware having already run and
    published request.state.user_id/roles before the route handler runs."""
    app = FastAPI()
    app.state.settings = _FakeSettings("production")

    @app.middleware("http")
    async def _inject_state(request: Request, call_next):
        if user_id is not None:
            request.state.user_id = user_id
            request.state.roles = roles or []
        return await call_next(request)

    app.include_router(auth_router)
    return app


# ---------------------------------------------------------------------------
# 1. Dev-token minting gate
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("environment", ["staging", "production", "test", ""])
def test_dev_token_404_outside_development(environment: str) -> None:
    client = TestClient(_build_app(environment))
    resp = client.post("/api/v1/auth/dev-token", json={"sub": "u1", "roles": ["admin"]})
    assert resp.status_code == 404


def test_dev_token_issues_valid_shape_in_development() -> None:
    client = TestClient(_build_app("development"))
    resp = client.post("/api/v1/auth/dev-token", json={"sub": "alice", "roles": ["analyst"]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["sub"] == "alice"
    assert body["roles"] == ["analyst"]
    assert body["expires_in"] > 0

    # Structurally a JWT (decodable without verifying the ephemeral key).
    payload = pyjwt.decode(body["access_token"], options={"verify_signature": False})
    assert payload["sub"] == "alice"
    assert payload["roles"] == ["analyst"]
    assert payload["exp"] > time.time()


def test_dev_token_defaults_and_ttl_bounds() -> None:
    client = TestClient(_build_app("development"))
    resp = client.post("/api/v1/auth/dev-token", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["sub"] == "dev-user"
    assert body["roles"] == ["admin"]

    # ttl_seconds is bounded (60..86400) at the request-model level.
    too_short = client.post("/api/v1/auth/dev-token", json={"ttl_seconds": 1})
    assert too_short.status_code == 422


# ---------------------------------------------------------------------------
# 2. Session "who am I"
# ---------------------------------------------------------------------------

def test_session_reports_verified_principal_from_middleware_state() -> None:
    client = TestClient(_build_app_with_state(user_id="u-42", roles=["operator"]))
    resp = client.get("/api/v1/auth/session")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"authenticated": True, "sub": "u-42", "roles": ["operator"], "source": "verified"}


def test_session_falls_back_to_unverified_decode_when_state_absent() -> None:
    client = TestClient(_build_app_with_state(user_id=None, roles=None))
    token = pyjwt.encode({"sub": "bob", "roles": ["auditor"]}, "irrelevant-secret", algorithm="HS256")
    resp = client.get("/api/v1/auth/session", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"authenticated": True, "sub": "bob", "roles": ["auditor"], "source": "unverified"}


def test_session_reports_unauthenticated_with_no_token() -> None:
    client = TestClient(_build_app_with_state(user_id=None, roles=None))
    resp = client.get("/api/v1/auth/session")
    assert resp.status_code == 200
    assert resp.json() == {"authenticated": False, "sub": None, "roles": [], "source": "none"}


def test_session_ignores_anonymous_placeholder() -> None:
    # SecurityMiddleware initialises user_id="anonymous" before auth resolves;
    # /session must not treat that sentinel as a real identity.
    client = TestClient(_build_app_with_state(user_id="anonymous", roles=[]))
    resp = client.get("/api/v1/auth/session")
    assert resp.json()["authenticated"] is False


# ---------------------------------------------------------------------------
# 3. Static frontend mount (ARCH-1: single deployable)
# ---------------------------------------------------------------------------

def test_no_dist_directory_leaves_root_liveness_route_untouched(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(aeam_main, "_FRONTEND_DIST", tmp_path / "does-not-exist")

    app = FastAPI()

    @app.get("/")
    async def _root() -> JSONResponse:
        return JSONResponse({"status": "alive"})

    aeam_main._mount_frontend_build(app)
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.json() == {"status": "alive"}


def test_spa_fallback_serves_index_for_client_routes(monkeypatch, tmp_path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html><body>AEAM Console</body></html>", encoding="utf-8")
    assets = dist / "assets"
    assets.mkdir()
    (assets / "app.js").write_text("console.log('hi');", encoding="utf-8")

    monkeypatch.setattr(aeam_main, "_FRONTEND_DIST", dist)

    app = FastAPI()

    @app.get("/")
    async def _root() -> JSONResponse:
        return JSONResponse({"status": "alive"})

    aeam_main._mount_frontend_build(app)
    client = TestClient(app)

    # Client-side route falls back to the SPA shell.
    resp = client.get("/incidents")
    assert resp.status_code == 200
    assert "AEAM Console" in resp.text

    # Built asset is served directly, not the SPA shell.
    resp = client.get("/assets/app.js")
    assert resp.status_code == 200
    assert "console.log" in resp.text

    # An unknown API path never falls through to the HTML shell.
    resp = client.get("/api/v1/does-not-exist")
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("application/json")

    # Root liveness route registered before the mount still wins (COMPAT-1).
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.json() == {"status": "alive"}
