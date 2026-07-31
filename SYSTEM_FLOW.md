# AEAM — System Flow

> One complete investigation, from signal to persisted record. Every stage below exists in the implementation.

---

## 1. The two entry points

There are exactly two ways an event enters AEAM. There is no scheduler and no third path.

```mermaid
graph LR
    subgraph A["Entry A — autonomous"]
        SRC["CompositeKPISource<br/>Sheets + activated datasets<br/>+ metric connectors"] --> MON["MonitorAgent<br/>every MONITOR_INTERVAL_SECONDS"]
        MON --> DET["RuleEngine + StatisticalDetector<br/>+ Forecast (+ changepoint, seasonal)"]
        DET --> DEDUP{"EventDeduplicator"}
        DEDUP -->|new| PUB1["publish"]
        DEDUP -->|duplicate| DROP["suppressed"]
    end
    subgraph B["Entry B — manual"]
        HTTP["POST /api/v1/trigger/"] --> VAL["Pydantic validation"] --> PUB2["publish"]
    end
    PUB1 --> BUS{{"EventBus"}}
    PUB2 --> BUS
    BUS --> ORCH["Orchestrator.handle_event"]

    style DROP fill:#868e96,color:#fff
```

**Entry A** requires `ENABLE_MONITOR_AGENT=true` *and* a live KPI source. Off by default.
**Entry B** is always available. `expected_value` is set to `value * 2` — a placeholder baseline used only for manual triggers, never in real detection paths.

**Dispatch is synchronous.** `EventBus.publish` calls handlers on the caller's thread, so `POST /trigger` returns only after the entire investigation and all actions have completed. That is deliberate: an immediate `202` with work still pending would hide latency behind an acknowledgement.

---

## 2. Full investigation lifecycle

```mermaid
stateDiagram-v2
    [*] --> EVENT_RECEIVED: handle_event allocates IncidentContext
    EVENT_RECEIVED --> INVESTIGATING: FSM transition
    INVESTIGATING --> DECIDING: DecisionEngine.decide

    DECIDING --> Finalize: decision == STOP
    DECIDING --> Evidence: decision == INVESTIGATE

    state Evidence {
        [*] --> Memory
        Memory --> Policy
        Policy --> CrossDataset
        CrossDataset --> Graph
        Graph --> Adaptive
        Adaptive --> RAG: only if "RAG" in agents
        RAG --> KPI
        KPI --> LLMReasoning: only if depth>=3 and LLM_ENABLED
        LLMReasoning --> [*]
    }

    Evidence --> Evaluate: EvaluationEngine.evaluate
    Evaluate --> INVESTIGATING: CONTINUE (depth+1)
    Evaluate --> Finalize: STOP
    Evaluate --> Escalate: depth >= MAX_INVESTIGATION_DEPTH
    Escalate --> Finalize: requires_human = true

    Finalize --> COMPLETE
    COMPLETE --> [*]
```

---

## 3. Stage-by-stage

### 3.1 Decision — deterministic routing

```
DecisionEngine.apply_priority_rules(event)
  CRITICAL → INVESTIGATE · agents [KPI, RAG] · confidence 0.95
  HIGH     → INVESTIGATE · agents [KPI, RAG] · confidence 0.90
  else     → INVESTIGATE · agents [KPI]      · confidence 0.70

if confidence >= 0.9  → return immediately (no LLM)
elif LLM_ENABLED and depth > 2 → LLM augmentation
else → rule decision stands
```

**Two consequences worth knowing.** RAG is reachable only at `CRITICAL`/`HIGH`; since `MonitorAgent` assigns `HIGH` only at ≥2 detection signals, a single-signal autonomous detection is `MEDIUM` and receives no document evidence. And because `CRITICAL`/`HIGH` short-circuit at ≥0.9, the DecisionEngine's own LLM path is reachable *only* for the severities where RAG is skipped. Both are documented in `decision_engine.py`.

### 3.2 Evidence stages — advisory, isolated, once per incident

Each runs at most once per incident (guarded by a `_has_*_finding` check), each in its own `try/except`, each appending exactly one findings entry. A stage that fails records a structured failure and the investigation continues.

