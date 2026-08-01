# AEAM — Interview Walkthrough & Question Bank

> Sections 8 and 9 of the demonstration package. Companion to [PRESENTATION_GUIDE.md](PRESENTATION_GUIDE.md).
> Every answer below is grounded in the actual implementation. Where something is partial, mocked, or local-only, the answer says so.

---

# Part 1 · "Walk me through your project"

## The 5-minute answer

"AEAM is an autonomous investigation platform for business anomalies.

The problem is narrow. Every company with metrics has solved detection — Datadog, Grafana, Prometheus all work. What nobody has solved is the forty minutes after the alert, when a human pulls up prior incidents, checks policies, opens other dashboards, and searches the wiki for a runbook. That work is mechanical, it's the slowest part of incident response, and it scales with headcount rather than compute.

Architecturally it's a modular monolith — one FastAPI process holding eight agents, backed by Postgres, Redis and Qdrant, with a React console.

The flow is: an event enters either from a Monitor Agent polling KPI feeds, or from an HTTP trigger. A single Orchestrator picks it up and runs six independent evidence stages — enterprise memory of past incidents, policy matching, cross-dataset correlation, adaptive statistical baselines, business-graph traversal, and hybrid RAG over runbooks. An evaluation loop decides whether it has enough to stop, up to five levels of depth. At the end it synthesises a priority-ordered plan, explains why each recommendation exists, scores its own quality, withholds consequential actions behind a human approval gate, and persists the whole chain as one replayable record.

Three decisions I'd defend. First, detection is deterministic — the language model never decides whether something is wrong, only helps explain why. Second, grounding is enforced: every cited cause must trace to a chunk that was actually retrieved, or the response is rejected. Third, honesty over capability — 'not consulted', 'insufficient data' and 'measured zero' are three distinct states in every API response.

It's about 54,000 lines of Python with 1,729 tests passing. It runs locally — I haven't deployed it, though the Docker and Cloud Run configs are written."

## The 10-minute answer

Everything above, then:

"Let me go deeper on the three parts I think are most interesting.

**The retrieval pipeline.** It's six composable stages, each independently switchable, each falling back to the stage beneath it if construction fails — so retrieval degrades but never breaks startup. Dense vector search over Qdrant, BM25 lexical search, fused with Reciprocal Rank Fusion. Then multi-query expansion, cross-encoder reranking, an evidence-diversity filter, and business-relevance ranking.

Why hybrid: dense embeddings miss exact identifiers — a metric name like `checkout_db_latency_ms`, an error code, a service name. Those are the highest-signal tokens in an operational corpus and cosine similarity smooths them away. BM25 catches them exactly and misses paraphrase. Why RRF specifically: cosine and BM25 scores live on incompatible scales, so blending them needs per-corpus calibration that drifts. RRF uses rank position only.

Then two validation gates. A guardrail scans the raw response for sensitive patterns before it's parsed or persisted. Then a grounding validator requires every cited cause to reference a retrieved chunk — a cause the model invented fails validation and the whole pass is recorded as failed, visibly.

**The concurrency model.** Every investigation allocates a stack-local context holding its own short-term memory and state machine, so `handle_event` is fully reentrant. The Monitor thread and any number of HTTP request threads can run concurrently without cross-contamination. Shared collaborators are shared deliberately — they're read-only or individually thread-safe. Only per-incident state was ever the hazard.

There was a real race I found and fixed: the BM25 index is rebuilt in place by the ingestion thread while request threads search it. A reader could hold the old term-frequency list against a mid-rebuild document list and hit an IndexError. It's now snapshot-and-swap under a lock — build into locals, publish all seven parallel structures together, and search takes one consistent snapshot then scores outside the lock.

**The governance model.** Consequential actions are withheld behind a configurable multi-tier approval chain, and the parameters are stored verbatim so approving executes exactly the withheld call — never a re-derived one. Notifications are never gated, because withholding the Slack alert would suppress the message telling a reviewer an approval is waiting.

I'd also mention I ran a formal defect review near the end. Twenty-two issues triaged by category, severity, fix risk and dependency. Everything Critical and High is fixed; the rest is documented with the reason. Several were the system reporting incorrectly about itself — the health endpoint returned `database: ok` without ever querying the database, for instance. That's exactly the class of bug the architecture exists to prevent, so finding it in my own code was instructive."

## The 20-minute answer

Everything above, then work through these in whatever order the interviewer's questions pull you:

**1 · The agent mesh (3 min).** Eight roster agents. Orchestrator coordinates. Monitor is the only autonomous loop. KPI Agent characterises what changed and is structurally forbidden from asserting why. Forecast runs Prophet inside monitor cycles only. RAG produces chunk-cited hypotheses. Planning synthesises. Report writes prose. Action is the sole component allowed to call external APIs. Supervisor observes and cannot coordinate — enforced by its import graph, which contains no Orchestrator, no ActionAgent, no EventBus, and no execute method.

