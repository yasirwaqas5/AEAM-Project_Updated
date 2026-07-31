# AEAM — Interview Guide

> Technical depth, design rationale, and the trade-offs behind each decision. Every claim here is verifiable in the code.

---

## 1. The 60-second architecture answer

> "AEAM is a modular monolith — one FastAPI process holding eight agents coordinated by a single Orchestrator, backed by PostgreSQL, Redis and Qdrant.
>
> An event enters one of two ways: a Monitor thread polling KPI feeds, or an HTTP trigger. The Orchestrator allocates a per-incident context and runs six evidence stages — enterprise memory, policy matching, cross-dataset correlation, business-graph traversal, adaptive baselining, and document RAG — each isolated so one failure degrades that stage only. An evaluation loop decides whether to keep investigating, up to depth five. At finalization it synthesises an execution plan, explains it, scores its own quality, withholds consequential actions behind a human approval gate, executes the rest, and persists the whole chain as one replayable record.
>
> The constraint that shaped everything: detection is deterministic. The LLM never decides *whether* something is wrong — only helps explain *why*, and only from retrieved documents it must cite."

---

## 2. Agent responsibilities

| Agent | One-line answer |
|---|---|
| **Orchestrator** | The only coordinator. Drives the lifecycle, owns no detection logic, writes no SQL directly, calls no external API. |
| **Monitor** | The only autonomous loop. Polls KPI sources, runs deterministic detection, publishes events. |
| **KPI** | Characterises *what* changed — deviation, persistence, trend — from real history. Never asserts *why*. |
| **Forecast** | Prophet forecast and deviation detection, inside monitor cycles only. |
| **RAG** | Retrieves documents and produces chunk-cited causal hypotheses. |
| **Planning** | Synthesises all evidence into one priority-ordered plan. |
| **Report** | Human-readable summary. |
| **Action** | The sole component permitted to call external APIs. |
| **Supervisor** | Observes the mesh. Cannot coordinate — enforced by its imports. |

**Follow-up you should expect: "Why is the Supervisor not a second orchestrator?"**

> Because it structurally can't be. It imports no Orchestrator, ActionAgent, PlanningAgent, EventBus, RuleEngine or LLM client, and has no `handle_event`, `execute`, `dispatch`, `coordinate`, `restart` or `plan` method. Its only inputs are two read-only telemetry callables and its only output is a report. The single-coordinator invariant is preserved by what it cannot reach, not by what it declines to do. That distinction matters — a convention can be violated by the next contributor; an import graph can't be, accidentally.

---

## 3. How an investigation works

```
handle_event
  ├─ allocate IncidentContext (own ShortTermMemory + FSM)  ← reentrancy
  ├─ DecisionEngine.decide → severity routing
  ├─ evidence stages, each once, each isolated:
  │    memory → policy → cross_dataset → graph → adaptive → RAG → KPI
  ├─ EvaluationEngine → STOP | CONTINUE (recurse) | ESCALATE (depth 5)
  └─ finalize:
       plan → explain → score → approval gate → actions → persist → remember
```

**Key design point to volunteer:** all per-incident state lives on a stack-local context, so `handle_event` is fully reentrant. The Monitor thread and N HTTP triggers can run concurrently without cross-contamination. Shared collaborators are shared deliberately — they're read-only or individually thread-safe. Only per-incident state was ever the hazard.

---

## 4. How RAG works

Six composable stages, each independently flag-gated, each falling back to the stage beneath it on construction failure:

```
query formulation (deterministic)
  → multi-query expansion (LLM)
    → dense (Qdrant) + BM25 → RRF fusion
      → cross-encoder rerank (top 20 → top k)
        → evidence diversity (dedup + per-doc cap)
          → business relevance ranking
            → strict prompt → LLM → guardrail → parse → grounding validation
```

**Expect: "Why hybrid retrieval?"**

