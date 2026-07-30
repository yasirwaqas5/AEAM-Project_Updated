# AEAM — Technical Review Board: Runtime Inconsistency Triage

**Input.** The 17 findings in [RUNTIME_ARCHITECTURE.md](RUNTIME_ARCHITECTURE.md) §13, derived from a runtime investigation of `main` @ `98c307c`.

**Output.** 22 triaged issues. Three documented findings were **split** because they bundle defects of different category and severity, and one was **escalated** on review.

| Split / escalation | Reason |
|---|---|
| §13.3 → **A** (queue retained) + **B** (`last_event_time` synthetic) | One is intentional design, the other is an instrumentation falsehood. Different verdicts. |
| §13.8 → **A** (second `LLMService`) + **B** (grounded root cause overwritten) | A is Low tech debt. B is a High record-integrity defect that the runtime document filed as a secondary note. |
| §13.16 → **A** (email recipient) + **B** (hardcoded doc date) + **C** (unreachable handlers) | A is a data-disclosure vector, not a hardcoding nit. See below. |
| §13.16A **escalated** | Filed in the runtime document as a minor hardcoded value. On review, `ops@company.com` is a **third-party registered domain**, and full incident reports are sent to it whenever SMTP is configured. That is exfiltration by default, not a config nit. Moved to Priority 1. |

**Assumption on "release."** Interpreted as the next deployment with `ENVIRONMENT != development`. This is the only reading that makes the wait/no-wait question answerable, because it is the point at which `SecurityMiddleware` begins enforcing, the placeholder JWT key is rejected, and `docker-compose.yml`'s `ENABLE_MONITOR_AGENT=true` default takes effect.

**Verdicts are recommendations.** The board has not modified any source file.

---

## Summary matrix

| # | § | Issue | Category | Sev | Fix? | Wait? | Risk | Prio |
|---|---|---|---|---|---|---|---|---|
| 1 | 13.1 | `SourceRepository` unbound — metric connectors never composed | Critical Bug | **Critical** | Yes | **No** | Low | **1** |
| 2 | 13.5 | `/health` database check runs no query | Instrumentation | **Critical** | Yes | **No** | Med | **1** |
| 3 | 13.16A | Incident reports emailed to `ops@company.com` | Critical Bug | **High** | Yes | **No** | Low | **1** |
| 4 | 13.14 | `HEARTBEAT_STALE_SECONDS` < `MONITOR_INTERVAL_SECONDS` | Configuration | **High** | Yes | **No** | Low | **1** |
| 5 | 13.8B | Depth-≥3 LLM overwrites grounded RAG root cause | Functional Bug | **High** | Yes | **No** | Low | **1** |
| 6 | 13.7 | RAG unreachable below `HIGH` severity | Functional Bug | High | Yes | Yes | Med | 2 |
| 7 | 13.2 | `run_simulation.py` publishes every event twice | Functional Bug | Medium | Yes | Yes | Low | 2 |
| 8 | 13.3B | `last_event_time` derived from an always-empty queue | Instrumentation | Medium | Yes | Yes | Low | 2 |
| 9 | 13.11 | `PlaceholderJobProcessor` is the worker's default | Technical Debt | Medium | Yes | Yes | Low | 2 |
| 10 | 13.6 | Triple `Settings()`, dual `RedisClient`, one pool never closed | Technical Debt | Medium | Yes | Yes | Low | 2 |
| 11 | 13.15 | Compiled-rule approval silently not in force until restart | Documentation | Medium | Optional | Yes | Low | 2 |
| 12 | 13.9 | `action_taken` scoring criterion is unreachable | Functional Bug | Medium | Optional | Yes | **High** | 2 |
| 13 | 13.16B | Startup knowledge docs carry a fixed `date` literal | Functional Bug | Low | Yes | Yes | Low | 3 |
| 14 | 13.4 | `LongTermMemory` vector client is a no-op stub | Documentation | Low | Yes | Yes | Low | 3 |
| 15 | 13.10 | `decisions` table created, never written | Technical Debt | Low | Optional | Yes | Low | 3 |
| 16 | 13.12 | `EventBus` documents `"*"`, composition root uses `"ALL"` | Documentation | Low | Yes | Yes | Low | 3 |
| 17 | 13.13 | Upload endpoint builds its own `IngestionSubmitter` | Technical Debt | Low | Yes | Yes | Low | 3 |
| 18 | 13.16C | `webhook` / `sheets` handlers in no runbook | Technical Debt | Low | Optional | Yes | Low | 3 |
| 19 | 13.8A | Depth-≥3 path constructs a second `LLMService` | Technical Debt | Low | Yes | Yes | Low | 3 |
| 20 | 13.3A | `EventPriorityQueue` retained with no producer/consumer | Intentional Design | Low | **No** | — | — | WF |
| 21 | 13.17 | `ENVIRONMENT=development` disables all security | Intentional Design | Low* | **No** | — | — | WF |

\* Low *as designed*. Critical if misdeployed — see the guard recommendation in issue 21.

---

## Priority 1 — must fix before release

### 1. `SourceRepository` is unbound in the composition root — metric connectors can never be composed

**§13.1** · Category: **Critical Bug** · Severity: **Critical**

**User impact.** An operator enables `CONNECTORS_ENABLED=true` plus `CONNECTOR_SNOWFLAKE_ENABLED=true`, sees the connector reported as `enabled: true` in `GET /api/v1/connectors/health`, watches its document-side sync succeed, and never receives a single KPI row from it. `container.metric_connectors` is permanently `[]`. The only evidence is one `ERROR` line at startup. SAP, Salesforce, Snowflake, and BigQuery — the entire metrics half of the F7 feature set shipped in the most recent commit — are non-functional, and the platform reports the opposite.