**2 · Root-cause precedence (2 min).** Three components can write the root cause. RAG passes three gates — guardrail, JSON parse, grounding validation. The depth-≥3 LLM reasoning pass passes only one. Originally the LLM path wrote unconditionally, so the *least*-validated writer won by running last, and could overwrite a chunk-cited cause with free text or the literal string "Unknown". Fixed: the write is guarded on "nothing better is already there", mirroring the KPI Agent's rule, and the LLM's view is retained as its own advisory finding.

**3 · The evaluation loop and its honest flaw (2 min).** Score = 0.4 for a root cause, 0.3 for ≥3 evidence items, 0.2 for confidence above 0.8, 0.1 for an action taken. The fourth is structurally unreachable — actions execute after evaluation in the lifecycle, so no correct implementation of the current ordering could satisfy it. Achievable maximum is 0.9, reaching STOP requires confidence above 0.8, and consequently most investigations escalate. I deliberately did not "fix" that: every remedy trades human oversight for throughput and invalidates any fitted confidence calibration. It's a product decision, documented in the code.

**4 · Data architecture (2 min).** `incidents.findings` is the product — a JSON array where every stage appends one entry. The console parses it client-side, replay reconstructs from it, observability aggregates it. Two DDL sources kept in lock-step: Alembic migrations as the production truth and a startup path for dev convenience, with a test asserting they agree.

**5 · Enterprise memory (2 min).** Every finalized incident is embedded into a second Qdrant collection and recalled as evidence. It reuses the same pipeline classes as document RAG pointed at a different collection — composition, not duplication. It remembers failures too. Placeholder-derived output is quarantined so synthetic content can't poison recalls.

**6 · Observability and cost (2 min).** Prometheus metrics, optional OTel tracing with one root span per investigation, thread-local per-incident cost attribution for tokens and retrieval volume, heartbeat supervision for both worker threads. Design rule: a signal degrading *quality* is disclosed but never flips overall status; a signal indicating *unavailability* does.

**7 · Testing (2 min).** 1,613 backend, 116 frontend. The approach: separate the deterministic core from the probabilistic edge. Rules, statistics, planning, explainability, evaluation, replay and gating are pure functions over data and tested directly. The LLM boundary is tested through contracts — does a failed call preserve the real provider error, does a permanent error skip retries, does grounding validation reject an uncited cause. I don't assert on model output.

**8 · What I'd do differently (3 min).** Be genuinely self-critical here — it's the highest-signal part of the answer. See Q97–Q100.

**9 · Limitations and deployment (2 min).** Not deployed. Doesn't remediate. Single-tenant. Connectors implemented but never run against a live tenant. One LLM provider.

---

# Part 2 · 100 Interview Questions

## A · Architecture & Design (1–14)

**Q1. Why a modular monolith instead of microservices?**
The investigation loop is synchronous and evidence-dense — one incident touches seven components each reading state the others produced. Distributing that replaces function calls with network hops and turns a stack-local context into a distributed transaction, for no throughput gain at this volume. The modularity that matters — clear seams, injected dependencies, no cross-agent reach-through — is enforced by composition instead. The honest cost is that it scales vertically only.

**Q2. When would you split it?**
When one component's resource profile diverges sharply from the rest. The embedding and reranking models are the obvious candidate — they're memory-heavy and CPU-bound while everything else is I/O-bound. I'd extract those into an inference service first, because that seam is already clean: everything goes through `EmbeddingService` and the reranker wrapper.

**Q3. What's the single most important architectural constraint?**
Deterministic detection. Rules and statistics decide whether something is wrong; the LLM only helps explain why, and only from documents it must cite. A model that can trigger an investigation can hallucinate an incident.

**Q4. How do you prevent agents becoming a distributed ball of mud?**
One coordinator, enforced structurally. Agents don't call each other — the Orchestrator calls them. The Supervisor, which is the component most likely to drift into being a second coordinator, imports no Orchestrator, ActionAgent, PlanningAgent, EventBus or LLM client and has no execute/dispatch/plan method. The absence is the enforcement, which is stronger than a convention.

**Q5. Walk me through the composition root.**
`aeam/main.py` is the only place concrete implementations are instantiated. Everything else takes dependencies as constructor arguments. The lifespan runs ~40 ordered steps and that order *is* the dependency graph — you can read it top to bottom and know what depends on what.

**Q6. How does dependency injection work without a DI framework?**
Constructor injection by hand. Every agent and engine takes its collaborators as arguments; nothing reaches into a global. A framework would add magic without adding capability at this size, and it would obscure the one thing I most want readable — the wiring order.

