"""
aeam/tests/test_phase_e13_certification.py

Phase E13 — Enterprise Certification & Scale Validation.

Acceptance criteria under test:

1. **Login federates against a real IdP.** A token minted by a test
   identity provider, signed with a key published only through that IdP's
   JWKS document, is accepted by the *unchanged* E3 verification path —
   and rejected when its issuer, audience, expiry, signing key, or
   algorithm is wrong. The IdP here is a real RSA keypair plus a real
   JWKS document served through a stubbed transport: real cryptography,
   no network.
2. **SSO is fail-closed.** Enabling OIDC without the configuration it
   requires aborts startup rather than degrading to a weaker posture, and
   the console's SSO endpoints honestly report *why* SSO is unavailable
   instead of offering a control that cannot work.
3. **The relying-party flow works end to end.** ``/sso/config`` returns
   what the browser needs (and never the client secret); ``/sso/callback``
   exchanges an authorization code and surfaces IdP rejections honestly.
4. **A full restore drill succeeds** across the database and the object
   store, with the Redis posture declared and Qdrant honestly reported.
   The drill refuses to run against its own source.
5. **CI blocks on a known-vulnerable dependency fixture**, generates an
   SBOM, and scans the image — asserted as a workflow contract so the
   gates cannot be quietly removed or turned into warnings.
6. **Article XVI reads 100% checked with evidence**, every evidence link
   resolves to a file that exists, and the re-scored audit shows no
   category below the agreed floor.
7. **Backward compatibility.** A deployment that does not enable SSO
   behaves exactly as it did in E3.

Infrastructure: real cryptography, real SQLite, real FastAPI TestClient,
stubbed HTTP transport (TEST-3). No network, no live IdP, no Qdrant.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
from pathlib import Path

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient

import aeam.api.auth as auth_api
from aeam.api.auth import router as auth_router
from aeam.api.system import router as system_router
from aeam.config.settings import Settings
from aeam.integrations.database import DatabaseClient
from aeam.main import _build_jwt_auth
from aeam.middleware.security_middleware import _PUBLIC_PATHS
from aeam.security.jwt_auth import JWTAuth
from aeam.storage.blob_store import LocalDiskBlobStore

REPO_ROOT = Path(__file__).resolve().parents[2]


# ===========================================================================
# A test identity provider — real keys, real JWKS, no network.
# ===========================================================================

_IDP_ISSUER = "https://idp.test.example.com"
_IDP_CLIENT_ID = "aeam-console"
_IDP_KID = "test-idp-key-1"

_IDP_RSA = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_IDP_PRIVATE_PEM: str = _IDP_RSA.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
).decode()

# A second, unrelated keypair — the "attacker's" key. It is never published
# in the IdP's JWKS, so a token signed with it must never verify.
_ROGUE_RSA = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_ROGUE_PRIVATE_PEM: str = _ROGUE_RSA.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
).decode()


def _jwks_document() -> dict:
    """The IdP's published JWKS — the only channel through which AEAM ever
    learns the signing key."""
    numbers = _IDP_RSA.public_key().public_numbers()
    return {
        "keys": [
            {
                **json.loads(pyjwt.algorithms.RSAAlgorithm.to_jwk(_IDP_RSA.public_key())),
                "kid": _IDP_KID,
                "use": "sig",
                "alg": "RS256",
                # Asserted below so a PyJWT change of JWK shape is caught here
                # rather than as a mysterious verification failure.
                "_n_bits": numbers.n.bit_length(),
            }
        ]
    }


def _discovery_document() -> dict:
    return {
        "issuer": _IDP_ISSUER,
        "authorization_endpoint": f"{_IDP_ISSUER}/authorize",
        "token_endpoint": f"{_IDP_ISSUER}/oauth/token",
        "jwks_uri": f"{_IDP_ISSUER}/.well-known/jwks.json",
    }


def _idp_token(
    *,
    sub: str = "alice@example.com",
    roles: list[str] | None = None,
    issuer: str = _IDP_ISSUER,
    audience: str = _IDP_CLIENT_ID,
    expires_in: int = 300,
    private_pem: str = _IDP_PRIVATE_PEM,
    kid: str | None = _IDP_KID,
) -> str:
    """Mint a token exactly as the IdP would."""
    now = _dt.datetime.now(tz=_dt.timezone.utc)
    payload = {
        "sub": sub,
        "iss": issuer,
        "aud": audience,
        "iat": int(now.timestamp()),
        "exp": int((now + _dt.timedelta(seconds=expires_in)).timestamp()),
        "roles": roles or ["analyst"],
    }
    headers = {"kid": kid} if kid else None
    return pyjwt.encode(payload, private_pem, algorithm="RS256", headers=headers)


class _StubResponse:
    """Minimal stand-in for requests.Response."""

    def __init__(self, payload, status_code: int = 200, text: str = ""):
        self._payload = payload
        self.status_code = status_code
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("Not JSON")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


@pytest.fixture(autouse=True)
def _clear_discovery_cache():
    """Discovery results are cached per process; a test's IdP must never
    leak into the next one's."""
    auth_api.reset_discovery_cache()
    yield
    auth_api.reset_discovery_cache()