**Root cause.** [main.py:810](aeam/main.py:810) calls `SourceRepository(container.db).list_all()`. The name is never imported, assigned, or defined in the module (the `from aeam.registry.repositories import (...)` block at lines 38–49 omits it; confirmed by AST analysis of the module's bindings). It raises `NameError` at call time, which the surrounding `except Exception` on line 812 swallows into a log line and `_metric_connectors = []`. `aeam/api/connectors.py` imports the symbol *locally inside its handlers*, which is why the API surface works and masks the defect.

The deeper cause is the broad `except Exception` wrapping a **construction-time** block. It was written to absorb upstream connector failures — a legitimate goal — but it also absorbs programming errors in the composition root itself, converting a would-be startup crash into a silent capability loss.

**Should it be fixed?** **Yes.** A shipped feature is inoperative and health reports it as operative. That combination is the honesty violation this codebase's own charter treats as its worst defect class.

**Can it wait?** **No.** Not because default deployments are affected — they are not, both gating flags default off — but because the release publishes F7 as a capability. Shipping it means shipping a claim the code cannot honour.

**Risk of fixing.** **Low.** Adding the missing import is one line and cannot alter the default posture (the block is unreachable with connectors disabled). The accompanying `except` narrowing carries slightly more risk and should be scoped to *not* catching `NameError`/`AttributeError`/`ImportError`, so composition-root bugs surface loudly while upstream failures still degrade gracefully.

**Dependencies.** None to fix. Blocks all future connector-metrics work — no metric-connector behaviour can be validated until this lands. A regression test needs `CONNECTORS_ENABLED=true` + `CONNECTOR_MOCK_MODE=true` (the mock clients exist precisely to make this testable without credentials) asserting `metric_members > 0`.

---

### 2. `GET /health` reports `database: "ok"` without querying the database

**§13.5** · Category: **Instrumentation Issue** · Severity: **Critical**

**User impact.** With Postgres unreachable, `/health` returns `200 {"status": "healthy", "checks": {"database": "ok", ...}}`. Every automated failure-detection mechanism that consumes it is defeated: a load balancer keeps routing to a dead instance, a container orchestrator neither restarts nor rolls back, and the console StatusBar renders a green database indicator sourced directly from this field. Meanwhile every request that touches persistence fails. The platform's primary self-report is confidently wrong in exactly the scenario it exists to catch.

**Root cause.** [main.py:1477](aeam/main.py:1477):

```python
try:
    status["checks"]["database"] = "ok"
except Exception as e:
    status["status"] = "degraded"
    status["checks"]["database"] = f"error: {str(e)}"
```

The `try` body is a dictionary assignment, which cannot raise. The exception handler is unreachable and the value unconditional. This reads as an incomplete implementation: the scaffold for a real probe was written, the probe was never inserted, and the surrounding structure makes it look implemented. Redis and the queue in the same function *are* genuinely probed, which is what makes this field the one entry a reader would most reasonably trust.

**Should it be fixed?** **Yes.** Every other honesty control in this system depends on the health endpoint telling the truth.

**Can it wait?** **No.** The release moves the platform into an environment where an orchestrator is making restart and traffic decisions from this payload.

**Risk of fixing.** **Medium** — the highest of the Priority 1 set, and it needs sequencing rather than caution. Adding a real `SELECT 1` means `/health` can now return `503` where it previously returned `200`. That is the correct behaviour, but it is a live behaviour change: any pre-existing latent connectivity problem becomes a failed deploy or a restart loop on the first rollout. It also adds one round-trip per poll (console `HealthProvider` polls every 15s per open tab, plus orchestrator probes) — negligible against a pooled connection, but it should use the existing pool and a short timeout rather than opening a connection.

**Dependencies.** Sequence **after** issue 4 (§13.14). Both checks flip overall `status` to `degraded`, and landing them together makes it impossible to tell which one caused a deploy to fail. Fix the heartbeat noise first, confirm a quiet baseline, then make the database check real. Also verify against `deploy/cloudrun.yaml` and `docker-compose.yml` whether `/health` is wired as a **liveness** probe (restart) or a **readiness** probe (de-route) — the blast radius differs sharply, and the board did not confirm which.

---

### 3. Every finalized incident emails a full report to a third-party domain

**§13.16A** · Category: **Critical Bug** · Severity: **High**

**User impact.** `_finalize_incident` unconditionally attempts an email step to a hardcoded recipient list. When SMTP credentials are configured, every finalized incident sends `ReportAgent`'s full detailed report — root cause, evidence, confidence, executed actions, and any free-text the investigation surfaced — to an address at a domain the operator does not control and cannot revoke. Under the deployment's own declared `DATA_CLASSIFICATION=internal` posture, that is an outbound disclosure of internal operational data to an uncontrolled third party, occurring automatically on every incident with no configuration surface to stop it short of removing SMTP credentials entirely.

**Root cause.** [orchestrator.py:1455](aeam/agents/orchestrator/orchestrator.py:1455) passes `{"to": ["ops@company.com"], ...}`. This is placeholder-shaped content that survived into a path with real external side effects. It is currently masked because the repository's `.env` configures no SMTP credentials, so `EmailActions` raises `NonRetryableActionError` and the step is recorded as skipped — the defect is latent behind a missing credential, not behind a flag anyone reviewed.

**Should it be fixed?** **Yes.** No email should leave the platform to an address that is not operator-configured.

**Can it wait?** **No.** The gate is "did someone configure SMTP", which is a routine production step and not a decision anyone would associate with data egress.

**Risk of fixing.** **Low.** Introduce a required setting (e.g. `INCIDENT_REPORT_RECIPIENTS`), and — importantly — **skip the email step entirely when it is unset** rather than falling back to any default. A fail-closed default is the only correct posture for an egress path. Report the skip with a reason, matching how every other withheld action is recorded.

**Dependencies.** None. Interacts with issue 18 (§13.16C): both are symptoms of the action catalog having handlers and parameters that were never audited end-to-end against a real deployment.

---

### 4. Default heartbeat staleness threshold is shorter than the default monitor interval

**§13.14** · Category: **Configuration Issue** · Severity: **High**

**User impact.** `HEARTBEAT_STALE_SECONDS` defaults to 120; `MONITOR_INTERVAL_SECONDS` defaults to 300. `MonitorAgent` refreshes its heartbeat once per cycle, so with defaults the heartbeat is 120–300s old for roughly 60% of every cycle, and `/health` reports `monitor_agent: "stale"` and `status: "degraded"` → **HTTP 503** for the majority of the time. A perfectly healthy platform reports itself broken.

This is not hypothetical. `docker-compose.yml` sets `ENABLE_MONITOR_AGENT: "${ENABLE_MONITOR_AGENT:-true}"`, so a plain `docker compose up` with no overrides lands exactly in this state. If `/health` is wired as a liveness probe anywhere, the result is a **restart loop** — and each restart re-loads two transformer models and re-ingests the startup knowledge directory, so recovery is slow and the loop is expensive.

**Root cause.** Two independent defaults, set in different phases for different reasons, with a required relationship between them that is documented in prose (`HEARTBEAT_STALE_SECONDS`'s own docstring asks operators to tune it) and enforced nowhere. Prose is not a constraint.

**Should it be fixed?** **Yes.**

**Can it wait?** **No.** It is a single value, and the failure mode is a self-inflicted outage signal on the most standard startup command in the repository.

**Risk of fixing.** **Low.** Two options, and the board recommends **both**: (a) derive or floor the default so the threshold always exceeds the monitor interval with margin (e.g. `max(120, 2 × MONITOR_INTERVAL_SECONDS)`); (b) add a startup validator that refuses or loudly warns when `HEARTBEAT_STALE_SECONDS <= MONITOR_INTERVAL_SECONDS`. (a) fixes today's defaults; (b) prevents an operator recreating the bug by raising the interval — which is the more likely future occurrence.

**Dependencies.** Must land **before** issue 2 (§13.5). See that entry's sequencing note.

---

### 5. Depth-≥3 LLM reasoning unconditionally overwrites a grounded, validated root cause

**§13.8B** · Category: **Functional Bug** · Severity: **High**

**User impact.** An investigation that reached depth 3 with `LLM_ENABLED=true` discards a chunk-cited, guardrail-checked, grounding-validated RAG root cause and replaces it with unvalidated free-text from a second LLM call. `root_cause_source` flips from `"rag"` to `"llm_reasoning"`, and the persisted `chunk_ids` no longer correspond to the reported cause — the audit trail's citations point at evidence for a conclusion that is no longer on the record. Worse, `insight.get("root_cause", "Unknown")` means a response that parses as JSON but omits the key writes the literal string **`"Unknown"`** over a real grounded finding.

The asymmetry is the core of it. The RAG path passes `validate_output` (sensitive-pattern guardrail), `parse_llm_json`, **and** `RAGResponseValidator.validate` (grounding against retrieved chunks). The depth-≥3 path passes only `parse_llm_json`. **The least-validated writer wins.** `KPIAgent`, sitting a few lines away, demonstrates the intended contract explicitly: `if result.get("root_cause") and not ctx.stm.get("root_cause")`. The LLM path omits that guard.

**Root cause.** [orchestrator.py:881](aeam/agents/orchestrator/orchestrator.py:881) calls `ctx.stm.set("root_cause", ...)` with no precedence check. The stage was added as a last-resort escape hatch for investigations that had found nothing, and the "nothing was found" precondition was never encoded — so it fires whether or not something was found.

**Should it be fixed?** **Yes.** This is the fabricated-traceability defect class the codebase's own Article X language identifies as the worst possible failure in this platform, reachable through a two-line omission.

**Can it wait?** **No.** It cannot fire in the shipped production posture (`LLM_ENABLED` defaults false and `deploy/cloudrun.yaml` sets it false), which is a fair argument for deferring — the board rejects it on two grounds. First, the fix is a guard clause with no design question attached. Second, the local `.env` runs `LLM_ENABLED=true`, so every incident a developer or reviewer inspects today can carry a corrupted root cause, which erodes trust in the record precisely where it is being evaluated.

**Risk of fixing.** **Low.** Mirror `KPIAgent`'s precedence rule, and record the superseded LLM output as its own advisory finding rather than dropping it, so nothing is lost. Also remove the `"Unknown"` default — an absent key is a parse failure and belongs on the existing `llm_reasoning_error` path.

**Dependencies.** None to fix. **Must land before** issue 6 (§13.7): widening RAG to `MEDIUM`/`LOW` produces grounded causes on far more incidents, and every one of them would then be exposed to this overwrite.

---

## Priority 2

### 6. RAG is unreachable for `MEDIUM` and `LOW` severity

**§13.7** · Category: **Functional Bug** · Severity: **High**

**User impact.** `DecisionEngine.apply_priority_rules` routes only `CRITICAL` and `HIGH` to `agents=["KPI","RAG"]`; everything else gets `agents=["KPI"]`. Because `MonitorAgent._derive_severity` assigns `HIGH` only at ≥2 detection signals, **a single-signal autonomous detection is `MEDIUM` and therefore never performs document retrieval.** Those incidents close with `validation_status: "SKIPPED"`, zero retrieved chunks, no citations, and a root cause that can only be `KPIAgent`'s statistical characterisation — which by explicit design states *what* changed and never *why*. The document corpus, the five-stage retrieval pipeline, and the entire policy-and-runbook knowledge base contribute nothing to the most common class of autonomously-detected incident.

A second emergent consequence: because `CRITICAL`/`HIGH` return early at `confidence >= 0.9`, `DecisionEngine`'s own LLM augmentation path is reachable **only** for `MEDIUM`/`LOW` at depth > 2 — precisely the severities where RAG is skipped. The two intelligence escalation paths are mutually exclusive by accident.

**Root cause.** Two independently reasonable decisions that were never composed: a severity-gated agent routing table (defensible as LLM cost control) and a signal-count-based severity derivation. Nobody re-derived the intersection. The proof is in the code itself — the method's docstring table still documents the *pre-change* values (`agents [KPI]` for CRITICAL/HIGH, `0.70` for MEDIUM/LOW) directly above a dict that contradicts it, with a bare `# <-- restored to original value` comment and no rationale.

**Should it be fixed?** **Yes**, in two separable parts. The **docstring correction is unambiguous and should land immediately** — a docstring that contradicts the dict beneath it will keep generating wrong conclusions. The **routing change requires a product decision** the board cannot make: enabling RAG for `MEDIUM`/`LOW` multiplies retrieval load and LLM spend across the highest-volume severity band.

**Can it wait?** **Yes.** It degrades investigation quality; it breaks nothing. The board's recommendation is to ship with the current routing, land the docstring fix, and treat the routing decision as a scoped follow-up with measured cost.

**Risk of fixing.** **Medium.** Latency, spend, and Qdrant/cross-encoder load all rise on the highest-volume path. If pursued, prefer a targeted widening (e.g. `MEDIUM` gets RAG only at depth ≥ 2, or only when `KPIAgent` returned no grounded characterisation) over a blanket change, and measure against `docs/PERFORMANCE_BASELINES.md` before and after.

**Dependencies.** Docstring fix: none. Routing change: **after** issue 5 (§13.8B); should be measured against the recorded performance budgets, which gate CI.

---

### 7. `run_simulation.py` publishes every event twice

**§13.2** · Category: **Functional Bug** · Severity: **Medium**

**User impact.** `MonitorAgent.process_kpi` already publishes on success; the script publishes the returned event again, bypassing `EventDeduplicator` (which runs *inside* `process_kpi`, before its own publish). One simulated anomaly yields two full investigations, two `incidents` rows, **two sets of real external side effects** (duplicate Slack messages, duplicate Jira tickets, duplicate emails — `ActionAgent`'s idempotency is keyed on `incident_id`, which differs between the two investigations, so it does not suppress them), and double LLM spend.

The consequence that outlasts the demo: duplicated incidents pollute every downstream aggregate computed from `incidents`. `LearningAgent` calibration fits against them, `BusinessGraphBuilder` derives edges from them, and `ObservabilityEngine` summarises them. Any calibration or graph built after running the documented demo path is fitted on inflated data.

**Root cause.** `process_kpi`'s publish is a side effect its name does not advertise, and the script — written when the responsibility split was less settled — treats it as a pure factory and publishes the result itself.

**Should it be fixed?** **Yes.** It is the documented onboarding and end-to-end demo path, referenced in `CLAUDE.md` and in the console's own empty states.

**Can it wait?** **Yes.** It is a developer tool; no production path touches it.

**Risk of fixing.** **Low.** Delete the redundant publish. Worth also noting for the same file: the injected `DummyForecastAgent` always returns `is_deviation: False`, so the demo never exercises the forecast signal and always produces a single-signal `MEDIUM` event — which, per issue 6, is exactly the severity that gets no RAG. The documented demo therefore showcases the platform at its least capable.

**Dependencies.** None.

---

### 8. `last_event_time` is derived from a queue that is always empty

**§13.3B** · Category: **Instrumentation Issue** · Severity: **Medium**

**User impact.** `GET /api/v1/system/status` returns a `last_event_time` derived from `EventPriorityQueue`, which has no producer (issue 20) and is therefore permanently empty — so the value falls back to the current UTC timestamp on every call. The field **always** reports "just now" regardless of whether the platform has processed an event in the last minute or has been idle for a week. It is a liveness indicator that cannot indicate anything, and it is indistinguishable from a working one.

**Root cause.** The field was implemented against the queue when the queue was expected to carry events. `MonitorAgent`'s push was removed in Phase E1; this consumer of it was not revisited. The endpoint's own docstring is candid ("taken from the container's priority queue size as a proxy"), but the response field name promises a measurement.

**Should it be fixed?** **Yes.** Either derive it from real data — `SELECT MAX(timestamp) FROM incidents` is index-backed by `idx_incidents_timestamp` — or return `null` with a stated reason. Both are honest; a synthesised "now" is not.

**Can it wait?** **Yes.** No automated decision consumes it.

**Risk of fixing.** **Low.** A consumer expecting a non-null string must tolerate `null` if that option is chosen; the `MAX(timestamp)` option avoids the contract change entirely and is the board's preference.

**Dependencies.** Related to issue 20 but independently fixable, and should be fixed independently — the queue stays, this field should not depend on it.

---

### 9. `PlaceholderJobProcessor` is `IngestionWorker`'s default processor

**§13.11** · Category: **Technical Debt** · Severity: **Medium**

**User impact.** None today — the composition root always injects a `RoutingJobProcessor`. The exposure is the failure *mode*: any caller that constructs `IngestionWorker` without a processor gets a worker that marks every job **100% complete and `DONE`** with stage text `"placeholder — no extraction/embedding implemented yet (Phase B1.2)"`, having extracted nothing, embedded nothing, and indexed nothing. Documents would show as successfully ingested and be permanently invisible to retrieval. Silent success is the most expensive failure shape available, and it is the default argument.

**Root cause.** [worker.py:106](aeam/ingestion/worker.py:106): `self._processor = processor or PlaceholderJobProcessor()`. A Phase B1.2 infrastructure-only scaffold retained as a convenience default after the real processors arrived.

**Should it be fixed?** **Yes.** Make `processor` a required constructor argument and delete the placeholder, or move it under an explicitly-named test double.

**Can it wait?** **Yes.** Unreachable in production.

**Risk of fixing.** **Low.** Requires auditing every `IngestionWorker(...)` construction site (production + tests) for reliance on the default.

**Dependencies.** None.

---

### 10. Three `Settings()` instances, two `RedisClient` instances, one connection pool never closed

**§13.6** · Category: **Technical Debt** · Severity: **Medium**

**User impact.** Two live configuration objects govern one process. `create_app()`'s instance drives `SecurityMiddleware`'s `environment` — and therefore the entire development auth bypass — plus `app.state.settings`. The lifespan's instance drives every agent and engine. They read the same `.env` and normally agree, so there is no user-visible impact today. The hazard is a posture split: any sequence that changes the environment between `create_app()` and lifespan entry (a test that patches `os.environ`, a wrapper that mutates config before startup) yields a process where **the security middleware and the agents disagree about which environment they are in**. That failure would present as an inexplicable auth behaviour, and nothing in the code would point at the cause.

Separately, `create_app()`'s `RedisClient` (owned by `RateLimiter`) is never closed — the shutdown hook only closes `container.redis`. One leaked pool per process, not per request, so it does not grow; it is untidy rather than dangerous.

**Root cause.** A genuine ordering constraint: middleware must be registered in `create_app()`, before the lifespan builds the container, so it cannot use the container's clients. The constraint is real; the response — construct duplicates — was the expedient one rather than deferring resolution into the lifespan.

**Should it be fixed?** **Yes**, as a clarity and correctness-hazard fix.

**Can it wait?** **Yes.** No current impact.

**Risk of fixing.** **Low** if scoped to sharing the single `Settings`/`RedisClient` via `app.state` and closing both at shutdown. **Medium** if it turns into restructuring middleware registration — do not let it.

**Dependencies.** None. Touches the security bootstrap, so it wants a deliberate review rather than a drive-by edit.

---

### 11. An approved compiled rule is silently not in force until restart

**§13.15** · Category: **Documentation Issue** · Severity: **Medium**

**User impact.** An operator approves a compiled rule through the governance API, receives a success response, sees the rule marked adopted in the console, and detection behaviour does not change — possibly for weeks, until the process next restarts. There is no indication anywhere in the response or the UI that the approval is pending activation. The operator's mental model ("I have adopted this rule, therefore it is enforced") is wrong, and nothing corrects it.

The asymmetry sharpens it: **dataset activation is re-read every monitor cycle and takes effect immediately**, while rule adoption requires a restart. Two governance surfaces in the same console behave oppositely, and neither says so.

**Root cause.** The restart-applied behaviour itself is **Intentional Design** — `PolicyAgent.active_overrides()` is read once at [main.py:852](aeam/main.py:852), deliberately reusing Phase D4's documented restart-applied configuration posture rather than introducing a second live-reload mechanism. That trade-off is defensible. What is missing is *disclosure*: the decision was recorded in a code comment and never surfaced to the operator taking the action.

**Should it be fixed?** **Optional** — and the board recommends doing it, because the cost is a response field and the alternative is an operator believing a control is active when it is not.

**Can it wait?** **Yes.**

**Risk of fixing.** **Low** for the disclosure route: add `pending_activation: true` (with a reason) to the approval response and render it in the console. **High** for the alternative route of making overrides live-reloadable — that introduces a second dynamic-configuration mechanism in the detection path, which is the thing the original decision correctly avoided. **Do not take that route.**

**Dependencies.** Backend field + frontend surfacing; coordinate so they ship together.

---

### 12. The `action_taken` scoring criterion is unreachable

**§13.9** · Category: **Functional Bug** · Severity: **Medium**

**User impact.** `EvaluationEngine` documents and implements four additive criteria totalling 1.0, awarding `+0.1` for `memory.get("action_taken") is True`. `handle_event` sets `action_taken = False` at initialisation and **nothing writes it again during the investigation loop** — actions run only in `_finalize_incident`, after evaluation has concluded, and the outcome goes into the persistence payload rather than back into STM. The criterion can never fire. The real maximum score is 0.9.

This is outcome-relevant, not cosmetic. An incident with a root cause (+0.4) and ≥3 evidence items (+0.3) but confidence ≤ 0.8 scores 0.7 — below the 0.8 STOP threshold — and therefore CONTINUEs to depth 5 and ESCALATEs. Had the fourth criterion been reachable it would have scored 0.8 and STOPped. The documented model and the running model disagree about which incidents resolve and which escalate.

**Root cause.** A lifecycle ordering constraint, not an omitted write: evaluation necessarily precedes action execution, so no correct implementation could set `action_taken` before scoring. The criterion encodes a signal that does not exist at the time it is read. It is a design residue from a lifecycle where actions were expected mid-loop.

**Should it be fixed?** **Optional**, and the board is deliberate about the direction. The correct fix is to **delete the criterion and re-document the model as three criteria** — possibly re-weighting the remaining three to total 1.0. Do **not** attempt to make it live: that would require executing actions before the investigation has concluded, inverting the human-approval gate's position in the lifecycle. The cure is far worse.

**Can it wait?** **Yes.**

**Risk of fixing.** **High** — the highest in this report, and it is the reason this sits in Priority 2 rather than 3. Any re-weighting changes the STOP/CONTINUE/ESCALATE distribution for every future incident, which changes investigation depth, LLM spend, and escalation volume. It also **invalidates every fitted calibration model**: Phase F2 calibrations were fitted against confidence values produced under the current distribution, so a re-weighting silently degrades an active calibration's accuracy. Deleting the criterion without re-weighting (accepting a 0.9 maximum, documented) is the low-risk variant and the board's recommendation.

**Dependencies.** Any re-weighting **must** be paired with a forced recalibration and a review of `docs/adaptive_learning.md`. Sequence last of all Priority 2 work.

---

## Priority 3

### 13. Startup knowledge documents carry a fixed `date` literal

**§13.16B** · Category: **Functional Bug** · Severity: **Low**

**Impact.** [main.py:317](aeam/main.py:317) tags every startup-ingested document with `"date": "2026-07-04"`. `BusinessRelevanceScorer` reads `date` to award a recency bonus (`RETRIEVAL_RECENCY_BONUS` within `RETRIEVAL_RECENCY_WINDOW_DAYS`, default 30). As wall-clock time passes that literal ages, so the startup runbooks' ranking silently decays relative to uploaded documents — and the decay is invisible because nothing reports the value's provenance. **Fix:** Yes — use the file's mtime or the ingestion timestamp. **Wait:** Yes. **Risk:** Low; it changes retrieval ranking for the startup corpus, so re-run the retrieval evaluation harness (`aeam/tests/retrieval_eval.py`). **Dependencies:** none.

### 14. `LongTermMemory`'s vector client is a no-op stub

**§13.4** · Category: **Documentation Issue** · Severity: **Low**

**Impact.** [main.py:405](aeam/main.py:405) injects `_NoOpVectorClient` (all methods `pass`), so `LongTermMemory`'s documented capability — "vector storage to support embedding-based retrieval of historical incidents and decisions" — is inert. Functionally harmless: `EnterpriseMemoryEngine` provides real incident vectors through its own Qdrant collection. The cost is that a reader of `LongTermMemory` or its `VectorClient` protocol believes a subsystem exists that does not. **Fix:** Yes — correct the docstring to state that vector persistence lives in `EnterpriseMemoryEngine`, and consider making the parameter `Optional[...] = None` so the stub class is unnecessary. **Wait:** Yes. **Risk:** Low. **Dependencies:** none.

### 15. `decisions` table created, never written

**§13.10** · Category: **Technical Debt** · Severity: **Low**

**Impact.** The table is created at every startup and by migration `0001`; `DatabaseClient.insert_decision` and `LongTermMemory.log_decision` implement writes to it; no production call site invokes either. Decisions are recorded inside `incidents.findings` instead. Every deployment carries a permanently empty table and two live-looking unused methods — a maintenance trap for anyone who assumes decision history is queryable relationally. **Fix:** Optional. Prefer documenting it as intentionally unused (dropping it requires a migration and forecloses a plausible future normalisation). **Wait:** Yes. **Risk:** Low to document; Medium to drop (irreversible migration). **Dependencies:** dropping requires a migration revision plus the E5 startup-DDL/migration parity test.

### 16. `EventBus` documents `"*"` as the catch-all; the composition root registers `"ALL"`

**§13.12** · Category: **Documentation Issue** · Severity: **Low**

**Impact.** `publish` dispatches `handlers[event_type] + handlers["*"] + handlers["ALL"]`; `register_handler`'s docstring documents only `"*"`; [main.py:1161](aeam/main.py:1161) uses `"ALL"`. Both work. A developer following the docstring registers under `"*"` and gets correct-but-differently-ordered dispatch relative to the Orchestrator — a subtle ordering surprise in the one place ordering matters. **Fix:** Yes — document both keys and their dispatch order, or converge on one. **Wait:** Yes. **Risk:** Low to document; Medium to converge (touches the sole handler registration and the tests that use `"ALL"`). **Dependencies:** none.

### 17. Upload endpoint constructs its own `IngestionSubmitter`

**§13.13** · Category: **Technical Debt** · Severity: **Low**

**Impact.** The lifespan builds `container.ingestion_submitter` and describes it as "the ONE ingestion entry point, shared with the upload API" ([main.py:783](aeam/main.py:783)); the upload endpoint instead constructs a fresh instance per request. Same class, same dependencies, identical behaviour today. The exposure is future: any state, cache, or metric added to `IngestionSubmitter` would apply to connector syncs and silently not to uploads — the exact divergence the shared-instance design exists to prevent. **Fix:** Yes — use `container.ingestion_submitter`. **Wait:** Yes. **Risk:** Low. **Dependencies:** none.

### 18. `webhook` and `sheets` action handlers appear in no runbook

**§13.16C** · Category: **Technical Debt** · Severity: **Low**

**Impact.** Both are registered in `ActionAgent`'s registry with circuit breakers, and no runbook's `action_plan` references either, so neither is reachable from any investigation. They are maintained, tested surface with no execution path. Note that `is_gated_step` correctly defaults unknown steps to *gated*, so adding them to a runbook later is safe by default. **Fix:** Optional — either add them to a runbook where they belong or document them as API-only handlers. **Wait:** Yes. **Risk:** Low to document; Medium to wire into a runbook (introduces new external side effects into the investigation path, which needs the same review any new action would). **Dependencies:** none.

### 19. Depth-≥3 path constructs a second `LLMService`

**§13.8A** · Category: **Technical Debt** · Severity: **Low**

**Impact.** [orchestrator.py:839](aeam/agents/orchestrator/orchestrator.py:839) instantiates `LLMService(settings=self._settings)` per investigation pass rather than using the injected shared instance. `Settings.LLM_TIMEOUT_SECONDS`'s own documentation asserts that all six call sites "share one LLMService instance, so one setting governs all six" — they do not; this is a seventh. Behaviour is near-equivalent (same `Settings`, module-level metrics collectors, provider check re-runs), but per-instance state such as `LLMService`'s circuit breaker is not shared, so this call path can hammer a failing provider that the shared client has already circuit-broken. **Fix:** Yes — use the injected service. **Wait:** Yes. **Risk:** Low. **Dependencies:** land alongside issue 5 (§13.8B); both edit the same block.

---

## Won't Fix — Intentional Design

### 20. `EventPriorityQueue` retained with no producer and no consumer

**§13.3A** · Category: **Intentional Design** · Severity: **Low**

**Verdict: No fix.** The decision is explicit and documented at [main.py:260-265](aeam/main.py:260): a correct, tested primitive whose producer was removed in Phase E1 because it was unbounded and never drained, retained because `/health` and `/api/v1/system/status` report its size and the response field must not disappear (COMPAT-4), with a real consumer expected in the concurrency work. That is a coherent compatibility argument, honestly recorded.

**One rider.** The queue stays; `last_event_time` should stop depending on it (issue 8). Retaining a zero-valued field for compatibility is defensible. Deriving a *timestamp* from that zero and reporting it as a measurement is not — the compatibility argument covers the field's existence, not a fabricated value.

### 21. `ENVIRONMENT=development` disables all authentication, RBAC, and rate limiting

**§13.17** · Category: **Intentional Design** · Severity: **Low as designed** / **Critical if misdeployed**

**Verdict: No fix to the behaviour.** It is deliberate, documented in `CLAUDE.md`, warned about in the settings docstrings, and matched by a fail-closed contract everywhere else (non-development startup aborts without real JWT key material; OIDC aborts in every environment if half-configured). `SecurityMiddleware` returning before any check at [security_middleware.py:352](aeam/middleware/security_middleware.py:352) is the intended local-development affordance.

**Two riders the board does want on the record.**

First, **every RBAC statement in the runtime documentation is unenforced in the local posture.** Anyone reasoning about authorisation from a development instance is reasoning about a different system. That belongs in the README (see RUNTIME_ARCHITECTURE.md §14 item 5), not in code.

Second, and this is the one worth acting on: the only thing standing between this bypass and a production incident is a single environment variable. The existing controls are strong for *key material* (non-development startup aborts without a real key) but there is **no positive assertion that a deployment artifact never ships `ENVIRONMENT=development`**. `docker-compose.yml` already defaults to `production` and comments on exactly this risk, which shows the concern was anticipated. **Optional, low-risk hardening:** a CI check asserting no tracked deployment artifact sets `ENVIRONMENT=development`, and a startup log line at `WARNING` or above that states unambiguously that all authorisation is disabled. Neither changes the design; both make misdeployment loud. This is hardening of an accepted design, not a reversal of it.

---

## Recommended implementation order

Three waves. Ordering within a wave is by dependency, not severity.

### Wave A — before release (5 issues)

| Step | Issue | Why here |
|---|---|---|
| A1 | **1** — `SourceRepository` import (+ narrow the `except`, + mock-mode smoke test) | Independent, one line, unblocks all connector-metrics validation. Do it first so the F7 claim is true at release. |
| A2 | **4** — heartbeat/interval defaults + startup validator | Must precede A3. Also stops `docker compose up` from self-reporting 503. |
| A3 | **2** — real database probe in `/health` | **After A2.** Both flip overall status; landing them together makes a failed deploy undiagnosable. Confirm a quiet baseline from A2 first, then make the DB check real. Verify liveness-vs-readiness wiring before merging. |
| A4 | **5** — root-cause precedence guard (+ **19**, same block) | Independent. Record-integrity fix; two-line guard. Must precede any Wave C work on issue 6. |
| A5 | **3** — operator-configured email recipients, fail-closed when unset | Independent. Closes an egress path before the platform reaches an environment with real SMTP. |

**Wave A exit criteria.** A connector-metrics smoke test passes with mock mode on; `/health` returns 503 within one poll of stopping Postgres and 200 with a healthy MonitorAgent under default settings; no email is dispatched when recipients are unconfigured; a grounded RAG root cause survives a depth-5 investigation with `LLM_ENABLED=true`.

### Wave B — after release, low risk, no behavioural blast radius (7 issues)

| Step | Issue | Note |
|---|---|---|
| B1 | **7** — `run_simulation.py` redundant publish | Fix the `DummyForecastAgent` demo weakness at the same time. |
| B2 | **8** — `last_event_time` from `MAX(incidents.timestamp)` | Index-backed; no contract change. |
| B3 | **9** — require an explicit `IngestionWorker` processor | Audit all construction sites. |
| B4 | **6 (docstring only)** — correct the `DecisionEngine` severity table | Do **not** bundle the routing change. |
| B5 | **14, 15, 16, 17, 18** — documentation and dead-surface cleanup batch | One reviewable changeset; no runtime behaviour changes. |
| B6 | **10** — single shared `Settings` + `RedisClient`, both closed at shutdown | Last in the wave: touches the security bootstrap, wants a deliberate review. |

### Wave C — requires a decision or carries behavioural blast radius (4 issues)

| Step | Issue | Gate |
|---|---|---|
| C1 | **11** — `pending_activation` disclosure on rule approval | Needs backend + frontend shipped together. Take the disclosure route, not live reload. |
| C2 | **13** — real ingestion date for startup documents | Re-run `aeam/tests/retrieval_eval.py`; ranking changes. |
| C3 | **6 (routing)** — widen RAG below `HIGH` | **Requires a product/cost decision.** Gated on Wave A4. Measure against `docs/PERFORMANCE_BASELINES.md` before and after. Prefer targeted widening over blanket. |
| C4 | **12** — `action_taken` criterion removal or re-weighting | **Last.** Any re-weighting invalidates active F2 calibrations and must be paired with a forced recalibration. Prefer deletion + documentation (0.9 maximum, stated) over re-weighting. |

### Optional, unsequenced

Issue **21**'s hardening riders (CI assertion that no tracked artifact sets `ENVIRONMENT=development`; unmissable startup warning when authorisation is disabled). Independent of every other item; can land in any wave.

---

## Priority buckets

**Priority 1 — must fix before release**
1. §13.1 — `SourceRepository` unbound; metric connectors never composed *(Critical Bug / Critical)*
2. §13.5 — `/health` database check runs no query *(Instrumentation / Critical)*
3. §13.16A — incident reports emailed to a third-party domain *(Critical Bug / High)*
4. §13.14 — heartbeat threshold shorter than monitor interval *(Configuration / High)*
5. §13.8B — depth-≥3 LLM overwrites grounded root cause *(Functional / High)*

**Priority 2**
6. §13.7 — RAG unreachable below `HIGH` *(Functional / High — docstring now, routing later)*
7. §13.2 — `run_simulation.py` double publish *(Functional / Medium)*
8. §13.3B — `last_event_time` synthetic *(Instrumentation / Medium)*
9. §13.11 — `PlaceholderJobProcessor` default *(Technical Debt / Medium)*
10. §13.6 — duplicate `Settings`/`RedisClient` *(Technical Debt / Medium)*
11. §13.15 — rule approval not in force until restart *(Documentation / Medium)*
12. §13.9 — `action_taken` criterion unreachable *(Functional / Medium — highest fix risk in the report)*

**Priority 3**
13. §13.16B — hardcoded startup document date *(Functional / Low)*
14. §13.4 — no-op vector client *(Documentation / Low)*
15. §13.10 — unused `decisions` table *(Technical Debt / Low)*
16. §13.12 — `EventBus` wildcard docstring *(Documentation / Low)*
17. §13.13 — upload builds its own submitter *(Technical Debt / Low)*
18. §13.16C — unreachable `webhook`/`sheets` handlers *(Technical Debt / Low)*
19. §13.8A — second `LLMService` instance *(Technical Debt / Low)*

**Won't Fix — Intentional Design**
20. §13.3A — `EventPriorityQueue` retained for API compatibility *(rider: decouple issue 8 from it)*
21. §13.17 — `ENVIRONMENT=development` security bypass *(riders: CI artifact assertion + loud startup warning)*

---

## Board observations

Four patterns account for most of this report, and they are more useful than the individual findings.

**1. Broad `except Exception` around construction, not just operation.** Issue 1 is a `NameError` in the composition root converted into a silent capability loss. The pattern is used correctly and deliberately for *upstream* failures — a connector that cannot authenticate must not stop startup — but applying it to a block that also contains the platform's own wiring means composition-root bugs degrade instead of crashing. **Recommendation:** in composition code, exclude `NameError`, `AttributeError`, `ImportError`, and `TypeError` from broad handlers. Those are never upstream failures.

**2. Placeholder-shaped values on paths with real side effects.** `ops@company.com` (issue 3), `"date": "2026-07-04"` (issue 13), `expected_value = value * 2` in the trigger API, and `"dummy-public-key"` in development. The codebase is unusually disciplined about *labelling* placeholders — `root_cause_source="placeholder"` and its Enterprise Memory quarantine are exemplary — but that discipline was applied to *analytical output* and not to *configuration and egress*. **Recommendation:** extend the placeholder audit to every value that leaves the process.

**3. Two settings with a required relationship and no enforcement.** Issue 4 is the acute case; issue 12's calibration-invalidation coupling is the same shape. Prose in a docstring is not a constraint. **Recommendation:** where two settings must relate, add a startup validator. `Settings` is already a Pydantic model — a model validator is the natural home and costs nothing.

**4. Validation asymmetry between writers of the same field.** Issue 5 is the sharpest instance: three writers can set `root_cause`, and they pass three different validation depths, with the least-validated one winning by virtue of running last. `KPIAgent` alone encodes the precedence rule. **Recommendation:** when several components write one field, put the precedence rule in one place that all of them go through, rather than in each writer's own discretion.

**Countervailing observation, and the board wants it on the record.** The reason a review this specific was possible in two passes is that the codebase documents its own trade-offs in the code, at the decision site, with the rationale attached — the `EventPriorityQueue` retention note, the E1 scheduler-removal comment, the E9 gating classification, the "no environment backdoor" note on `ENABLE_MONITOR_AGENT`. Several findings above are *only* findings because a comment stated the intent clearly enough to compare against the behaviour. Issue 11 is the exception that proves it: the trade-off was recorded in a comment for developers and never surfaced to the operator taking the action. **The gap is not discipline; it is that internal rationale has not been carried through to the operator-facing surface.** That is a narrower and much more tractable problem than it first appears.
