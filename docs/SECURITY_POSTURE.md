# AEAM Security Posture

**Phase E13 — Enterprise Certification.** This document is the security half of
the enterprise evidence pack. It states what AEAM enforces, where it enforces
it, and what it deliberately does not do. Everything below is verifiable in the
repository; the file paths are the evidence.

Companion documents: [`docs/ENTERPRISE_CERTIFICATION.md`](ENTERPRISE_CERTIFICATION.md)
(the Article XVI sweep), [`docs/DISASTER_RECOVERY.md`](DISASTER_RECOVERY.md),
[`docs/human_in_the_loop.md`](human_in_the_loop.md),
[`docs/ai_governance.md`](ai_governance.md).

---

## 1. Identity posture

**AEAM validates identity; it never issues it.** There is no user database, no
password store, no credential-reset flow, and no session cookie. The platform
accepts bearer tokens minted by the organization's identity provider and
verifies them cryptographically on every request.

Two verification modes share one code path (`aeam/security/jwt_auth.py`):

| Mode | Key source | Expected `iss` | Expected `aud` | When to use |
|---|---|---|---|---|
| Static key (E3) | `JWT_PUBLIC_KEY` or `JWT_PUBLIC_KEY_PATH` | `aeam-auth` (or `JWT_ISSUER`) | `aeam-api` (or `JWT_AUDIENCE`) | A deployment whose tokens are minted by an internal service with a fixed key. |
| Enterprise SSO (E13) | The IdP's JWKS document, key selected by the token's `kid` | `OIDC_ISSUER` | `OIDC_CLIENT_ID` | Federated sign-in against Entra ID, Okta, Auth0, Keycloak, Ping, or any OIDC-compliant provider. |

Both modes enforce, on every request: signature, algorithm allow-list
(`RS256` by default), `exp`, `iss`, `aud`. All three claims are *required* —
a token missing any of them is rejected, never accepted with defaults.

### Enabling enterprise SSO

Register AEAM's console at the IdP as a **public client** using the
authorization-code flow with PKCE, redirect URI `https://<console-host>/auth/callback`.
Then set:

```
OIDC_ENABLED=true
OIDC_ISSUER=https://login.example.com/                 # the IdP's issuer URL
OIDC_CLIENT_ID=aeam-console                            # as registered
OIDC_REDIRECT_URI=https://aeam.example.com/auth/callback
OIDC_SCOPES=openid profile email
OIDC_ROLES_CLAIM=roles                                 # or groups, per your IdP
```

Endpoints are read from `<OIDC_ISSUER>/.well-known/openid-configuration` at
startup. Pin them with `OIDC_JWKS_URL`, `OIDC_AUTHORIZATION_ENDPOINT` and
`OIDC_TOKEN_ENDPOINT` for an IdP that publishes no discovery document — with
all three pinned, AEAM never calls discovery at all.

A confidential client is supported (`OIDC_CLIENT_SECRET`, sourced from Secret
Manager) but not recommended: the console is a browser application, and PKCE
is the correct protection for it. When a secret *is* configured, the
authorization-code exchange runs server-side in
`POST /api/v1/auth/sso/callback` so the secret never reaches the browser.

**Role mapping.** The claim named by `OIDC_ROLES_CLAIM` supplies the caller's
roles, which must be members of the vocabulary in `aeam/security/rbac.py`.
An unrecognised role grants nothing — deny by default (SEC-1). Map your IdP
groups to AEAM's roles at the IdP, not inside AEAM: a role-mapping layer
in the platform would be a second authorization system.

### Fail-closed contract (SEC-4)

Startup aborts, loudly, in these cases — implemented in
`aeam/main.py` (`_build_jwt_auth`, `_build_oidc_jwt_auth`):