@pytest.fixture()
def stub_idp(monkeypatch):
    """Serve the test IdP's discovery + JWKS documents over stubbed HTTP.

    ``requests.get`` (used by discovery) and PyJWT's JWKS fetcher (used by
    verification) are both redirected here, so the code under test runs its
    real URL construction, caching, and kid-selection logic.
    """
    calls: dict[str, int] = {"discovery": 0, "jwks": 0}

    def _fake_get(url, timeout=None, **kwargs):
        if url.endswith("/.well-known/openid-configuration"):
            calls["discovery"] += 1
            return _StubResponse(_discovery_document())
        raise AssertionError(f"Unexpected GET {url}")

    def _fake_fetch_data(self):
        calls["jwks"] += 1
        return _jwks_document()

    monkeypatch.setattr(auth_api.requests, "get", _fake_get)
    monkeypatch.setattr(pyjwt.PyJWKClient, "fetch_data", _fake_fetch_data)
    return calls


def _oidc_settings(**overrides) -> Settings:
    base = dict(
        DATABASE_URL="sqlite:///:memory:",
        REDIS_URL="redis://localhost:6379/0",
        VECTOR_DB_URL="http://localhost",
        ENVIRONMENT="staging",
        OIDC_ENABLED=True,
        OIDC_ISSUER=_IDP_ISSUER,
        OIDC_CLIENT_ID=_IDP_CLIENT_ID,
        OIDC_REDIRECT_URI="https://aeam.example.com/auth/callback",
    )
    base.update(overrides)
    return Settings(**base)


def _auth_app(settings: Settings) -> TestClient:
    """The auth router with `settings` published where the router reads it.

    SecurityMiddleware is deliberately absent: these endpoints are in
    ``_PUBLIC_PATHS`` (asserted separately), so mounting the middleware
    would test the middleware, not the endpoints.
    """
    app = FastAPI()
    app.include_router(auth_router)
    app.state.settings = settings
    return TestClient(app)


# ===========================================================================
# 1. Federated verification on the E3 path (JWKS)
# ===========================================================================


def test_jwks_document_is_a_usable_rsa_key(stub_idp):
    """Guards the fixture itself: a malformed JWKS would make every
    negative test below pass for the wrong reason."""
    key = _jwks_document()["keys"][0]
    assert key["kty"] == "RSA"
    assert key["_n_bits"] == 2048
    assert key["kid"] == _IDP_KID


def test_idp_token_verifies_through_jwks(stub_idp):
    auth = JWTAuth(
        public_key="",
        jwks_url=f"{_IDP_ISSUER}/.well-known/jwks.json",
        issuer=_IDP_ISSUER,
        audience=_IDP_CLIENT_ID,
    )
    payload = auth.verify(_idp_token(sub="alice@example.com", roles=["admin"]))

    assert payload["sub"] == "alice@example.com"
    assert payload["roles"] == ["admin"]
    assert auth.uses_jwks is True
    assert stub_idp["jwks"] >= 1, "The JWKS document was never fetched."


def test_token_signed_with_an_unpublished_key_is_rejected(stub_idp):
    """The core federation guarantee: only keys the IdP publishes can
    authenticate anyone."""
    auth = JWTAuth(
        public_key="",
        jwks_url=f"{_IDP_ISSUER}/.well-known/jwks.json",
        issuer=_IDP_ISSUER,
        audience=_IDP_CLIENT_ID,
    )
    rogue = _idp_token(private_pem=_ROGUE_PRIVATE_PEM)

    with pytest.raises(pyjwt.InvalidTokenError):
        auth.verify(rogue)


@pytest.mark.parametrize(
    "kwargs, reason",
    [
        ({"issuer": "https://evil.example.com"}, "wrong issuer"),
        ({"audience": "some-other-app"}, "wrong audience"),
        ({"expires_in": -60}, "expired"),
        ({"kid": "unknown-kid"}, "unknown kid"),
    ],
)
def test_malformed_or_mismatched_idp_tokens_are_rejected(stub_idp, kwargs, reason):
    auth = JWTAuth(
        public_key="",
        jwks_url=f"{_IDP_ISSUER}/.well-known/jwks.json",
        issuer=_IDP_ISSUER,
        audience=_IDP_CLIENT_ID,
    )
    with pytest.raises(pyjwt.InvalidTokenError):
        auth.verify(_idp_token(**kwargs))


