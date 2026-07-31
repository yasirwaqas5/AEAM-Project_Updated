<div align="center">

# AEAM

### Autonomous Enterprise AI Agent Mesh

**An intelligence platform that investigates business anomalies the way a senior analyst would — with memory, policy, evidence, and an audit trail.**

[![Version](https://img.shields.io/badge/version-1.0.0-0b7285)](RELEASE_NOTES_v1.0.0.md)
[![Python](https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-modular%20monolith-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![React](https://img.shields.io/badge/React-18%20%2B%20Vite-61DAFB?logo=react&logoColor=black)](https://react.dev/)
[![Tests](https://img.shields.io/badge/tests-1%2C729%20passing-2f9e44)](#test-status)
[![Docker](https://img.shields.io/badge/docker-compose%20ready-2496ED?logo=docker&logoColor=white)](#docker-deployment)
[![License](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

</div>

---

## Project Overview

AEAM detects an anomaly in a business metric, investigates it across six independent evidence sources, produces a priority-ordered execution plan with an explanation of *why*, withholds anything consequential behind a human approval gate, and persists the entire causal chain as one replayable record.

It is a **modular monolith**: one FastAPI process, one React console, three stores (PostgreSQL, Redis, Qdrant). Every agent is a class in that process, coordinated by a single Orchestrator. There is no message broker, no microservice fleet, and no hidden scheduler.

The system is built around one uncomfortable commitment: **it would rather say "I don't know" than produce a confident answer it cannot ground.** Every number in the console is measured or explicitly marked unavailable. That principle shaped more of this codebase than any other.

> **AEAM diagnoses and notifies. It does not remediate.**
> Every action it can execute is safe and reversible — a Jira ticket, a Slack message, an email report, a local diagnostic snapshot, a monitoring flag. "Investigation Resolved" means the investigation concluded, not that your database was repaired. This is a deliberate boundary, not a missing feature.

---

## Why AEAM Exists

When a business metric moves the wrong way, the expensive part is never the detection. It is the forty minutes an analyst then spends asking:

- Has this happened before, and what did we do about it?
- Is there a policy that governs this metric?
- Did anything else move at the same time?
- Is this a spike or a sustained shift?
- What do our runbooks say?
- What should we actually do, and who has to approve it?

Those six questions are the six evidence stages of an AEAM investigation. The platform runs all of them while the analyst is still reading the alert, cites its sources, and hands over a plan with the reasoning attached.

**What makes this different from "an LLM with a vector database":**

| Concern | AEAM's position |
|---|---|
| Detection | **Deterministic first.** Rules and statistics decide *whether* something is wrong. The LLM never triggers an investigation. |
| Grounding | Every cited cause must trace to a retrieved chunk, or validation fails the response. |
| Precedence | A chunk-cited RAG cause outranks free-text LLM reasoning. The least-validated writer never wins. |
| Absence | "Not consulted", "insufficient data", and "measured zero" are three different states, and the API distinguishes all three. |
| Authority | The Supervisor Agent observes the mesh and **cannot** coordinate — enforced by what it is unable to import. |

---

## Key Features

| Capability | What it actually does |
|---|---|
| **Deterministic detection** | YAML-governed rules + Z-score/percentile statistics + Prophet forecast deviation. Optional changepoint and seasonal-hybrid detectors. |
| **Six-source investigation** | Enterprise Memory, Policy Registry, Cross-Dataset correlation, Business Graph, Adaptive Detection, and document RAG — each isolated, each advisory. |
| **Hybrid RAG** | Dense (Qdrant) + BM25 fused by Reciprocal Rank Fusion, multi-query expansion, cross-encoder reranking, evidence-diversity filtering, business-relevance ranking. Six composable stages, each independently flag-gated. |
| **Enterprise Memory** | Every finalized incident is embedded into a dedicated Qdrant collection and recalled as evidence for future investigations. The mesh compounds. |
| **Execution planning** | Priority-ordered recommendations (policy > memory > cross-dataset > adaptive > retrieval > runbook) with conflict detection and evidence-quality grading. |
| **Explainability** | A decision graph, evidence graph, recommendation trace, confidence breakdown, contradictions, missing evidence, and stated assumptions — per incident. |
| **AI self-evaluation** | Each investigation is scored across ten transparent quality components with its formula published. |
| **Human-in-the-loop** | Consequential steps are withheld behind a configurable multi-tier approval chain. Notifications are never gated. |
| **Timeline Replay** | Reconstructs any investigation stage-by-stage from the persisted audit trail. Read-only — it never re-executes. |
| **Connector framework** | Eight enterprise connectors (SharePoint, Confluence, GitHub, Google Drive, SAP, Salesforce, Snowflake, BigQuery) funnelling into the same ingestion path as a manual upload. |
| **Observability** | Prometheus metrics, optional OpenTelemetry tracing, per-incident cost attribution, heartbeat supervision, and a mesh-health score with a published formula. |

---

## Architecture Overview

```mermaid
graph TB
    subgraph EXT["External Systems"]
        SHEETS["Google Sheets"]
        UPSTREAM["SharePoint · Confluence · GitHub<br/>Drive · SAP · Salesforce<br/>Snowflake · BigQuery"]
        SLACK["Slack"]
        JIRA["Jira"]
        SMTP["SMTP"]
        LLM["Groq LLM"]
    end

    subgraph PROC["AEAM — single FastAPI process"]
        API["18 API routers<br/>+ /health · /metrics · SPA"]
        SEC["SecurityMiddleware<br/>JWT · RBAC · rate limit · audit"]
        BUS{{"EventBus<br/>synchronous, in-process"}}
        ORCH["<b>Orchestrator</b><br/>single coordinator"]

        subgraph DETECT["Detection"]
            RULES["RuleEngine (YAML)"]
            STATS["StatisticalDetector"]
            FC["ForecastAgent (Prophet)"]
        end

        subgraph EVID["Evidence — advisory, isolated"]
            MEM["Enterprise Memory"]
            POL["Policy Registry"]
            CROSS["Cross-Dataset"]
            GRAPH["Business Graph"]
            ADAPT["Adaptive Detection"]
            RAG["RAG Agent"]
            KPI["KPI Agent"]
        end

        subgraph DECIDE["Synthesis & Governance"]
            PLAN["Planning Agent"]
            EXPL["Explainability"]
            EVAL["AI Evaluation"]
            GATE["Human Approval Gate"]
            ACT["Action Agent"]
        end

        WORKERS["Background threads:<br/>MonitorAgent · IngestionWorker"]
        SUP["Supervisor Agent<br/>(read-only oversight)"]
    end

    subgraph STORES["Stores"]
        PG[("PostgreSQL<br/>22 tables")]
        RD[("Redis")]
        QD[("Qdrant<br/>documents + memories")]
        BLOB[("BlobStore<br/>local | S3")]
    end

    SHEETS --> WORKERS
    UPSTREAM --> API
    API --> SEC --> BUS
    WORKERS --> DETECT --> BUS
    BUS --> ORCH
    ORCH --> EVID
    ORCH --> DECIDE
    RAG --> QD
    MEM --> QD
    ACT --> SLACK & JIRA & SMTP
    RAG --> LLM
    DECIDE --> PG
    EVID --> PG
    WORKERS --> BLOB
    SEC --> RD
    SUP -.observes.-> PROC
```

**Full detail:** [ARCHITECTURE.md](ARCHITECTURE.md) · [RUNTIME_ARCHITECTURE.md](RUNTIME_ARCHITECTURE.md) (line-level implementation trace)

---

## System Workflow

```mermaid
sequenceDiagram
    participant S as Signal
    participant B as EventBus
    participant O as Orchestrator
    participant E as Evidence Stages
    participant P as Planning
    participant G as Approval Gate
    participant A as Action Agent
    participant D as Persistence

    S->>B: Event (trigger or MonitorAgent)
    B->>O: handle_event (synchronous)
    O->>O: DecisionEngine — severity routing
    O->>E: memory → policy → cross-dataset → graph → adaptive → RAG → KPI
    E-->>O: findings[] (each advisory, each isolated)
    O->>O: EvaluationEngine — STOP / CONTINUE / ESCALATE
    Note over O: loops to depth 5 max
    O->>P: synthesize execution plan
    P->>O: plan + explainability + quality score
    O->>G: human_approval_required?
    G-->>A: notifications (never gated)
    G--xA: consequential steps withheld
    A->>D: action_logs
    O->>D: incidents row + Qdrant memory + approval record
```

**Full detail:** [SYSTEM_FLOW.md](SYSTEM_FLOW.md)

---

## Multi-Agent Architecture

Eight agents form the roster; seven engines support them. One coordinator, by constitutional rule.

```mermaid
graph LR
    ORCH["<b>Orchestrator</b><br/>the only coordinator"]

    MON["Monitor Agent"] -->|events| ORCH
    ORCH --> KPI["KPI Agent"]
    ORCH --> RAGA["RAG Agent"]
    ORCH --> PLANA["Planning Agent"]
    ORCH --> REP["Report Agent"]
    ORCH --> ACTA["Action Agent"]
    MON --> FCA["Forecast Agent"]
    SUPA["Supervisor Agent"] -.->|observes only| ORCH

    style ORCH fill:#0b7285,color:#fff
    style SUPA fill:#495057,color:#fff
```

| Agent | Role | Runs |
|---|---|---|
| **Orchestrator** | Drives the incident lifecycle; the single coordinator | Per event |
| **Monitor Agent** | Autonomous KPI polling and anomaly detection | Daemon thread (flag-gated) |
| **KPI Agent** | Grounded statistical characterisation of *what* changed | Every investigation depth |
| **Forecast Agent** | Prophet forecast + deviation detection | Inside monitor cycles |
| **RAG Agent** | Document retrieval and chunk-cited causal hypotheses | HIGH/CRITICAL severity |
| **Planning Agent** | Synthesises all evidence into one execution plan | Per finalization |
| **Report Agent** | Human-readable investigation report | Per finalization |
| **Action Agent** | The sole component permitted to call external APIs | Per approved action |
| **Supervisor Agent** | Read-only mesh oversight; advisory only | On read |

**Full detail:** [AGENT_REFERENCE.md](AGENT_REFERENCE.md)

---

## Retrieval Pipeline

Six composable stages. Each is independently flag-gated and falls back to the previous stage on construction failure — retrieval degrades, it never breaks startup.

```mermaid
graph LR
    Q["Query<br/>formulation"] --> MQ["Multi-Query<br/>expansion"]
    MQ --> DENSE["Dense<br/>Qdrant"]
    MQ --> BM["BM25<br/>lexical"]
    DENSE --> RRF["RRF<br/>fusion"]
    BM --> RRF
    RRF --> RERANK["Cross-encoder<br/>rerank"]
    RERANK --> DIV["Evidence<br/>diversity"]
    DIV --> BIZ["Business<br/>relevance"]
    BIZ --> OUT["Top-K<br/>to LLM"]

    style OUT fill:#2f9e44,color:#fff
```

Then: strict prompt assembly (retrieved chunks only) → LLM → **guardrail validation** → JSON parse → **grounding validation** (every cited cause must reference a retrieved chunk) → structured findings.

**Full detail:** [RAG_PIPELINE.md](RAG_PIPELINE.md)

---

## Enterprise Connector Framework

Eight connectors, one contract, zero per-connector branching in the sync engine.

```mermaid
graph LR
    subgraph DOCS["Document connectors"]
        SP["SharePoint"]
        CF["Confluence"]
        GH["GitHub"]
        GW["Google Drive"]
    end
    subgraph METRICS["Metric connectors"]
        SAP["SAP"]
        SF["Salesforce"]
        SNOW["Snowflake"]
        BQ["BigQuery"]
    end

    DOCS --> SYNC["ConnectorSyncEngine"]
    SYNC --> SUB["IngestionSubmitter<br/><i>same entry point as upload</i>"]
    SUB --> WORKER["IngestionWorker"]
    WORKER --> QD[("Qdrant")]
    METRICS --> COMP["CompositeKPISource"]
    COMP --> MON["MonitorAgent"]

    style SUB fill:#0b7285,color:#fff
```

**The architectural point:** connector content does not travel a connector path — it travels the upload path. Same validator, same blob store, same content-addressed dedup, same job, same worker, same chunker, same embeddings, same collection. After ingestion a SharePoint page is indistinguishable from an uploaded PDF except for its provenance row.

All eight default **off**. `CONNECTOR_MOCK_MODE=true` runs a full, honest sync against deterministic in-repo fixtures before any credential exists.

**Full detail:** [CONNECTORS.md](CONNECTORS.md)

---

## Folder Structure

```
AEAM/
├── aeam/                          # Backend — FastAPI modular monolith
│   ├── main.py                    # Composition root: all wiring, lifespan, health
│   ├── agents/                    # The agent mesh
│   │   ├── orchestrator/          # Coordinator, decision/evaluation engines, runbooks
│   │   ├── monitor/               # Autonomous KPI polling loop
│   │   ├── kpi/                   # Rule engine, statistical + advanced detectors
│   │   ├── forecast/              # Prophet models, backtesting harness
│   │   ├── rag/                   # 6-stage retrieval pipeline, validators, chunking
│   │   ├── action/                # Slack, Jira, email, webhook, diagnostics handlers
│   │   ├── planning/ supervisor/  # Phase F6 formalized agents
│   │   ├── policy/ learning/      # Compiled rules, confidence calibration
│   │   └── report/                # Human-readable investigation reports
│   ├── api/                       # 18 routers
│   ├── intelligence/              # Cross-dataset, adaptive, graph, explainability, eval
│   ├── memory/                    # Short-term (per-incident) + long-term + enterprise
│   ├── ingestion/                 # Upload → blob → job → worker → index
│   ├── connectors/                # 8 enterprise connectors + registry + sync engine
│   ├── integrations/              # Postgres, Redis, Qdrant, embeddings, secrets
│   ├── security/                  # JWT, RBAC, rate limiting, audit, guardrails
│   ├── governance/                # Human-in-the-loop approval service
│   ├── monitoring/                # Prometheus metrics, tracing, heartbeats
│   ├── config/                    # Settings (149 fields) + detection_rules.yaml
│   └── tests/                     # 1,613 backend tests
├── frontend/                      # React 18 + Vite console (17 pages)
│   └── src/{pages,components,layout,lib,config}
├── migrations/                    # 12 Alembic revisions — the schema truth
├── deploy/                        # cloudrun.yaml + env.yaml (canonical var manifest)
├── docs/                          # 18 operational/governance documents
├── scripts/                       # DR drill, Qdrant checks, runbook ingestion
└── knowledge.md CONSTITUTION.md ROADMAP.md
```

---

## Technology Stack

| Layer | Technology |
|---|---|
| **API** | FastAPI, Uvicorn, Pydantic v2 (`BaseSettings`, `extra="forbid"`) |
| **Relational** | PostgreSQL 15 · SQLAlchemy 2 · Alembic (12 revisions) |
| **Vector** | Qdrant — `aeam_documents` + `aeam_incident_memories` |
| **Cache / coordination** | Redis — dedup windows, idempotency, rate limits, dataset activation |
| **Embeddings** | `sentence-transformers` — `all-MiniLM-L6-v2` (384-d) |
| **Reranking** | `cross-encoder/ms-marco-MiniLM-L-6-v2` |
| **Forecasting** | Prophet |
| **LLM** | Groq (`llama-3.1-8b-instant` default, configurable) |
| **Observability** | Prometheus · OpenTelemetry (optional) |
| **Frontend** | React 18 · Vite · React Router · React Three Fiber |
| **Testing** | pytest (1,613) · Vitest (116) |
| **Storage** | Local disk or any S3-compatible endpoint |

---

## Installation

**Prerequisites:** Python 3.11+, Node 18+, Docker.

```bash
git clone https://github.com/<your-org>/aeam.git && cd aeam
```

```bash
cp .env.example .env
```

> The copied file boots as-is in development. Fill in `LLM_API_KEY` to enable real reasoning; everything else has a working default.

```bash
python -m venv .venv && .venv/Scripts/activate && pip install -r requirements.txt
```

```bash
cd frontend && npm install && cd ..
```

---

## Environment Variables

149 settings, all declared in [`aeam/config/settings.py`](aeam/config/settings.py). Canonical manifest: [`deploy/env.yaml`](deploy/env.yaml).

**Required — no defaults, startup fails without them:**

| Variable | Example |
|---|---|
| `DATABASE_URL` | `postgresql://postgres:secret@localhost:5432/postgres` |
| `REDIS_URL` | `redis://localhost:6379/0` |
| `VECTOR_DB_URL` | `http://localhost:6333` |
| `ENVIRONMENT` | `development` \| `staging` \| `production` \| `test` |

**The flags that decide whether anything happens:**

| Variable | Default | Effect |
|---|---|---|
| `ENABLE_MONITOR_AGENT` | `false` | **Nothing is detected autonomously without this.** Sole gate, no environment backdoor. |
| `ENVIRONMENT` | — | `development` **bypasses all authentication, RBAC and rate limiting**. |
| `LLM_ENABLED` / `USE_MOCK_LLM` | `false` / `true` | Both must be set for real LLM calls. `groq` is the only implemented provider. |
| `SLACK_BOT_TOKEN` | `""` | **No Action Agent exists without it** — every action records as skipped. |
| `INCIDENT_REPORT_RECIPIENTS` | `""` | Empty means no incident email is sent. Fail-closed by design. |
| `HUMAN_APPROVAL_ENFORCED` | `true` | Withholds consequential steps. Deliberately *not* editable via the admin API. |
| `BUSINESS_GRAPH_ENABLED` | `false` | Graph participates in investigations. |
| `CONNECTORS_ENABLED` | `false` | Master switch for all eight connectors. |
| `OIDC_ENABLED` | `false` | Enterprise SSO. Half-configured ⇒ startup aborts, in every environment. |

---

## Running Locally

```bash
docker start aeam-postgres aeam-redis aeam-qdrant
```
```bash
uvicorn aeam.main:app --reload --port 8080
```
```bash
cd frontend && npm run dev
```

Console: **http://localhost:5173** · API docs: **http://localhost:8080/docs**

> First boot downloads two transformer models (~100 MB). Subsequent starts are fast.

**Trigger your first investigation:**

```bash
curl -X POST http://localhost:8080/api/v1/trigger/ -H 'Content-Type: application/json' -d '{"event_type":"DB_LATENCY","metric":"api_latency_ms","value":950,"severity":"HIGH"}'
```

> Use `HIGH` or `CRITICAL`. `MEDIUM`/`LOW` deliberately skip RAG — see [Known Limitations](#known-limitations).

---

## Docker Deployment

```bash
POSTGRES_PASSWORD=<choose-one> docker compose up --build
```

Brings up Postgres, Redis, Qdrant and AEAM together. The compose file sets a **production** posture by default (`ENVIRONMENT=production`, `USE_MOCK_LLM=true`, `HUMAN_APPROVAL_ENFORCED=true`), so a JWT public key is required — this is intentional fail-closed behaviour.

```bash
docker compose ps && curl -s localhost:8080/health
```

---

## Cloud Deployment

[`deploy/cloudrun.yaml`](deploy/cloudrun.yaml) is a complete, annotated Cloud Run service definition.

**Ephemeral-compute requirements** (Cloud Run recycles instances):

| Setting | Value | Why |
|---|---|---|
| `BLOB_STORAGE_BACKEND` | `s3` | Local disk evaporates on recycle |
| `FORECAST_MODEL_DIR` | durable mount | Avoids retraining every instance |
| `CONFIG_PERSISTENCE_MODE` | `ephemeral` | Admin config writes do not survive |
| `ENVIRONMENT` | `production` | Enables authentication |

Health endpoints: `/health` (dependency-checked, 200/503) and `/metrics` (Prometheus).

> **Probe guidance:** use `/health` as a **readiness** probe. It returns 503 when the database is unreachable or a background worker's heartbeat is stale — correct for de-routing, aggressive for restarts.

---

## Screenshots

> Replace these placeholders with real captures before publishing.

| View | Placeholder |
|---|---|
| Dashboard — live mesh + AI health | `docs/images/dashboard.png` |
| Investigation Workspace — causal chain | `docs/images/investigation.png` |
| Retrieval Explorer — stage-by-stage trace | `docs/images/retrieval-explorer.png` |
| Timeline Replay — measured durations | `docs/images/replay.png` |
| Human Review — approval queue | `docs/images/human-review.png` |
| Demo GIF | `docs/images/demo.gif` |

---

## Example Investigation

A real, unedited record from this deployment (`hardening_probe_2`, DB_LATENCY, HIGH):

```
Decision            INVESTIGATE · confidence 0.90 · source: rule
Enterprise Memory   3 similar incidents recalled (top similarity 0.577)     266 ms
Policy Registry     0 matches                                                 9 ms
Cross-Dataset       0 activated datasets to correlate against                10 ms
Adaptive Detection  insufficient history (0 points, 10 required)              7 ms
Knowledge Retrieval 5 chunks · validation PASSED · 12 causes cited        5,447 ms
KPI Analysis        -50.0% vs expected · no detector breach                  11 ms
Evaluation          STOP (score 0.90) — root cause + evidence + confidence
Execution Plan      3 recommendations · evidence: medium · approval required  1 ms
Explainability      1 contradiction flagged · confidence 0.85 → 0.50         1 ms
AI Evaluation       quality 0.3449 across 10 components                       0 ms
Human Approval      2 steps withheld (diagnostics, monitoring)
Actions             jira ✓  slack ✓  email skipped (no recipients configured)
─────────────────────────────────────────────────────────────────────────────
Status  RESOLVED   Root cause  "Inefficient queries"   Source  rag
Total   5.75 s     Cost  2 LLM calls · 2,106 tokens · 5 retrieval chunks
```

Note what the platform did **not** do: it did not claim the database was fixed, it did not execute the diagnostics snapshot without approval, and it flagged its own evidence conflict rather than hiding it.

---

## Example Action Flow

```mermaid
graph TD
    FIN["finalize_incident"] --> PLAN{"human_approval<br/>_required?"}
    PLAN -->|no| ALL["Execute all runbook steps"]
    PLAN -->|yes| SPLIT["Split by gating class"]

    SPLIT --> NOTIFY["<b>Never gated</b><br/>jira · slack · email"]
    SPLIT --> HELD["<b>Withheld</b><br/>diagnostics · monitoring<br/>webhook · sheets"]

    NOTIFY --> EXEC["ActionAgent.execute<br/>circuit breaker → idempotency<br/>→ retry → action_logs"]
    HELD --> REC["Recorded verbatim in<br/>incident_approvals.pending_actions"]

    REC --> REVIEW["Human Review queue"]
    REVIEW -->|approve| RELEASE["Chain satisfied →<br/>same ActionAgent, recorded params"]
    REVIEW -->|reject| HALT["Chain halted permanently"]

    style NOTIFY fill:#2f9e44,color:#fff
    style HELD fill:#e8590c,color:#fff
```

**Informing humans is never gated** — withholding the Slack alert would suppress the very message telling a reviewer that an approval is waiting.

**Full detail:** [ACTION_PIPELINE.md](ACTION_PIPELINE.md)

---

## Enterprise Capabilities

| Domain | Implementation |
|---|---|
| **Identity** | RS256 JWT; optional OIDC federation (JWKS, PKCE). AEAM validates tokens; it never issues enterprise credentials. |
| **Authorization** | Role-based, longest-prefix endpoint mapping, deny-by-default. |
| **Audit** | Dual-sink (file + `audit_logs` table), hash-chained entries. |
| **Governance** | Multi-tier approval chains with per-severity and policy-driven overrides. |
| **Knowledge governance** | Policy lifecycle, memory expunge/correct, every curation attributed. |
| **Data classification** | Tenancy, classification and PII postures declared and served at `/api/v1/system/compliance`. |
| **DR** | Per-store backup posture with a rehearsable drill (`scripts/dr_drill.py`). |
| **Performance** | Budgets in `aeam/tests/fixtures/performance_budgets.json`, CI-gated. |
| **Supply chain** | Certification pack re-verified by tests on every run. |

---

## Roadmap Completion

| Phase | Scope | Status |
|---|---|---|
| **1–9** | Core mesh, orchestration, RAG, actions, security, hardening | ✅ |
| **B1.1–B1.8** | Enterprise data layer, ingestion, dataset intelligence, activation | ✅ |
| **C1–C7** | Memory, policy intelligence & registry, cross-dataset, adaptive detection, advanced retrieval, execution planning | ✅ |
| **D1–D5** | Explainability, AI evaluation, observability, configuration, administration | ✅ |
| **E1–E13** | Truth hygiene, concurrency, security, storage, migrations, scale, autonomous ops, AI governance, human-in-the-loop, console, observability, knowledge governance, certification | ✅ |
| **F1–F7** | Detection uplift, learning/calibration, policy compilation, business graph, replay, agent mesh, connector framework | ✅ |
| **Hardening** | 22 triaged defects; all Critical and High resolved | ✅ |

Full history: [ROADMAP.md](ROADMAP.md) · Governing principles: [CONSTITUTION.md](CONSTITUTION.md)

---

## Known Limitations

Stated plainly, because the alternative is someone discovering them in a demo.

1. **AEAM does not remediate.** It diagnoses, notifies, and files tickets. No action modifies your production systems.
2. **RAG does not run below `HIGH` severity.** `DecisionEngine` routes only `CRITICAL`/`HIGH` to retrieval. Since severity is derived from signal count, a single-signal autonomous detection is `MEDIUM` and receives no document evidence. A deliberate cost decision, documented in code.
3. **`investigation_success_rate` is structurally low.** `STOP` requires confidence strictly above 0.8; the fourth scoring criterion (`action_taken`) is unreachable because actions run after evaluation. Most investigations escalate to a human. This errs toward more oversight, and changing it is a product decision, not a bug fix.
4. **No autonomous detection by default.** `ENABLE_MONITOR_AGENT=false`. Events enter via `POST /api/v1/trigger` until you enable it and activate a dataset.
5. **Similarity is not always available.** Chunks retrieved lexically, or whose cosine is dropped during multi-query fusion, report `similarity n/a` rather than a fake `0%`.
6. **Startup knowledge bypasses the registry.** `aeam/knowledge/*.md` is embedded directly into Qdrant, so the Knowledge Center may show `0 documents` while retrieval works.
7. **`decisions` table is unused.** Created by schema and migration; decisions live inside `incidents.findings`.
8. **Forecast-vs-actual charting is unavailable.** No per-incident forecast history endpoint exists.
9. **Single-tenant by declaration.** No tenant discriminator exists in any store. Isolation is achieved by deploying separately.

---

## Future Work

Explicitly **out of scope for v1.0.0** and not implemented:

- Autonomous remediation with rollback semantics
- Multi-tenancy with per-tenant isolation
- A metric-history API enabling forecast-vs-actual charts
- Streaming/event-driven ingestion (currently poll + explicit sync)
- Native multi-provider LLM support beyond Groq
- Horizontal scale-out (the modular monolith is deliberately single-process)

---

## Contributing

Contributions are welcome. Before opening a PR:

```bash
.venv/Scripts/python.exe -m pytest aeam/tests -q
```
```bash
cd frontend && npx vitest run && npm run build
```

**Conventions this codebase holds to:**

- **Honesty over capability.** Never report a number you did not measure. `"not consulted"`, `"insufficient data"` and a measured zero are three different states.
- **Additive change.** New response fields are additive; readers ignore unknown keys.
- **Flag-gated behaviour.** New behaviour ships behind a flag whose default preserves the current posture.
- **Document the trade-off at the decision site**, in the code, with the rationale attached.
- **One coordinator.** The Orchestrator is the only component that coordinates.

---

## License

Released under the [MIT License](LICENSE).

---

<div align="center">

**[Architecture](ARCHITECTURE.md)** · **[System Flow](SYSTEM_FLOW.md)** · **[Agents](AGENT_REFERENCE.md)** · **[RAG](RAG_PIPELINE.md)** · **[Actions](ACTION_PIPELINE.md)** · **[Connectors](CONNECTORS.md)** · **[Demo](DEMO_GUIDE.md)** · **[Interview Guide](INTERVIEW_GUIDE.md)**

</div>