| Stage | Reads | Produces |
|---|---|---|
| **Memory** | Qdrant `aeam_incident_memories` | Similar resolved incidents + outcomes |
| **Policy** | `policies` table (+ embeddings) | Matched enterprise policies |
| **Cross-Dataset** | Activated datasets via `DatasetKPISource` | Pearson correlations: supporting / contradicting |
| **Graph** | `graph_nodes` / `graph_edges` | Known relationships within a traversal budget |
| **Adaptive** | `metrics` history + event metadata | Longer-horizon baseline, seasonality |
| **RAG** | Qdrant `aeam_documents` | Chunk-cited possible causes |
| **KPI** | `metrics` history + detector metadata | Deviation, persistence, trend, detectors fired |

All three of Memory, Policy and RAG use the **same query formulation** (`RAGAgent._formulate_query`) so they search on identical vocabulary.

### 3.3 Root-cause precedence

Three components may write `root_cause`. The rule is precedence, not recency:

```mermaid
graph TD
    KPI["KPI Agent<br/>statistical characterisation"] -->|"writes only if unset"| RC{{"root_cause"}}
    RAG["RAG Agent<br/>chunk-cited, guardrail-checked,<br/>grounding-validated"] -->|"always wins"| RC
    LLM["LLM reasoning (depth>=3)<br/>parse-checked only"] -->|"writes only if unset"| RC

    style RAG fill:#2f9e44,color:#fff
    style LLM fill:#e8590c,color:#fff
```

`root_cause_source` records which one won (`rag` | `llm_reasoning` | `kpi_analysis`). The KPI Agent never asserts causation — it states measured fact ("X is 50% below expected") and defers to any real explanation.

### 3.4 Evaluation — when to stop

```
score  = 0.4  root_cause present
       + 0.3  >= 3 evidence items
       + 0.2  confidence strictly > 0.8
       + 0.1  action_taken  ← structurally unreachable (actions run after evaluation)

depth >= MAX_INVESTIGATION_DEPTH (5) → ESCALATE   (overrides score)
score >= 0.8                          → STOP
otherwise                             → CONTINUE (recurse)
```

**The achievable maximum is 0.9, not 1.0.** Reaching `STOP` therefore requires confidence above 0.8, which is why most investigations escalate to a human. This errs toward oversight; changing it is a product decision, documented in `evaluation_engine.py`.

### 3.5 Finalization

```mermaid
graph TD
    START["FSM → COMPLETE"] --> CAL["Confidence calibration<br/>(if enabled; raw always retained)"]
    CAL --> PLAN["Execution planning<br/>priority: policy > memory > cross-dataset<br/>> adaptive > retrieval > runbook"]
    PLAN --> EXPL["Explainability<br/>decision graph · contradictions · assumptions"]
    EXPL --> AIEVAL["AI Evaluation<br/>10 quality components"]
    AIEVAL --> GATE{"Approval gate<br/>active?"}
    GATE -->|yes| HOLD["Withhold gated steps<br/>params recorded verbatim"]
    GATE -->|no| RUN["Execute all steps"]
    HOLD --> NOTIFY["Notifications execute anyway"]
    RUN --> NOTIFY
    NOTIFY --> REPORT["ReportAgent → email (if recipients set)"]
    REPORT --> AUDIT["Append audit_summary"]
    AUDIT --> W1[("incidents row")]
    W1 --> W2[("incident_approvals, if gated")]
    W2 --> W3[("Qdrant memory<br/>placeholder-sourced quarantined")]
```

---

## 4. Every write one investigation performs

| # | Store | Table / collection | Condition |
|---|---|---|---|
| 1 | PostgreSQL | `metrics` | Monitor path only |
| 2 | PostgreSQL | `forecast_backtests` | `FORECAST_BACKTEST_ENABLED` |
| 3 | Redis | dedup key | Monitor path only |
| 4 | Redis | idempotency key (24 h) | Per action |
| 5 | PostgreSQL | `action_logs` | Per action attempt |
| 6 | PostgreSQL | `incidents` | Always, at finalize |
| 7 | PostgreSQL | `incident_approvals` | Gate active |
| 8 | Qdrant | `aeam_incident_memories` | Unless placeholder-sourced |
| 9 | PostgreSQL + file | `audit_logs` | Non-development requests |

`decisions` is created by the schema and **never written** — decisions live inside `incidents.findings`.

---

## 5. Human approval flow

