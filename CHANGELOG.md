# Changelog

All notable changes to AEAM are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [1.0.0] — 2026-08-01

First public release. The roadmap (Phases 1–9, B1, C1–C7, D1–D5, E1–E13, F1–F7) is feature-complete, a formal hardening pass is closed, and the architecture is frozen.

### Added — Core platform

- **Modular monolith** — one FastAPI process, 18 API routers, a React 18 console, PostgreSQL + Redis + Qdrant.
- **Agent mesh** — eight roster agents (Orchestrator, Monitor, KPI, Forecast, RAG, Planning, Report, Action) plus a read-only Supervisor, coordinated by a single Orchestrator.
- **Synchronous EventBus** with exception isolation and aggregate error reporting.
- **Per-incident isolation** — every investigation allocates its own `IncidentContext` (short-term memory + state machine), making `handle_event` fully reentrant.

### Added — Detection

- Deterministic `RuleEngine` driven by `detection_rules.yaml`.
- `StatisticalDetector` — Z-score and percentile bounds.
- `ForecastAgent` — Prophet models with a backtesting harness and holdout-MAPE refusal.
- Optional `ChangepointDetector` and `SeasonalHybridDetector` (flag-gated, default off).
- `KPIAgent` — grounded statistical characterisation replacing the deleted placeholder.

### Added — Retrieval

- Six-stage composable pipeline: dense → BM25/RRF hybrid → multi-query expansion → cross-encoder reranking → evidence diversity → business-relevance ranking.
- Two validation gates: sensitive-pattern guardrail and grounding validation against retrieved chunks.
- Deterministic query rewriting with an exhaustion guard.
- Developer-only retrieval debug tracer (`/api/v1/debug/retrieval`, 404 in production).

### Added — Intelligence

- **Enterprise Memory** — incidents embedded into a dedicated Qdrant collection and recalled as evidence.
- **Policy Intelligence & Registry** — LLM extraction of business rules at ingestion; metric + semantic matching at investigation time.
- **Cross-Dataset Analyzer** — Pearson correlation against other activated datasets.
- **Business Graph** — bounded traversal of persisted relationships (flag-gated).
- **Adaptive Detection** — longer-horizon baselines and day-of-week seasonality.
- **Execution Planning** — priority-ordered recommendations with conflict detection.
- **Explainability** — decision graph, evidence graph, confidence breakdown, contradictions, assumptions.
- **AI Evaluation** — ten-component investigation quality score with published formula.
- **Learning / Calibration** — isotonic confidence recalibration with held-out ECE measurement (flag-gated).

### Added — Governance

- Human-in-the-loop approval with multi-tier chains, per-severity and policy-driven overrides.
- Runbook gating classification — notifications never gated; unknown steps gated by default.
- Knowledge governance — policy lifecycle, memory expunge/correct, attributed curation.
- Dual-sink audit logging (file + `audit_logs`).
- Compliance posture disclosure (`/api/v1/system/compliance`).

### Added — Data & connectors

- Content-addressed ingestion (local disk or S3) with three-layer deduplication.
- Dataset registration, schema inference, and explicit activation.
- Eight enterprise connectors funnelling into the shared ingestion path.
- Deterministic mock mode for credential-free demonstration.

### Added — Observability

- Prometheus metrics across incidents, agents, actions, LLM usage and connectors.
- Optional OpenTelemetry tracing with one root span per investigation.
- Per-incident cost attribution (tokens, retrieval volume, action outcomes).
- Heartbeat supervision for both background threads.
- Timeline Replay reconstructing investigations from the persisted audit trail.
- Mesh-health scoring with a published formula and disclosed components.

### Fixed — Hardening pass

Twenty-two issues triaged; all Critical and High resolved.

**Critical**
- `SourceRepository` was called but never imported in the composition root. The resulting `NameError` was swallowed by a broad handler, silently disabling every metrics connector while health reported them enabled. Import added; composition-root handlers now re-raise `NameError`/`AttributeError`/`ImportError`/`TypeError`.
- `/health` reported `database: "ok"` unconditionally — the check sat inside a `try` whose body could not raise, so an unreachable database still returned `healthy`. Replaced with a real pooled query.
- Incident reports were emailed to a hardcoded third-party address. Recipients are now operator-configured and the step fails closed when unset.