- Non-development environment with no key material configured.
- `JWT_PUBLIC_KEY_PATH` set but unreadable, outside development.
- `OIDC_ENABLED=true` with `OIDC_ISSUER` or `OIDC_CLIENT_ID` missing —
  **in every environment, including development.** An operator who switched
  federation on has declared an intent; silently running the placeholder-key
  posture instead would be the platform lying about who is authenticated.
- `OIDC_ENABLED=true` with an unreachable IdP or an issuer publishing no
  `jwks_uri`.

Only `ENVIRONMENT=development` falls back to the well-known placeholder key,
and it logs a warning naming the placeholder every time it does.

### What an IdP outage does

A JWKS fetch failure is converted to an invalid-token error, so requests get
`401` — never `500`, and never an accepted request. Sign-in stops working;
nothing becomes more permissive. See
`aeam/tests/test_phase_e13_certification.py::test_unreachable_jwks_endpoint_fails_closed_as_invalid_token`.

---

## 2. Authorization

Every mapped route resolves to a `(resource, action)` pair through
`_ENDPOINT_RBAC_MAP` in `aeam/middleware/security_middleware.py`, matched
longest-prefix-first. Configuration-writing surfaces — `/admin/config`,
`/data-center`, `/knowledge/curate`, `/knowledge/delete`, `/knowledge/reindex`,
`/debug/retrieval` — sit in the strictest tier (`admin:config`), reachable by
the `admin` role alone (SEC-7). Casting a human-review verdict releases
withheld execution and is therefore guarded by `actions:approve`, the same
grant as direct action approval.

**Parity is a review-blocking rule (SEC-3):** a change that adds or renames a
route updates the map in the same change. The per-router × per-role matrix in
`aeam/tests/test_phase_e3_security.py` and the E13 coverage assertion in
`aeam/tests/test_phase_e13_certification.py` enforce it.

### Endpoints that are authenticated but ungated

| Path | Why |
|---|---|
| `/`, `/health`, `/docs`, `/openapi.json`, `/redoc`, `/favicon.ico` | Liveness and API description. No platform data. |
| `/api/v1/auth/dev-token` | Development-only token minting; the route itself returns 404 outside a development posture. |
| `/api/v1/auth/sso/config` | Public OIDC parameters — client id, redirect URI, authorization URL. All three travel in the browser's address bar during a normal sign-in. Never includes the client secret. |
| `/api/v1/auth/sso/callback` | The authorization-code exchange. Pre-auth by necessity: it is how a token is obtained. It grants nothing by itself — any token it returns must still pass full verification on the next request. |
| `/api/v1/auth/session` | *Not* public. Requires a verified token in every non-development environment; it carries no permission grant beyond "prove who you are". |

The development bypass (`ENVIRONMENT=development` skips all checks) is
strictly conditional and has never widened (SEC-2). It is exercised as a
regression in `aeam/tests/test_phase_e3_security.py`.

---

## 3. Rate limiting

Redis-backed, keyed on principal + endpoint, 100 requests per 60-second
window (`aeam/security/rate_limiter.py`). Shared across instances because the
Redis domain is shared, so horizontal scale does not multiply the limit.

---

## 4. Audit trail

Every authenticated request produces an audit record naming the acting
principal, the endpoint, and the outcome (`aeam/security/audit_logger.py`).

- **Durable sink:** the `audit_logs` table — survives instance recycle and is
  visible across instances (ARCH-7).
- **File sink:** best-effort convenience, `AUDIT_LOG_FILE`. Ephemeral on
  Cloud Run by design; it is never the system of record.
- **Never blocking:** an audit write failure is logged and swallowed, never
  propagated into the request (SEC-6).
- **Queryable:** `GET /api/v1/audit` (`aeam/api/audit.py`) filters by
  principal and time window, mapped to `logs:view` so the `auditor` role
  reaches it by construction.

Configuration changes, dataset activation, knowledge curation, policy
lifecycle transitions and human-review verdicts all carry mandatory
attribution — who, why, when — in their own persisted records, in addition to
the request-level audit row.