```mermaid
sequenceDiagram
    participant O as Orchestrator
    participant G as HumanReviewService
    participant DB as incident_approvals
    participant R as Reviewer
    participant A as ActionAgent

    O->>G: gate active? (service wired ∧ enforced ∧ plan requires approval)
    G->>G: resolve chain — policy roles > severity override > default
    O->>DB: record pending_actions with params VERBATIM
    O->>A: notifications only (never gated)
    R->>G: POST /review/incidents/{id}/approve
    G->>G: advance tier
    alt chain satisfied
        G->>A: execute recorded params — same agent, same call
        A->>DB: action_logs
    else more tiers remain
        G->>DB: awaiting next tier
    end
    R->>G: POST .../reject → chain halted permanently
```

Parameters are stored **verbatim** so an approval later executes exactly the withheld call — never a re-derived or re-planned one.

---

## 6. Replay flow

```mermaid
graph LR
    INC[("incidents.findings")] --> B["InvestigationReplayBuilder"]
    B --> STAGES["Ordered stage sequence<br/>recorded order preserved"]
    B --> GAPS["Explicit gaps<br/>'introduced in Phase Fx'"]
    INC --> T["TimelineBuilder"]
    T --> DUR["Measured durations only"]
    T --> UNATTR["Unattributed remainder<br/>disclosed, never distributed"]

    style B fill:#0b7285,color:#fff
```

**Replay reconstructs; it never re-executes.** The module imports no detector, no agent, no LLM. Replaying an incident a thousand times leaves the database bit-identical.

Three honesty rules: recorded order *is* the order (a stage recorded twice appears twice); absence is reported as a gap, never filled; time is measured or absent, and the remainder between measured stage time and the measured total is disclosed as unattributed.

---

## 7. Enterprise Memory flow

```mermaid
graph LR
    FIN["finalize_incident"] --> Q{"root_cause_source<br/>== placeholder?"}
    Q -->|yes| SKIP["Quarantined — logged, not stored"]
    Q -->|no| EMB["Embed summary"] --> QD[("aeam_incident_memories")]
    QD --> RECALL["recall_similar_incidents<br/>next investigation"]
    RECALL --> EV["memory finding"]
    EV --> PLAN2["Execution planning"] & EX2["Explainability"] & AE2["AI Evaluation"]

    style SKIP fill:#c92a2a,color:#fff
```

Every incident is remembered **regardless of outcome** — a failed investigation is still useful memory. The one exception is placeholder-derived output, quarantined so synthetic content never poisons future recalls.

---

## 8. Ingestion flow

```mermaid
graph LR
    UP["POST /ingest/upload"] --> V["validate_upload"]
    SYNC["Connector sync"] --> SUB
    V --> SUB["IngestionSubmitter"]
    SUB --> BLOB[("BlobStore<br/>content-addressed")]
    SUB --> JOB[("ingestion_jobs<br/>QUEUED")]
    JOB --> W["IngestionWorker<br/>polls every 2s"]
    W --> ROUTE{"parent type"}
    ROUTE -->|document| DOC["extract → chunk → embed → Qdrant<br/>→ BM25 refresh → policy extraction"]
    ROUTE -->|dataset| DS["read → infer schema → register"]
    DOC --> IDX[("aeam_documents")]
    DS --> REG[("datasets + schemas")]
    REG -.->|"explicit activation required"| MON["MonitorAgent"]

    style SUB fill:#0b7285,color:#fff
```

**Registration is not activation.** A dataset becomes a live KPI feed only when its id is added to the Redis activation set via `POST /api/v1/data-center/datasets/{id}/activate`.

---

## 9. Frontend data flow

| Surface | Source | Refresh |
|---|---|---|
| StatusBar / TopBar | `/health` + `/api/v1/system/status` | 15 s |
| Dashboard | `/system/status`, `/metrics`, `/observability/`, `/incidents/` | 30 s |
| Knowledge Center | `/knowledge/*`, `/ingest/jobs` | 15 s |
| Every intelligence panel | `incidents.findings`, parsed client-side | on mount |
| Replay timeline | `/replay/{id}/timeline` | on mount |
| Mesh health | `/mesh/health` | on mount |

**The console's investigation detail is one `GET /api/v1/incidents/` fetch.** Evidence, Policy, Cross-Dataset, Execution Plan, Explainability, AI Evaluation and Memory panels all derive from that single response. `audit_summary` is the contract they depend on.
