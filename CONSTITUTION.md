# The AEAM Engineering Constitution

**Status:** Permanent. This is the highest-level engineering document in the project.
**Authority:** Every future contribution — human or AI — is bound by this document. Where any other document, comment, or convention conflicts with it, this document wins. Where this document conflicts with the honesty laws in Article II, the honesty laws win.
**Basis:** The 2026-07 Engineering Audit, which examined every subsystem and concluded: *zero subsystems require redesign*. The architecture is the asset. This constitution exists to keep it that way.

Laws are numbered for citation (e.g., "violates ARCH-4"). A change that cannot state which laws it satisfies is not ready for review.

---

## Article I — Vision

AEAM is an autonomous enterprise intelligence platform: it detects business anomalies deterministically, investigates them with a mesh of strictly bounded agents and advisory intelligence engines, acts only through safe and reversible actions, and can explain every conclusion it has ever reached from the evidence that produced it.

Its competitive property is not intelligence — it is **trustworthiness**. An enterprise adopts AEAM because every incident record is a complete, honest, replayable audit trail. The audit scored Explainability at 90/100, the highest mark in the platform; the vision is a platform where every other category earns that score *without ever lowering that one*.

**V-1.** The measure of every change is: does it make AEAM more trustworthy to an enterprise operator, auditor, or risk officer? Capability gained at the cost of trustworthiness is a regression.

**V-2.** The end state is a production-grade platform inside a Fortune 500 enterprise. Features that cannot eventually survive that environment (identity, audit, scale, compliance) are demos, and must be labeled as such.

---

## Article II — Core Philosophy

These five laws are the platform's identity. They outrank everything else in this document.

**PHIL-1 — Honesty over capability.** AEAM never fabricates. A missing value is reported as explicitly unavailable, with the real reason. "Not consulted," "consulted but found nothing," and "insufficient data" are three different truths and are never conflated. This applies to backend contracts, engine outputs, persisted findings, reports, and every pixel of the UI. (Audit: this discipline is "the platform's most valuable and least replaceable asset.")

**PHIL-2 — Composition over modification.** New capability wraps existing components; it does not edit them. The retrieval stack, the composite KPI sources, the composite rule engine, and every C/D-phase engine were all added with zero changes to what they wrap. This is the canonical growth pattern.

**PHIL-3 — Determinism first, LLM last.** Detection is deterministic. Decisions are rule-first with the LLM as a bounded last resort. Synthesis engines (planning, explainability, evaluation, observability) are pure functions with no LLM at all. An LLM is used only where deterministic logic genuinely cannot do the job, and its output is always validated before it is trusted.

**PHIL-4 — Advisory intelligence, deterministic authority.** Intelligence engines produce evidence, never decisions. No advisory finding may trigger, suppress, or override a deterministic rule, a decision engine outcome, or an action. The only path to an external side effect runs through the deterministic runbook catalog and the ActionAgent.

**PHIL-5 — Graceful degradation, loud truth.** A missing optional dependency degrades one capability, never the platform. Startup never breaks because a feature failed to construct; the investigation loop never dies because an evidence source raised. But degradation is always *logged and surfaced* — silent degradation is a honesty violation.

---

## Article III — Architectural Principles

*Audit basis: Architecture 78, Separation of Concerns 85, Modularity 82 — the strongest categories. These laws freeze what earned those scores.*

**ARCH-1.** AEAM is a **modular monolith** and remains one. Splitting it into services is a constitutional amendment, not a refactor. (Audit verdict: redesign required — zero subsystems.)

**ARCH-2.** There is exactly **one composition root** (the application lifespan). All construction and wiring happens there, by constructor injection. No module creates its own infrastructure clients, reads global state, or performs service discovery.

**ARCH-3.** **Shared singletons stay singular.** One embedding service, one vector client, one LLM service, one long-term memory, one activation store. A change that constructs a second instance of a shared dependency must justify why reuse was impossible.

**ARCH-4.** **Strict layering.** Core primitives know nothing of agents. Agents know nothing of HTTP. API routes contain no business logic. Infrastructure clients contain no application semantics. Pure-logic modules perform no I/O. Every module's docstring declares what it deliberately does *not* do, and reviews enforce those declarations.

**ARCH-5.** **Events are immutable; findings are append-only.** An `Event` is never mutated after creation. An incident's findings list only grows; nothing edits or deletes a prior entry. The consolidated audit summary is the single source of truth for an incident's outcome.

