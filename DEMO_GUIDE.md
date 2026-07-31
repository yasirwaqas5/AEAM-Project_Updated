# AEAM — Demo Guide

> Two runnable demos: a 10-minute walkthrough and a 30-minute deep dive. Every output below was produced by this system.

---

## Project overview (30 seconds)

> "AEAM investigates business anomalies the way a senior analyst would. When a metric moves, it asks six questions in parallel — has this happened before, is there a policy, did anything correlate, is it a spike or a shift, what do the runbooks say, and what should we do — then produces a plan with the reasoning attached and withholds anything consequential behind a human approval gate. It's a modular monolith: one FastAPI process, eight agents, three stores."

**The one-line differentiator:**

> "The interesting constraint isn't that it uses an LLM. It's that the LLM is never allowed to decide *whether* something is wrong, and every cause it cites has to trace back to a retrieved chunk or the response is rejected."

---

## Prerequisites

```bash
docker start aeam-postgres aeam-redis aeam-qdrant
```
```bash
uvicorn aeam.main:app --reload --port 8080
```
```bash
cd frontend && npm run dev
```

**Pre-flight — run this before the audience is watching:**

```bash
curl -s localhost:8080/health
```

You want `"status":"healthy"` with `database: ok`, `qdrant: ok (2 collections)`, `llm: ok (provider=groq…)`. If `llm` says `mock`, reasoning will be a stub — set `LLM_ENABLED=true`, `USE_MOCK_LLM=false`, `LLM_API_KEY=…`.

**First boot downloads two transformer models (~100 MB).** Never demo on a cold start.

---

## Example datasets and documents

| Asset | Location | Role |
|---|---|---|
| `startup_runbook.md` | `aeam/knowledge/` | Auto-ingested every boot. The DB-latency and API-latency sections are what RAG cites. |
| `policy_latency.md` | upload via Knowledge Center | Gives the Policy Registry something to match |
| Any CSV with a date + numeric column | upload → **activate** in Data Center | Turns Cross-Dataset and Adaptive Detection from "no signal" into real evidence |

> **Registration is not activation.** A dataset only becomes a live KPI feed once activated in Data Center.

---

## 10-minute demo

### 1 · Dashboard (90 s)

Open **http://localhost:5173**.

- **Live Architecture** — the mesh, with each node's real state. Point out that Monitor reads `disabled` and *says so* rather than pretending.
- **AI Health** — hover the formula. "Unweighted mean of 8 computable components." Not a vanity number.
- **StatusBar** — every dependency probed, including Qdrant and the LLM.

> "Everything on this screen is measured. Where it can't measure, it says 'n/a' instead of showing zero."

### 2 · Trigger an investigation (60 s)

Go to **Trigger**, or:

```bash
curl -X POST http://localhost:8080/api/v1/trigger/ -H 'Content-Type: application/json' -d '{"event_type":"DB_LATENCY","metric":"checkout_db_latency_ms","value":950,"severity":"HIGH","metadata":{"service":"checkout"}}'
```

> "Note it takes about six seconds and the HTTP call doesn't return until it's done. That's deliberate — an immediate 202 with work still pending would hide the latency."

**Use `HIGH` or `CRITICAL`.** `MEDIUM`/`LOW` skip RAG by design.

### 3 · Investigation Workspace (3 min) — *the centrepiece*

Open **Investigation** → newest incident.

- **Causal chain** — Trigger → Detection → Investigation → RAG Decision → Retrieved Evidence → Validation → Confidence → Recommended Action → Human Review → Execution.
- **Evidence tab** — five chunks, each with source, business relevance, and *why it ranked there* in plain language.
- **Plan & Why tab** — the confidence breakdown. Point at a contradiction if one fired:

> "It found two candidate causes 0.1 apart and flagged that as ambiguous — then reduced its own confidence from 0.85 to 0.50 because of it. It argued against itself."

- **Quality tab** — ten components, its own weaknesses listed.

### 4 · Human Review (2 min)

Open **Human Review**.