def test_unreachable_jwks_endpoint_fails_closed_as_invalid_token(monkeypatch):
    """An IdP outage must produce a 401-shaped failure, never an accepted
    request and never a 500."""
    def _boom(self):
        raise ConnectionError("IdP unreachable")

    monkeypatch.setattr(pyjwt.PyJWKClient, "fetch_data", _boom)

    auth = JWTAuth(
        public_key="",
        jwks_url=f"{_IDP_ISSUER}/.well-known/jwks.json",
        issuer=_IDP_ISSUER,
        audience=_IDP_CLIENT_ID,
    )
    with pytest.raises(pyjwt.InvalidTokenError):
        auth.verify(_idp_token())


def test_algorithm_allow_list_rejects_an_unlisted_algorithm(stub_idp):
    """A token must not be accepted merely because it is well-formed:
    the algorithm is an allow-list, so `alg: none` and friends cannot pass."""
    auth = JWTAuth(
        public_key="",
        jwks_url=f"{_IDP_ISSUER}/.well-known/jwks.json",
        issuer=_IDP_ISSUER,
        audience=_IDP_CLIENT_ID,
        algorithms=["RS512"],
    )
    with pytest.raises(pyjwt.InvalidTokenError):
        auth.verify(_idp_token())  # signed RS256, not in the allow-list


def test_empty_algorithm_list_falls_back_to_the_engine_default(stub_idp):
    """An empty override must never mean 'accept nothing' *or* 'accept
    anything' — it means 'use the engine-owned default' (ENG-6)."""
    auth = JWTAuth(
        public_key="",
        jwks_url=f"{_IDP_ISSUER}/.well-known/jwks.json",
        issuer=_IDP_ISSUER,
        audience=_IDP_CLIENT_ID,
        algorithms=[],
    )
    assert auth.verify(_idp_token())["sub"] == "alice@example.com"


# ===========================================================================
# 2. Fail-closed startup and backward compatibility
# ===========================================================================


def test_oidc_startup_resolves_jwks_from_discovery(stub_idp):
    auth = _build_jwt_auth(_oidc_settings())

    assert auth.uses_jwks is True
    assert stub_idp["discovery"] == 1, "Discovery should run exactly once at startup."
    assert auth.verify(_idp_token())["sub"] == "alice@example.com"


def test_pinned_jwks_url_skips_discovery_entirely(stub_idp):
    auth = _build_jwt_auth(
        _oidc_settings(
            OIDC_JWKS_URL=f"{_IDP_ISSUER}/.well-known/jwks.json",
            OIDC_AUTHORIZATION_ENDPOINT=f"{_IDP_ISSUER}/authorize",
            OIDC_TOKEN_ENDPOINT=f"{_IDP_ISSUER}/oauth/token",
        )
    )
    assert auth.uses_jwks is True
    assert stub_idp["discovery"] == 0, "Fully pinned configuration must not call the IdP."


@pytest.mark.parametrize(
    "overrides, missing",
    [
        ({"OIDC_ISSUER": ""}, "OIDC_ISSUER"),
        ({"OIDC_CLIENT_ID": ""}, "OIDC_CLIENT_ID"),
    ],
)
def test_incomplete_oidc_configuration_aborts_startup(stub_idp, overrides, missing):
    with pytest.raises(RuntimeError) as exc:
        _build_jwt_auth(_oidc_settings(**overrides))
    assert missing in str(exc.value)
    assert "SEC-4" in str(exc.value)


def test_oidc_fails_closed_even_in_development(stub_idp):
    """SEC-4 with no environment escape hatch: an operator who switched SSO
    on has declared an intent, and silently running the placeholder-key
    posture instead would be the platform lying about who is authenticated."""
    with pytest.raises(RuntimeError):
        _build_jwt_auth(_oidc_settings(ENVIRONMENT="development", OIDC_ISSUER=""))


def test_unreachable_idp_at_startup_aborts_rather_than_degrading(monkeypatch):
    def _boom(url, timeout=None, **kwargs):
        raise ConnectionError("no route to IdP")

    monkeypatch.setattr(auth_api.requests, "get", _boom)

    with pytest.raises(RuntimeError) as exc:
        _build_jwt_auth(_oidc_settings())
    assert "SEC-4" in str(exc.value)


def test_issuer_without_jwks_uri_aborts_startup(monkeypatch):
    def _fake_get(url, timeout=None, **kwargs):
        document = _discovery_document()
        document.pop("jwks_uri")
        return _StubResponse(document)

    monkeypatch.setattr(auth_api.requests, "get", _fake_get)

    with pytest.raises(RuntimeError) as exc:
        _build_jwt_auth(_oidc_settings())
    assert "jwks_uri" in str(exc.value)