**ARCH-6.** **One external chokepoint.** The ActionAgent is the only component permitted to call external APIs. No exceptions, no "just this once."

**ARCH-7.** **State placement must match the deployment target.** Any state that must survive a restart or be visible to a second instance lives in shared, durable infrastructure — never on the local filesystem of a compute instance. (Audit gate #3: blobs, models, and the audit log currently violate this on the declared Cloud Run target. New state may not repeat the violation.)

**ARCH-8.** **Concurrency assumptions must be declared.** No component may silently assume single-event-at-a-time processing. Per-incident state must be isolated per incident. (Audit gate #2: the non-reentrant orchestrator is the platform's most serious latent fault; nothing new may deepen that assumption.)

**ARCH-9.** **Contracts over classes.** Seams are defined by protocols (`KPIRowSource`, `DatasetActivation`, `HistoricalDataSource`, the retrieval `search` contract). New implementations satisfy the protocol; consumers never learn concrete types.

---

## Article IV — Engineering Principles

**ENG-1.** **Feature flags with fallback.** Every optional capability is gated by a setting and wrapped in construction-time try/except that falls back to the prior working stage. A new feature that can break startup is unacceptable by construction.

**ENG-2.** **Fail fast on required config; degrade on optional integrations.** Missing required settings stop boot with a clear error. Missing optional credentials produce a functioning platform minus one integration, with the absence logged.

**ENG-3.** **Never-raise boundaries are contracts.** Paths marked never-raise (evidence engines, KPI sources, report generation, action handlers via the agent) return structured error results instead of exceptions — and this behavior is tested, not assumed.

**ENG-4.** **Idempotency wherever repetition is possible.** Anything that could run twice — actions, ingestion, evidence stages, finalization — carries an idempotency guard. Guards are checked against durable state where duplication has external consequences.

**ENG-5.** **Placeholders are quarantined.** Placeholder logic must be (a) labeled in code, (b) labeled in its output, (c) listed in project documentation, and (d) **barred from contaminating knowledge stores** — synthetic root causes, simulated evidence, and mock data must never be persisted into organizational memory or presented as analysis. (Audit finding: the KPI placeholder currently poisons Enterprise Memory; this law makes that a defect, not a pattern.)

**ENG-6.** **One source of truth per constant.** Defaults, vocabularies, and thresholds are defined once in their owning module and imported everywhere else — never re-typed. (Precedent: the configuration registry imports engine defaults rather than duplicating literals.)

**ENG-7.** **Lockstep pairs are documented on both sides.** Where duplication across runtimes is unavoidable (backend/frontend status derivation), both files must name each other and any change updates both in the same commit.

**ENG-8.** **No decorative components.** Every constructed object must have a consumer. (Audit finding: the priority queue is pushed but never drained. Dead machinery misleads readers and leaks resources; either it gains a consumer or it is acknowledged as removed scope.)

---

## Article V — Agent Principles

**AGENT-1.** Every agent has a written responsibility boundary *and a written forbidden list* in its module docstring. The forbidden list is as binding as the responsibility list. Current boundaries are canonical:

- **Orchestrator** — coordinates the incident lifecycle. Never detects, never calls external APIs, never writes the database directly.
- **Monitor / detectors** — deterministic detection and event creation. Never LLM, never orchestration, never direct DB access, never external APIs.
- **Forecast** — model lifecycle and deviation analysis. Never creates events, never retrains in the background, always returns a result.
- **RAG** — research only. Never decides, never persists, never mutates the event.
- **Action** — the sole external executor. Never reasons, never decides, never uses an LLM.
- **Report** — content generation only. Never acts, never decides, never writes.

**AGENT-2.** Advisory engines follow the uniform engine shape: stateless, injected once, invoked once per incident under an idempotency guard, appending exactly one typed findings entry, never raising, never feeding back into rules/decisions/actions.

**AGENT-3.** Agents communicate through events and findings — never by calling each other directly. The Orchestrator is the only component that invokes agents, and only through injected references.

**AGENT-4.** A new agent or engine requires: a declared boundary and forbidden list, a findings type (if it produces evidence), honesty states for its unavailable conditions, and observability exposure (Article XI) — before any capability code is reviewed.

**AGENT-5.** Only actions from the safe, reversible, human-authored runbook catalog may execute autonomously. Destructive or irreversible actions never enter the catalog. If the platform ever gains approval-gated execution, approval must be *enforced* at the execution boundary — a computed flag that is not enforced must be documented as advisory (Audit: `human_approval_required` is currently advisory; presenting it otherwise would violate PHIL-1).

---

## Article VI — AI Principles

**AI-1.** **The LLM is always optional.** Every LLM-using path has a deterministic fallback and functions with the LLM disabled. Mock mode is a first-class citizen.

**AI-2.** **Grounding is mandatory.** LLM claims about evidence must cite the evidence (chunk-level provenance), and ungrounded or externally-referenced output is *rejected*, never repaired or partially trusted. Validation failure is a structured, visible outcome.

**AI-3.** **The LLM informs; it never authorizes.** No LLM output may trigger an action, suppress a rule, or alter a deterministic decision. (PHIL-4 applied to AI specifically.)

**AI-4.** **Parse honestly.** All LLM output flows through the shared resilient parser. Unparseable output produces a structured error record — field values are never guessed from broken output.

**AI-5.** **Confidence is never invented.** Report only confidence values that were genuinely computed. Never decompose a real number into fabricated per-source weights; label heuristic scores as heuristic, never as probabilities. (Precedent: the Explainability engine's refusal to invent additive confidence breakdowns is constitutional behavior.)

**AI-6.** **Every LLM call site is accountable.** New call sites must declare their generation parameters, failure mode, and cost/latency exposure. (Audit: the platform currently has no token accounting or budget controls — new AI surface may not grow that debt silently.)

**AI-7.** **Prompts are a trust boundary.** Content originating from users, documents, or external systems is untrusted input to a prompt; guardrails (injection sanitization, output scanning) must be wired wherever untrusted content reaches an LLM. (Audit: guardrails exist and are tested but unwired — a standing violation to be retired, not extended.)

---

## Article VII — RAG Principles

*Audit basis: RAG scored 68 — "the strongest engineering in the platform... the pattern the rest of the platform should continue to follow."*

**RAG-1.** The retrieval stack is a chain of **drop-in wrappers with one uniform contract** (`search(query, filter_criteria, top_k)` plus the passthrough properties). New stages wrap the existing composition; they never edit inner stages, and they preserve the contract exactly.

**RAG-2.** **Evidence schema is append-only.** Every stage preserves every key it received (`chunk_id`, `text`, `metadata`, `similarity`, and all provenance added upstream) and only *adds* keys. Removing or renaming an evidence key breaks citations, validation, and the UI — it is a compatibility violation (Article XIX).

**RAG-3.** Every stage is **flag-gated with graceful fallback** to the prior stage on construction failure. RAG must never be able to break startup.

**RAG-4.** **Ingestion is idempotent** through deterministic chunk identity. Re-ingesting identical content produces identical IDs and no duplicates.

**RAG-5.** **Retrieval never fabricates.** Zero results is an honest empty answer. Automatic filter relaxation is permitted only when the relaxed results are labeled as relaxed. Query reformulation is deterministic or LLM-expanded-but-validated; exhaustion is declared, not hidden by repetition.

**RAG-6.** **Index freshness assumptions must be stated and surfaced.** Any index built at startup (the lexical index) embodies a staleness assumption; that assumption must be documented at the build site and visible to operators. (Audit finding: BM25 staleness silently skews hybrid fusion for post-boot documents.)

**RAG-7.** **Ranking bonuses must be explainable.** Any signal that reorders evidence must attach human-readable reasons for every bonus actually applied — and never a reason for a signal that was not found.

---

## Article VIII — Memory Principles

**MEM-1.** **Working memory is ephemeral and per-incident.** It is initialized at incident start, cleared at finalization, and never shared across incidents. (With ARCH-8: "per-incident" must eventually mean *isolated per concurrent incident*, not per-process.)

**MEM-2.** **Persisted incidents are immutable history.** Once recorded, an incident row and its findings are never edited. Corrections are new records, not mutations.

**MEM-3.** **Organizational memory records honest outcomes** — failures and escalations included, because "what didn't work" is real knowledge. Fields that weren't recorded are omitted, never defaulted or guessed.

**MEM-4.** **Memory quality is a first-class property.** Nothing synthetic, simulated, or placeholder may be remembered as organizational knowledge (ENG-5). When curation capabilities arrive, correcting or expunging a bad memory must itself leave an audit trail.

**MEM-5.** **Recall is advisory.** Remembered incidents inform investigations as evidence; they never auto-apply past actions.

**MEM-6.** **Every memory store has a declared owner and retention posture** before enterprise deployment (Article XVI). Unbounded, unowned accumulation is technical debt by definition.

---

## Article IX — Security Principles

*Audit basis: Security scored 30 — "the framework is correct; these laws exist to make it real and keep it real." Security is the #1 gate to enterprise deployment.*

**SEC-1.** **Deny by default.** The security posture of any new surface is: authenticated, authorized, rate-limited, audited — unless explicitly and visibly exempted (public health/liveness endpoints).

**SEC-2.** **The development bypass never widens.** Environment-conditional bypasses must remain strictly conditional (never `or True`, never a new bypass path), and production must never run with a development posture. (Canonical law inherited from project documentation.)

**SEC-3.** **Authorization parity is part of every change.** A change that adds or renames an API route must update the RBAC mapping in the same change. (Audit finding: six routers currently authenticate without authorizing — parity drift is now a review-blocking defect.)

**SEC-4.** **Placeholder credentials fail closed and loudly.** A security component constructed with a placeholder (the dummy JWT key) must refuse to present itself as functional. Silent 401s that look like auth are worse than an explicit "identity is not configured."

**SEC-5.** **No secrets in code, logs, or version control.** Secret *names* may be logged; secret *values* never. Hardcoded credentials in deployment artifacts are defects.

**SEC-6.** **The audit trail is append-only, tamper-evident, and durable.** Audit write failures never block operations but must be observable. An audit log that dies with the instance does not satisfy this law (Audit: `/tmp` placement is a standing violation).

**SEC-7.** **Configuration-writing surfaces are privileged.** Any endpoint that can change platform behavior (settings, activation, deletion, purge) carries the strictest authorization tier and leaves an attributable audit record.

**SEC-8.** **Environment honesty.** Each environment's declared posture must match its actual behavior: production must run production functions (autonomy on, debug surfaces off, security enforced). A "production" configuration that disables the product's core loop violates PHIL-1 at the deployment layer. (Audit gate #4.)

---

## Article X — Explainability Principles

*Audit basis: 90/100 — the platform's crown jewel. These laws are descriptive of current behavior and prescriptive forever.*

**EXPL-1.** **Every conclusion is traceable.** Every recommendation traces to a specific evidence item (policy ID, memory incident ID, dataset:metric, chunk ID) — or is honestly labeled as baseline/runbook guidance with no originating evidence. Fabricated traceability is the worst possible defect in this platform.

**EXPL-2.** **Explanations restate; they never recompute.** Explanation layers reorganize what earlier stages already computed. An explanation that produces new judgments is a reasoning engine wearing a costume, and is forbidden.

**EXPL-3.** **Three states, always distinguished:** not consulted / consulted with no signal / insufficient data — each with its own wording, in findings, reports, APIs, and UI.

**EXPL-4.** **Adjustments are disclosed.** Any modification to a confidence or score (conflict caps, penalties) is reported with its magnitude and its reason.

**EXPL-5.** **The UI inherits the honesty contract.** "N/A" with a reason always beats an invented visual. Session-local interactions are labeled session-local. Windows and sample sizes are disclosed ("of the last N shown"). Nothing renders as real that isn't.

**EXPL-6.** **Conflicts are surfaced, never resolved silently.** Contradictory evidence appears in the record as a conflict, with its consequences (capped confidence, forced review) stated.

---

## Article XI — Observability Principles

**OBS-1.** **One measurement pipeline.** Self-observation reads what the system already persists (findings, metrics, logs) — never a second, parallel metrics store. (Precedent: the D3 engine is a pure function over persisted incidents.)

**OBS-2.** **Every published metric states its semantics** — window, reset behavior, and data source. Process-lifetime counters must not masquerade as historical truth, and differently-shaped data sources are not merged into one number.

**OBS-3.** **Unavailable metrics say so.** `available: false` plus the real reason, always. (Precedent: per-incident duration is honestly reported as not persisted.)

**OBS-4.** **New engines are born observable.** Any component that participates in investigations must expose its consultation and outcome through the findings model, so cross-incident measurement works with zero additional plumbing.

**OBS-5.** **Logs are structured and correlated.** Every investigation-path log line carries the incident identifier. Debug artifacts (`print` statements) do not ship in production paths (Audit: standing violations in the startup sequence are debt, not precedent).

**OBS-6.** **Observability of the platform itself is an enterprise gate** (Article XVI): metrics must be scraped, logs aggregated, and failures alertable before any deployment is called production.

---

## Article XII — Coding Standards

**CODE-1.** Public APIs are fully typed. Docstrings document Args, Returns, Raises — and for boundary components, the forbidden list (AGENT-1).

**CODE-2.** Dependencies arrive by constructor injection; seams are protocols; module-level mutable state is forbidden outside the composition root.

**CODE-3.** Pure-logic modules import no I/O clients. If the docstring says "no I/O, no LLM," the imports must prove it.

**CODE-4.** Comments explain *constraints and why* — architectural rationale, honesty guarantees, invariants the code cannot express. Never narration of what the next line does.

**CODE-5.** Broad exception handling appears only at declared never-raise boundaries, is annotated as deliberate, and always logs what was swallowed.

**CODE-6.** Optional heavy dependencies are imported lazily so their absence degrades one capability, not a module import (precedent: per-format extraction parsers).

**CODE-7.** Repository hygiene: scratch scripts, verification one-offs, logs, and generated artifacts do not live at the repository root or in version control (Audit: current root clutter is debt to retire, not a pattern to follow).

**CODE-8.** No linter or type-checker is currently configured; until one is adopted (Article XVII), the existing code's conventions — naming, docstring shape, section dividers, error taxonomies — *are* the style guide. Match the surrounding file.

---

## Article XIII — Documentation Standards

**DOC-1.** **A module docstring is a contract**: what it owns, what it explicitly does not do, and what it never does (raise, fabricate, call externally). Reviews hold code to its own docstring.

**DOC-2.** **Docs never claim more than the code does.** A docstring describing removed or unimplemented behavior is a defect (Audit: the logs module still describes mock data its endpoint no longer serves). Placeholder and advisory-only behavior is documented as such wherever an operator might assume otherwise.

**DOC-3.** **Gotchas are maintained.** The known-issues documents (CLAUDE.md, knowledge.md) are updated in the same change that creates, changes, or retires a gotcha.

**DOC-4.** **Lockstep pairs, disclosed limitations, and staleness assumptions are documented at the site of the code**, not only in external documents (precedents: status-derivation pair, composite-source collision limitation).

**DOC-5.** **Every phase leaves a written trace**: what was added, what was deliberately excluded, and its regression suite. The codebase's legible phase history is an asset; keep it legible.

---

## Article XIV — Testing Philosophy

**TEST-1.** **Phase suites are permanent regression ledgers.** They are never deleted when code is refactored; they are the proof that composition did not change wrapped behavior.

**TEST-2.** **Tests must be able to fail the build.** A CI pipeline that ignores test results provides negative value — false confidence. (Audit: the current `|| true` is unconstitutional; no future pipeline may reproduce it.)

**TEST-3.** **Declare infrastructure honestly.** Tests requiring live services (Qdrant, Redis) are labeled as integration tests; unit tests run with no external dependencies. A contributor must be able to know, without running them, which tests need what.

**TEST-4.** **Never-raise claims are tested claims.** Every declared never-raise boundary has a test that forces the failure and asserts the structured degradation.

**TEST-5.** **Every bug fix ships with the regression test that would have caught it.**

**TEST-6.** **Concurrency claims require concurrency tests.** Any change touching shared state or the investigation lifecycle must demonstrate behavior under concurrent events (Audit gate #2: the absence of such tests is why the platform's most serious fault stayed invisible).

**TEST-7.** **Honesty states are test targets.** "Not consulted" vs "no signal" vs "insufficient data" distinctions are asserted in tests, because they are contractual behavior (PHIL-1), not presentation.

---

## Article XV — Definition of Done

A change is **Done** only when all of the following hold:

1. **Boundary-clean** — respects every affected component's responsibility and forbidden list; no layering violation (ARCH-4).
2. **Composed, not carved** — wraps or extends rather than edits, or satisfies the modification rules of Article XVIII.
3. **Degrades gracefully** — flagged if optional, falls back on construction failure, cannot break startup or the investigation loop.
4. **Honest** — every unavailable/failure state reports itself truthfully end-to-end (backend → findings → API → UI); no fabricated values anywhere.
5. **Idempotent where re-runnable** (ENG-4).
6. **Secured** — RBAC mapping updated for any route change (SEC-3); no new secret exposure; privileged surfaces treated as privileged.
7. **Observable** — participation visible in findings/metrics/logs with declared semantics (OBS-4).
8. **Compatible** — old persisted data still renders; old call sites still work; evidence/finding schemas only grew (Article XIX).
9. **Tested** — unit + regression coverage, never-raise proofs, honesty-state assertions; the phase ledger updated.
10. **Documented** — module contracts current, gotchas updated, lockstep pairs synchronized, placeholders labeled.
11. **Configured correctly** — new tunables are optional Settings overrides whose defaults live in the owning engine (ENG-6), with constraints declared once.
12. **Clean** — no debug prints, no root-level scratch files, no dead constructs (ENG-8).

---

## Article XVI — Enterprise Readiness Checklist

*A release may describe itself as enterprise-ready only when every item below is true. This list is the audit's four gates plus its recurring findings, frozen as acceptance criteria. It is a checklist, not a roadmap — it prescribes no order and no design.*

**Identity & access**
- [ ] Real key material end-to-end: token issuance, verification, rotation — no placeholder credentials anywhere (SEC-4).
- [ ] RBAC coverage parity with the entire API surface (SEC-3).
- [ ] Frontend authentication and role-aware UI; no unauthenticated console.
- [ ] Enterprise SSO/OIDC integration path exercised.

**Integrity & concurrency**
- [ ] Per-incident investigation state provably isolated under concurrent events (ARCH-8), with tests (TEST-6).
- [ ] Placeholder analysis quarantined from organizational memory and operator-facing conclusions (ENG-5).
- [ ] Approval semantics enforced or explicitly documented as advisory (AGENT-5).

**State & durability**
- [ ] All durable state (blobs, models, audit, configuration) survives instance recycle and is visible across instances (ARCH-7).
- [ ] Schema evolution mechanism in place; no hand-applied production DDL.
- [ ] Retention and backup/restore posture declared for every store (MEM-6).

**Operations**
- [ ] Production environment actually runs the autonomous loop, with debug surfaces off (SEC-8).
- [ ] CI gates on tests (TEST-2); dependency and image scanning in the pipeline.
- [ ] Metrics scraped, logs aggregated, failures alertable (OBS-6).
- [ ] Unbounded endpoints paginated; UI usable at a year of incident volume.
- [ ] LLM usage has cost visibility and limits (AI-6); guardrails wired (AI-7).
- [ ] Background workers supervised: a dead monitor or ingestion thread is detected, not discovered.

**Governance**
- [ ] Durable, attributable audit trail for operator and configuration actions (SEC-6, SEC-7).
- [ ] Data classification/PII posture stated for incidents, documents, and memory.
- [ ] Multi-tenancy position stated explicitly (supported, or single-tenant by declaration).

---

## Article XVII — Rules for Introducing New Technologies

**TECH-1.** **The burden of proof is on the new technology.** It must resolve a violation of this constitution or a documented audit gap that the existing stack demonstrably cannot. "Modern," "popular," and "standard elsewhere" are not arguments. (Constitutional precedent: BM25 was implemented in stdlib Python rather than adopting a retrieval framework; activation used the already-wired Redis rather than a new table.)

**TECH-2.** **Reuse-first test, in order:** (1) Can an existing shared singleton do this? (2) Can an existing pattern (wrapper, protocol, engine shape) do this? (3) Can an already-installed dependency do this? Only then may a new dependency be argued for.

**TECH-3.** **Orchestration and RAG frameworks are constitutionally rejected** (LangChain/Haystack/LlamaIndex-class). AEAM's value is that its reasoning pipeline is its own auditable code. Wrapping it in a framework destroys the explainability guarantees of Article X.

**TECH-4.** **New dependencies are scoped and lazy.** A heavy or optional dependency is imported at point of use so its absence degrades one capability (CODE-6). Every new dependency is recorded with its purpose in the requirements manifest.

**TECH-5.** **A new infrastructure service** (broker, database, cache, model host) is a constitutional-level decision: it must be justified against ARCH-1/ARCH-3, its state placement must satisfy ARCH-7, and its failure mode must satisfy PHIL-5 before any code depends on it.

**TECH-6.** **Model changes are technology changes.** Swapping the embedding model, reranker, or LLM provider invalidates stored vectors, thresholds, or prompt contracts; each such change must state its migration and re-validation consequences (RAG-4, AI-2).

---

## Article XVIII — Rules for Modifying Existing Components

**MOD-1.** **Wrapping is the default; editing is the exception.** To edit a component another component wraps or depends on, the change must demonstrate that (a) composition cannot achieve the goal, (b) the full regression ledger stays green, and (c) existing behavior is byte-identical for all existing inputs unless the change is an approved bug fix.

**MOD-2.** **The frozen core.** These may only be extended additively, never narrowed or reshaped: the `Event` model's existing fields; the findings entry convention (`type` + `data`, append-only); the evidence schema's existing keys; the retrieval `search` contract; the canonical investigation status vocabulary; the runbook catalog's safety invariant (reversible actions only); the never-raise contracts of declared boundaries.

**MOD-3.** **Placeholders are replaced, never grown.** New logic must not be layered onto a placeholder (the KPI investigation stub, the trigger baseline formula). Replacing a placeholder requires honoring the interfaces its consumers already depend on, and removing its labels everywhere they exist (code, docs, UI).

**MOD-4.** **Bug fixes fix the contract, not just the symptom.** A fix updates the docstring/contract if the documented behavior was wrong (e.g., a "most recent N" contract returning the oldest N), ships its regression test (TEST-5), and updates every documented gotcha it retires (DOC-3).

**MOD-5.** **Lockstep changes are atomic.** Both sides of a documented lockstep pair change in one commit (ENG-7).

**MOD-6.** **Known deliberate trade-offs may be revisited only knowingly.** Documented decisions (fail-open idempotency under Redis failure, synchronous investigation, restart-applied configuration) are not bugs; changing one requires acknowledging the original rationale in the change description and stating why the trade-off no longer holds.

**MOD-7.** **Deletion is a modification.** Removing a component requires proving nothing consumes it (code, tests, UI, docs) — and dead code discovered in that search is handled under ENG-8, not ignored.

---

## Article XIX — Rules for Preserving Backward Compatibility

**COMPAT-1.** **Persisted incidents render forever.** Every reader of findings (backend engines, APIs, UI) must handle every historical shape. New readers implement the established pattern: read the current form first, fall back for records that predate it, and never break on either. (Precedent: the UI's audit-summary-first helpers with legacy fallbacks.)

**COMPAT-2.** **Additive parameters, no-op defaults.** New constructor and function parameters default to `None`/no-op so every existing call site keeps its exact prior behavior. (Precedent: the entire D4 configuration layer.)

**COMPAT-3.** **Configuration never re-declares defaults.** New settings are optional overrides; the real default lives once, in the owning engine (ENG-6). Unset must always mean "exactly the behavior before this setting existed."

**COMPAT-4.** **API responses grow; they do not shrink.** Adding fields is safe; removing or renaming fields, changing types, or changing status semantics requires a versioned surface and a deprecation notice. The implicit frontend contract counts as a consumer.

**COMPAT-5.** **Schema changes are additive and idempotent.** Tables and columns are added with guarded DDL; nothing existing is dropped, renamed, or retyped outside an explicit, approved migration event (and until a migration mechanism exists, destructive change is simply prohibited).

**COMPAT-6.** **Vocabularies only grow.** Status values, finding types, job states, and action labels may gain members; existing members never change meaning. Consumers must tolerate unknown members gracefully.

**COMPAT-7.** **Evidence provenance is permanent.** Chunk IDs, citation semantics, and stored memory fields keep their meaning across releases; anything that would invalidate historical citations (e.g., changing chunk identity derivation) is a TECH-6 event with stated re-indexing consequences.

---

## Amendment

This constitution is permanent in intent, not frozen in text. An amendment requires: (1) a written proposal naming the laws it changes and why the audit-established rationale no longer holds; (2) demonstration that no existing law already accommodates the need; (3) explicit maintainer approval recorded in the document's history. Articles II (Core Philosophy) and X (Explainability) may be strengthened by amendment but never weakened: honesty is not negotiable.

*Adopted 2026-07-24, on the basis of the full-codebase read and Engineering Audit of the same date.*
