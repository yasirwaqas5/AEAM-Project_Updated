# AEAM Canonical Evolution Roadmap — The E-Series

**Status:** Canonical development plan. Subordinate only to [CONSTITUTION.md](CONSTITUTION.md).
**Naming:** Continues the project's phase lineage (A: blueprint/shell → B: data layer → C: intelligence → D: explainability/config → **E: enterprise hardening** → **F: advanced intelligence, agentic depth & ecosystem**).
**End state:** The **E-series** ends when every item of Constitution Article XVI (Enterprise Readiness Checklist) is checked with evidence (E13). At that point AEAM may call itself a production-grade enterprise autonomous intelligence platform. The **F-series** builds *new capability* on that certified baseline — deeper detection intelligence, adaptive learning, a formalized agent mesh, and the enterprise connector ecosystem — without reopening the hardening work. Every F-phase is still governed by the Constitution and every capability AEAM will ever ship has an explicit phase here: this roadmap is the definitive implementation contract for the remainder of the project.

## Governance

1. **The Constitution rules this roadmap.** Every phase lists the laws it satisfies; a phase implementation that cannot cite its laws is not reviewable (Article XV).
2. **Phase gate = Definition of Done (Article XV), applied to the whole phase.** A phase ships only when its acceptance criteria hold and the full regression ledger is green in gating CI.
3. **Every phase is independently valuable and shippable.** No phase leaves the platform worse, less honest, or half-migrated. Rollback is defined per phase.
4. **No timelines.** Complexity is rated S / M / L / XL (scope and risk, not duration). Order is dependency-driven, not calendar-driven.
5. **Audit re-scoring** occurs after E7 (operational midpoint) and after E13 (certification), using the same 13 categories as the 2026-07 Engineering Audit. The F-series is re-scored on completion of each cluster (F-Intelligence, F-Agentic, F-Ecosystem) against the same categories, extended with capability-maturity notes.
6. **The E-series is a prerequisite spine for the F-series.** No F-phase may begin until its cited E-phase dependencies have shipped: advanced intelligence assumes a hardened, observable, governed platform underneath it. F-phases are still individually valuable and independently shippable within that constraint.

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
E13 Certification ◀── all of the above (E-series capstone; certified baseline)
        │
        ▼   (F-series builds on the certified baseline)
F-Intelligence:  F1 Detection/Statistical/Forecast Uplift ──▶ F2 Learning & Feedback
                 F3 Policy Compilation & Validation ──▶ (feeds F2 calibration)
                 F4 Correlation & Business Graph
F-Agentic:       F5 Investigation & Timeline Replay
                 F6 Agent Mesh Formalization (Supervisor + Planning Agents)
F-Ecosystem:     F7 Enterprise Connector Framework & Connectors