**Q7. What's `AppContainer` for?**
It holds the singletons constructed at startup so route handlers and background tasks can reach them via `request.app.state.container`. It's a service locator at the edge, with constructor injection everywhere behind it.

**Q8. Why a synchronous event bus?**
Because `POST /trigger` returning after the investigation completes is honest. A queue would return 202 immediately and hide six seconds of latency behind an acknowledgement. It also keeps the failure model simple — the exception surfaces to the caller rather than dying in a worker.

**Q9. What breaks if load increases?**
Request threads block for the duration of an investigation, so throughput is bounded by thread-pool size times investigation duration. At that point you'd move to a queue with a worker pool — which is exactly why the priority queue primitive exists in the codebase, unused, waiting for a real consumer.

**Q10. Explain `IncidentContext`.**
Every call to `handle_event` allocates a fresh context holding that incident's own short-term memory, state machine, start timestamp, and stage timings. Nothing per-incident lives on the Orchestrator instance, which is what makes it reentrant.

**Q11. Why is the Orchestrator stateless between incidents?**
Because it's shared across threads. Any per-incident field on it would be a race. Moving that state to a stack-local context makes concurrent investigations structurally impossible to cross-contaminate rather than merely unlikely to.

**Q12. How do you version the API?**
URL prefix — `/api/v1/...`. Changes are additive: new response fields are added, never removed or repurposed, and readers ignore unknown keys. That convention is what let the hardening pass add fields to existing responses without breaking the console.

**Q13. What's the modular monolith's biggest weakness here?**
Shared in-process state. Prometheus counters, the BM25 index and thread-local cost scopes all live in the process. Run two instances and you get per-instance metrics and per-instance lexical indexes. It works, but observability becomes per-instance — a real consequence I'd flag in an architecture review.

**Q14. How would you make it horizontally scalable?**
Three changes: move the BM25 index to a shared service or accept per-instance staleness with a disclosed refresh; push Prometheus through a pushgateway or accept per-instance scraping; and replace the synchronous bus with a queue plus workers. None is architecturally blocked — but none is implemented, and I wouldn't claim otherwise.

---

## B · FastAPI & Backend (15–24)

**Q15. Why FastAPI?**
Pydantic validation at the boundary, automatic OpenAPI, native async, and dependency injection that doesn't fight hand-rolled construction. The Pydantic settings model in particular is load-bearing here — 149 settings validated at startup with `extra="forbid"`.

**Q16. Why `extra="forbid"` on Settings?**
A typo'd environment variable should abort startup, not silently become a no-op. If you set `ENABLE_MONITOR_AGNT=true` you want a crash, not a system that quietly never monitors anything. It cost me one release-audit fix — `POSTGRES_PASSWORD` is needed by docker-compose but wasn't declared, so `cp .env.example .env` crashed. The right fix was declaring the real variable, not relaxing the check.

**Q17. Why is the app constructed at import time?**
`app = create_app()` at module bottom is the standard ASGI pattern and lets `uvicorn aeam.main:app` work directly. The trade-off is that an import error anywhere is a hard startup failure with no partial-service mode — which I'd argue is correct for this system.

**Q18. Explain the lifespan.**
An async context manager. Startup builds the container, wires every agent and engine in dependency order, starts background threads, and registers the Orchestrator on the event bus. Shutdown signals the ingestion worker, disposes the DB pool, and closes both Redis clients.

**Q19. Why sync endpoints rather than async?**
The investigation path is CPU- and blocking-I/O-bound — embeddings, reranking, SQLAlchemy. A sync `def` endpoint runs in FastAPI's threadpool, which is correct for blocking work. Declaring it `async` and then blocking would stall the event loop.

**Q20. How does middleware ordering work?**
Starlette applies middleware in reverse registration order, so registering SecurityMiddleware then CORS means CORS runs outermost. That's deliberate — a CORS preflight shouldn't need a token.

**Q21. How does the SPA get served?**
The built Vite bundle is mounted from `frontend/dist` with a catch-all GET that returns `index.html` for anything not matching an API or infra prefix. One deployable rather than two. If `dist` doesn't exist it's a silent no-op, so local dev keeps using the Vite dev server.

**Q22. How do you handle background work?**
Two daemon threads started in the lifespan — the ingestion worker (always) and the monitor (flag-gated). No Celery, no APScheduler. A scheduler stub was removed earlier because it was constructed, never started, and published a synthetic hardcoded event.

**Q23. Why threads over asyncio for those workers?**
Both do blocking work — SQLAlchemy queries, blob reads, model inference. Threads are the right primitive for blocking I/O in a mostly-sync codebase, and they interoperate with the sync request path without colouring every function async.

**Q24. How is graceful shutdown handled?**
The ingestion worker has a `threading.Event` the lifespan sets, so it exits after its current cycle. The monitor is a daemon thread and exits with the process — which is a known asymmetry I'd tighten with a stop event if I revisited it.

