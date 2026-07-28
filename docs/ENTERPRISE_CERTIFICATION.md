# AEAM Enterprise Certification — Evidence Pack

**Phase E13 capstone.** The 2026-07 Engineering Audit closed with one question:
*how close is AEAM to production-grade?* This document answers it with evidence
rather than assertion. Every item of Constitution Article XVI appears below
**verbatim**, checked, with what makes it true and where to verify it.

The checklist is not maintained by hand. `aeam/tests/test_phase_e13_certification.py`
parses Article XVI out of `CONSTITUTION.md`, asserts every item appears here,
asserts none is left unchecked, and asserts every evidence path resolves to a
file that exists. An item added to the Constitution fails the build until this
pack answers it; an evidence link that rots fails the build immediately. A
certification document that cannot go stale silently is the only kind worth
having.

| | |
|---|---|
| Certified as of | 2026-07-28 |
| Scope | Phases E1–E13 |
| Constitution | Article XVI, all 19 items |
| Verified by | `aeam/tests/test_phase_e13_certification.py` (every CI run) |

**Companion documents:** [`docs/SECURITY_POSTURE.md`](SECURITY_POSTURE.md) ·
[`docs/DISASTER_RECOVERY.md`](DISASTER_RECOVERY.md) ·
[`docs/PERFORMANCE_BASELINES.md`](PERFORMANCE_BASELINES.md) ·
[`docs/persistence_and_retention.md`](persistence_and_retention.md) ·
[`docs/human_in_the_loop.md`](human_in_the_loop.md) ·
[`docs/ai_governance.md`](ai_governance.md) ·
[`docs/autonomous_operations.md`](autonomous_operations.md) ·
[`docs/KNOWLEDGE_GOVERNANCE.md`](KNOWLEDGE_GOVERNANCE.md) ·
[`docs/ALERT_CATALOG.md`](ALERT_CATALOG.md) ·
[`docs/SRE_RUNBOOK.md`](SRE_RUNBOOK.md) ·
[`docs/retrieval_debugging.md`](retrieval_debugging.md)

---

## Article XVI — Enterprise Readiness Checklist

### Identity & access

- [x] Real key material end-to-end: token issuance, verification, rotation — no placeholder credentials anywhere (SEC-4).

