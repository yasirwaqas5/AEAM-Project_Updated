# AEAM Canonical Evolution Roadmap — The E-Series

**Status:** Canonical development plan. Subordinate only to [CONSTITUTION.md](CONSTITUTION.md).
**Naming:** Continues the project's phase lineage (A: blueprint/shell → B: data layer → C: intelligence → D: explainability/config → **E: enterprise hardening**).
**End state:** Every item of Constitution Article XVI (Enterprise Readiness Checklist) checked with evidence. At that point AEAM may call itself a production-grade enterprise autonomous intelligence platform.

## Governance

1. **The Constitution rules this roadmap.** Every phase lists the laws it satisfies; a phase implementation that cannot cite its laws is not reviewable (Article XV).
2. **Phase gate = Definition of Done (Article XV), applied to the whole phase.** A phase ships only when its acceptance criteria hold and the full regression ledger is green in gating CI.
3. **Every phase is independently valuable and shippable.** No phase leaves the platform worse, less honest, or half-migrated. Rollback is defined per phase.
4. **No timelines.** Complexity is rated S / M / L / XL (scope and risk, not duration). Order is dependency-driven, not calendar-driven.
5. **Audit re-scoring** occurs after E7 (operational midpoint) and after E13 (certification), using the same 13 categories as the 2026-07 Engineering Audit.

## Explicit non-goals (constitutionally excluded)

- No microservice split, no rewrite of the modular monolith (ARCH-1; audit verdict: zero redesigns).
- No orchestration/RAG frameworks (TECH-3).
- No second configuration mechanism, second metrics pipeline, or second retrieval implementation (ENG-6, OBS-1, RAG-1).
- No new infrastructure service unless it retires a constitutional violation (TECH-5).

## Dependency map

```
E1 Truth & Hygiene ──┬─▶ E2 Concurrency ──▶ E7 Autonomous Ops ──▶ E11 Observability
                     ├─▶ E3 Identity ─────┬─▶ E9 Human-in-the-Loop
                     │                    ├─▶ E10 Enterprise Console
                     │                    └─▶ E11 Observability
                     ├─▶ E4 Durable State (needs E3 audit sink)
                     ├─▶ E5 Migrations ───┬─▶ E6 Scale Contracts ──▶ E10
                     │                    ├─▶ E9
                     │                    └─▶ E12 Knowledge Governance
                     └─▶ E8 AI Governance
E13 Certification ◀── all of the above (capstone)
```

---

## Phase E1 — Truth & Hygiene Baseline

- **Goal:** Every signal the platform emits about itself — CI results, organizational memory, docstrings, health counts — becomes true.
- **Why this phase exists:** The audit found the platform's honesty contract violated *by its own tooling*: CI cannot fail (`|| true`), placeholder "Simulated root cause" analysis is persisted into Enterprise Memory as organizational knowledge, a metric-history query returns the oldest N while promising the newest N, and dead machinery (the never-drained priority queue, the commented-out scheduler) misleads readers. These are cheap, risk-free fixes that everything later depends on: no subsequent phase can trust its green build until this one ships.
- **Business value:** Trustworthy regression signal for all future work; an organizational memory an auditor can believe; removal of the "demo residue" that undermines enterprise credibility.
- **Technical objective:** CI gates on test results. Placeholder-derived findings carry an explicit placeholder marker end-to-end, are excluded from `EnterpriseMemoryEngine.remember_incident`, and are visibly labeled in the UI. Metric-history windowing honors its documented most-recent-N contract. Debug `print` statements leave the startup path. Root-level scratch files/logs leave version control. The unconsumed priority queue and the scheduler stub each receive an explicit ENG-8 disposition (consumed, or removed with the design note updated). Stale docstrings (`logs.py` mock claim, static `agents_active` count) are corrected.
- **Constitution laws satisfied:** TEST-2, ENG-5, ENG-8, MOD-4, DOC-2, DOC-3, OBS-5, CODE-7, PHIL-1.
- **Audit findings addressed:** CI `|| true`; Enterprise Memory poisoning; oldest-N history defect; vestigial queue; debug prints; repo-root clutter; docstring drift; hardcoded agent count.
- **Existing modules reused:** Everything — this phase adds no capability; it corrects contracts in place.
- **New modules:** None.
- **Files expected to change:** `.github/workflows/deploy.yml`, `aeam/agents/orchestrator/orchestrator.py` (memory quarantine + placeholder marker), `aeam/integrations/database.py` (history window), `aeam/api/logs.py`, `aeam/api/system.py`, `aeam/main.py` (prints; queue/scheduler disposition), repo root cleanup, `CLAUDE.md`, `knowledge.md`.
- **Dependencies:** None — this is phase one by design.
- **Acceptance criteria:** A deliberately red test fails the pipeline. No incident finalized after this phase writes placeholder-derived root causes into Enterprise Memory, and placeholder findings are machine-identifiable. History queries return newest-N with a regression test proving it. `git ls-files` shows no scratch artifacts at root. No `print()` in production paths.
- **Testing strategy:** New regression tests for the quarantine and windowing fixes; a meta-test (or pipeline check) that CI gating works; full phase ledger green under the now-gating CI.
- **Documentation updates:** Retire/update the affected gotchas in `CLAUDE.md`/`knowledge.md`; record ENG-8 dispositions where the dead constructs lived.
- **Rollback strategy:** Pure revert — no schema or persisted-data changes are involved.
- **Future extensibility:** Every later phase inherits a CI that can actually reject it, and a memory store worth building governance on (E12).
- **Estimated complexity:** S.
- **Enterprise maturity gained:** Testing 60→~75; the first standing constitutional violations retired; Article XVI "CI gates on tests" closed.