> Dense embeddings miss exact identifiers — a metric named `sales_f1_e2e`, an error code, a service name. Those are the highest-signal tokens in an operational corpus and they're precisely what cosine similarity smooths away. BM25 nails them and misses paraphrase. You need both.

**Expect: "Why RRF and not weighted score blending?"**

> Cosine similarity and BM25 scores live on incompatible scales with different distributions, so blending them requires per-corpus calibration that drifts as the corpus grows. RRF uses only rank position — `1/(k + rank)` — so it needs no calibration and can't be destabilised when one retriever's score distribution shifts. It's the boring choice, which is why it's the right one.

**Expect: "Why rerank if you already fused?"**

> A bi-encoder embeds the query and document independently, so it never sees them together. A cross-encoder does, and is substantially more accurate — at a cost that makes it impossible to run over a corpus. So it runs over 20 fused candidates. That's the whole trade: retrieve broadly and cheaply, rank precisely and expensively, on a small set.

**Expect: "Why the diversity filter?"**

> Without it, five near-identical chunks from one runbook section crowd out a contradicting chunk from a different document. The model then sees unanimous evidence that was never unanimous. The filter drops near-duplicates by Jaccard overlap and caps chunks per source document.

---

## 5. How planning works

`ExecutionPlanningEngine` synthesises every accumulated finding into one plan. No retrieval, no detection, no LLM call — pure synthesis over evidence that already exists.

**Evidence priority:** `policy > memory > cross_dataset > adaptive > retrieval > runbook`

**Expect: "Why that order?"**

> It's ordered by how binding the evidence is. A matched enterprise policy is a rule someone wrote down and approved — that outranks a statistical correlation. Memory is precedent: what actually happened last time. Cross-dataset and adaptive are measurements. Retrieval is documentary. Standard runbook guidance is the floor — it applies when nothing specific was found, and the plan says so explicitly rather than presenting generic advice as though it were derived from evidence.

The plan carries `evidence_quality`, detected conflicts, a confidence figure, and `human_approval_required` — which is forced when quality is `insufficient`/`low` or when conflicts exist.

---

## 6. How actions work

**The boundary, stated first:** every executable action is safe and reversible. Jira ticket, Slack message, email report, local diagnostic snapshot, local monitoring flag. AEAM does not remediate.

```
ActionAgent.execute
  → registry lookup
  → circuit breaker (3 failures → open 60s)
  → idempotency check (Redis, 24h, keyed on incident+type+params)
  → retry with exponential backoff + jitter
      (config/validation errors fail fast — retrying an invalid payload is waste)
  → action_logs row: duration, retries, failure reason, validation result
```

**Expect: "Why is 'safe' not enough to skip approval?"**

> Because safe and "an operator is content for it to happen without being asked" are different properties. A diagnostics snapshot is reversible but it still touches system state. The split is stated once, in `NEVER_GATED_STEPS`, so the Orchestrator and the review API can't disagree about it — and unknown steps default to gated, which is the conservative direction.

**Expect: "Why aren't notifications gated?"**

> Withholding the Slack alert would suppress the very message telling a reviewer an approval is waiting. Gating the notification makes the gate self-defeating.

---

## 7. How replay works

`InvestigationReplayBuilder` and `TimelineBuilder` are read-only projections over one persisted incident row.

**Three honesty rules, each enforced by construction:**

1. **Recorded order is the order.** Stages are emitted in the sequence they appear in the findings array — never re-sorted into a canonical pipeline. A stage that ran twice appears twice, with its occurrence number.
2. **Absence is reported, never filled.** A stage the record doesn't contain becomes an explicit gap naming the phase that introduced it — so an incident predating a stage reads as "no such entry was recorded", not a fabricated step.
3. **Time is measured or absent.** Durations come from `stage_durations`, measured at finalize. Where measured stage time doesn't sum to the measured total, the remainder is disclosed as unattributed rather than distributed across stages.

**Expect: "Why not re-execute?"**