> "Slack and Jira went out. Diagnostics and monitoring were withheld. The parameters are stored verbatim, so approving runs exactly that call — never a re-planned one. And notifications are never gated, because withholding the alert would suppress the message telling you an approval is waiting."

Approve one. Show the verdict appearing in history with attribution.

### 5 · Close (90 s)

Open **Replay** → step through stages.

> "This is reconstructed from the persisted audit trail, not re-executed. Replaying a thousand times leaves the database bit-identical. Stages that didn't exist when this incident ran are shown as explicit gaps rather than being invented."

---

## 30-minute deep dive

Everything above, plus:

### 6 · Retrieval Explorer (6 min)

Open **Retrieval Explorer** → pick the incident.

Walk the stage funnel: **Query expansion (4 variants) → Dense (16) → BM25 (20) → RRF (20) → Rerank (15) → Business relevance (5) → Selected (5)**.

> "Dense retrieval misses exact identifiers — a metric name, an error code. BM25 misses paraphrase. RRF fuses them by rank rather than score, so it needs no calibration between two incompatible scales."

Show **Where chunks were dropped**: survived every stage 5, dropped at fusion 4, by reranker 5, by diversity 10.

> "The diversity filter is why you don't see five near-identical chunks from one runbook section. Without it the model sees unanimous evidence that was never unanimous."

### 7 · Memory Center (4 min)

> "Every finalized incident is embedded into a second Qdrant collection and recalled as evidence for future investigations. The mesh compounds — investigation nine is better informed than investigation one."

Show the recall graph and hit rate. Then the honest part:

> "It remembers failures too. A failed investigation is still useful memory — it tells you what didn't work. The one exception is placeholder-derived output, which is quarantined so synthetic content can't poison future recalls."

### 8 · Knowledge & Data Center (4 min)

Upload a runbook. Watch the job progress through EXTRACTING → INDEXING → DONE. Show policies extracted.

Then Data Center — connectors.

> "Eight enterprise connectors, all off by default. The architectural point is that connector content doesn't travel a connector path — it travels the upload path. Same validator, same blob store, same dedup, same worker, same collection. After ingestion, a SharePoint page is indistinguishable from an uploaded PDF except for its provenance row."

Optionally demo `CONNECTOR_MOCK_MODE=true` — a full honest sync with no credentials.

### 9 · Observability & Agents (4 min)

**Analytics** → Enterprise Observability. Every rate with its numerator and denominator.

> "Retrieval success 22%. That's not flattering, and it's real — most of these incidents had no matching documents. The platform reports it rather than hiding it."

**Agents** → Mesh Health, Supervisor.

> "The Supervisor observes and recommends. It can't coordinate — it imports no Orchestrator, no ActionAgent, no EventBus, and has no execute method. The single-coordinator rule is enforced by what it can't reach, not by what it declines to do."

### 10 · Code tour (6 min)

| File | Point |
|---|---|
| `aeam/main.py` | One composition root. Every dependency injected. Read the lifespan order — it *is* the dependency graph. |
| `orchestrator.py::_investigate` | Each evidence stage in its own try/except, appending one findings entry. Failure degrades a stage, never the investigation. |
| `evaluation_engine.py` | The `_CRITERIA` note: the fourth criterion is structurally unreachable, why that's a lifecycle consequence, and why fixing it is a product decision. |
| `hybrid_retrieval.py` | Snapshot-and-swap under a lock, with the exact race it prevents documented. |
| `supervisor_agent.py` | The import list *is* the enforcement. |

---

## Expected outputs

A real record from this deployment:

```
Decision            INVESTIGATE · confidence 0.90 · source: rule
Enterprise Memory   3 similar incidents (top similarity 0.577)              266 ms
Policy Registry     0 matches                                                 9 ms
Cross-Dataset       0 activated datasets                                     10 ms
Adaptive Detection  insufficient history (0 points, 10 required)              7 ms
Knowledge Retrieval 5 chunks · validation PASSED · 12 causes cited        5,447 ms
KPI Analysis        -50.0% vs expected                                       11 ms
Evaluation          STOP (score 0.90)
Execution Plan      3 recommendations · evidence: medium · approval required
Explainability      1 contradiction · confidence 0.85 → 0.50
AI Evaluation       quality 0.3449 across 10 components
Actions             jira ✓  slack ✓  email skipped (no recipients)
─────────────────────────────────────────────────────────────────────────
RESOLVED · "Inefficient queries" · source rag · 5.75 s · 2 LLM calls
```