---

## C · Frontend & React (25–32)

**Q25. Why React with Vite?**
Vite's dev server and HMR are fast, and the build output is a static bundle FastAPI can serve directly. React because the console is state-heavy and component composition maps well onto the panel structure.

**Q26. How does the frontend authenticate?**
`lib/api.js` monkey-patches `window.fetch` once at module import: same-origin `/api/*`, `/health` and `/metrics` calls get an `Authorization: Bearer` header, and a 401 routes to a registered handler. Every page uses plain `fetch` and gets auth for free rather than every call site being touched.

**Q27. Isn't patching global fetch risky?**
It's a real trade-off. The alternative was threading a client through every component or touching ~40 call sites. It's guarded — patched once, scoped to same-origin API paths only, and pass-through for everything else. I'd reconsider it in a larger codebase, but here it kept the auth layer in one file.

**Q28. How are routes guarded?**
`nav.js` is the single source of truth for both sidebar visibility and route permission, so a route's guard and its menu entry can't drift. `RequireAuth` waits for the boot probe and redirects unauthenticated users; `RedirectIfAuthenticated` does the inverse on `/login`.

**Q29. Why does the console derive so much client-side?**
Because `incidents.findings` already contains the complete evidence trail. Adding per-panel endpoints would mean re-parsing the same JSON server-side into eight different shapes. One fetch, parsed once, is simpler — and it means the console and replay read identical data.

**Q30. What's the downside of that?**
Payload size grows with findings depth, and there's no server-side filtering per panel. At significantly higher incident volume I'd add a projection endpoint. Today the paginated list endpoint with `X-Total-Count` handles it.

**Q31. How do you handle timestamps?**
This was a real bug. Postgres `TIMESTAMP` columns return values with no zone marker, and `new Date()` parses those as *local* time — so verdicts and action logs were shifted by the viewer's UTC offset. Fixed centrally in a `parseTs` helper that treats a zone-less value as UTC, so all three affected surfaces were corrected in one place.

**Q32. How is the 3D mesh rendered?**
React Three Fiber, lazy-loaded so it never enters the initial bundle. Nodes are laid out on two interleaved tiers rather than one flat ring — with fourteen nodes on a single circle the labels collided in screen space.

---

## D · PostgreSQL & Persistence (33–41)

**Q33. Why Postgres?**
Relational integrity for incidents, approvals, verdicts, jobs and provenance; JSON columns for the findings trail; and mature indexing. SQLite is supported for local development and the schema is identical.

**Q34. Why is `findings` a JSON column instead of normalised tables?**
Because the shape is genuinely heterogeneous — a memory finding, a policy match, a cross-dataset correlation and an execution plan have almost nothing in common. Normalising would mean a dozen sparse tables joined on every read, for data that's always read together and never queried by inner field. It's an append-only audit trail, not a queryable entity.

**Q35. What's the cost of that decision?**
You can't index inside it, so cross-incident aggregates parse JSON in Python. That's why the observability endpoint has a bounded read window with the window disclosed in the response. If aggregate queries became hot I'd add generated columns for the few fields that matter.

**Q36. How do you manage schema?**
Alembic — twelve revisions, the production truth. There's also a `CREATE IF NOT EXISTS` startup path for dev convenience, and a test asserts the two produce an identical schema so they can't drift.

**Q37. How is connection pooling configured?**
SQLAlchemy `QueuePool` with configurable size, overflow and timeout. The timeout matters — a caller waits a bounded time for a connection and then fails fast, rather than blocking unboundedly under load.

**Q38. You mentioned a SQLite concurrency fix.**
SQLite's default busy timeout is zero, so the first write-lock contention between AEAM's own threads raised `database is locked` immediately. Since the ingestion worker, the monitor and request threads all write through one client, that's reachable in normal local operation. I added `busy_timeout` and WAL for SQLite only — Postgres is untouched. It also fixed a pre-existing failing concurrency test whose root cause was a locked read being swallowed into a partial graph build.

**Q39. How do you prevent SQL injection?**
Parameterised queries everywhere via SQLAlchemy `text()` with bound parameters. Table names can't be parameterised, so there's an explicit validator restricting them to alphanumerics and underscores, and filter columns come from a whitelist rather than user input.

**Q40. What's the `decisions` table?**
Created by the schema and by migration 0001, and never written. Decisions live inside `incidents.findings` instead. I left it rather than dropping it — an irreversible migration to remove an empty table isn't worth it — but it's documented as unused so nobody assumes decision history is queryable relationally.

**Q41. How would you handle retention?**
There's a documented retention posture and a read-time windowing cap on the observability aggregate. Actual deletion isn't implemented — I'd add a scheduled prune with the same disclosure discipline: report what was pruned rather than silently shrinking history.

---

## E · Redis (42–46)