> Because then it isn't replay, it's re-investigation — and it would produce a different answer, since the corpus, the models and the calibration all move. It also imports no detector, agent or LLM, so it *can't* re-execute. Replaying a thousand times leaves the database bit-identical.

---

## 8. Why deterministic detection precedes LLM reasoning

**The strongest answer:**

> Because an LLM that can trigger an investigation can hallucinate an incident. Detection answers a yes/no question about numbers — is this value anomalous — and that question has a correct answer computable from a rule and a z-score. Handing it to a probabilistic model adds no capability and introduces a failure mode where the system invents work.
>
> So the split is: deterministic components decide *whether*, the LLM helps explain *why*, and only from documents it must cite. Concretely, the LLM never triggers an investigation, never overrides a rule, and never outranks a chunk-cited cause.

**The precedence detail that shows depth:**

> Three components can write `root_cause`. RAG passes three gates — a sensitive-data guardrail, JSON parsing, and grounding validation against retrieved chunks. The depth-≥3 LLM reasoning pass passes only one. Originally the LLM path wrote unconditionally, so the *least*-validated writer won purely by running last, and could overwrite a chunk-cited cause with free text — or with the literal string "Unknown" if the key was missing. That's fixed: the write is now guarded on "nothing better is already there", mirroring the KPI Agent's rule, and the LLM's view is retained as its own advisory finding so nothing is lost.

---

## 9. Why Enterprise Memory exists

> Investigation nine should be better informed than investigation one. Every finalized incident is embedded into a dedicated Qdrant collection and recalled as evidence for future investigations — the mesh compounds.
>
> Two design choices worth defending. First, it remembers **failures too** — a failed investigation tells you what didn't work, which is genuinely useful evidence, not noise. Second, it reuses the *same* pipeline classes as document RAG, pointed at a second collection. Same embedding model, same Qdrant client, different namespace. Composition, not duplication.
>
> The one exclusion: placeholder-derived output is quarantined. Synthetic content is not organizational knowledge, and remembering it would poison future recalls with fabricated precedent.

---

## 10. Why explainability exists

> Because a recommendation nobody can interrogate is a recommendation nobody should act on — and this system asks humans to approve things.
>
> Per incident it produces a decision graph (which recommendation came from which evidence item), an evidence graph, a recommendation trace in plain language, a confidence breakdown per source, detected contradictions, missing evidence, and stated assumptions.
>
> The part I'd point at: it flags contradictions **against itself**. On a real incident it found two candidate causes 0.1 apart, marked that as ambiguous causation, and reduced its own confidence from 0.85 to 0.50 as a result — recorded with the reason. A system that only reports what supports its conclusion isn't explainable, it's persuasive.

---

## 11. Constitution principles

Four rules, each enforced structurally rather than by convention.

| Principle | Enforcement | Consequence you can point to |
|---|---|---|
| **One coordinator** | Supervisor's import graph | It literally cannot dispatch |
| **Honesty over capability** | Absence ≠ zero, everywhere | `/health` probes; `similarity n/a` not `0%`; `not recorded` not `0.0` |
| **Deterministic before probabilistic** | Routing + precedence | LLM can't trigger, override, or outrank |
| **Advisory evidence** | Findings never re-enter decision path | Memory/policy/graph inform; they don't decide |

---

## 12. Engineering decisions and trade-offs

| Decision | Chose | Over | Because |
|---|---|---|---|
| Deployment | Modular monolith | Microservices | Evidence-dense synchronous loop; distribution buys nothing at this volume and costs a distributed transaction |
| Event bus | Synchronous in-process | Kafka / RabbitMQ | `POST /trigger` returning after completion is honest; a queue hides latency behind an ack |
| Fusion | RRF | Score blending | No calibration needed between incompatible scales |
| Memory | 2nd Qdrant collection | 2nd store / shared collection | Same model, same client, separate namespace |
| Config defaults | Engine-owned, `None` = unconfigured | Duplicated in Settings | The literal lives once; admin API imports it |
| Extra env vars | `extra="forbid"` | `ignore` | A typo'd variable must fail loudly, not silently no-op |
| Rule adoption | Restart-applied | Live reload | Reuses the documented D4 posture rather than adding a second dynamic-config path in detection |
| Approval settings | Not admin-editable | Editable | A gate one API call can disable isn't a governance control |
| SQLite | busy_timeout + WAL | Leave default | AEAM's own threads contend; default timeout is zero |