**"Insufficient history" and "0 matches" are the demo working correctly.** They demonstrate the honesty contract. Do not apologise for them — explain them.

---

## Talking points

| Moment | Say this |
|---|---|
| Investigation completes | "Six evidence sources, each isolated. If cross-dataset throws, the investigation continues and the record says cross-dataset failed." |
| Contradiction shown | "It argued against itself and reduced its own confidence. Most systems would report 0.85 and move on." |
| Actions withheld | "Safe isn't the same as 'an operator is content for it to happen unasked'." |
| RAG shows n/a similarity | "That chunk came in lexically, so no cosine exists. Showing 0% would imply it matched nothing — it was cited by the model at 80% confidence." |
| Escalated, not resolved | "Reaching STOP needs confidence above 0.8. Most investigations escalate. That errs toward human oversight, which is the safe direction." |

---

## Questions interviewers ask

**"Is this just RAG with extra steps?"**
> No. RAG is one of six evidence sources and it's the only one that touches an LLM. Detection is entirely deterministic — rules and statistics decide whether something is wrong. The LLM can't trigger an investigation, can't override a rule, and can't outrank a chunk-cited cause. Take the LLM away and you still get detection, correlation, adaptive baselining, planning and gating.

**"What happens when the LLM hallucinates?"**
> Two gates. A guardrail scans the raw response before it's parsed or persisted. Then a grounding validator requires every cited cause to reference a chunk that was actually retrieved — a cause the model invented fails validation and the whole pass is recorded as failed, visibly, in the evidence panel.

**"Why a monolith?"**
> The investigation loop is synchronous and evidence-dense. One incident touches seven components each reading state the others produced. Distributing that replaces function calls with network hops and turns a stack-local context into a distributed transaction, for no throughput gain at this volume. The modularity that matters is enforced by composition — injected dependencies, no cross-agent reach-through.

**"How do you know it works?"**
> 1,729 tests. But more usefully: the platform reports its own quality. AI health is 40%, retrieval success 22%, resolution rate 11%. Those are unflattering and real. A system that reported 95% on this corpus would be lying.

**"What would you fix first?"**
> RAG doesn't run below HIGH severity, and since severity comes from signal count, a single-signal autonomous detection gets no document evidence. It's a defensible cost decision but the interaction wasn't designed — it emerged. It's documented in `decision_engine.py` with the consequence spelled out.

**"Is it production-ready?"**
> The codebase is. Deployment needs four things: `ENVIRONMENT` not development, real JWT key material, configured email recipients, and `ENABLE_MONITOR_AGENT` if you want autonomous detection. Three of those four the platform enforces itself — it refuses to start rather than degrade silently.

---

## Demo failure modes

| Symptom | Cause | Fix |
|---|---|---|
| Investigation has no evidence | Severity was `MEDIUM`/`LOW` | Use `HIGH` |
| Everything escalates | Needs confidence > 0.8 | Expected — explain it |
| `email` skipped | `INCIDENT_REPORT_RECIPIENTS` empty | Deliberate fail-closed |
| RAG returns nothing | Qdrant down or corpus empty | `docker start aeam-qdrant`; upload docs |
| Knowledge Center shows 0 documents | Startup runbook bypasses the registry | Upload one to populate it |
| Slow first request | Model download | Warm up before demoing |

---

## Reset between demos

```bash
docker exec aeam-postgres psql -U postgres -c "TRUNCATE incidents, incident_approvals, review_verdicts, action_logs CASCADE;"
```

> Leaves Qdrant intact so retrieval still works. Only do this if you want a clean incident list — the existing history makes Enterprise Memory more interesting to show.