**Q42. What does Redis do here?**
Four distinct things: event deduplication windows, action idempotency records with a 24-hour TTL, rate limiting, and the dataset activation set.

**Q43. Why Redis for dataset activation rather than Postgres?**
Because it's mutable at runtime and read on every monitor cycle by two consumers — the KPI source composition and the rule engine's domain provider. A set with atomic add/remove is the natural fit, and both consumers read it live so activation takes effect without a restart.

**Q44. What happens if Redis is down?**
Health reports it degraded. Deduplication and idempotency degrade — you could get duplicate events or a repeated action. Rate limiting fails, and in a non-development posture that's a real availability question I'd want to answer explicitly before production: fail open or fail closed.

**Q45. How does idempotency work?**
A key derived from `(incident_id, action_type, parameters)` with a 24-hour TTL. A repeated identical action returns `ALREADY_EXECUTED` without calling the handler. Because the key includes the incident id, two separate investigations of the same underlying problem *will* both act — which is why the simulation script double-publishing was a real bug.

**Q46. Why a 24-hour TTL?**
Long enough that a retried or replayed action within an operational day is suppressed; short enough that the same incident type recurring next week isn't wrongly suppressed. It's a judgement call, not a derived number.

---

## F · Qdrant, Embeddings & Vectors (47–54)

**Q47. Why Qdrant?**
Payload filtering alongside vector search — which the metadata-aware retrieval stage needs — plus a simple scroll API the BM25 index builds from, and it runs locally in a container.

**Q48. Why two collections?**
`aeam_documents` for the knowledge corpus and `aeam_incident_memories` for incident summaries. Same embedding model, same client, different namespace. Mixing them would mean a document chunk could be returned as a "similar past incident", which is a category error.

**Q49. Why `all-MiniLM-L6-v2`?**
384 dimensions, small enough to run on CPU with acceptable latency, and good enough quality for operational prose. The trade-off is that it's weaker on long documents and domain jargon than a larger model. Given the reranker sits downstream and does the precision work, a fast recall-oriented embedder is the right division of labour.

**Q50. How do you chunk?**
Sentence-based, 300 characters with 50 overlap. Overlap prevents a cause statement being split across a boundary. Chunk ids are deterministic, so re-ingesting identical content upserts rather than duplicating.

**Q51. What's the similarity threshold and why?**
0.5 cosine. Below that, results were mostly noise in this corpus. It's a tuned constant, not a derived one, and it's surfaced in the evidence panel per query attempt so a reader can see what threshold produced a given result.

**Q52. How do you handle embedding-model changes?**
You'd have to re-index — vectors from different models aren't comparable. There's no migration path implemented. I'd version the collection name and re-ingest, which is straightforward because the blob store holds the original bytes.

**Q53. What if Qdrant is unreachable?**
Health reports it. Retrieval returns nothing, so RAG records a failed pass with the reason and the investigation continues on the other five evidence sources. There's no fallback path — that's stated in the docs rather than silently degraded.

**Q54. How large can the corpus get before this design strains?**
The BM25 index is in-process and rebuilt by scrolling the whole collection, so build time grows linearly and memory holds the full token structures. At tens of thousands of chunks that's fine; at millions I'd move lexical search into Qdrant's own sparse-vector support or a dedicated search engine.

---

## G · Hybrid Search, BM25, RRF, Reranking (55–66)

**Q55. Why hybrid rather than dense-only?**
Dense embeddings miss exact identifiers — a metric name, an error code, a service name. Those are the highest-signal tokens in an operational corpus and cosine similarity smooths them away. BM25 catches them exactly and misses paraphrase entirely. Operational text needs both.

**Q56. Explain BM25.**
A term-frequency scoring function with two corrections: saturation, so the tenth occurrence of a term adds far less than the second; and length normalisation, so a long document doesn't score highly just for containing more words. Terms are weighted by inverse document frequency, so rare terms matter more.

**Q57. Explain RRF.**
Reciprocal Rank Fusion. Each document scores `1 / (k + rank)` in each result list, summed across lists, with k around 60. A document ranked highly by either retriever surfaces; one ranked highly by both surfaces strongly.

**Q58. Why RRF over weighted score blending?**
Cosine similarity and BM25 scores live on incompatible scales with different distributions. Blending them requires per-corpus calibration that drifts as the corpus grows. RRF uses only rank position, so it needs no calibration and can't be destabilised when one retriever's score distribution shifts.

**Q59. What does k control?**
How quickly the reciprocal decays with rank. Larger k flattens the curve, so lower-ranked results contribute more. 60 is the value from the original paper and it's a reasonable default; I didn't tune it against this corpus, which I'd say honestly.

**Q60. Why rerank if you already fused?**
A bi-encoder embeds query and document independently and never sees them together. A cross-encoder does, and is substantially more accurate — at a cost that makes it impossible over a corpus. So it runs over 20 fused candidates. That's the whole trade: retrieve broadly and cheaply, rank precisely and expensively, on a small set.