**High**
- `HEARTBEAT_STALE_SECONDS` (120) was shorter than `MONITOR_INTERVAL_SECONDS` (300), so an enabled MonitorAgent reported itself stale for most of every cycle and flipped `/health` to 503. The monitor threshold now floors at `2 × interval + 30`.
- Depth-≥3 LLM reasoning overwrote grounded, chunk-cited root causes unconditionally — and could write the literal string `"Unknown"`. Now guarded on precedence; the LLM view is retained as an advisory finding.
- Race condition between BM25 in-place refresh (ingestion thread) and live search (request threads), with a reachable `IndexError`. Fixed by snapshot-and-swap under a lock.
- SQLite had no busy timeout, so AEAM's own threads hit immediate `database is locked`. Added `busy_timeout` + WAL; PostgreSQL unchanged. This also resolved a pre-existing failing concurrency test.
- `LLMService` discarded the real provider error, persisting only `"Failed to generate LLM response after retries"`. The underlying exception now reaches the incident record.
- The Supervisor reported the Action and KPI agents as never executed immediately after they executed, because roster names and metric labels differ. Resolved via an explicit alias map with the resolved label disclosed.

**Medium / Low**
- `last_event_time` was synthesised as "now" on every call; now read from the newest incident.
- Successful retrievals were persisted as `retrieved_count: 0` when the LLM later failed.
- `IngestionWorker` defaulted to a placeholder processor that marked jobs complete without indexing.
- `run_simulation.py` published each event twice, doubling incidents and external side effects.
- Duplicate `Settings`/`RedisClient` instances; the middleware Redis pool was never closed.
- Upload endpoint constructed its own `IngestionSubmitter` instead of the shared one.
- Non-retryable LLM errors were retried, adding dead latency; the circuit breaker never reset on success.
- Startup knowledge documents carried a frozen `date` literal feeding the recency bonus.
- Email failures reported a misleading "Missing Google Cloud credentials" reason.
- Partial graph builds reported unqualified success.
- Orphaned dataset activations could never be cleared (`deactivate` 404'd on deleted datasets).
- `/health` did not report Qdrant or the LLM; the console rendered both as permanent `n/a`.
- The Dashboard mesh reported the Monitor Agent as "active" whenever any incident existed, contradicting the StatusBar.
- Login page rendered unconditionally, so a valid development session could not enter the app.
- Timestamps from `TIMESTAMP` columns were parsed as local time, shifting verdicts and action logs by the viewer's UTC offset.

### Fixed — Release audit

- `cp .env.example .env` aborted startup: `POSTGRES_PASSWORD` is required by docker-compose but was undeclared in `Settings`, which uses `extra="forbid"`. Declared as a documented deployment-only field; `extra="forbid"` deliberately retained.
- FastAPI reported `version="0.1.0"` in `/docs` and `/openapi.json`.
- 55 documentation links used editor-style `path:line` form, which does not resolve on GitHub. Converted to `#L` anchors.
- Removed the obsolete `version:` key from `docker-compose.yml`.

### Known limitations

Documented in [README](README.md#known-limitations). In summary: AEAM does not remediate; RAG does not run below `HIGH` severity; the evaluation model's fourth criterion is structurally unreachable so most investigations escalate; autonomous detection is off by default; single-tenant by declaration.

### Security

- RS256 JWT with optional OIDC federation (JWKS, PKCE).
- Fail-closed startup: non-development environments abort without real key material; half-configured OIDC aborts in every environment.
- Deny-by-default RBAC with longest-prefix endpoint mapping.
- `ENVIRONMENT=development` bypasses all authentication — intentional, documented, and the single most consequential setting.

---

[1.0.0]: https://github.com/<your-org>/aeam/releases/tag/v1.0.0