def test_sso_disabled_keeps_the_e3_static_key_path(tmp_path):
    """COMPAT: a deployment that never touches OIDC behaves exactly as E3."""
    pem_path = tmp_path / "public.pem"
    pem_path.write_text(
        _IDP_RSA.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode(),
        encoding="utf-8",
    )

    settings = Settings(
        DATABASE_URL="sqlite:///:memory:",
        REDIS_URL="redis://localhost:6379/0",
        VECTOR_DB_URL="http://localhost",
        ENVIRONMENT="staging",
        JWT_PUBLIC_KEY_PATH=str(pem_path),
    )
    auth = _build_jwt_auth(settings)

    assert auth.uses_jwks is False
    # Default issuer/audience are still the engine-owned E3 values.
    payload = auth.verify(_idp_token(issuer="aeam-auth", audience="aeam-api"))
    assert payload["sub"] == "alice@example.com"


def test_static_key_construction_still_rejects_an_empty_key():
    """The pre-E13 guard must not have been widened by the JWKS branch."""
    with pytest.raises(ValueError):
        JWTAuth(public_key="   ")


# ===========================================================================
# 3. The console's relying-party endpoints
# ===========================================================================


def test_sso_endpoints_are_reachable_before_authentication():
    """They are how a caller obtains a token; requiring one would be circular."""
    assert "/api/v1/auth/sso/config" in _PUBLIC_PATHS
    assert "/api/v1/auth/sso/callback" in _PUBLIC_PATHS


def test_sso_config_reports_disabled_with_a_reason():
    client = _auth_app(
        Settings(
            DATABASE_URL="sqlite:///:memory:",
            REDIS_URL="redis://localhost:6379/0",
            VECTOR_DB_URL="http://localhost",
            ENVIRONMENT="staging",
        )
    )
    body = client.get("/api/v1/auth/sso/config").json()

    assert body["enabled"] is False
    assert "OIDC_ENABLED" in body["reason"], "A disabled control must say why (EXPL-3/5)."


def test_sso_config_returns_the_redirect_parameters(stub_idp):
    client = _auth_app(_oidc_settings())
    body = client.get("/api/v1/auth/sso/config").json()

    assert body["enabled"] is True
    assert body["authorization_endpoint"] == f"{_IDP_ISSUER}/authorize"
    assert body["client_id"] == _IDP_CLIENT_ID
    assert body["redirect_uri"] == "https://aeam.example.com/auth/callback"
    assert body["response_type"] == "code"
    assert body["code_challenge_method"] == "S256"


def test_sso_config_never_exposes_the_client_secret(stub_idp):
    client = _auth_app(_oidc_settings(OIDC_CLIENT_SECRET="super-secret-value"))
    raw = client.get("/api/v1/auth/sso/config").text

    assert "super-secret-value" not in raw, "SEC-5: a secret must never leave the process."