**Q61. What's the latency cost of the reranker?**
About a second on CPU for 20 candidates in the traced runs. It's the second-largest component of retrieval latency after query expansion. On GPU it would be negligible; on CPU it's a real cost you're paying for precision.

**Q62. Why the evidence-diversity filter?**
Without it, five near-identical chunks from one runbook section crowd out a contradicting chunk from another document, and the model sees unanimous evidence that was never unanimous. It drops near-duplicates by Jaccard overlap and caps chunks per source document, backfilling if too few distinct documents exist.

**Q63. What's business-relevance ranking?**
A final adjustment — never an override — of semantic relevance. Bonuses for entity matches from the incident metadata, for authoritative document types like runbooks, and for recency. Every adjustment emits a plain-language reason so the console can explain why a chunk placed where it did.

**Q64. Why is multi-query expansion before retrieval and not after?**
Because it's a recall mechanism. Different phrasings surface different documents; you need all of them in the candidate pool before ranking. Running it after would just re-rank the same candidates.

**Q65. What happens when all query variants fail?**
There's an exhaustion guard: once every variant has returned zero chunks, RAG becomes a no-op for the rest of the incident rather than repeating a search that cannot succeed. Validation records "all three query variants exhausted", and the investigation escalates rather than fabricating an answer.

**Q66. How do you evaluate retrieval quality?**
There's a retrieval evaluation harness in the test suite, and the platform reports its own retrieval success rate across investigations — currently 22%. That's a real, unflattering number driven by a deliberately small corpus. What I don't have is a labelled relevance dataset with precision/recall at k, which is what I'd build next for principled tuning.

---

## H · RAG & LLM (67–76)

**Q67. How do you stop the model hallucinating a root cause?**
Two independent gates. A guardrail scans the raw response for sensitive patterns before it's parsed or persisted. Then a grounding validator requires every cited cause to reference a chunk that was actually retrieved. A cause the model invented has no traceable chunk id, fails validation, and the whole pass is recorded as failed — visibly, in the evidence panel, not silently dropped.

**Q68. Why is the LLM not allowed to trigger investigations?**
Because a model that can trigger an investigation can hallucinate an incident. Detection is a yes/no question about numbers with a correct answer computable from a rule and a z-score. Handing that to a probabilistic model adds no capability and introduces a failure mode where the system invents work.

**Q69. What's in the prompt?**
Retrieved chunk text only, in a strict template, at temperature 0.2 with a bounded token limit. The model is asked for JSON with causes, chunk ids and confidences. It isn't given the raw event beyond the fields needed to frame the question.

**Q70. Do you persist the prompt?**
No — and the Retrieval Explorer says so explicitly rather than reconstructing something that might not byte-match what was sent. The retrieved chunks and the raw response are both persisted, which is enough to audit the reasoning.

**Q71. How do you handle malformed JSON?**
A tolerant parser handles markdown fences and leading or trailing prose. If it still can't parse, that's a failure result with the raw response attached — never a silent fallback to a default answer.

**Q72. Which LLM provider and why only one?**
Groq, for latency. There's an explicit provider-support check: configuring any other provider while real calls are enabled aborts startup rather than silently promising a vendor the code can't reach. That's a deliberate honesty choice, but it's a genuine limitation — one provider is a single point of dependency.

**Q73. How do you control LLM cost?**
Three ways. Temperature and token caps per call. Severity routing — RAG only runs at HIGH and CRITICAL, which is a deliberate cost control. And per-incident cost attribution: tokens, calls and retrieval volume are recorded against the incident that caused them, so cost is attributable rather than aggregate.

**Q74. What happens when the LLM fails?**
Retries with exponential backoff, except for permanent errors — a 401, 403 or 404 can't succeed on retry, so those fail fast rather than burning three attempts and seven seconds of sleep. The real provider error is preserved into the message and therefore into the persisted incident record. That was a bug I fixed: the original code swallowed the real exception and raised a generic "after retries" string, so seven historical incidents recorded a failure with no diagnosable cause.

**Q75. Is there a circuit breaker on the LLM?**
Yes, with a failure count and a timeout. A bug I fixed: the failure count only ever grew — success never reset it — so a service with two historical failures tripped on the next one regardless of thousands of successes in between.

**Q76. Could you run this fully offline?**
Mostly. Detection, correlation, adaptive baselining, memory recall, policy matching, planning, explainability, evaluation and gating all work without an LLM. You'd lose document-grounded causal hypotheses and query expansion. With a self-hosted OpenAI-compatible endpoint you'd get those back too, but the provider layer would need extending — it isn't today.

---

## I · Agents & Orchestration (77–84)

