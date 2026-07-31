# AEAM v1.0.0 — Release Notes

**Released:** 2026-08-01
**Status:** General availability
**License:** MIT

---

## What AEAM is

An intelligence platform that investigates business anomalies the way a senior analyst would — with memory, policy, evidence, and an audit trail.

A metric moves. AEAM detects it deterministically, investigates across six independent evidence sources, produces a priority-ordered plan with an explanation of *why*, withholds anything consequential behind a human approval gate, and persists the entire causal chain as one replayable record.

**One boundary, stated up front:** AEAM diagnoses and notifies. It does not remediate. Every executable action is safe and reversible — a Jira ticket, a Slack message, an email report, a local diagnostic snapshot, a monitoring flag.

---

## Architecture

A **modular monolith**: one FastAPI process holding every agent as an ordinary Python object, wired once in a single composition root. Three stores — PostgreSQL (22 tables), Redis, Qdrant (two collections). One React 18 console with 17 pages.

Not microservices, deliberately. The investigation loop is synchronous and evidence-dense: one incident touches seven components each reading state the others produced. Distributing that replaces function calls with network hops and turns a stack-local context into a distributed transaction, for no throughput gain at target volume.

**Four constitutional invariants**, each enforced structurally rather than by convention:

| Invariant | Enforcement |
|---|---|
| One coordinator | The Supervisor imports nothing that could dispatch |
| Honesty over capability | Absence, insufficiency and measured zero are distinct in every response |
| Deterministic before probabilistic | The LLM cannot trigger, override, or outrank |
| Advisory evidence | Findings never re-enter the decision path |

---

## Enterprise features

| Domain | Capability |
|---|---|
| **Identity** | RS256 JWT; optional OIDC federation via JWKS with PKCE. AEAM validates tokens; it never issues enterprise credentials. |
| **Authorization** | Deny-by-default RBAC, longest-prefix endpoint mapping. |
| **Governance** | Multi-tier approval chains with per-severity and policy-driven overrides; parameters stored verbatim so an approval executes exactly what was withheld. |
| **Audit** | Dual-sink (file + `audit_logs`), hash-chained. |
| **Knowledge governance** | Policy lifecycle, memory expunge/correct, every curation attributed. |
| **Compliance** | Tenancy, data-classification and PII postures declared and served at `/api/v1/system/compliance`. |
| **DR** | Per-store backup posture with a rehearsable drill that refuses to restore over its own source. |
| **Performance** | Budgets in `performance_budgets.json`, CI-gated. |

---

## RAG

Six composable stages, each independently flag-gated, each falling back to the stage beneath it on construction failure — retrieval degrades, it never breaks startup.

```
dense (Qdrant) + BM25 → RRF fusion → multi-query expansion
  → cross-encoder rerank → evidence diversity → business relevance
```

Then two validation gates: a sensitive-pattern guardrail before the response is parsed or persisted, and **grounding validation** requiring every cited cause to reference a chunk that was actually retrieved. A cause the model invented fails validation and the pass is recorded as failed — visibly, in the evidence panel.

RRF was chosen over score blending because cosine and BM25 scores live on incompatible scales requiring per-corpus calibration; RRF uses rank only.

---

## Planning

`ExecutionPlanningEngine` synthesises every accumulated finding into one plan — no retrieval, no detection, no LLM call.

Evidence priority: `policy > memory > cross_dataset > adaptive > retrieval > runbook`, ordered by how binding the evidence is. When only lower-priority evidence exists, the plan says so explicitly rather than presenting generic guidance as evidence-derived.

Approval is forced when evidence quality is `insufficient`/`low` or when conflicts are detected.

---

## Actions

Circuit breaker (3 failures → open 60 s) → idempotency (Redis, 24 h) → retry with exponential backoff and jitter, with configuration and validation errors failing fast → `action_logs` row carrying duration, retry count, failure reason and validation result.

Notifications are **never** gated: withholding the Slack alert would suppress the message telling a reviewer an approval is waiting. Everything else is gated when the plan requires approval, and unknown steps default to gated.

---

## Replay

Read-only reconstruction from the persisted audit trail. It imports no detector, agent or LLM, so it cannot re-execute — replaying a thousand times leaves the database bit-identical.

Recorded order is the order. Absence is reported as an explicit gap naming the phase that introduced the stage. Time is measured or absent, and the remainder between measured stage time and the measured total is disclosed as unattributed rather than distributed.

---

## Observability

Prometheus metrics, optional OpenTelemetry tracing with one root span per investigation, per-incident cost attribution (tokens, retrieval volume, action outcomes), heartbeat supervision for both background threads, and a mesh-health score that publishes its own formula.

Design rule: a signal that degrades *quality* (BM25 staleness, Qdrant reachability, LLM posture) is disclosed but never flips overall status. A signal that indicates *unavailability* does.

---

## Connector framework

Eight connectors — SharePoint, Confluence, GitHub, Google Drive, SAP, Salesforce, Snowflake, BigQuery — with one contract and no per-connector branching in the sync engine.

Connector content does not travel a connector path; it travels the **upload** path. Same validator, blob store, dedup, job, worker, chunker, embeddings and collection. After ingestion a SharePoint page is indistinguishable from an uploaded PDF except for its provenance row.

Three independent idempotency layers. All eight default off. `CONNECTOR_MOCK_MODE` runs a full honest sync before any credential exists.

---

## Hardening