def test_sso_callback_exchanges_the_authorization_code(stub_idp, monkeypatch):
    captured: dict = {}

    def _fake_post(url, data=None, headers=None, timeout=None, **kwargs):
        captured["url"] = url
        captured["data"] = data
        return _StubResponse(
            {
                "access_token": _idp_token(),
                "token_type": "Bearer",
                "expires_in": 3600,
                "id_token": "id.token.value",
            }
        )

    monkeypatch.setattr(auth_api.requests, "post", _fake_post)

    client = _auth_app(_oidc_settings())
    response = client.post(
        "/api/v1/auth/sso/callback",
        json={"code": "auth-code-123", "code_verifier": "verifier-abc"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "Bearer"
    assert body["expires_in"] == 3600
    assert body["id_token"] == "id.token.value"

    assert captured["url"] == f"{_IDP_ISSUER}/oauth/token"
    assert "grant_type=authorization_code" in captured["data"]
    assert "code_verifier=verifier-abc" in captured["data"]
    assert "client_secret" not in captured["data"], (
        "No secret is configured, so none must be sent — the public-client "
        "+ PKCE posture."
    )


def test_sso_callback_returns_a_token_the_verifier_actually_accepts(stub_idp, monkeypatch):
    """End-to-end: the console signs in and its next API call would pass
    the unchanged SecurityMiddleware verification."""
    monkeypatch.setattr(
        auth_api.requests,
        "post",
        lambda *a, **k: _StubResponse({"access_token": _idp_token(roles=["admin"])}),
    )

    client = _auth_app(_oidc_settings())
    token = client.post("/api/v1/auth/sso/callback", json={"code": "c"}).json()["access_token"]

    verifier = _build_jwt_auth(_oidc_settings())
    assert verifier.verify(token)["roles"] == ["admin"]


def test_sso_callback_forwards_the_configured_client_secret(stub_idp, monkeypatch):
    captured: dict = {}

    def _fake_post(url, data=None, **kwargs):
        captured["data"] = data
        return _StubResponse({"access_token": _idp_token()})

    monkeypatch.setattr(auth_api.requests, "post", _fake_post)

    client = _auth_app(_oidc_settings(OIDC_CLIENT_SECRET="confidential-client-secret"))
    client.post("/api/v1/auth/sso/callback", json={"code": "c"})

    assert "client_secret=confidential-client-secret" in captured["data"]


def test_sso_callback_reports_an_idp_rejection_honestly(stub_idp, monkeypatch):
    monkeypatch.setattr(
        auth_api.requests,
        "post",
        lambda *a, **k: _StubResponse(
            {"error": "invalid_grant", "error_description": "Code already used."},
            status_code=400,
        ),
    )

    client = _auth_app(_oidc_settings())
    response = client.post("/api/v1/auth/sso/callback", json={"code": "stale"})

    assert response.status_code == 400
    assert "Code already used." in response.json()["detail"]


def test_sso_callback_reports_an_unreachable_idp_as_502(stub_idp, monkeypatch):
    def _boom(*a, **k):
        raise ConnectionError("token endpoint down")

    monkeypatch.setattr(auth_api.requests, "post", _boom)

    client = _auth_app(_oidc_settings())
    assert client.post("/api/v1/auth/sso/callback", json={"code": "c"}).status_code == 502


def test_sso_callback_is_absent_when_sso_is_disabled():
    client = _auth_app(
        Settings(
            DATABASE_URL="sqlite:///:memory:",
            REDIS_URL="redis://localhost:6379/0",
            VECTOR_DB_URL="http://localhost",
            ENVIRONMENT="staging",
        )
    )
    assert client.post("/api/v1/auth/sso/callback", json={"code": "c"}).status_code == 404


# ===========================================================================
# 4. Tenancy / data-classification declaration
# ===========================================================================


def _system_client(settings: Settings) -> TestClient:
    class _Container:
        pass

    container = _Container()
    container.settings = settings

    app = FastAPI()
    app.include_router(system_router)
    app.state.container = container
    return TestClient(app)


def test_compliance_endpoint_states_the_declared_postures():
    body = _system_client(
        Settings(
            DATABASE_URL="sqlite:///:memory:",
            REDIS_URL="redis://localhost:6379/0",
            VECTOR_DB_URL="http://localhost",
            ENVIRONMENT="staging",
        )
    ).get("/api/v1/system/compliance").json()

    assert body["tenancy"]["model"] == "single-tenant"
    assert "no tenant discriminator" in body["tenancy"]["enforced_by"]
    assert body["data_classification"]["level"] == "internal"
    assert body["data_classification"]["pii_posture"] == "not-expected"
    assert body["identity"]["mode"] == "static-key"
    assert body["identity"]["is_identity_provider"] is False
    assert body["retention"]["backup_runbook"] == "docs/DISASTER_RECOVERY.md"


def test_compliance_endpoint_reflects_an_sso_deployment(stub_idp):
    body = _system_client(_oidc_settings()).get("/api/v1/system/compliance").json()

    assert body["identity"]["mode"] == "enterprise-sso"
    assert body["identity"]["issuer"] == _IDP_ISSUER


def test_tenancy_model_rejects_an_undeclarable_value():
    """A deployment cannot invent a tenancy position the platform does not
    implement."""
    with pytest.raises(Exception):
        Settings(
            DATABASE_URL="sqlite:///:memory:",
            REDIS_URL="redis://localhost:6379/0",
            VECTOR_DB_URL="http://localhost",
            ENVIRONMENT="staging",
            TENANCY_MODEL="sort-of-multi-tenant",
        )


def test_system_compliance_is_covered_by_the_rbac_map():
    """SEC-3 parity: a new route without an RBAC mapping is a defect."""
    from aeam.middleware.security_middleware import _ENDPOINT_RBAC_MAP

    assert any(
        "/api/v1/system/compliance".startswith(prefix)
        for prefix, _, _ in _ENDPOINT_RBAC_MAP
    )


# ===========================================================================
# 5. Backup / restore drill (the rehearsal IS the test)
# ===========================================================================


@pytest.fixture()
def dr_drill():
    """Import the operational drill script by path.

    It lives in ``scripts/`` rather than ``aeam/`` on purpose (E13 adds no
    application module), so it is loaded the way an operator runs it.
    """
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location(
        "aeam_dr_drill", REPO_ROOT / "scripts" / "dr_drill.py"
    )
    module = importlib.util.module_from_spec(spec)
    # Registered before execution because dataclasses resolves string
    # annotations through sys.modules; a path-loaded module that skips this
    # fails at class-definition time on newer Pythons.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(spec.name, None)


def _seed_source(db: DatabaseClient, blob_store, count: int = 25) -> list[str]:
    """Populate a source deployment with incidents and their blobs."""
    hashes: list[str] = []
    for index in range(count):
        db.insert(
            table="incidents",
            data={
                "incident_id": f"INC-{index:04d}",
                "event_id": f"EVT-{index:04d}",
                "event_type": "kpi_anomaly",
                "metric": f"metric-{index}",
                "severity": "HIGH" if index % 2 else "CRITICAL",
                "timestamp": "2026-07-01T00:00:00Z",
                "requires_human": bool(index % 3 == 0),
                "findings": json.dumps([{"type": "root_cause", "data": {"i": index}}]),
            },
        )
        ref = blob_store.put(f"runbook body {index}".encode("utf-8"))
        hashes.append(ref.content_hash)
    return hashes


def test_full_restore_drill_succeeds_and_verifies(dr_drill, tmp_path):
    source = DatabaseClient(database_url=f"sqlite:///{(tmp_path / 'source.db').as_posix()}")
    target = DatabaseClient(database_url=f"sqlite:///{(tmp_path / 'restore.db').as_posix()}")
    source_blobs = LocalDiskBlobStore(root_dir=str(tmp_path / "blobs-src"))
    target_blobs = LocalDiskBlobStore(root_dir=str(tmp_path / "blobs-dst"))

    hashes = _seed_source(source, source_blobs)
    for content_hash in hashes:
        source.insert(
            table="documents",
            data={
                "doc_id": content_hash[:16],
                "title": f"{content_hash[:8]}.md",
                "doc_type": "runbook",
                "content_hash": content_hash,
            },
            returning_column="doc_id",
        )

    try:
        evidence = dr_drill.run_drill(
            source_db=source,
            restore_db=target,
            backup_dir=tmp_path / "backup",
            source_blob_store=source_blobs,
            restore_blob_store=target_blobs,
            vector_db_url="",
        )
    finally:
        source.dispose()
        target.dispose()

    by_name = {r["name"]: r for r in evidence["results"]}

    assert evidence["passed"] is True, f"Drill failed: {evidence['results']}"
    assert by_name["database.backup"]["status"] == "ok"
    assert by_name["database.restore"]["status"] == "ok"
    assert by_name["database.verify"]["status"] == "ok"
    assert by_name["blobs.backup"]["status"] == "ok"
    assert by_name["blobs.restore"]["status"] == "ok"
    assert by_name["database.backup"]["data"]["row_counts"]["incidents"] == 25

    # Redis posture is declared, not implemented (MEM-6 requires the statement).
    assert by_name["redis.posture"]["data"]["backed_up"] == "no"
    assert by_name["redis.posture"]["data"]["rationale"]

    # Qdrant is derived state; unreachable is an honest skip, never a silent pass.
    assert by_name["qdrant.snapshot"]["status"] == "skipped"
    assert "VECTOR_DB_URL" in by_name["qdrant.snapshot"]["detail"]

    # Every restored blob is byte-identical (content addressing proves it).
    for content_hash in hashes:
        assert target_blobs.exists(content_hash)
        assert target_blobs.get(content_hash) == source_blobs.get(content_hash)


def test_drill_detects_a_lossy_restore(dr_drill, tmp_path):
    """The drill must be able to FAIL, or it proves nothing."""
    source = DatabaseClient(database_url=f"sqlite:///{(tmp_path / 'source.db').as_posix()}")
    target = DatabaseClient(database_url=f"sqlite:///{(tmp_path / 'restore.db').as_posix()}")
    blobs = LocalDiskBlobStore(root_dir=str(tmp_path / "blobs"))

    try:
        _seed_source(source, blobs, count=10)
        backup_dir = tmp_path / "backup"
        dr_drill.backup_database(source, backup_dir)

        # Simulate a partial restore: drop rows from the backup before it is
        # applied, exactly as a truncated dump or an interrupted copy would.
        payload = json.loads((backup_dir / "database.json").read_text(encoding="utf-8"))
        full = list(payload["tables"]["incidents"])
        payload["tables"]["incidents"] = full[:5]
        (backup_dir / "database.json").write_text(json.dumps(payload), encoding="utf-8")
        dr_drill.restore_database(target, backup_dir)

        # Restore the full manifest so verification compares against truth.
        payload["tables"]["incidents"] = full
        (backup_dir / "database.json").write_text(json.dumps(payload), encoding="utf-8")
        result = dr_drill.verify_database(target, backup_dir)
    finally:
        source.dispose()
        target.dispose()

    assert result.status == "failed"
    assert result.data["row_count_mismatches"]["incidents"] == {"expected": 10, "actual": 5}


def test_drill_refuses_to_restore_over_its_own_source(dr_drill, tmp_path):
    db = DatabaseClient(database_url=f"sqlite:///{(tmp_path / 'only.db').as_posix()}")
    try:
        with pytest.raises(ValueError) as exc:
            dr_drill.run_drill(
                source_db=db,
                restore_db=db,
                backup_dir=tmp_path / "backup",
            )
        assert "different database" in str(exc.value)
    finally:
        db.dispose()


def test_drill_records_a_documented_posture_for_every_store(dr_drill):
    """MEM-6: every store has a declared retention/backup posture."""
    assert "incidents" in dr_drill.BACKED_UP_TABLES
    assert "audit_logs" in dr_drill.BACKED_UP_TABLES
    # E9 governance state: an approval chain that did not survive recovery
    # would silently release work a human never signed off on.
    assert "incident_approvals" in dr_drill.BACKED_UP_TABLES
    assert "review_verdicts" in dr_drill.BACKED_UP_TABLES
    assert dr_drill.REDIS_POSTURE["backed_up"] == "no"
    assert dr_drill.REDIS_POSTURE["recovery"]


def test_drill_serializes_json_columns_for_reinsertion(dr_drill):
    """PostgreSQL returns json/jsonb columns as native dicts, which the
    driver cannot adapt back into an INSERT. Regression for a defect the
    first live drill against real PostgreSQL surfaced: the export must
    canonicalise them, or a restore fails on any incident with findings."""
    findings = {"b": 2, "a": [1, {"z": None}]}

    exported = dr_drill._json_safe(findings)
    assert isinstance(exported, str)
    # Canonical: exporting the same content twice must be byte-identical,
    # or the verification digest would never match after a round trip.
    assert exported == dr_drill._json_safe({"a": [1, {"z": None}], "b": 2})
    assert json.loads(exported) == findings

    assert dr_drill._json_safe(["x", "y"]) == '["x", "y"]'
    assert dr_drill._json_safe("already text") == "already text"
    assert dr_drill._json_safe(7) == 7


# ===========================================================================
# 6. Supply-chain gates (workflow contract)
# ===========================================================================


@pytest.fixture(scope="module")
def workflow_text() -> str:
    path = REPO_ROOT / ".github" / "workflows" / "deploy.yml"
    assert path.exists(), "CI workflow is missing."
    return path.read_text(encoding="utf-8")


def test_ci_runs_a_dependency_audit(workflow_text):
    assert "pip-audit --requirement requirements.txt" in workflow_text


def test_ci_proves_the_dependency_gate_blocks_a_vulnerable_fixture(workflow_text):
    fixture = REPO_ROOT / "deploy" / "security" / "vulnerable-fixture-requirements.txt"
    assert fixture.exists(), "The negative-test fixture is missing."

    pins = [
        line.strip()
        for line in fixture.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert pins, "The fixture pins nothing; it cannot be vulnerable."
    assert all("==" in pin for pin in pins), (
        "Every fixture entry must be exactly pinned — a range could resolve "
        "to a patched version and silently stop being vulnerable."
    )

    assert "vulnerable-fixture-requirements.txt" in workflow_text
    assert "reported the known-vulnerable fixture as CLEAN" in workflow_text, (
        "The workflow must fail when the scanner passes the fixture — a "
        "scanner that never fails is indistinguishable from one that is off."
    )
    # A non-zero exit alone is not evidence: pip-audit also exits non-zero on
    # an unresolvable requirements file. The gate must confirm an advisory
    # was actually reported.
    assert 'grep -qi "known vulnerabilit"' in workflow_text, (
        "The gate self-test must verify the scanner NAMED a vulnerability, "
        "not merely that it exited non-zero."
    )
    assert "WITHOUT reporting a vulnerability" in workflow_text


def test_ci_generates_an_sbom(workflow_text):
    assert "cyclonedx-py environment" in workflow_text
    assert "aeam-sbom.json" in workflow_text


def test_ci_scans_the_container_image(workflow_text):
    assert "trivy-action" in workflow_text
    assert "CRITICAL,HIGH" in workflow_text
    assert 'exit-code: "1"' in workflow_text, "The image scan must block, not warn."


def test_supply_chain_and_performance_gates_are_blocking(workflow_text):
    # Comment lines are excluded: the header prose explains *why* these
    # escapes are forbidden, and matching its own words would be a
    # self-defeating assertion.
    directives = "\n".join(
        line for line in workflow_text.splitlines() if not line.strip().startswith("#")
    )
    assert "continue-on-error" not in directives, (
        "A gate with continue-on-error is a warning wearing a gate's name."
    )
    assert "|| true" not in directives
    assert "needs: [test, supply-chain, performance]" in directives


def test_ci_runs_the_performance_budget_suite(workflow_text):
    assert "test_phase_e13_performance.py" in workflow_text


# ===========================================================================
# 7. Article XVI sweep — 100% checked, every item evidenced
# ===========================================================================

_CERTIFICATION_DOC = REPO_ROOT / "docs" / "ENTERPRISE_CERTIFICATION.md"


def _article_xvi_items() -> list[str]:
    """Every checklist item in the Constitution's Article XVI, verbatim.

    Parsed from CONSTITUTION.md rather than duplicated here, so an item
    added to the Constitution immediately fails this suite until the
    certification pack answers it.
    """
    constitution = (REPO_ROOT / "CONSTITUTION.md").read_text(encoding="utf-8")
    section = constitution.split("## Article XVI")[1].split("## Article XVII")[0]
    return [
        line.strip()[6:].strip()
        for line in section.splitlines()
        if line.strip().startswith("- [ ]") or line.strip().startswith("- [x]")
    ]


def test_article_xvi_has_the_expected_shape():
    """Guards the parser: a silently-empty item list would make every
    assertion below vacuously true."""
    items = _article_xvi_items()
    assert len(items) >= 18, f"Parsed only {len(items)} Article XVI items."
    assert any("SSO/OIDC" in item for item in items)
    assert any("Multi-tenancy position" in item for item in items)


def test_certification_pack_checks_every_article_xvi_item():
    assert _CERTIFICATION_DOC.exists(), "The enterprise evidence pack is missing."
    pack = _CERTIFICATION_DOC.read_text(encoding="utf-8")

    unanswered = [item for item in _article_xvi_items() if item not in pack]
    assert not unanswered, (
        "Article XVI items absent from the certification pack: " + "; ".join(unanswered)
    )

    unchecked = re.findall(r"^\s*- \[ \]", pack, re.M)
    assert not unchecked, (
        f"{len(unchecked)} Article XVI item(s) remain unchecked in the "
        "certification pack — E13 requires 100%."
    )

    checked = re.findall(r"^\s*- \[x\]", pack, re.M)
    assert len(checked) >= len(_article_xvi_items())


def test_every_certification_evidence_link_resolves():
    """An evidence link pointing at a file that does not exist is worse
    than no link: it asserts proof that cannot be inspected."""
    pack = _CERTIFICATION_DOC.read_text(encoding="utf-8")

    # Backtick-quoted repo paths are the pack's evidence convention.
    candidates = set(re.findall(r"`([A-Za-z0-9_./-]+\.(?:py|md|yml|yaml|json|jsx|js|txt))`", pack))
    assert len(candidates) >= 20, f"Only {len(candidates)} evidence paths found in the pack."

    missing = sorted(str(p) for p in candidates if not (REPO_ROOT / p).exists())
    assert not missing, f"Evidence paths that do not exist: {missing}"


def _roadmap_projected_scores() -> dict[str, int]:
    """The roadmap's projected post-E13 scorecard — the agreed floor.

    Parsed from ROADMAP.md rather than restated here, so the floor cannot
    drift from the contract it came from. Values like '80+' and '~70' are
    read as their numeric part.
    """
    roadmap = (REPO_ROOT / "ROADMAP.md").read_text(encoding="utf-8")
    section = roadmap.split("## Projected end-state scorecard")[1].split("\n\n", 2)[1]
    scores: dict[str, int] = {}
    for line in section.splitlines():
        match = re.match(r"^\|\s*([A-Za-z][A-Za-z /-]+?)\s*\|\s*(\d+)\s*\|\s*~?(\d+)\+?\s*\|", line)
        if match:
            scores[match.group(1).strip()] = int(match.group(3))
    return scores


def test_audit_rescore_meets_the_agreed_floor():
    """The roadmap's projected end-state scorecard is the agreed floor —
    per category, not one blanket number. A re-score that quietly lands
    below it is a failed certification, not a footnote."""
    pack = _CERTIFICATION_DOC.read_text(encoding="utf-8")

    rows = re.findall(r"^\|\s*([A-Za-z][A-Za-z /-]+?)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|", pack, re.M)
    scored = {name.strip(): (int(before), int(after)) for name, before, after in rows}

    floor = _roadmap_projected_scores()
    assert len(floor) >= 10, f"Parsed only {len(floor)} categories from the roadmap scorecard."

    unscored = sorted(set(floor) - set(scored))
    assert not unscored, f"Categories the re-score never reports: {unscored}"

    below = {
        name: (after, floor[name])
        for name, (_, after) in scored.items()
        if name in floor and after < floor[name]
    }
    assert not below, f"Categories below the agreed floor (actual, floor): {below}"

    regressed = {name: (b, a) for name, (b, a) in scored.items() if a < b}
    assert not regressed, f"Categories that scored WORSE after E13: {regressed}"


def test_evidence_pack_documents_are_all_present():
    for name in (
        "ENTERPRISE_CERTIFICATION.md",
        "SECURITY_POSTURE.md",
        "DISASTER_RECOVERY.md",
        "PERFORMANCE_BASELINES.md",
    ):
        assert (REPO_ROOT / "docs" / name).exists(), f"docs/{name} is missing."