**Q77. What makes this multi-agent rather than one function?**
Explicit contracts and isolation. Each agent has defined inputs, outputs and a declared failure mode, and each evidence stage runs in its own error boundary — a failure degrades that stage, not the investigation. It also lets the evidence sources be independently switchable, which is how the system stays honest about which ones actually contributed.

**Q78. Isn't that just modular code with a fancy name?**
Partly, and I'd rather say that than oversell it. What makes "agent" defensible here is the roster: each has its own heartbeat, its own metric label, its own span, and appears in an observability surface as an independently-reasoned-about participant. But they're in-process classes, not autonomous processes negotiating with each other, and I wouldn't claim otherwise.

**Q79. How do agents communicate?**
They don't. The Orchestrator calls them and collects findings. There's no agent-to-agent channel by design — that's what keeps the single-coordinator invariant meaningful.

**Q80. Explain the evaluation loop.**
After each depth, a score is computed from four criteria. Depth limit overrides everything and escalates. Score ≥ 0.8 stops. Otherwise it recurses. Maximum depth is five.

**Q81. You said one criterion is unreachable. Explain.**
`action_taken` awards 0.1, but actions execute in finalization — after evaluation has concluded. No correct implementation of the current lifecycle could set it before scoring. So the achievable maximum is 0.9 and reaching STOP requires confidence above 0.8, which is why most investigations escalate.

**Q82. Why didn't you fix it?**
Because every available remedy — re-weighting the three reachable criteria, or lowering the threshold — changes when the system auto-resolves without a human. That's a product and safety decision, not a hardening call: it trades human oversight for throughput. It also invalidates any fitted confidence calibration, since those were fitted against the current distribution. I documented it thoroughly and left the decision to whoever owns that trade-off.

**Q83. How does root-cause precedence work?**
RAG's chunk-cited cause wins. The KPI Agent writes only if nothing is set. The depth-≥3 LLM pass also writes only if nothing is set. The rule is precedence by validation depth, not recency — the most-validated source wins, not the last one to run.

**Q84. Why can't the KPI Agent state a cause?**
Because a statistical characterisation answers *what* changed, not *why*. Asserting "sales fell because of checkout latency" from a z-score is fabricated traceability. Its root cause is always a literal statement of measured fact attributed to the detectors that produced it, and any real explanation supersedes it.

---

## J · Concurrency & Thread Safety (85–89)

**Q85. Where are the concurrency hazards?**
Three: per-incident state (solved by stack-local contexts), the BM25 index rebuilt by one thread while others search it (solved by snapshot-and-swap under a lock), and SQLite write contention between AEAM's own threads (solved by busy timeout and WAL).

**Q86. Walk me through the BM25 race in detail.**
`search()` collects candidate indices by enumerating the term-frequency list, then dereferences the document list by index. `build()` rebuilt seven parallel structures in place. A reader could capture the old term-frequency list — 556 entries — then dereference the new, mid-rebuild document list holding twelve, and raise `IndexError`. It surfaced as a spurious "Retrieval failed" whenever a document was ingested during an investigation. The fix builds into local variables and publishes all seven together under a lock; search takes one consistent snapshot and scores outside the lock, so concurrent searches aren't serialised.

**Q87. How did you find it?**
By tracing the code path rather than by observing a failure — then reproducing it with four reader threads against forty rebuilds. The test is in the suite.

**Q88. Is the Orchestrator thread-safe?**
Yes, by having no per-incident instance state. Its collaborators are shared deliberately — they're read-only or individually thread-safe. That's documented as an explicit concurrency contract rather than an accident.

**Q89. What about the GIL?**
It's not the bottleneck here. The work is I/O-bound — database, Redis, Qdrant, HTTP to the LLM — plus model inference in native code that releases the GIL. Threads are the right primitive for this profile.

---

## K · Observability, Testing & Operations (90–95)

**Q90. What does `/health` actually check?**
Ten things: database via a real query, Redis ping, queue size, both worker heartbeats, BM25 freshness, Qdrant reachability, LLM posture, and the two request-scoped agents. The design rule is that a signal degrading *quality* is disclosed but never flips overall status, while one indicating *unavailability* does.

**Q91. You found a bug in your own health check.**
Yes, and it's the one I'd lead with. The database check was `checks["database"] = "ok"` inside a `try` whose body was a dictionary assignment — it couldn't raise, so the handler was unreachable and the value unconditional. An entirely unreachable database still reported healthy. That defeats every automated failure-detection mechanism consuming it. It now issues a real pooled query.

**Q92. How do you test a system with an LLM in it?**
By separating the deterministic core from the probabilistic edge. Rules, statistics, planning, explainability, evaluation, replay and gating are pure functions over data — tested directly. The LLM boundary is tested through contracts: does a failed call preserve the real provider error, does a permanent error skip retries, does grounding validation reject an uncited cause, does the breaker reset on success. I never assert on model output.