---

## Phase E2 — Concurrent Investigation Integrity

- **Goal:** Any number of simultaneous events produce complete, mutually uncorrupted investigations.
- **Why this phase exists:** Audit gate #2 — the platform's most serious latent fault. The Orchestrator holds per-incident state (`_active_event`, one shared `ShortTermMemory`, one shared FSM) as instance attributes; a Monitor-thread event and an HTTP trigger arriving together interleave state. Autonomy (E7), scale (E6), and approval workflows (E9) are all unsafe until this is fixed.
- **Business value:** The platform cannot corrupt its core deliverable — the incident record — under real load. This is the difference between a demo and a system of record.
- **Technical objective:** Per-incident isolation of working state: each `handle_event` operates on its own investigation context (fresh `ShortTermMemory` and `IncidentStateMachine` per incident — precisely the usage the FSM's own docstring already recommends: "in production, prefer creating a fresh instance per incident"). The Orchestrator remains the single coordinator class; only state *placement* moves. Synchronous EventBus semantics are unchanged and re-documented.
- **Constitution laws satisfied:** ARCH-8, TEST-6, MOD-1 (a justified edit: composition cannot fix internal state), COMPAT-1/2 (no persisted-shape or call-site changes).
- **Audit findings addressed:** Orchestrator non-reentrancy (Orchestration 58); absence of concurrency tests.
- **Existing modules reused:** `ShortTermMemory` and `IncidentStateMachine` unchanged as classes; DecisionEngine, EvaluationEngine, every C/D engine, notifications, runbooks — all untouched.
- **New modules:** At most a small per-incident context holder inside the orchestrator package; no new subsystem.
- **Files expected to change:** `aeam/agents/orchestrator/orchestrator.py`, `aeam/main.py` (STM/FSM no longer wired as singletons), new concurrency test module.
- **Dependencies:** E1 (gating CI).
- **Acceptance criteria:** N concurrent triggers yield N finalized incidents whose findings each reference only their own event; interleaved Monitor+trigger runs never cross-contaminate; the entire existing phase ledger passes unmodified (proof of behavioral identity for the single-event case).
- **Testing strategy:** A TEST-6 suite: threaded simultaneous triggers, monitor-vs-trigger races, assertion that every findings entry belongs to its incident; soak run at modest concurrency.
- **Documentation updates:** Orchestrator module contract updated (single-event assumption deleted); `knowledge.md` architecture note.
- **Rollback strategy:** Revert — persisted data shapes are untouched, so rollback has no data consequences.
- **Future extensibility:** Enables E7 (autonomy on in production), multi-instance operation (with E4), and any future queued/async execution without another state overhaul.
- **Estimated complexity:** M.
- **Enterprise maturity gained:** Reliability 55→~70; audit gate #2 closed.

---

## Phase E3 — Identity & Access Enforcement

- **Goal:** Working authentication and authorization end-to-end on the backend, with a durable, attributable audit trail.
- **Why this phase exists:** Audit gate #1, Security 30/100: the JWT verifier is built on `"dummy-public-key"`, no issuance path exists, six routers authenticate without authorizing, and the audit log dies with the instance in `/tmp`. The security *framework* is correct (middleware ordering, RS256 with issuer/audience, RBAC matrix, Redis rate limiting); this phase makes it real without redesigning it.
- **Business value:** AEAM becomes deployable inside an actual organization: access is deniable, attributable, and auditable — the first question every enterprise security review asks.
- **Technical objective:** Key material loaded from configuration/SecretManager (static PEM or JWKS source — capabilities PyJWT already has); startup **fails closed and loudly** in non-development environments if key material is placeholder/absent (SEC-4). RBAC path map reaches parity with the full router surface, with configuration-writing endpoints (`/admin/config`, activation, purge/delete) in the strictest tier (SEC-3, SEC-7). Posture: AEAM *validates* enterprise-issued tokens; it never becomes an identity provider (a dev-only token-minting utility is documented as dev-only). Audit records gain a durable sink — an additive `audit_logs` table following the existing `action_logs` precedent — alongside the file sink. CORS origins become configuration.
- **Constitution laws satisfied:** SEC-1 through SEC-7, ARCH-7 (audit durability), COMPAT-4 (additive surface only).
- **Audit findings addressed:** Dummy key; RBAC drift across six routers; unauthorized config writes; ephemeral `/tmp` audit; hardcoded CORS.
- **Existing modules reused:** `JWTAuth`, `RBAC`, `RateLimiter`, `AuditLogger`, `SecurityMiddleware` — all structurally unchanged; `SecretManager` finally earns its keep; `DatabaseClient.insert` for the audit sink.
- **New modules:** None (one additive table, new RBAC map entries).
- **Files expected to change:** `aeam/main.py` / `create_app` (key loading, fail-closed), `aeam/middleware/security_middleware.py` (map parity), `aeam/security/audit_logger.py` (durable sink), `aeam/config/settings.py` (key/CORS settings, engine-owned defaults per ENG-6), security docs.
- **Dependencies:** E1. (The audit table can use the existing idempotent-DDL convention; it is re-baselined under E5.)
- **Acceptance criteria:** In a staging posture: valid token with the right role passes; wrong role receives 403 on *every* mapped router including admin; placeholder key aborts startup with an explicit message; audit entries survive instance restart and name the acting principal; development posture behaves exactly as today.
- **Testing strategy:** Extend the Phase-8 security suite into a per-router × per-role authorization matrix; fail-closed startup tests; durable-audit persistence tests; regression: dev bypass unchanged.
- **Documentation updates:** Security posture document (token expectations, role matrix, environment behavior); `CLAUDE.md` 401-in-dev gotcha updated.
- **Rollback strategy:** Posture is configuration-driven; reverting to development posture restores current behavior instantly. The audit table is additive and inert if unused.
- **Future extensibility:** The JWKS-capable verification path is the landing zone for full SSO/OIDC in E13; the audit table is the substrate for E11's audit query surface and E9's reviewer attribution.
- **Estimated complexity:** M.
- **Enterprise maturity gained:** Security 30→~60; Article XVI identity items (backend half) closed.

---

## Phase E4 — Durable State & Deployment Alignment

- **Goal:** Every piece of durable state survives instance recycle and is visible across instances; deployment artifacts describe a production that actually works.
- **Why this phase exists:** Audit gate #3: the declared target (Cloud Run, maxScale 5) contradicts three local-disk dependencies — blobs, Prophet models, and (pre-E3) the audit log — plus a hardcoded database password in compose and a "production" compose posture that locks out its own API. ARCH-7 makes state placement a constitutional matter.
- **Business value:** Uploaded organizational knowledge and trained models stop being losable; the platform can be deployed by an ops team from the artifacts alone, without tribal knowledge.
- **Technical objective:** An object-store `BlobStore` implementation behind the **existing abstract class** (the abstraction explicitly anticipated S3/Azure/GCS; callers depend only on the ABC — zero caller changes). Forecast model artifacts get a configurable, durable location (resolving the CWD-relative vs `aeam/models/` ambiguity). The Settings-page write path becomes environment-honest: configuration persistence either survives recycle or the UI discloses per-environment non-persistence (PHIL-1 applied to ops). Deployment artifacts corrected: no hardcoded credentials, coherent production env vars, documented instance-count posture (which components are single-writer).
- **Constitution laws satisfied:** ARCH-7, SEC-5, SEC-8 (partially — full autonomy honesty lands in E7), TECH-1/TECH-5 (the new storage dependency is justified precisely because it retires a constitutional violation), PHIL-5.
- **Audit findings addressed:** Local-disk blobs on ephemeral compute; model-path mismatch; compose `secret` password; config evaporation on recycle; deploy/env.yaml emptiness.
- **Existing modules reused:** `BlobStore` ABC and every caller (ingest API, processors, `DatasetKPISource`, knowledge previews) unchanged; `ForecastAgent` unchanged except path sourcing; D5 admin API unchanged except disclosure fields.
- **New modules:** One storage backend module in `aeam/storage/`.
- **Files expected to change:** `aeam/storage/` (new backend), `aeam/config/settings.py` (backend selection, model dir), `aeam/main.py` (construction switch), `aeam/agents/forecast/forecast_agent.py` (configurable dir), `deploy/cloudrun.yaml`, `docker-compose.yml`, `deploy/env.yaml`, `aeam/api/administration.py` (persistence disclosure).
- **Dependencies:** E1; E3 (audit durability already handled there).
- **Acceptance criteria:** Kill-and-recreate the app instance: uploads, models, audit history, and configuration state are intact (or the config UI honestly displayed its non-persistence beforehand). Two instances read the same blob. No credential literals in any tracked deployment artifact. The blob-backend contract test suite passes identically against local-disk and object backends.
- **Testing strategy:** One contract suite run against both backends (the ABC is the contract); deployment smoke checklist executed against a recycled instance.
- **Documentation updates:** Deployment guide (state placement table per ARCH-7); `CLAUDE.md` model-path gotcha resolved.
- **Rollback strategy:** Backend selection is a setting — flip back to `local` for single-node deployments; no data migration is destructive (content-addressing makes copy-forward safe).
- **Future extensibility:** Azure/other backends are one more ABC implementation; multi-instance scale-out (E6/E7) now has a truthful state substrate.
- **Estimated complexity:** M.
- **Enterprise maturity gained:** Production readiness 45→~60; audit gate #3 closed.

---

## Phase E5 — Persistence Evolution & Data Lifecycle

- **Goal:** The schema can evolve safely, hot queries are indexed, and every store has a declared retention posture.
- **Why this phase exists:** The audit flagged "no migration mechanism" as below enterprise bar, and COMPAT-5 currently *prohibits* destructive change precisely because no safe mechanism exists. Later phases (E9, E12) need additive tables/columns done properly. Meanwhile `incidents`/`metrics` grow unbounded and unindexed.
- **Business value:** Database change management an enterprise DBA can approve; predictable query performance at years of history.
- **Technical objective:** Adopt a migration mechanism (Alembic — TECH-1 justified: it is the native companion of the already-installed SQLAlchemy, replaces prohibited hand-applied DDL, and adds no framework surface; TECH-2 reuse-first satisfied). Baseline the current schema; keep the startup `CREATE IF NOT EXISTS` path as a dev-only convenience, labeled as such. Add indexes for the audited hot paths (incidents by timestamp; metrics by metric+timestamp). Declare retention ownership and posture per table/store (MEM-6) — declaration in this phase; enforcement tooling may follow later phases.
- **Constitution laws satisfied:** COMPAT-5, MEM-6, TECH-1/TECH-2, DOC-2.
- **Audit findings addressed:** No migrations; unindexed hot queries; unbounded `metrics`/`incidents` growth ungoverned; SQLite/PG drift risk (migrations become the single schema truth).
- **Existing modules reused:** `DatabaseClient` runtime behavior unchanged; `enterprise_schema.py` becomes the documented baseline reference.
- **New modules:** Migration directory (infrastructure, not application code).
- **Files expected to change:** `requirements.txt` (+alembic), new `migrations/`, `aeam/integrations/database.py` (dev-only labeling), ops documentation, `CLAUDE.md`.
- **Dependencies:** E1.
- **Acceptance criteria:** A fresh database built purely from migrations is schema-identical to today's startup DDL result; an existing populated database upgrades in place; downgrade of the baseline-adjacent revisions works; indexed query plans verified on a large synthetic dataset; a retention table (store → owner → posture) exists in docs.
- **Testing strategy:** Migration up/down tests in gating CI against PostgreSQL (production dialect, addressing drift); query-plan/latency assertions on synthetic volume.
- **Documentation updates:** Retire the `CLAUDE.md` "no migration tool — apply by hand" gotcha; ops guide for running migrations per environment.
- **Rollback strategy:** `alembic downgrade`; the retained startup DDL is a safety net for dev environments.
- **Future extensibility:** Unblocks every additive table/column in E6, E9, E12 under COMPAT-5.
- **Estimated complexity:** S–M.
- **Enterprise maturity gained:** Article XVI schema-evolution item closed; Maintainability and Production readiness both rise.

---

## Phase E6 — Scale Contracts (API and Console Consumption)

- **Goal:** Bounded payloads and bounded client work at any incident-history size.
- **Why this phase exists:** The audit found three hot paths doing `SELECT *`-of-everything (incidents API, observability, and thereby every console page), a console that aggregates the world in the browser on every poll, and two per-request cost curves that grow forever (C3 re-embedding every policy per incident; D3 parsing all history per dashboard view). Scalability scored 35 — the platform's worst category after security.
- **Business value:** The console stays responsive after years of operation; investigation latency stops growing with policy count; infrastructure cost curves flatten.
- **Technical objective:** Additive pagination/filtering parameters on the unbounded list endpoints, with **defaults that preserve today's exact behavior** (COMPAT-2/4); published response models in OpenAPI so the frontend contract becomes explicit; frontend adoption (paged fetching, table virtualization) for Incidents, pickers, Memory, Analytics; observability retention-limit gains a sane default with disclosed windowing (OBS-2); policy embeddings computed once at extraction time and stored (additive column via E5), so C3 matching stops re-embedding the corpus per incident.
- **Constitution laws satisfied:** COMPAT-2/4/6, OBS-2, ENG-6, PHIL-5, EXPL-5 (all windows disclosed in UI).
- **Audit findings addressed:** No pagination anywhere; API 58; C3 O(policies) embedding cost; D3 O(history) aggregation; console unusable at volume.
- **Existing modules reused:** All routers extended in place; `ui.jsx` helpers unchanged; D3 engine unchanged (only its read window governed); `PolicyRegistry` matching logic unchanged except vector sourcing.
- **New modules:** None.
- **Files expected to change:** `aeam/api/incidents.py`, `logs.py`, `knowledge.py`, `observability.py`; `aeam/intelligence/policy_registry.py` + `policy_extraction`/processor (stored vectors); one migration; frontend `pages/Incidents.jsx`, `Investigation.jsx` (picker), `Memory.jsx`, `Analytics.jsx`, `Dashboard.jsx`, shared fetch helpers.
- **Dependencies:** E5 (migration for the embedding column); E1.
- **Acceptance criteria:** Parameter-less API calls are byte-compatible with today (contract tests). Paged calls are bounded regardless of table size. A 100k-incident synthetic dataset renders every console page within a stated budget. Policy-match latency is flat with respect to policy count.
- **Testing strategy:** Old-vs-new default contract tests; performance assertions on synthetic volume in CI; frontend smoke against large fixtures.
- **Documentation updates:** API contract document (pagination semantics, stability guarantees); lockstep notes where the frontend consumes new fields.
- **Rollback strategy:** Parameters are additive and ignorable; frontend paging behind a flag during rollout.
- **Future extensibility:** Cursor semantics become the substrate for exports, webhooks, and any future external API consumers.
- **Estimated complexity:** M–L.
- **Enterprise maturity gained:** Scalability 35→~55; Article XVI pagination item closed.

---

## Phase E7 — Autonomous Operations Enablement

- **Goal:** The production posture genuinely runs the autonomous loop, supervised and observable, with fresh retrieval indexes.
- **Why this phase exists:** Audit gate #4: shipped production config disables MonitorAgent (the gating condition `ENABLE_MONITOR_AGENT or env != production` turns autonomy *off* precisely where it matters), the monitor and ingestion threads are unsupervised (a dead thread is discovered, not detected), and the BM25 index silently goes stale for post-boot documents, skewing hybrid retrieval. "24/7 autonomous platform" must be true to be sold (SEC-8, PHIL-1).
- **Business value:** The product performs its core function — unattended detection and investigation — in production, and operators can see that it is alive.
- **Technical objective:** Monitor gating made explicit and honest (the flag is authoritative; no environment backdoor in either direction). Heartbeat instrumentation for the monitor and ingestion worker threads, surfaced through the existing `/health` checks and StatusBar chips (OBS-4; no new pipeline). The E1-decided disposition of the scheduler stub is completed. BM25 freshness: the lexical index refreshes when ingestion completes (reusing the existing `from_qdrant` build path via the existing worker's job-completion hook), with the staleness window disclosed until refresh lands (RAG-6). Multi-instance posture of dedup/idempotency re-verified (they are Redis-shared; documented).
- **Constitution laws satisfied:** SEC-8, PHIL-5, OBS-3/4, RAG-6, ENG-8.
- **Audit findings addressed:** Autonomy off in production; unsupervised daemon threads; BM25 staleness; scheduler dead code.
- **Existing modules reused:** `MonitorAgent`, `IngestionWorker`, `BM25Index.from_qdrant`, health endpoint, `HealthProvider`/`StatusBar` — all extended, none reshaped.
- **New modules:** None.
- **Files expected to change:** `aeam/main.py` (gating, supervision wiring), `aeam/monitoring/` (heartbeat metrics), `aeam/agents/rag/hybrid_retrieval.py` (rebuild entry point), `aeam/ingestion/processor.py` (completion hook), frontend `HealthProvider.jsx`/`StatusBar.jsx`, deployment env files.
- **Dependencies:** **E2 (mandatory — autonomy without concurrency isolation is unsafe)**, E1; E4 recommended (durable state before 24/7 operation).
- **Acceptance criteria:** A production-posture stack detects a seeded anomaly end-to-end with zero manual triggers. A deliberately killed monitor thread is visible in `/health` (and the console) within one cycle. A document ingested at runtime is lexically retrievable without a restart. Debug retrieval surface remains off in production.
- **Testing strategy:** End-to-end autonomous test in staging posture; supervision unit tests (thread death → degraded health); BM25 freshness regression; dedup behavior under two concurrent pollers.
- **Documentation updates:** Ops runbook for the autonomous loop; retire the `CLAUDE.md` scheduler gotcha; environment posture matrix (what runs where).
- **Rollback strategy:** All behavior behind existing settings — flags off restores today's posture exactly.
- **Future extensibility:** The heartbeat pattern covers any future background worker; freshness hooks generalize to future index types.
- **Estimated complexity:** M.
- **Enterprise maturity gained:** Reliability ~70→78; audit gate #4 closed; the platform's headline claim becomes true.

---

## Phase E8 — AI Governance

- **Goal:** Every LLM interaction is bounded, guarded, truthful about its provider, and accounted for.
- **Why this phase exists:** The audit found `llm_guardrails` written and tested but wired to nothing (AI-7 standing violation), a default provider (`gemini`) that the service cannot actually run (only `groq` is implemented), no timeouts, and zero token/cost visibility (AI-6). For an enterprise, ungoverned AI spend and unguarded prompts are procurement blockers.
- **Business value:** Predictable AI costs; injection-resistant prompt boundaries; configuration that cannot silently promise an unimplemented vendor.
- **Technical objective:** Wire `sanitize_input` at every point untrusted content enters a prompt (retrieved chunks and document text feeding RAG, policy extraction, query expansion) and `validate_output` before LLM text is persisted or surfaced. Provider registry truth: the supported-provider list equals the implemented list; an unsupported configured provider fails startup loudly (SEC-4 pattern applied to AI config); defaults become coherent. Per-call timeouts and disclosed generation parameters at every call site; token/latency/cost counters published through the existing Prometheus module with OBS-2 semantics. Environment posture for `USE_MOCK_LLM` documented per environment.
- **Constitution laws satisfied:** AI-1 through AI-7, OBS-2, MOD-4, DOC-2.
- **Audit findings addressed:** Guardrails unwired; impossible provider default; no cost accounting; no timeouts; mock-default masking integration issues.
- **Existing modules reused:** `llm_guardrails` (finally consumed), `LLMService` (extended in place), shared `parse_llm_json` untouched, `monitoring/metrics.py`.
- **New modules:** None.
- **Files expected to change:** `aeam/services/llm_service.py`, `aeam/agents/rag/rag_agent.py` (prompt assembly boundary), `aeam/intelligence/policy_extraction.py`, `aeam/agents/rag/query_expansion.py`, `aeam/config/settings.py`, `aeam/monitoring/metrics.py`.
- **Dependencies:** E1.
- **Acceptance criteria:** An injection-pattern corpus passes through every guarded boundary with patterns stripped and incidents logged; configuring an unimplemented provider aborts startup with a clear message; every LLM call site has a timeout and appears in the AI call-site register; cost/token metrics visible at `/metrics` with declared semantics; full RAG regression ledger unchanged.
- **Testing strategy:** Guardrail integration tests at each boundary; provider configuration matrix tests; grounding/validation regression (AI-2 behavior must be byte-identical).
- **Documentation updates:** AI call-site register (AI-6: parameters, budget, failure mode per site); provider support statement.
- **Rollback strategy:** Guardrail wiring behind a setting during staged rollout; provider truth-check is configuration-level.
- **Future extensibility:** A second real provider lands behind the truthful registry without touching call sites; budgets become enforceable limits later without new plumbing.
- **Estimated complexity:** S–M.
- **Enterprise maturity gained:** Retires two standing constitutional violations; Article XVI LLM governance items closed.

---

## Phase E9 — Human-in-the-Loop Enforcement

- **Goal:** Review verdicts persist with attribution, and `human_approval_required` actually gates execution.
- **Why this phase exists:** The audit's sharpest enterprise finding: C7 computes `human_approval_required`, but the runbook executes regardless (safety rests solely on the reversible-action catalog), and the Human Review workspace — the platform's governance showcase — is honest but session-local because no write path exists. AGENT-5 requires approval to be enforced or explicitly advisory; enterprises require the former.
- **Business value:** Genuine human governance over autonomous action — the single capability most demanded of autonomous systems by risk, legal, and compliance functions.
- **Technical objective:** Additive review persistence (verdict tables with reviewer attribution from E3 identity) and endpoints following the existing repository/router patterns. Finalization honors the gate: when the plan requires approval, gated runbook steps are recorded as pending (notifications still dispatch — informing humans is never gated); an authorized approval executes the pending steps through the **unchanged** ActionAgent with full audit; rejection records the decision. The Human Review page switches from session-local to persisted, removing its honest disclaimer because it stops being true. Status vocabulary grows additively if a pending-approval state is warranted (COMPAT-6).
- **Constitution laws satisfied:** AGENT-5, SEC-7, EXPL-5/6, COMPAT-1/5/6, MEM-2 (verdicts are new records, not incident mutations).
- **Audit findings addressed:** Advisory-only approval flag; session-local Human Review (top UX gap); no incident ownership/attribution.
- **Existing modules reused:** ActionAgent (identical `execute` contract), C7's already-computed flag, runbook catalog, notifications, findings model, repository pattern, E3 audit sink.
- **New modules:** One review router + repository module (mirroring existing per-domain patterns).
- **Files expected to change:** New `aeam/api/` router and registry repository, migration (E5), `aeam/agents/orchestrator/orchestrator.py` (finalize gating), `aeam/middleware/security_middleware.py` (RBAC entries — SEC-3 in the same change), `frontend/src/pages/HumanReview.jsx`, `frontend/src/components/ui.jsx` helpers.
- **Dependencies:** E3 (attribution + authz), E5 (migrations), E2 (lifecycle integrity).
- **Acceptance criteria:** An approval-required incident executes zero gated actions until an authorized approval; approval executes exactly the recorded pending steps, idempotently; verdicts survive restart and appear in the audit trail with the acting principal; incidents predating this phase render unchanged (COMPAT-1); non-approval incidents behave exactly as today.
- **Testing strategy:** Gating matrix (required × approved/rejected/ignored × role); end-to-end approve/reject flows; idempotency tests on double-approval; legacy-incident rendering regression.
- **Documentation updates:** Governance workflow guide; AGENT-5 advisory disclaimer replaced by the enforced semantics; runbook doc updated.
- **Rollback strategy:** Enforcement behind a setting (off = today's advisory behavior); tables are additive and inert when disabled.
- **Future extensibility:** SLA timers, reviewer assignment, and escalation chains build on the same tables without schema reshaping.
- **Estimated complexity:** L.
- **Enterprise maturity gained:** Enterprise readiness 40→~60; the flagship governance capability ships.

---

## Phase E10 — Enterprise Console (Authentication, Roles, Production Serving)

- **Goal:** The console works behind real security, adapts to the operator's role, is served like a product, and gains a test baseline.
- **Why this phase exists:** The audit found the frontend architecturally sound (65) but enterprise-blocked: no authentication concept, no role awareness, no production serving story (Vite dev server only), zero tests, and the documented Linux/Docker import-casing hazard live in the tree.
- **Business value:** The whole organization can use the console — not one trusted laptop on a proxied dev server.
- **Technical objective:** Session/token handling against E3 (bearer attachment, 401 → login UX, session expiry honesty); role-aware navigation and controls driven by the same RBAC vocabulary (no second permission model — ENG-6 applied across runtimes as a documented lockstep pair); a production serving posture (static build mounted by the monolith — preserving ARCH-1's single deployable — with CORS/origin coherence from E3/E4); error boundaries; a frontend test baseline covering the `ui.jsx` incident-shape helpers and above all the `deriveStatus` lockstep (TEST for ENG-7); casing hygiene fixes.
- **Constitution laws satisfied:** SEC-1 (console inside the perimeter), EXPL-5, ENG-7, CODE-7, TEST-2 (frontend tests join gating CI), TECH-1 (a test runner is the one justified new tool — it retires the "zero frontend tests" audit finding).
- **Audit findings addressed:** No frontend auth; no production build/serving; zero frontend tests; `Agentlogcard.jsx` casing hazard; role-blind UI.
- **Existing modules reused:** AppShell, HealthProvider, ToastHost, the entire page set and design system — unchanged in structure.
- **New modules:** A small auth/session context in `frontend/src/layout/`.
- **Files expected to change:** `frontend/src/layout/` (auth context, shell gating), `App.jsx` (route guards), `config/nav.js` (role tags), `vite.config.js`/build scripts, `package.json` (test runner), component casing renames, `aeam/main.py` (static mount), CI workflow.
- **Dependencies:** E3 (identity); E6 (paged fetching) strongly recommended first.
- **Acceptance criteria:** Unauthenticated users reach only the login surface; an analyst cannot see or invoke admin configuration; token expiry produces an honest re-auth prompt, never silent empty data; `deriveStatus` lockstep covered by tests on both runtimes; production build served by the monolith passes the full console smoke.
- **Testing strategy:** Frontend unit tests in gating CI (helpers, status lockstep, auth context); one end-to-end smoke path (login → dashboard → incident → evidence panels).
- **Documentation updates:** Frontend README (build/serve/auth); lockstep documentation extended to the role vocabulary.
- **Rollback strategy:** Auth UI behind an environment flag preserving today's open-dev behavior for local work.
- **Future extensibility:** Role-aware UI grows automatically as the RBAC matrix grows; the session layer is the landing zone for SSO redirects in E13.
- **Estimated complexity:** M–L.
- **Enterprise maturity gained:** Article XVI frontend-auth item closed; the last all-or-nothing access gap removed.

---

## Phase E11 — Platform Observability & Audit Surface

- **Goal:** AEAM is watchable by ops and queryable by auditors — from outside its own UI.
- **Why this phase exists:** Audit: Observability 55 — metrics exist but nothing scrapes or alerts; per-incident duration is honestly unavailable (D3 discloses it) because finalize never persists it; the audit trail (durable since E3) has no query surface. OBS-6 makes platform-of-the-platform observability an enterprise gate.
- **Business value:** On-call can be paged when AEAM degrades; compliance can self-serve audit questions; the D3 dashboard finally reports investigation duration truthfully.
- **Technical objective:** Persist per-incident duration into the existing `audit_summary` finding at finalize (one additive field — closing D3's disclosed gap the honest way: measured, not merged from Prometheus). D3 reports duration as available for post-phase incidents and honestly unavailable for older ones (COMPAT-1 in action). Scrape/alert posture shipped as deployment artifacts (Prometheus scrape config + alert rules on the metrics that already exist plus E7 heartbeats — OBS-1: no second pipeline). A read-only audit query endpoint over the E3 durable sink, RBAC'd to the auditor role. Log correlation review: every investigation-path log line carries the incident id.
- **Constitution laws satisfied:** OBS-1 through OBS-6, SEC-6, COMPAT-1, EXPL-3 (mixed-history honesty).
- **Audit findings addressed:** Duration not persisted; no alerting; audit unqueryable; "nothing scrapes /metrics."
- **Existing modules reused:** Orchestrator finalize (one field), D3 `ObservabilityEngine` (new metric follows its existing honest-availability pattern), metrics module, E3 audit table, auditor RBAC role.
- **New modules:** One thin audit-query router.
- **Files expected to change:** `aeam/agents/orchestrator/orchestrator.py` (additive field), `aeam/intelligence/observability.py`, `aeam/api/observability.py`, new audit router + RBAC entry, `deploy/` (scrape/alert artifacts), frontend Analytics/Dashboard (duration display with honest mixed-history labeling).
- **Dependencies:** E3 (audit sink, roles), E7 (heartbeats to alert on).
- **Acceptance criteria:** A seeded failure (dead monitor thread, error-rate spike) fires an alert in staging; D3 shows real duration for new incidents and the honest reason for old ones; an auditor-role user can query audit history by principal and time window; no second metrics store exists anywhere.
- **Testing strategy:** Metric semantics tests (OBS-2); alert-rule tests against synthetic series; mixed-history D3 regression; audit query authorization tests.
- **Documentation updates:** An SRE runbook *for AEAM itself* (symptoms → metrics → actions); alert catalog with declared semantics.
- **Rollback strategy:** Additive field is ignored by all existing readers; alert artifacts are deployment-side and removable.
- **Future extensibility:** Distributed tracing, if ever justified under TECH-1, plugs into the same correlation ids without contract changes.
- **Estimated complexity:** M.
- **Enterprise maturity gained:** Observability 55→~75; Article XVI operations items closed.

---

## Phase E12 — Knowledge, Policy & Memory Governance

- **Goal:** The platform's knowledge stores — documents, extracted policies, organizational memory — become curated, correctable, and quality-measured.
- **Why this phase exists:** The audit found extracted policies live forever with no lifecycle (every row matches investigations until manually deleted from the DB), organizational memory has no correction path (MEM-4 requires one), uploaded documents never earn the authoritative-source retrieval bonus because upload stores format-as-type (a MOD-4 contract defect), and retrieval quality has no regression harness — corpus drift is invisible.
- **Business value:** Compliance-grade knowledge lifecycle (who approved this policy? retire it); a memory store that can be corrected under audit; retrieval quality protected against silent degradation.
- **Technical objective:** Policy lifecycle: additive status field (active / pending-review / retired) with endpoints and Knowledge Center UI; `PolicyRegistry` matches active policies only (COMPAT-6: default active preserves current behavior). Memory curation: expunge/correct operations on `aeam_incident_memories` entries (existing Qdrant client capabilities) that leave an audit record (MEM-4). Semantic document typing at upload (declared doc_type separate from format), fixing the relevance-bonus defect for uploaded runbooks. Retrieval evaluation harness: a golden query set with expected evidence, run as a gating regression (reusing the RetrievalDebugTracer as the instrument — it already replays the live pipeline).
- **Constitution laws satisfied:** MEM-4/MEM-6, RAG-7, MOD-4, DOC-2, COMPAT-6, SEC-7 (curation is privileged).
- **Audit findings addressed:** Ungoverned policy rows; uncorrectable memory; doc_type semantic mismatch; absent retrieval-quality evaluation.
- **Existing modules reused:** `PolicyRepository`, registry patterns, `EnterpriseMemoryEngine`, `RetrievalDebugTracer`, Knowledge Center components, ingest validation.
- **New modules:** Retrieval evaluation suite (test infrastructure, not runtime code).
- **Files expected to change:** `aeam/api/knowledge.py`, `aeam/api/ingest.py` (additive type field), `aeam/intelligence/policy_registry.py`, `aeam/memory/enterprise_memory.py`, migration (E5), `frontend/src/pages/KnowledgeCenter.jsx`, `Memory.jsx`, new eval fixtures/tests.
- **Dependencies:** E5 (migration), E3 (privileged curation endpoints), E6 useful.
- **Acceptance criteria:** A retired policy never matches a new investigation; a memory correction removes the entry from recall and records who/why/when; an uploaded document declared as a runbook receives the authoritative bonus with the reason attached (RAG-7); the golden-set evaluation gates a deliberately degraded retrieval change.
- **Testing strategy:** Lifecycle matrix tests; curation audit-trail tests; relevance-bonus regression; the eval harness itself running in gating CI with stated thresholds.
- **Documentation updates:** Knowledge governance guide (policy lifecycle, memory correction procedure); retrieval evaluation methodology.
- **Rollback strategy:** Status defaults preserve current matching; curation endpoints flag-gated; eval suite is additive CI.
- **Future extensibility:** Document ACLs and per-audience knowledge scoping build on the same lifecycle fields; the eval harness absorbs future model changes (TECH-6 re-validation).
- **Estimated complexity:** M–L.
- **Enterprise maturity gained:** Article XVI governance items (policy lifecycle, memory curation, data classification groundwork) closed.

---

## Phase E13 — Enterprise Certification & Scale Validation

- **Goal:** Close Article XVI completely, prove the platform under load, and produce the evidence pack an enterprise security/procurement review requires.
- **Why this phase exists:** The capstone. After E1–E12, the remaining audit items are integration and proof, not construction: SSO, tenancy declaration, backup/DR rehearsal, supply-chain scanning, and performance baselines. The audit's closing question — "how close is AEAM to production-grade?" — is answered here with evidence rather than assertion.
- **Business value:** AEAM passes a Fortune 500 vendor/security review: identity federates with the enterprise, recovery is rehearsed, scale limits are known numbers, and compliance questions have documented answers.
- **Technical objective:** OIDC/SSO integration on the E3 verification path (JWKS from the enterprise IdP; the console's E10 session layer handles the redirect flow). Tenancy position declared and documented (single-tenant-per-deployment is an acceptable, honest answer — PHIL-1 applies to positioning). Backup/restore and disaster-recovery runbooks written *and rehearsed* (database, object store, Qdrant collections, Redis posture). Supply-chain hardening in CI: dependency audit, image scanning, SBOM generation (TEST-2-grade gating). Performance baselines with recorded budgets: concurrent investigation throughput (E2), ingestion throughput, console responsiveness at volume (E6), autonomous-loop cycle stability (E7). Final Article XVI sweep with evidence links per item; audit re-score.
- **Constitution laws satisfied:** Article XVI wholesale; SEC-1..8 completion; TEST-2/TEST-6 at system scope; V-2.
- **Audit findings addressed:** Every remaining open item: SSO, DR/backup absence, no security scanning, no load/perf baselines, undeclared tenancy.
- **Existing modules reused:** E3 verification path, E10 session layer, E4 state substrate, E11 alerting — this phase adds integration and evidence, not new subsystems.
- **New modules:** None in the application; CI/deployment artifacts and documentation only.
- **Files expected to change:** CI workflow (scanning/SBOM), deploy artifacts, `aeam/config/settings.py` (IdP configuration), frontend session layer (redirect flow), documentation set (DR runbooks, tenancy statement, compliance pack, performance baseline report).
- **Dependencies:** All prior phases (capstone by definition; individual items may land as their prerequisites complete).
- **Acceptance criteria:** Login federates against a real IdP in staging; a full restore drill from backups succeeds and is documented; CI blocks on a known-vulnerable dependency fixture; load tests meet recorded budgets; Article XVI reads 100% checked, each item linking its evidence; the re-scored audit shows no category below the agreed floor.
- **Testing strategy:** The rehearsals *are* the tests: restore drill, failover behavior, load suites with budget assertions in CI, SSO integration tests against a test IdP.
- **Documentation updates:** The enterprise evidence pack: security posture, DR/backup runbooks, tenancy and data-classification statements, performance baselines, final audit re-score.
- **Rollback strategy:** Integration-level: IdP config reverts to E3 static-key posture; scanning gates can be staged as warn-then-block.
- **Future extensibility:** The evidence pack becomes the recurring certification artifact; subsequent F-series phases (connectors, Tier-3 extraction, RAG-over-tables — already registered as candidate scope) start from a certified baseline.
- **Estimated complexity:** L (breadth, not depth).
- **Enterprise maturity gained:** Declaration of enterprise production maturity, backed by evidence.

---

## Projected end-state scorecard (post-E13, against the 2026-07 audit)

| Category | Audit | Projected | Carried by |
|---|---|---|---|
| Architecture | 78 | 80+ | Preserved throughout (ARCH-1) |
| Separation of concerns | 85 | 85+ | Untouched by design |
| Modularity | 82 | 85 | Every phase extends via existing seams |
| Scalability | 35 | ~70 | E2, E4, E5, E6 |
| Reliability | 55 | ~80 | E2, E7 |
| Fault tolerance | 60 | ~75 | E4, E7, E8 |
| Observability | 55 | ~80 | E7, E11 |
| Explainability | 90 | 90+ | Protected by every phase's honesty criteria |
| Security | 30 | ~85 | E3, E8, E9, E10, E13 |
| Testability | 65 | ~85 | E1, E10, E12, E13 |
| Maintainability | 72 | ~85 | E1, E5 |
| Production readiness | 45 | ~85 | E4, E7, E11, E13 |
| Enterprise readiness | 40 | ~85 | E3, E9, E10, E12, E13 |

*Adopted 2026-07-24 as the canonical development plan, on the basis of the full-codebase read, the Engineering Audit, and the Engineering Constitution of the same date.*