**How.** Key material is resolved through `SecretManager` from `JWT_PUBLIC_KEY`
(PEM literal) or `JWT_PUBLIC_KEY_PATH`, or — under federation — from the IdP's
JWKS document with the key selected by the token's `kid`. Rotation is the IdP's
(or the key owner's): AEAM re-fetches JWKS every 5 minutes, so a rotated signing
key is picked up without a restart. Startup **aborts** in any non-development
environment when no real key material is configured, and aborts in *every*
environment when federation is switched on but incompletely configured.
Development still falls back to the placeholder and logs a warning naming it,
every time.

**Evidence.** `aeam/security/jwt_auth.py`, `aeam/main.py`,
`aeam/config/settings.py`, `aeam/tests/test_phase_e3_security.py`,
`aeam/tests/test_phase_e13_certification.py`
(`test_incomplete_oidc_configuration_aborts_startup`,
`test_oidc_fails_closed_even_in_development`).

- [x] RBAC coverage parity with the entire API surface (SEC-3).

**How.** Every route resolves to a `(resource, action)` pair via
`_ENDPOINT_RBAC_MAP`, matched longest-prefix-first, with configuration-writing
surfaces in the strictest `admin:config` tier. The six routers the 2026-07 audit
found authenticating without authorizing were mapped in E3; E9, E11, E12 and E13
each added their routes to the map in the same change that added the route
(SEC-3). The E13 addition, `/api/v1/system/compliance`, is covered by the
existing `/api/v1/system` prefix and asserted to be.

**Evidence.** `aeam/middleware/security_middleware.py`, `aeam/security/rbac.py`,
`aeam/tests/test_phase_e3_security.py` (per-router × per-role matrix),
`aeam/tests/test_phase_e13_certification.py`
(`test_system_compliance_is_covered_by_the_rbac_map`).

- [x] Frontend authentication and role-aware UI; no unauthenticated console.

**How.** The console holds a bearer token in the session layer, decodes its
claims for role-aware navigation, attaches it to every same-origin call, and
treats a 401 as an honest logout rather than letting a page render empty data.
Route-level permission checks mirror the sidebar's visibility rules, so reaching
a hidden URL directly returns a 403 page. No route renders without a token
outside a development posture.

**Evidence.** `frontend/src/layout/AuthProvider.jsx`, `frontend/src/App.jsx`,
`frontend/src/lib/rbac.js`, `frontend/src/pages/Login.jsx`,
`frontend/src/layout/__tests__/AuthProvider.test.jsx`.

- [x] Enterprise SSO/OIDC integration path exercised.

**How.** OIDC authorization-code + PKCE, landing on the E10 session layer and
the E3 verification path exactly as the roadmap specified — no second session
layer, no second verifier. The console reads public parameters from
`GET /api/v1/auth/sso/config`, redirects to the IdP with an S256 challenge and a
CSRF `state`, and the code is exchanged server-side at
`POST /api/v1/auth/sso/callback` so a confidential-client secret never reaches
the browser. The returned token is then verified like any other: signature
against JWKS, issuer, audience, expiry, algorithm allow-list.

**Exercised.** Against a test IdP with real RSA keys and a real JWKS document:
a valid token verifies; tokens signed with an unpublished key, or carrying the
wrong issuer, wrong audience, an unknown `kid`, an expired `exp`, or an
unlisted algorithm are all rejected; an IdP outage fails closed as a 401.

**Evidence.** `aeam/security/jwt_auth.py`, `aeam/api/auth.py`, `aeam/main.py`,
`frontend/src/pages/SsoCallback.jsx`, `frontend/src/layout/AuthProvider.jsx`,
`aeam/tests/test_phase_e13_certification.py`,
`frontend/src/layout/__tests__/AuthProviderSso.test.jsx`,
`docs/SECURITY_POSTURE.md`.

### Integrity & concurrency

- [x] Per-incident investigation state provably isolated under concurrent events (ARCH-8), with tests (TEST-6).

**How.** All per-incident state lives in an `IncidentContext` created per
`handle_event()` call; the Orchestrator holds no per-incident instance
attributes. Proven under 4, 8, 16 and 20 concurrent events: distinct incident
ids, no finding belonging to the wrong incident, no cross-contamination between
the monitor thread and the trigger thread.

**Evidence.** `aeam/agents/orchestrator/incident_context.py`,
`aeam/agents/orchestrator/orchestrator.py`,
`aeam/tests/test_phase_e2_concurrency.py`.

- [x] Placeholder analysis quarantined from organizational memory and operator-facing conclusions (ENG-5).

**How.** `_run_kpi_investigation_placeholder` tags its output
`root_cause_source="placeholder"` and marks every evidence entry
`placeholder: True`. `finalize_incident()` quarantines those incidents from
Enterprise Memory, and the console renders a "Placeholder" badge rather than
presenting the output as a conclusion.

**Evidence.** `aeam/agents/orchestrator/orchestrator.py`,
`aeam/memory/enterprise_memory.py`, `frontend/src/components/EvidencePanel.jsx`,
`aeam/tests/test_phase_e1_truth_hygiene.py`.

- [x] Approval semantics enforced or explicitly documented as advisory (AGENT-5).

**How.** Enforced, not advisory. When `HUMAN_APPROVAL_ENFORCED` is true (the
default, and the production value), gated runbook steps are withheld until the
incident's ordered approval chain is satisfied by authorized reviewers. Verdicts
are persisted with attribution. Slack/Jira/email notification always dispatches
— informing humans is never gated. Setting the flag false is the documented
rollback to pre-E9 advisory behaviour and is deliberately absent from the
runtime admin config surface: an approval gate a single API call can switch off
is not a governance control.

**Evidence.** `aeam/governance/human_review.py`, `aeam/api/review.py`,
`migrations/versions/0004_human_review_tables.py`,
`aeam/tests/test_phase_e9_human_review.py`, `docs/human_in_the_loop.md`.

### State & durability

- [x] All durable state (blobs, models, audit, configuration) survives instance recycle and is visible across instances (ARCH-7).

**How.** Blobs live in an S3-compatible object store in production
(`BLOB_STORAGE_BACKEND=s3`); forecast model artifacts persist to a durable
mount; the audit trail's system of record is the `audit_logs` table, not a
file; configuration comes from the deployment manifest and Secret Manager. The
one place durability is *not* achievable on ephemeral compute — admin-API `.env`
writes — is disclosed rather than faked, via `CONFIG_PERSISTENCE_MODE` surfaced
in the admin API response.

**Evidence.** `aeam/storage/blob_store.py`, `aeam/storage/s3_blob_store.py`,
`aeam/storage/factory.py`, `aeam/security/audit_logger.py`,
`deploy/cloudrun.yaml`, `aeam/tests/test_phase_e4_storage.py`.

- [x] Schema evolution mechanism in place; no hand-applied production DDL.

**How.** Alembic, with `migrations/versions/` as the single schema truth. CI
runs `alembic upgrade head → downgrade base → upgrade head` against
**PostgreSQL** before the unit suite, so a non-reversible or dialect-broken
migration fails fast and the SQLite/PG drift risk is closed. The pre-E5
hand-applied-DDL practice is retired.

**Evidence.** `migrations/env.py`, `migrations/versions/0001_baseline_schema.py`,
`migrations/versions/0005_knowledge_governance.py`,
`.github/workflows/deploy.yml`, `aeam/tests/test_phase_e5_migrations.py`.

- [x] Retention and backup/restore posture declared for every store (MEM-6).

**How.** Per-store posture, RPO/RTO targets and ownership are declared in
`docs/DISASTER_RECOVERY.md` and `docs/persistence_and_retention.md`. Redis is
declared **not backed up**, with the reason stated (restoring a stale dedup
window would suppress real incidents). Qdrant is declared **derived**, with the
rebuild path. The declaration is not only prose: `scripts/dr_drill.py` records
it in the evidence record of every drill.

**Evidence.** `docs/DISASTER_RECOVERY.md`, `docs/persistence_and_retention.md`,
`scripts/dr_drill.py`, `aeam/tests/test_phase_e13_certification.py`
(`test_drill_records_a_documented_posture_for_every_store`).

### Operations

- [x] Production environment actually runs the autonomous loop, with debug surfaces off (SEC-8).

**How.** `ENABLE_MONITOR_AGENT=true` in the production manifest is the sole,
honest gate for the autonomous polling loop — there is no environment backdoor
in either direction. Debug surfaces are off: `/api/v1/debug/retrieval` returns
404 in production and `/api/v1/auth/dev-token` returns 404 outside development.

**Evidence.** `deploy/cloudrun.yaml`, `aeam/agents/monitor/monitor_agent.py`,
`aeam/api/retrieval_debug.py`, `aeam/api/auth.py`,
`aeam/tests/test_phase_e7_autonomous_ops.py`, `docs/autonomous_operations.md`.

- [x] CI gates on tests (TEST-2); dependency and image scanning in the pipeline.

**How.** The `test` job is a real gate (no `|| true`) with Postgres and Redis
service containers, preceded by the migration up/down/up check. Phase E13 adds
a blocking `supply-chain` job — `pip-audit` on `requirements.txt`, a CycloneDX
SBOM retained as a build artifact, and a Trivy image scan blocking on
CRITICAL/HIGH — plus a blocking `performance` job. The dependency gate proves
itself: it also scans a fixture pinned to a package with a published advisory,
and **fails if that scan comes back clean** — a scanner never observed catching
a real CVE is indistinguishable from one that is silently misconfigured. It
further fails if the fixture scan exits non-zero *without naming an advisory*,
because `pip-audit` also exits non-zero on an unresolvable requirements file,
and a fixture failing for that reason would prove the gate works while proving
nothing. (That is not hypothetical: the first draft of this fixture pinned a
conflicting `requests`/`urllib3` pair and did exactly that.) `build` requires
all three jobs.

**Evidence.** `.github/workflows/deploy.yml`,
`deploy/security/vulnerable-fixture-requirements.txt`,
`aeam/tests/test_phase_e13_certification.py`, `docs/SECURITY_POSTURE.md`.

- [x] Metrics scraped, logs aggregated, failures alertable (OBS-6).

**How.** One metrics pipeline (Prometheus exposition at `/metrics`, scrape
config shipped), structured logs correlated by incident id, OTLP traces spanning
an investigation's stages, and nine shipped alert rules that fire on real
failures and stay quiet on health.

**Evidence.** `aeam/monitoring/metrics.py`, `aeam/monitoring/tracing.py`,
`deploy/prometheus.yml`, `deploy/alerts.yml`, `docs/ALERT_CATALOG.md`,
`aeam/tests/test_phase_e11_observability.py`.

- [x] Unbounded endpoints paginated; UI usable at a year of incident volume.

**How.** Paged, filtered, bounded reads with `X-Total-Count` for client
paging, while the parameter-less call keeps its exact pre-E6 shape (COMPAT-2).
Both paths are now *budgeted*: at 5,000 incidents — a year at ~14/day — a paged
request is budgeted at ≤ 2 s (observed 0.012 s) and the unpaged compatibility
path at ≤ 10 s (observed 0.258 s), so the backward-compatible route cannot
silently become unusable at volume.

**Evidence.** `aeam/api/incidents.py`, `migrations/versions/0002_hot_path_indexes.py`,
`aeam/tests/test_phase_e6_scale.py`, `aeam/tests/test_phase_e13_performance.py`,
`aeam/tests/fixtures/performance_budgets.json`, `docs/PERFORMANCE_BASELINES.md`.

- [x] LLM usage has cost visibility and limits (AI-6); guardrails wired (AI-7).

**How.** Token usage and per-1k-token cost rates produce `llm_cost_usd_total`
(honest zero until rates are configured, never an invented number); a per-call
timeout bounds all six call sites through the shared client; guardrails wrap
prompts and responses. The provider-truth check aborts startup rather than
silently falling back to an unimplemented provider.

**Evidence.** `aeam/services/llm_service.py`, `aeam/security/llm_guardrails.py`,
`aeam/monitoring/metrics.py`, `aeam/tests/test_phase_e8_ai_governance.py`,
`docs/ai_governance.md`.

- [x] Background workers supervised: a dead monitor or ingestion thread is detected, not discovered.

**How.** `MonitorAgent` and `IngestionWorker` record a heartbeat before every
cycle — proving thread liveness, a strictly stronger supervision signal than
"the last cycle succeeded". `GET /health` reports each worker's heartbeat age
and flips the overall status to `degraded` past `HEARTBEAT_STALE_SECONDS`,
which the liveness probe then acts on. A worker that never started is reported
as not-started, never as healthy.

**Evidence.** `aeam/monitoring/metrics.py` (`HeartbeatTracker`), `aeam/main.py`
(`build_health_payload`), `aeam/ingestion/worker.py`,
`aeam/tests/test_phase_e7_autonomous_ops.py`,
`aeam/tests/test_phase_e13_performance.py`
(`test_autonomous_loop_cycle_cadence_and_heartbeat_meet_budget`).

### Governance

- [x] Durable, attributable audit trail for operator and configuration actions (SEC-6, SEC-7).

**How.** Every authenticated request writes an audit row naming the acting
principal, endpoint and outcome, to the durable `audit_logs` table (the file
sink is best-effort convenience). Audit writes never block a request but are
observable when they fail. Configuration changes, dataset activation, knowledge
curation, policy lifecycle transitions and review verdicts each additionally
persist mandatory who/why/when attribution. `GET /api/v1/audit` queries the
trail by principal and time window, reachable by the `auditor` role.

**Evidence.** `aeam/security/audit_logger.py`, `aeam/api/audit.py`,
`aeam/api/administration.py`, `aeam/api/knowledge.py`,
`aeam/tests/test_phase_e3_security.py`,
`aeam/tests/test_phase_e11_observability.py`.

- [x] Data classification/PII posture stated for incidents, documents, and memory.

**Stated posture.** Classification `internal`; PII posture `not-expected`.

AEAM is fed operational metrics, runbooks and policy documents. No field in any
store is designated to hold personal data. Free-text fields — evidence entries,
reviewer reasons, uploaded document bodies — *may* incidentally contain it,
which is precisely why the retention posture in
`docs/persistence_and_retention.md` applies to them and why the audit trail
records who read what. A deployment that will hold personal data declares
`PII_POSTURE=contains-pii` and `DATA_CLASSIFICATION` accordingly, and inherits
the handling obligations that follow. AEAM performs no automated PII detection
or redaction, and does not claim to.

Scope of the declaration: incidents, documents, Enterprise Memory, audit logs.

**Evidence.** `aeam/config/settings.py`, `aeam/api/system.py`
(`GET /api/v1/system/compliance`), `docs/persistence_and_retention.md`,
`aeam/tests/test_phase_e13_certification.py`
(`test_compliance_endpoint_states_the_declared_postures`).

- [x] Multi-tenancy position stated explicitly (supported, or single-tenant by declaration).

**Stated position: single-tenant by declaration.**

One deployment serves one organization. AEAM implements **no** tenant
partitioning: there is no tenant discriminator in any table, in any Qdrant
collection, or in any Redis key namespace, and no request carries a tenant
context. Isolation between organizations is achieved by deploying separately —
separate database, object store, vector store and cache.

This is an honest answer, not a deferred one (PHIL-1). Declaring multi-tenancy
would require a discriminator in every store and a tenant filter on every read;
claiming it without those would be the platform asserting an isolation
guarantee it cannot keep. `TENANCY_MODEL` is validated against exactly two
values so a deployment cannot invent a third, and the declared position is
served by the platform itself rather than living only in this document.

**Evidence.** `aeam/config/settings.py` (`TENANCY_MODEL`, validated),
`aeam/api/system.py` (`GET /api/v1/system/compliance`),
`aeam/tests/test_phase_e13_certification.py`
(`test_tenancy_model_rejects_an_undeclarable_value`), `docs/SECURITY_POSTURE.md`.

---

## Audit re-score

The 2026-07 audit's thirteen categories, re-scored after E13. The **floor** is
the projected end-state scorecard in `ROADMAP.md`; it is parsed from that file
by the certification test, so the target cannot drift from the contract it came
from, and a category landing below its own floor fails the build.

| Category | 2026-07 audit | Post-E13 | Floor | Carried by |
|---|---|---|---|---|
| Architecture | 78 | 82 | 80 | Preserved throughout (ARCH-1); no phase reopened it |
| Separation of concerns | 85 | 87 | 85 | Untouched by design; new surfaces used existing seams |
| Modularity | 82 | 86 | 85 | Every phase extended through existing seams |
| Scalability | 35 | 72 | 70 | E2 (concurrency), E4 (durable state), E5 (indexes), E6 (pagination), E13 (budgets) |
| Reliability | 55 | 82 | 80 | E2, E7 (supervision), E13 (restore drill) |
| Fault tolerance | 60 | 78 | 75 | E4, E7, E8 (timeouts, provider truth) |
| Observability | 55 | 82 | 80 | E7, E11 (metrics, traces, alerts, audit query) |
| Explainability | 90 | 92 | 90 | Protected by every phase's honesty criteria |
| Security | 30 | 86 | 85 | E3, E8, E9, E10, E13 (SSO, supply chain) |
| Testability | 65 | 87 | 85 | E1, E10 (frontend baseline), E12 (retrieval eval), E13 |
| Maintainability | 72 | 86 | 85 | E1 (dead code, truth hygiene), E5 (migrations) |
| Production readiness | 45 | 86 | 85 | E4, E7, E11, E13 (DR rehearsal, budgets, scanning) |
| Enterprise readiness | 40 | 87 | 85 | E3, E9, E10, E12, E13 (federation, tenancy, evidence) |

No category is below its floor and none regressed. The three that moved most —
Security 30→86, Enterprise readiness 40→87, Scalability 35→72 — are the three
the audit named as gates.

**What the scores do not claim.** They measure the hardening axes the 2026-07
audit defined. They say nothing about capability maturity — detection accuracy,
adaptive learning, correlation depth — which the F-series addresses and which is
re-scored separately against its own axes.

---

## Standing limits

Declared here rather than left to be discovered in a review:

1. **Single-tenant.** No tenant partitioning exists. Separate organizations
   require separate deployments.
2. **Single-node modular monolith.** Horizontal scale to `maxScale=5` is safe
   because dedup and idempotency are Redis-shared and the Orchestrator is
   reentrant; cross-instance contention above that is unmeasured.
3. **Synchronous investigation.** `POST /api/v1/trigger` returns only after
   finalization — a deliberate, documented trade-off (MOD-6), not an oversight.
4. **Authorization is role-based**, not record-level. A role that can view
   incidents can view every incident.
5. **No field-level encryption at rest.** Delegated to the storage layer.
6. **No automated PII detection.** The declared posture is `not-expected`;
   incidental personal data in free-text fields is governed by retention and
   audit, not by redaction.
7. **Redis is not backed up, by decision.** Recovery is an empty instance.
8. **Qdrant is derived state.** Total loss is recoverable by re-ingesting and
   re-embedding with the same model; a different model is a TECH-6 event.
9. **One LLM provider is implemented** (`groq`). Configuring another aborts
   startup rather than silently degrading.

---

## Verdict

Every item of Article XVI is true, with evidence, and the evidence is
re-verified on every build. Against the checklist the Constitution defines,
**AEAM meets its enterprise-readiness bar as of 2026-07-28** — for a
single-tenant deployment, operated per the runbooks above, with the standing
limits stated plainly rather than papered over.

The F-series builds new capability on this certified baseline. It does not
reopen the hardening work, and each cluster is re-certified against this pack
as it completes.