**Q93. What's the test breakdown?**
1,613 backend and 116 frontend. Roughly 27,000 lines of test code against 54,000 lines of source. Phase-organised, so each roadmap phase's acceptance criteria have a corresponding file.

**Q94. What's per-incident cost attribution?**
A thread-local scope opened when an investigation starts. Every LLM call records its tokens and cost into it, retrieval records chunk volume, actions record outcomes. At finalization it's snapshotted into the incident record. So cost is attributable to the incident that caused it, not just aggregate.

**Q95. How does replay work and why can't it re-execute?**
It reads one persisted incident row and returns a projection. It imports no detector, agent or LLM client, so it structurally cannot re-execute. Three honesty rules: recorded order is the order, absence is reported as an explicit gap naming the phase that introduced the stage, and time is measured or absent — with the remainder between measured stage time and measured total disclosed as unattributed rather than distributed.

---

## L · Security, Trade-offs & Self-Critique (96–100)

**Q96. Walk me through the security model.**
RS256 JWT with optional OIDC federation via JWKS and PKCE. Deny-by-default RBAC with longest-prefix endpoint mapping. Redis-backed rate limiting. Dual-sink audit logging. Fail-closed startup: non-development aborts without real key material, and half-configured OIDC aborts in *every* environment including development — because an operator who believes SSO is enforcing identity must never be running a posture that isn't.

The critical caveat I always state: `ENVIRONMENT=development` bypasses all of it. That's deliberate for local work, it's the single most consequential setting, and every RBAC claim is unenforced in that posture.

**Q97. What's the worst bug you shipped?**
A missing import in the composition root. `SourceRepository` was called but never imported, and a broad `except Exception` around the connector-composition block swallowed the `NameError` into a log line — so every metrics connector was silently dropped while the health endpoint reported them enabled. A shipped feature was inoperative and the platform said the opposite.

The fix was one import. The lesson was the handler: broad exception catching around *construction* code absorbs programming errors, not just upstream failures. Those handlers now re-raise `NameError`, `AttributeError`, `ImportError` and `TypeError` — none of which is ever an upstream failure.

**Q98. What would you do differently if you started over?**
Three things.

First, I'd design the severity-to-evidence routing and the severity derivation together. Right now RAG only runs at HIGH and CRITICAL, and severity is derived from signal count — so a single-signal autonomous detection is MEDIUM and gets no document evidence. Each decision is defensible alone; the interaction is emergent and nobody designed it.

Second, I'd decide the evaluation model's relationship to the action lifecycle up front, rather than discovering a criterion was unreachable after the ordering was fixed.

Third, I'd build the retrieval evaluation harness before the retrieval pipeline. I tuned six stages against intuition and spot checks. With a labelled relevance set I'd know which stages actually earn their latency — right now I can defend each one's rationale but I can't quantify its contribution.

**Q99. What are you least confident about?**
The connectors. All eight are implemented against a shared contract with a deterministic mock mode, and the mock path works end to end. But I've never run one against a live SharePoint or Snowflake tenant. Real APIs have pagination quirks, auth refresh behaviour and rate limits that mocks don't reproduce. I'd expect friction there, and I'd rather say that than imply it's proven.

Second: the deployment. The configs are written and the compose file validates, but I haven't run a production deployment. I won't claim it works end to end.

**Q100. Why should I believe the numbers you're showing me?**
Because several of them are unflattering and I'm showing them anyway. Retrieval success is 22%. Resolution rate is 11%. AI health is 40%. Every rate publishes its numerator and denominator, and the composite score publishes its formula.

More concretely: the hardening pass found four separate cases where the system was reporting incorrectly about *itself* — a health check that never queried the database, a timestamp field that always said "now", a supervisor reporting agents as never-executed immediately after they executed, and a dashboard node showing a disabled agent as active. I found and fixed all four before showing you this, and I'm telling you about them now. That's the reason to trust the rest.

---

## Rapid-fire facts to have ready

| Question | Answer |
|---|---|
| Lines of code | ~54k backend, ~15k frontend, ~27k tests |
| Tests | 1,613 backend + 116 frontend = 1,729 |
| API routers | 18 |
| Settings | 149, `extra="forbid"` |
| Migrations | 12 Alembic revisions |
| Agents | 8 roster + 13 supporting engines |
| Evidence sources | 6 |
| Retrieval stages | 6 |
| Connectors | 8 (all off by default) |
| Embedding model | `all-MiniLM-L6-v2`, 384-d |
| Reranker | `cross-encoder/ms-marco-MiniLM-L-6-v2` |
| LLM | Groq `llama-3.1-8b-instant` |
| Typical investigation | ~6 s, 2 LLM calls, ~2,100 tokens |
| Deployed? | **No — local only** |