---

## 5. Secrets

No secret value appears in code, logs, deployment manifests, or version
control (SEC-5). `deploy/cloudrun.yaml` sources every credential from Google
Secret Manager via `secretKeyRef`; `deploy/env.yaml` documents *where* each
credential comes from and never what it is. `SecretManager`
(`aeam/integrations/secret_manager.py`) resolves environment variables first,
then Settings, then the managed store.

`JWTAuth.__repr__` and `oidc.callback` logging are written so key material,
client secrets and authorization codes cannot reach a log line — asserted by
`test_sso_config_never_exposes_the_client_secret`.

---

## 6. Supply chain

The `supply-chain` job in `.github/workflows/deploy.yml` blocks the pipeline on:

1. **Dependency audit** — `pip-audit --strict` against `requirements.txt`.
2. **Gate self-test** — `pip-audit` against
   `deploy/security/vulnerable-fixture-requirements.txt`, a pin with a
   published advisory (PyYAML 5.3.1 / PYSEC-2021-142). The job **fails if
   that scan passes**, and also **fails if the scan exits non-zero without
   naming an advisory** — pip-audit exits non-zero on an unresolvable
   requirements file too, and a fixture that "fails" for that reason would
   prove the gate works while proving nothing. A scanner never observed
   catching a real CVE is indistinguishable from one that is silently
   misconfigured.
3. **SBOM** — CycloneDX JSON, retained 90 days as a build artifact.
4. **Image scan** — Trivy against the built image, blocking on
   `CRITICAL,HIGH` with `exit-code: 1`.

No step uses `|| true` or `continue-on-error`; the contract is asserted by
`aeam/tests/test_phase_e13_certification.py`.

---

## 7. Deliberate non-goals

State these plainly in a vendor review rather than letting them be discovered:

- **AEAM is not an identity provider.** No credential issuance, storage, reset,
  or MFA. The dev-token endpoint is development-only and 404s elsewhere.
- **AEAM is single-tenant by declaration.** No tenant discriminator exists in
  any table, Qdrant collection, or Redis key namespace. Isolation between
  organizations is achieved by deploying separately. See
  [`docs/ENTERPRISE_CERTIFICATION.md`](ENTERPRISE_CERTIFICATION.md).
- **No field-level encryption at rest.** Encryption at rest is the storage
  layer's job (managed PostgreSQL, object-store SSE, Qdrant volume
  encryption) and is configured out of band.
- **No automated PII detection or redaction.** The declared posture is
  `PII_POSTURE=not-expected`; free-text fields may incidentally contain
  personal data, which is why the retention posture applies to them.
- **Authorization is role-based, not attribute- or record-based.** A role that
  can view incidents can view every incident.

---

## 8. Environment posture matrix

| | development | staging | production |
|---|---|---|---|
| JWT verification | bypassed | enforced | enforced |
| RBAC | bypassed | enforced | enforced |
| Rate limiting | bypassed | enforced | enforced |
| Placeholder key permitted | yes (warns) | no (aborts) | no (aborts) |
| `/api/v1/auth/dev-token` | available | 404 | 404 |
| `/api/v1/debug/retrieval` | available | available | 404 |
| Autonomous loop | per `ENABLE_MONITOR_AGENT` | per flag | `true` (`deploy/cloudrun.yaml`) |
| Human approval enforced | per `HUMAN_APPROVAL_ENFORCED` | per flag | `true` |

`ENVIRONMENT` has no default and must be one of `development`, `staging`,
`production`, `test` (`aeam/config/settings.py`). **A deployed instance must
never run with `ENVIRONMENT=development`** — that single value disables the
entire security perimeter (SEC-8).

---

## 9. Reporting a vulnerability

Route through the deployment owner's normal security-incident process. AEAM
carries no independent disclosure channel; it is deployed software, not a
hosted service.