A formal review triaged **22 issues** by category, severity, fix risk and dependency. All Critical and High are resolved.

Highlights:

- A `NameError` in the composition root, swallowed by a broad handler, had silently disabled every metrics connector while health reported them enabled.
- `/health` reported `database: "ok"` without querying the database — the check sat inside a `try` whose body could not raise.
- Incident reports were emailed to a hardcoded third-party address whenever SMTP was configured.
- A race between BM25 in-place refresh and live search, with a reachable `IndexError`.
- SQLite had no busy timeout, so AEAM's own threads hit immediate `database is locked` — this also resolved a pre-existing failing test.
- Depth-≥3 LLM reasoning overwrote grounded, chunk-cited root causes unconditionally.

Full triage: [TECHNICAL_REVIEW_BOARD.md](TECHNICAL_REVIEW_BOARD.md) · Full list: [CHANGELOG.md](CHANGELOG.md)

---

## Testing

| Suite | Result |
|---|---|
| Backend (pytest) | **1,613 passed**, 1 skipped, 0 failed |
| Frontend (Vitest) | **116 passed**, 0 failed |
| **Total** | **1,729 passing** |

Verified additionally at runtime against a live deployment: startup, health, orchestration, RAG, enterprise memory, retrieval, actions (Jira, Slack, email), persistence, human review, replay, dashboard, and analytics.

---

## Statistics

| Metric | Value |
|---|---|
| Backend source (excl. tests) | ~54,000 LOC |
| Test code | ~27,000 LOC |
| Frontend source | ~15,400 LOC |
| API routers | 18 |
| Alembic revisions | 12 |
| Settings fields | 149 |
| Console pages | 17 |
| Roster agents | 8 |
| Enterprise connectors | 8 |

---

## Upgrading

First release — no upgrade path required.

```bash
git clone https://github.com/<your-org>/aeam.git && cd aeam && cp .env.example .env
```

The copied file boots as-is in development.

---

## Deployment checklist

Four preconditions before a non-development deployment. The platform enforces three itself — it refuses to start rather than degrade silently.

| # | Requirement | Enforced by |
|---|---|---|
| 1 | `ENVIRONMENT` is not `development` | Operator — this is the one AEAM cannot check for you |
| 2 | `JWT_PUBLIC_KEY` or `JWT_PUBLIC_KEY_PATH` set | Startup aborts without it |
| 3 | `INCIDENT_REPORT_RECIPIENTS` set if email is wanted | Step skips and records the reason |
| 4 | `ENABLE_MONITOR_AGENT=true` if autonomous detection is wanted | Defaults off; nothing is detected without it |

> `ENVIRONMENT=development` **bypasses all authentication, RBAC and rate limiting.** Every RBAC statement in the documentation is unenforced in that posture.

---

## Known limitations

1. **AEAM does not remediate.** No action modifies your production systems.
2. **RAG does not run below `HIGH` severity** — a deliberate cost decision. Single-signal autonomous detections are `MEDIUM` and receive no document evidence.
3. **Most investigations escalate.** `STOP` requires confidence strictly above 0.8 and the fourth scoring criterion is structurally unreachable. This errs toward human oversight; changing it is a product decision.
4. **No autonomous detection by default.**
5. **Similarity is not always available** — lexically-retrieved chunks report `n/a` rather than a misleading `0%`.
6. **Startup knowledge bypasses the registry**, so Knowledge Center may show `0 documents` while retrieval works.
7. **`decisions` table is unused** — decisions live inside `incidents.findings`.
8. **Forecast-vs-actual charting is unavailable** — no metric-history endpoint exists.
9. **Single-tenant by declaration** — no tenant discriminator exists in any store.

---

## Not in this release

Explicitly out of scope, not implemented, and not partially built:

- Autonomous remediation with rollback semantics
- Multi-tenancy with per-tenant isolation
- Metric-history API for forecast-vs-actual charting
- Streaming ingestion
- LLM providers beyond Groq
- Horizontal scale-out

---

## Documentation

| Document | Covers |
|---|---|
| [README.md](README.md) | Overview, install, deploy, quick start |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Style, invariants, layers, startup, concurrency, trade-offs |
| [SYSTEM_FLOW.md](SYSTEM_FLOW.md) | One investigation end to end |
| [AGENT_REFERENCE.md](AGENT_REFERENCE.md) | Every agent's contract and failure mode |
| [RAG_PIPELINE.md](RAG_PIPELINE.md) | Retrieval stages and validation gates |
| [ACTION_PIPELINE.md](ACTION_PIPELINE.md) | Runbooks, gating, approval, execution |
| [CONNECTORS.md](CONNECTORS.md) | Framework, idempotency, isolation, mock mode |
| [DEMO_GUIDE.md](DEMO_GUIDE.md) | 10-minute demo and 30-minute deep dive |
| [INTERVIEW_GUIDE.md](INTERVIEW_GUIDE.md) | Design rationale and trade-offs |
| [RUNTIME_ARCHITECTURE.md](RUNTIME_ARCHITECTURE.md) | Line-level implementation trace |
| [TECHNICAL_REVIEW_BOARD.md](TECHNICAL_REVIEW_BOARD.md) | Full defect triage |
| [docs/](docs/) | 18 operational and governance documents |

---

## Acknowledgements

Built on FastAPI, Qdrant, Prophet, sentence-transformers, React and Vite.

---

**Full changelog:** [CHANGELOG.md](CHANGELOG.md)