F-phase prerequisites (dependency-driven, not calendar-driven):
  F1 ← E5, E7        F2 ← E9, E12, F1      F3 ← E9, E12, E8
  F4 ← E5, E6, E4    F5 ← E11, E6, E3      F6 ← E7, E11, F1–F4
  F7 ← E3, E4, E5, E12
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
- **Technical objective:** Additive pagination/filtering parameters on the unbounded list endpoints, with **defaults that preserve today's exact behavior** (COMPAT-2/4); published response models in OpenAPI so the frontend contract becomes explicit; frontend adoption (paged fetching, table virtualization) for Incidents, pickers, Memory, Analytics; observability retention-limit gains a sane default with disclosed windowing (OBS-2); policy embeddings computed once at extraction time and stored (additive column via E5), so C3 matching stops re-embedding the corpus per incident. **Resource management** is addressed in the same phase as the bounded-work counterpart to bounded-payload: explicit, configured bounds on the shared resource pools the platform holds — database connection pool sizing, the Qdrant/embedding client reuse already established by ENG-6, and worker/thread-pool limits for the ingestion and monitor daemons — so a load spike degrades gracefully (bounded queues, disclosed back-pressure) instead of exhausting connections or memory. These are configuration and instrumentation, not new subsystems.
- **Constitution laws satisfied:** COMPAT-2/4/6, OBS-2, ENG-6, PHIL-5, EXPL-5 (all windows disclosed in UI).
- **Audit findings addressed:** No pagination anywhere; API 58; C3 O(policies) embedding cost; D3 O(history) aggregation; console unusable at volume.
- **Existing modules reused:** All routers extended in place; `ui.jsx` helpers unchanged; D3 engine unchanged (only its read window governed); `PolicyRegistry` matching logic unchanged except vector sourcing.
- **New modules:** None.
- **Files expected to change:** `aeam/api/incidents.py`, `logs.py`, `knowledge.py`, `observability.py`; `aeam/intelligence/policy_registry.py` + `policy_extraction`/processor (stored vectors); one migration; frontend `pages/Incidents.jsx`, `Investigation.jsx` (picker), `Memory.jsx`, `Analytics.jsx`, `Dashboard.jsx`, shared fetch helpers.
- **Dependencies:** E5 (migration for the embedding column); E1.
- **Acceptance criteria:** Parameter-less API calls are byte-compatible with today (contract tests). Paged calls are bounded regardless of table size. A 100k-incident synthetic dataset renders every console page within a stated budget. Policy-match latency is flat with respect to policy count. Under a synthetic load spike the database connection pool and worker pools stay within configured bounds and the platform sheds/queues work with disclosed back-pressure rather than exhausting resources.
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
- **Technical objective:** Additive review persistence (verdict tables with reviewer attribution from E3 identity) and endpoints following the existing repository/router patterns. Finalization honors the gate: when the plan requires approval, gated runbook steps are recorded as pending (notifications still dispatch — informing humans is never gated); an authorized approval executes the pending steps through the **unchanged** ActionAgent with full audit; rejection records the decision. The Human Review page switches from session-local to persisted, removing its honest disclaimer because it stops being true. Status vocabulary grows additively if a pending-approval state is warranted (COMPAT-6). **Multi-level (tiered) approval** is in scope for this phase, not deferred: the verdict schema carries an approval-tier/step so that a policy or severity can require an ordered chain of authorized approvals (e.g. analyst → manager → risk) before gated steps execute, with each tier's principal attributed independently. A single-tier requirement is the default and is behaviorally identical to a one-step chain (COMPAT-1/6). Reviewer assignment and SLA timers remain future extensibility on the same tables; the tier column and ordered-verdict model that make them possible are delivered here.
- **Constitution laws satisfied:** AGENT-5, SEC-7, EXPL-5/6, COMPAT-1/5/6, MEM-2 (verdicts are new records, not incident mutations).
- **Audit findings addressed:** Advisory-only approval flag; session-local Human Review (top UX gap); no incident ownership/attribution.
- **Existing modules reused:** ActionAgent (identical `execute` contract), C7's already-computed flag, runbook catalog, notifications, findings model, repository pattern, E3 audit sink.
- **New modules:** One review router + repository module (mirroring existing per-domain patterns).
- **Files expected to change:** New `aeam/api/` router and registry repository, migration (E5), `aeam/agents/orchestrator/orchestrator.py` (finalize gating), `aeam/middleware/security_middleware.py` (RBAC entries — SEC-3 in the same change), `frontend/src/pages/HumanReview.jsx`, `frontend/src/components/ui.jsx` helpers.
- **Dependencies:** E3 (attribution + authz), E5 (migrations), E2 (lifecycle integrity).
- **Acceptance criteria:** An approval-required incident executes zero gated actions until an authorized approval; approval executes exactly the recorded pending steps, idempotently; verdicts survive restart and appear in the audit trail with the acting principal; incidents predating this phase render unchanged (COMPAT-1); non-approval incidents behave exactly as today. A tiered-approval incident executes gated steps only after **every** required tier has approved in order; a rejection at any tier halts the chain and records which tier and principal rejected; a single-tier requirement behaves identically to today's one-step approval.
- **Testing strategy:** Gating matrix (required × approved/rejected/ignored × role); end-to-end approve/reject flows; idempotency tests on double-approval; legacy-incident rendering regression.
- **Documentation updates:** Governance workflow guide; AGENT-5 advisory disclaimer replaced by the enforced semantics; runbook doc updated.
- **Rollback strategy:** Enforcement behind a setting (off = today's advisory behavior); tables are additive and inert when disabled.
- **Future extensibility:** SLA timers, reviewer assignment, and escalation-on-timeout build on the same tables (the tier/ordered-verdict model shipped in this phase) without schema reshaping; the F2 Learning phase consumes these verdicts as its primary feedback signal.
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
- **Technical objective:** Persist per-incident duration into the existing `audit_summary` finding at finalize (one additive field — closing D3's disclosed gap the honest way: measured, not merged from Prometheus). D3 reports duration as available for post-phase incidents and honestly unavailable for older ones (COMPAT-1 in action). Scrape/alert posture shipped as deployment artifacts (Prometheus scrape config + alert rules on the metrics that already exist plus E7 heartbeats — OBS-1: no second pipeline). A read-only audit query endpoint over the E3 durable sink, RBAC'd to the auditor role. Log correlation review: every investigation-path log line carries the incident id. **Distributed tracing (OpenTelemetry)** is scheduled here as the correlation-and-latency complement to the existing Prometheus metrics, not a replacement for them (OBS-1 preserved: metrics remain the single metrics pipeline; OTel spans are a distinct, complementary tracing signal justified under TECH-1 because it retires the "no request/investigation tracing" audit gap): incident-scoped spans across the investigation path (decision → evidence stages → planning → action), exported via the OTLP standard so any enterprise backend can consume them, with the incident id as the trace correlation key already present in logs. **Platform cost analytics** extends E8's LLM token/cost counters into a first-class cost surface: per-incident and per-time-window cost roll-ups (LLM spend, action-execution counts, retrieval volume) published through the same metrics pipeline and rendered on the Analytics page, so operators can see what the platform costs to run — with every window disclosed (OBS-2, EXPL-5).
- **Constitution laws satisfied:** OBS-1 through OBS-6, SEC-6, COMPAT-1, EXPL-3 (mixed-history honesty).
- **Audit findings addressed:** Duration not persisted; no alerting; audit unqueryable; "nothing scrapes /metrics."
- **Existing modules reused:** Orchestrator finalize (one field), D3 `ObservabilityEngine` (new metric follows its existing honest-availability pattern), metrics module, E3 audit table, auditor RBAC role.
- **New modules:** One thin audit-query router.
- **Files expected to change:** `aeam/agents/orchestrator/orchestrator.py` (additive field), `aeam/intelligence/observability.py`, `aeam/api/observability.py`, new audit router + RBAC entry, `deploy/` (scrape/alert artifacts), frontend Analytics/Dashboard (duration display with honest mixed-history labeling).
- **Dependencies:** E3 (audit sink, roles), E7 (heartbeats to alert on).
- **Acceptance criteria:** A seeded failure (dead monitor thread, error-rate spike) fires an alert in staging; D3 shows real duration for new incidents and the honest reason for old ones; an auditor-role user can query audit history by principal and time window; no second metrics store exists anywhere. An investigation emits a single correlated OTel trace spanning its stages, exported over OTLP and joinable to the incident id; the Analytics cost surface reports LLM/action/retrieval cost per incident and per disclosed window for post-phase data and honestly discloses unavailability for older data.
- **Testing strategy:** Metric semantics tests (OBS-2); alert-rule tests against synthetic series; mixed-history D3 regression; audit query authorization tests.
- **Documentation updates:** An SRE runbook *for AEAM itself* (symptoms → metrics → actions); alert catalog with declared semantics.
- **Rollback strategy:** Additive field is ignored by all existing readers; alert artifacts are deployment-side and removable.
- **Future extensibility:** The OTel span tree shipped here is the substrate the F-series agents (F6 Supervisor) observe the mesh through; the cost surface becomes the input to future budget-enforcement (hard limits) without new plumbing.
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
- **Future extensibility:** The evidence pack becomes the recurring certification artifact; the **F-series (F1–F7 below)** — advanced detection/forecast intelligence, adaptive learning, policy compilation, the business graph, replay, the formalized agent mesh, and the enterprise connector ecosystem — starts from this certified baseline and is re-certified against it as each cluster completes.
- **Estimated complexity:** L (breadth, not depth).
- **Enterprise maturity gained:** Declaration of enterprise production maturity, backed by evidence.

---

# F-Series — Advanced Intelligence, Agentic Depth & Ecosystem

**Status:** Formally scheduled forward contract. The F-series builds *new capability* on the E13-certified baseline; it does not reopen enterprise hardening. It is bound by the same Constitution, the same phase-gate Definition of Done, and the same explicit non-goals (no microservice split, no framework adoption, no second config/metrics/retrieval pipeline, no new infra unless it retires a violation).

**Two invariants govern every F-phase.** They are the constitutional guarantees the C/D intelligence engines already honor, extended to all new intelligence:

1. **Advisory-source contract (AGENT-5, EXPL).** Every new intelligence engine or agent is an *advisory evidence source*. It appends its own findings entry and never overrides, suppresses, or triggers a deterministic `RuleEngine` / `DecisionEngine` / `ActionAgent` decision. New behavior that *acts* does so only through the existing gated, human-approvable (E9) path.
2. **Composition & single-coordinator (ARCH-1, ARCH-8, MOD-1).** New agents wrap existing engines with a stable contract; the Orchestrator remains the single coordinator; per-incident state stays on the E2 `IncidentContext`. Nothing here reshapes the mesh's topology — it deepens the nodes.

**Clusters:** F-Intelligence (F1–F4) deepens what the platform *knows*; F-Agentic (F5–F6) deepens how it *explains and coordinates*; F-Ecosystem (F7) deepens what it *connects to*.

---

## Phase F1 — Detection, Statistical & Forecast Intelligence Uplift

- **Goal:** Replace the KPI-investigation placeholder with a real KPI Agent and materially raise the accuracy of the statistical and forecast engines.
- **Why this phase exists:** The investigation loop still calls `_run_kpi_investigation_placeholder`, which emits a synthetic "Simulated root cause" that E1 correctly marked, quarantined, and badged — an honest placeholder, but a placeholder. The detection engines (rule + z-score/moving-average + Prophet) are sound but unimproved since Phase 5. Enterprise detection expects real KPI analysis in the investigation path and measurably better precision/recall than the current baseline.
- **Business value:** Investigations produce genuine machine analysis instead of an honestly-labeled stand-in; fewer false positives and earlier true detections directly reduce operator toil and missed incidents.
- **Technical objective:** A real **KPI Agent** that performs the investigation-time KPI analysis the placeholder stands in for — consuming the event's already-computed statistical/forecast metadata plus fetched history, producing a grounded, non-fabricated KPI finding (retires the ENG-5 placeholder path entirely; PHIL-1 satisfied by *removal*, not relabeling). **Statistical engine improvements**: additional deterministic detectors (e.g. robust seasonal-hybrid decomposition, changepoint detection) behind feature flags, each a pure detector with no I/O, composed by MonitorAgent exactly as `StatisticalDetector` is today. **Forecast improvements**: model-quality upgrades (backtesting harness, holdout MAPE tracking, optional multi-model selection) behind the existing `ForecastAgent` contract; any new model artifact is re-validated (TECH-6) before it can serve. Detection changes ship flag-gated with today's behavior as the default.
- **Existing modules reused:** `StatisticalDetector`, `RuleEngine`/`CompositeRuleEngine`, `ForecastAgent`, `MonitorAgent` composition, `AdaptiveDetectionEngine` (C5), the event metadata contract — all extended in place, none reshaped.
- **New modules:** One `KPIAgent` module (the long-deferred real one); optional new detector modules in `aeam/agents/kpi/`; a forecast backtesting harness (test/eval infrastructure).
- **Dependencies:** E1 (placeholder marker/quarantine it retires), E5 (any additive tables for backtest results), E7 (autonomous loop that feeds real detection), E11 (metrics to measure detection quality).
- **Constitution laws satisfied:** PHIL-1 (placeholder finally replaced by real analysis), MOD-1/MOD-4, AI-2 (grounded, non-fabricated KPI findings), TECH-6 (model re-validation), OBS-2 (quality metrics disclosed), COMPAT-2 (flag-gated, default-unchanged).
- **Audit findings addressed:** KPI-agent placeholder in the investigation path; detection engines unimproved since Phase 5; no forecast backtesting/quality tracking.
- **Acceptance criteria:** No finalized incident emits a placeholder-sourced root cause once the KPI Agent is enabled (the placeholder path is deleted, not merely bypassed); on a labeled synthetic dataset, detection precision/recall beats the recorded Phase-5 baseline by a stated margin; forecast holdout MAPE improves against the same baseline; every existing detector's contract and default output is byte-identical when new detectors are flag-off.
- **Testing strategy:** Labeled detection benchmark in gating CI (precision/recall thresholds); forecast backtest with MAPE assertions; regression proving flag-off behavior is identical to today; KPI-Agent grounding tests (no fabricated causes).
- **Rollback strategy:** Every improvement is flag-gated; disabling the flags restores Phase-5 detection exactly. The KPI Agent itself is gated so the (now-removed) placeholder can be reinstated from version control if a rollback is ever required.
- **Enterprise value:** The platform's core detection deliverable stops being a placeholder and becomes measurably-good real analysis — the credibility floor for an autonomous detection product.
- **Estimated complexity:** L.

---

## Phase F2 — Adaptive Learning, Feedback Loop & Confidence Recalibration

- **Goal:** The platform learns from resolved outcomes and human verdicts, and recalibrates its confidence so that a stated confidence means what it says.
- **Why this phase exists:** AEAM produces confidence scores and D2 quality scores but never closes the loop: a human's approve/reject verdict (E9) and an incident's eventual real outcome are recorded and then never used to improve future scoring. Enterprise autonomous systems are expected to demonstrably improve with feedback and to have calibrated confidence (a "0.8" should resolve correctly ~80% of the time).
- **Business value:** Confidence becomes trustworthy enough to drive automation thresholds and approval routing; the platform visibly improves as the organization uses it, which is the core promise of an "intelligence" product.
- **Technical objective:** A **feedback loop** that ingests E9 human verdicts and resolved-incident outcomes as labeled signal (read-only over existing records; MEM-2 — past incidents are never mutated). A **learning engine** that computes a **confidence recalibration** mapping (e.g. isotonic/Platt-style calibration over historical predicted-vs-actual outcomes) applied as a disclosed, reversible post-processing step on confidence at finalize — the raw model confidence is always retained alongside the calibrated value (EXPL: both shown). A **Learning Agent** that owns this loop as a first-class, *advisory* agent: it proposes calibration/threshold updates; it never silently rewrites scoring, and any change to an automation threshold routes through E9 approval. Calibration state is versioned and every recalibration is auditable.
- **Existing modules reused:** E9 verdict tables (primary signal), `EnterpriseMemoryEngine` (outcome history), D2 `AIEvaluationEngine` (quality scores), the confidence fields in the findings model, E11 metrics for tracking calibration drift.
- **New modules:** A learning/recalibration engine module and a `LearningAgent` (composition over the above).
- **Dependencies:** E9 (verdict signal — hard prerequisite), E12 (memory governance/curation so training signal is clean), F1 (better base detection to calibrate), E5 (calibration-state tables), E11 (drift metrics).
- **Constitution laws satisfied:** MEM-2 (no mutation of past records), MEM-4 (corrections respected as signal), AGENT-5 (advisory; threshold changes gated), EXPL-4/5 (calibration disclosed; raw + calibrated both surfaced), PHIL-1 (calibration measured, never asserted), TECH-6 (recalibration re-validated).
- **Audit findings addressed:** Open feedback loop (verdicts/outcomes recorded but unused); uncalibrated confidence; no demonstrable learning.
- **Acceptance criteria:** On a held-out history, the calibration curve (predicted vs. actual resolution) is measurably closer to the diagonal after recalibration than before; both raw and calibrated confidence are persisted and shown; no past incident record is mutated; every threshold change produced by the Learning Agent is human-approved (E9) and audit-logged; disabling the learning flag reverts to raw confidence exactly.
- **Testing strategy:** Calibration-improvement assertion on a labeled fixture in gating CI; MEM-2 immutability test (learning run touches zero historical rows); advisory-boundary test (Learning Agent cannot change a threshold without an approval record); drift-metric semantics tests.
- **Rollback strategy:** Recalibration is a flag-gated, reversible post-processing step; flag-off yields raw confidence identical to today. Calibration state is versioned, so any prior calibration can be restored.
- **Enterprise value:** Confidence you can build automation policy on, and a platform that provably gets better with use — the differentiator between a static tool and an adaptive one.
- **Estimated complexity:** L.

---

## Phase F3 — Policy Compilation, Validation & the Policy Agent

- **Goal:** Extracted policies become validated, conflict-checked, and compilable into candidate deterministic rules — surfaced by a dedicated Policy Agent — with deeper extraction from complex PDFs.
- **Why this phase exists:** C2/C3 extract policies from documents and match them as advisory evidence, and E12 governs their lifecycle — but a policy can never *become* an enforced rule without hand-editing `detection_rules.yaml`, extraction is Tier-1/2 (flat conditions), and nothing checks the policy corpus for internal contradictions. Enterprise policy intelligence means the gap between "a document says X" and "the engine enforces X" is bridged safely and auditably.
- **Business value:** Organizational policy in uploaded documents can be turned into governed detection behavior with human sign-off, not tribal YAML editing; contradictory policies are caught before they cause inconsistent decisions.
- **Technical objective:** **Rule compilation**: a compiler that turns a validated policy into a *proposed* `RuleEngine`-shaped rule definition — always a proposal, never auto-activated. Adoption of a compiled rule is a privileged, human-approved action (E9 + SEC-7); until adopted it is inert. Compiled rules, once adopted, feed the **same** deterministic `RuleEngine` — no second rule evaluator (ENG-6). **Policy validation**: static consistency/conflict analysis across the policy corpus (overlapping conditions with contradictory actions, threshold collisions, unreachable policies), surfaced in the Knowledge Center. **Tier-3 extraction**: richer PDF policy extraction recovering tabular and nested-conditional policy structure, through the E8-guarded LLM boundary. A **Policy Agent** wraps extraction + validation + matching + compilation as a first-class advisory agent.
- **Existing modules reused:** `policy_extraction`, `PolicyRegistry`/`PolicyRepository`, `RuleEngine`/`CompositeRuleEngine` (compiled rules target the existing engine), E9 approval path, E12 policy lifecycle/status, E8-guarded LLM boundary, Knowledge Center UI.
- **New modules:** A rule-compiler module, a policy-validator module, and a `PolicyAgent` (composition).
- **Dependencies:** E12 (policy lifecycle/status — hard prerequisite), E9 (approval to activate a compiled rule), E8 (guarded extraction), E5 (proposed-rule/validation tables), E3 (privileged compilation endpoints).
- **Constitution laws satisfied:** SEC-7 (rule adoption is privileged), AGENT-5 (proposals are advisory until approved), RAG-7/MOD-4 (extraction fidelity, doc-type honesty), ENG-6 (one rule engine), PHIL-1 (proposed vs. active never conflated), COMPAT-6 (default-active matching preserved).
- **Audit findings addressed:** No path from extracted policy to enforced rule; Tier-1/2 extraction only; no policy-corpus conflict detection.
- **Acceptance criteria:** A compiled rule is never enforced without a recorded human approval; the validator flags a deliberately contradictory policy pair before either can be adopted; Tier-3 extraction recovers tabular policy conditions on a fixture that Tier-1/2 misses; the deterministic decision path is byte-identical for any policy that has not been adopted as a rule.
- **Testing strategy:** Compilation-then-approval matrix (proposed/approved/rejected × role); conflict-detection tests on a contradictory-policy fixture; Tier-3 extraction fidelity tests; regression proving un-adopted proposals change no decision.
- **Rollback strategy:** Compilation and adoption are flag-gated and reversible; a compiled rule can be retired via E12 lifecycle, restoring prior deterministic behavior. Tier-3 extraction is additive to the stored policy, not a replacement.
- **Enterprise value:** Closes the loop from governed knowledge to governed enforcement — the capability that makes "Enterprise Policy Intelligence" more than document search.
- **Estimated complexity:** L.

---

## Phase F4 — Correlation Intelligence & Business Graph

- **Goal:** Cross-dataset correlation deepens into an explicit, queryable business graph relating metrics, datasets, services, policies, and incidents.
- **Why this phase exists:** C4 correlates one incident's metric against other activated datasets pairwise, per incident. There is no persistent structure capturing *how the business's signals relate* — so correlation cannot compound across incidents, and an operator cannot ask "what is connected to checkout latency?" Enterprise correlation intelligence expects a durable relationship model, not repeated pairwise scans.
- **Business value:** Faster, richer root-cause context (an incident inherits the known neighborhood of its metric); correlations that strengthen as history accumulates; an explainable map of the organization's signal topology.
- **Technical objective:** A **business graph**: typed entities (metric, dataset, service, policy, incident) and typed, weighted relationships (correlates-with, governed-by, derived-from, co-occurred-in-incident) built from existing dataset intelligence, policy matches, and incident history. It is an **advisory evidence source** like every C/D engine: it appends a `cross_dataset`/`graph` finding and never overrides a deterministic decision. A **correlation engine** upgrade makes C4 graph-aware (traverse known relationships instead of only pairwise-scanning), with all windows and edge-confidences disclosed (EXPL). The graph is durable (E4) and bounded on read (E6); concurrency-safe per E2.
- **Existing modules reused:** `CrossDatasetAnalyzer` (C4), `DatasetIntelligenceService`, `DatasetKPISource`, `StatisticalDetector`, the findings model (new advisory finding), PolicyRegistry (governed-by edges), incident history.
- **New modules:** A business-graph store/builder module and a graph-aware correlation extension.
- **Dependencies:** E4 (durable graph state), E5 (graph tables), E6 (bounded graph queries), E2 (concurrency-safe writes), C4 (the correlation base it extends).
- **Constitution laws satisfied:** ARCH-8 (concurrency-safe), AGENT-5/advisory contract (never overrides RuleEngine), MEM-6 (graph retention posture declared), EXPL-5 (edges and windows disclosed), COMPAT-1 (incidents without graph context render honestly).
- **Audit findings addressed:** Correlation is per-incident and pairwise-only; no persistent business-relationship model; correlation cannot compound over time.
- **Acceptance criteria:** Graph-derived correlations appear as their own advisory finding with disclosed edge confidence; graph queries are bounded regardless of graph size; on a labeled multi-dataset scenario, graph-aware correlation surfaces a corroborating signal that pairwise C4 misses — without altering any deterministic decision; the graph builds concurrency-safely under the E2 soak.
- **Testing strategy:** Graph-construction correctness tests; advisory-boundary regression (graph never changes a decision); bounded-query performance assertions on a synthetic large graph; multi-dataset correlation-uplift test.
- **Rollback strategy:** The graph and graph-aware path are flag-gated; flag-off restores pairwise C4 exactly. The graph store is additive and inert when disabled.
- **Enterprise value:** Turns scattered pairwise correlation into a compounding, explainable map of the business's signals — the substrate for genuinely context-aware investigation.
- **Estimated complexity:** L.

---

## Phase F5 — Investigation & Timeline Replay (Explainability Deepening)

- **Goal:** Any past investigation can be replayed stage-by-stage and reconstructed on a timeline, deepening explainability without re-executing anything.
- **Why this phase exists:** The findings model already records every investigation stage in order and the console has a Replay page, but there is no backend that reconstructs an investigation as a navigable sequence, and D1 explainability explains the *final* plan rather than the *unfolding* of the investigation. Enterprise explainability expects "show me exactly what happened, in order, and why" for any incident, including for audit and post-incident review.
- **Business value:** Auditors and reviewers can walk an investigation exactly as it unfolded; post-incident review and training gain a faithful, read-only reconstruction; explainability extends from "why this recommendation" to "how we got there."
- **Technical objective:** A read-only **replay system** that reconstructs an investigation purely from its persisted, ordered findings audit trail (it **reads** the record; it never re-executes a side effect — MEM-2, no ActionAgent calls, no LLM calls). **Investigation replay** exposes the ordered stages (decision → each advisory evidence source → planning → explainability → actions) with the state visible at each step. **Timeline replay** renders the causal chain against wall-clock/relative time using the E11 persisted durations. Incidents that predate a given stage replay with honest gaps (COMPAT-1/EXPL-3). This is an **explainability improvement**: it adds an explanation surface, changing no investigation behavior.
- **Existing modules reused:** The ordered `findings` JSON model, `audit_summary`, `RetrievalDebugTracer` pattern (read-only replay of a pipeline is a proven instrument here), D1 explainability output, E11 duration field, the existing `Replay.jsx` and `Timeline` frontend components, E3 RBAC.
- **New modules:** A replay-reconstruction endpoint and a timeline-builder (read-only over persisted findings).
- **Dependencies:** E11 (persisted duration for the timeline), E6 (bounded fetch of large findings sets), E3 (RBAC on replay/audit surfaces), D1 (explainability content to replay).
- **Constitution laws satisfied:** MEM-2 (replay is strictly read-only), EXPL-3 (mixed-history honesty), EXPL-5/6 (stage-level disclosure), COMPAT-1 (older incidents replay with honest gaps), SEC-6 (replay respects audit RBAC).
- **Audit findings addressed:** No investigation reconstruction/replay backend; explainability covers the final plan but not the unfolding; the Replay page has no durable data source.
- **Acceptance criteria:** A finalized incident replays its exact recorded stages in the exact recorded order; replay executes zero side effects (asserted: no ActionAgent/LLM invocation during replay); an incident missing a stage (e.g. pre-C7) shows an honest gap rather than a fabricated step; the timeline matches the persisted audit trail and durations.
- **Testing strategy:** Stage-order fidelity tests against seeded incidents; a side-effect-free assertion (replay of an incident triggers no external calls); mixed-history gap-honesty regression; timeline-vs-audit-trail equivalence tests.
- **Rollback strategy:** Entirely additive and read-only; the replay endpoints can be removed with zero data or behavior consequences.
- **Enterprise value:** Audit-grade, faithful reconstruction of autonomous decisions — a frequent hard requirement in regulated environments — delivered without any risk to live behavior.
- **Estimated complexity:** M.

---

## Phase F6 — Agent Mesh Formalization: Supervisor & Planning Agents

- **Goal:** The mesh gains an explicit Supervisor Agent for oversight and promotes the C7 execution-planning engine to a first-class Planning Agent — without adding a second coordinator.
- **Why this phase exists:** AEAM already runs an agent mesh, but two roles the enterprise architecture names are implicit: there is no agent that *observes the health and behavior of the mesh as a whole*, and the execution-planning capability lives as an engine (C7) rather than a named agent with a stable contract like RAG/Action/Report. Formalizing these completes the agentic model while preserving the single-coordinator invariant.
- **Business value:** Operators get a coherent "is the mesh healthy and behaving" view backed by an accountable agent; the planning capability becomes a first-class, independently-testable, independently-observable member of the roster.
- **Technical objective:** A **Supervisor Agent** that provides advisory oversight of the mesh — aggregating E7 heartbeats, E11 metrics/traces, and per-agent participation into a mesh-health and behavior-anomaly view, and escalating concerns through E9 (it **observes and reports; it never assumes coordination authority** — ARCH-1's single Orchestrator is untouched; the Supervisor is a monitor, not a second orchestrator). A **Planning Agent** that promotes the C7 `ExecutionPlanningEngine` to a named agent with a stable contract and its own roster entry and metrics — a *promotion by composition*, producing byte-identical planning output (no behavior change; the engine is wrapped, not rewritten). Both register in the E1 `agent_roster` and appear in the console mesh.
- **Existing modules reused:** The Orchestrator (unchanged single coordinator), `ExecutionPlanningEngine` (C7, wrapped), E7 heartbeat instrumentation, E11 metrics/OTel spans, the `agent_roster` (E1), the frontend Agent Mesh view.
- **New modules:** A `SupervisorAgent` and a `PlanningAgent` (both composition over existing capability).
- **Dependencies:** E7 (supervision/heartbeat substrate — hard prerequisite), E11 (observability the Supervisor consumes), F1–F4 (the deepened agents/engines there are what make a Supervisor and richer planning worth formalizing).
- **Constitution laws satisfied:** ARCH-1 (single coordinator preserved — Supervisor is advisory-only), ARCH-8 (no per-incident state added to shared agents), AGENT-5 (Supervisor escalations are advisory/gated), OBS-4 (mesh health surfaced), COMPAT-1 (Planning Agent output identical to the C7 engine).
- **Audit findings addressed:** Implicit supervisor/planning roles; execution planning not a first-class, separately-observable agent; no whole-mesh health accountability.
- **Acceptance criteria:** The Supervisor surfaces mesh health and a seeded behavior anomaly without ever taking coordination authority (asserted: it issues no `handle_event`/ActionAgent calls); the Planning Agent's output is byte-identical to the C7 engine's for the same input (promotion, not rewrite); both agents appear in `agent_roster`, `/metrics`, and the console mesh; the full C7 planning regression ledger passes unchanged.
- **Testing strategy:** Supervisor advisory-boundary test (no coordination side effects); Planning-Agent output-equivalence test against the C7 engine; roster/metrics presence tests; C7 regression unchanged.
- **Rollback strategy:** Both agents are additive and flag-gated; disabling them removes the roster entries and restores the C7 engine as the direct planning path, byte-identically.
- **Enterprise value:** A complete, legible agentic architecture with whole-mesh accountability — the organizational story enterprises expect from an "agent mesh," delivered without destabilizing the coordinator.
- **Estimated complexity:** M.

---

## Phase F7 — Enterprise Connector Framework & Data-Source Connectors

- **Goal:** A uniform connector framework and production connectors for the major enterprise systems, all feeding the existing ingestion/KPI pipeline.
- **Why this phase exists:** AEAM ingests via direct upload and a single Google Sheets connector; enterprise deployments need to pull knowledge and metrics from the systems the organization already runs. The `BlobStore` ABC and `CompositeKPISource`/`SheetsConnector` already establish the connector pattern — this phase generalizes it into a framework and implements the connectors, without a second ingestion path.
- **Business value:** Organizational knowledge and operational data flow in from where they already live — SharePoint, Confluence, GitHub, SAP, Salesforce, Snowflake, BigQuery, Google Workspace — instead of requiring manual export/upload; AEAM plugs into the enterprise rather than sitting beside it.
- **Technical objective:** A **connector framework**: a connector ABC (mirroring the established `BlobStore`/KPI-source abstraction) defining fetch/list/incremental-sync/credential contracts, with per-connector configuration and health. Each connector implements the ABC and feeds content into the **existing** ingestion pipeline (documents → chunk/embed/index) and/or `CompositeKPISource` (structured metrics) — **no second ingestion or detection path** (ENG-6, TECH-2). Connectors ship incrementally: document sources (**SharePoint, Confluence, GitHub, Google Workspace/Drive**) and structured/data-warehouse sources (**SAP, Salesforce, Snowflake, BigQuery**). All credentials flow through `SecretManager` only (SEC-5); a connector failure is isolated and degrades gracefully (never blocks other connectors or the pipeline). Ingested documents carry the E12 semantic doc-type and, where applicable, connector provenance.
- **Existing modules reused:** `SheetsConnector` (the precedent), `CompositeKPISource`, the ingestion pipeline (chunking/embedding/indexing), `BlobStore` (E4), `SecretManager`, E3 auth/secret handling, E12 doc-typing, `IngestionWorker`.
- **New modules:** A connector framework/ABC and one module per connector, under `aeam/connectors/`.
- **Dependencies:** E3 (credential/secret handling — hard prerequisite), E4 (durable storage for fetched artifacts), E5 (connector config/sync-state tables), E12 (semantic doc-typing for ingested content), E8 (guarded LLM processing of connector content).
- **Constitution laws satisfied:** SEC-5 (secrets via SecretManager), TECH-2 (reuse the single ingestion pipeline), ENG-6 (no second ingestion/detection path), MOD-1/MOD-4 (ABC + honest source typing), COMPAT-4 (connectors are additive), SEC-8 (connector sync honesty — disclosed staleness).
- **Audit findings addressed:** Single ingestion vector (upload + one Sheets connector); no framework for enterprise data sources; no incremental-sync or connector-health model.
- **Acceptance criteria:** Each connector implements the ABC and passes the shared connector contract suite; ingested content flows through the existing pipeline unchanged (a document from any connector is retrievable identically to an uploaded one); credentials are never present outside `SecretManager`; a deliberately failing connector degrades gracefully and does not block others or the pipeline; incremental sync re-ingests only changed content.
- **Testing strategy:** One connector contract suite run against every connector (the ABC is the contract) with mocked source APIs; credential-isolation tests (no literal secrets, all via SecretManager); failure-isolation tests (one connector down, others unaffected); incremental-sync correctness tests. Live third-party API calls are never in gating CI (mocked), matching the repo's existing external-service test posture.
- **Rollback strategy:** Each connector is independently flag-gated and removable; disabling all connectors restores today's upload + Sheets posture exactly. The framework ABC is inert with no connectors registered.
- **Enterprise value:** AEAM becomes a citizen of the enterprise data ecosystem, ingesting knowledge and signals from the systems the organization already depends on — the capability that turns a standalone platform into an integrated one.
- **Estimated complexity:** XL (breadth: one framework + eight connectors, each landing independently).

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

The scores above are the **E-series certification target** (the 2026-07 audit categories, hardened). The **F-series does not raise these hardening categories** — E13 already brings them to the agreed floor — it raises **capability maturity** on top of a certified base. F-series success is measured on capability axes the original audit did not score, re-scored per cluster (Governance point 5):

| Capability axis | Baseline (post-E13) | F-series target | Carried by |
|---|---|---|---|
| Detection & forecast accuracy | Phase-5 engines, KPI placeholder in-loop | Real KPI Agent; measured precision/recall & MAPE gains | F1 |
| Adaptive learning / calibration | Open loop; uncalibrated confidence | Closed feedback loop; calibrated, disclosed confidence | F2 |
| Policy enforcement depth | Advisory match only; Tier-1/2 extraction | Validated, human-approved rule compilation; Tier-3 | F3 |
| Correlation intelligence | Per-incident pairwise (C4) | Durable, compounding business graph | F4 |
| Explainability / auditability | Final-plan explanation (D1) | Faithful stage/timeline replay | F5 |
| Agentic completeness | Implicit supervisor/planning roles | Named Supervisor + Planning agents; whole-mesh accountability | F6 |
| Ecosystem integration | Upload + one Sheets connector | Connector framework + eight enterprise connectors | F7 |

*Adopted 2026-07-24 as the canonical development plan, on the basis of the full-codebase read, the Engineering Audit, and the Engineering Constitution of the same date. Revised 2026-07-25: the F-series (F1–F7) was formally scheduled and the remaining E-phases (E6 resource management, E9 multi-level approvals, E11 OpenTelemetry & cost analytics) refined, so that every enterprise capability AEAM will ship has an explicit, dependency-ordered implementation phase. No completed phase (E1, E2) and no existing Constitution or audit mapping was altered.*