---

## 13. Questions that probe for weakness

**"Your resolution rate is 11%. Isn't that bad?"**

> It's honest, and it's structural rather than accidental. Reaching STOP requires confidence strictly above 0.8, and the fourth scoring criterion — `action_taken` — is unreachable because actions execute after evaluation in the lifecycle. So the achievable maximum is 0.9 and most investigations escalate to a human.
>
> I chose not to "fix" that. Every available remedy — re-weighting the criteria, lowering the threshold — trades human oversight for throughput and invalidates any fitted confidence calibration. It's a product decision about auto-resolution, not a bug fix. It's documented in `evaluation_engine.py` with the full consequence spelled out for whoever makes that call.

**"You have known bugs in your repo. Why?"**

> I ran a formal review and triaged 22 issues by severity, fix risk and dependency. Everything Critical and High is fixed. What remains is documented with a reason: either it's intentional design, or fixing it requires a product decision I shouldn't make unilaterally. I'd rather ship a known, documented limitation than an undocumented surprise.

**"What broke in production that you didn't expect?"**

> A missing import in the composition root. `SourceRepository` was called but never imported, and a broad `except Exception` around the connector-composition block swallowed the `NameError` into a log line — so every metrics connector was silently dropped while the health endpoint reported them enabled. A shipped feature was inoperative and the platform said the opposite.
>
> The fix was one import. The lesson was the handler: broad exception catching around *construction* code absorbs programming errors, not just upstream failures. Those handlers now re-raise `NameError`, `AttributeError`, `ImportError` and `TypeError` — none of which is ever an upstream failure.

**"How do you test a system with this much nondeterminism?"**

> By separating the deterministic core from the probabilistic edge. Rules, statistics, planning, explainability, evaluation, replay and gating are all pure functions over data — 1,613 backend tests cover them directly. The LLM boundary is tested through its contracts: does a failed call preserve the real provider error, does a permanent error skip retries, does grounding validation reject an uncited cause. I don't assert on model output; I assert on what happens around it.

**"What would you build next?"**

> Nothing in the roadmap — it's feature-complete and frozen. If I had to pick one thing: a metric-history endpoint. It's the missing piece behind forecast-vs-actual charting, and its absence is currently disclosed in the UI as unavailable rather than faked. That's the right behaviour, but it's a gap.

---

## 14. Numbers worth memorising

| Metric | Value |
|---|---|
| Backend source (excl. tests) | ~54,000 LOC |
| Test code | ~27,000 LOC |
| Frontend source | ~15,400 LOC |
| Tests | 1,613 backend + 116 frontend = **1,729** |
| API routers | 18 |
| Alembic revisions | 12 |
| Settings fields | 149 |
| Console pages | 17 |
| Roster agents | 8 |
| Enterprise connectors | 8 |
| Retrieval stages | 6 |
| Evidence sources per investigation | 6 |

---

## 15. Closing statement

> "The thing I'd want you to take away is the honesty constraint. It's easy to build a system that always has an answer. This one distinguishes 'not consulted' from 'insufficient data' from 'measured zero' in every API response, probes its dependencies instead of assuming them, publishes the formula behind every score, and reports a 22% retrieval success rate rather than a flattering one.
>
> That constraint cost real effort — it's the reason `/health` runs a query instead of returning `ok`, and the reason a chunk with no cosine says `n/a` instead of `0%`. But it's what makes the output trustworthy enough to hand a human and ask them to approve it."
