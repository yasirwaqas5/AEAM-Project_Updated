# The Agent Mesh — Supervisor & Planning Agents

**Phase F6 — Agent Mesh Formalization.**

AEAM has run an agent mesh since Phase 2, but two roles the enterprise
architecture names were left implicit:

* **nothing observed the mesh as a whole.** Individual agents reported their
  own metrics, `/health` checked two background threads, and D3/E11 summarised
  investigation quality — so "is the mesh healthy, and is any agent behaving
  oddly?" lived in an operator's head across four dashboards.
* **execution planning was an engine, not an agent.** C7's
  `ExecutionPlanningEngine` did the work correctly but had no roster entry, no
  heartbeat, no metric label, and no stable named contract like RAG, Report,
  and Action have.

F6 formalizes both — and does it without adding a second coordinator.

---

## The single-coordinator invariant is untouched

ARCH-1 gives AEAM exactly one coordinator: the Orchestrator. This phase does
not change that, and the Supervisor is not a partial exception to it.

**The Supervisor observes. It has no coordination authority whatsoever.**
Enforced structurally, not by convention:

| Guarantee | How it is enforced |
|---|---|
| Cannot dispatch, execute, or decide | Imports no `Orchestrator`, `ActionAgent`, `PlanningAgent`, `EventBus`, `RuleEngine`, or LLM client |
| Cannot be *given* a coordinator | Its constructor takes only telemetry providers — there is no slot for one |
| Has no actionable method | No `handle_event` / `execute` / `dispatch` / `coordinate` / `restart` / `plan`; its only public members are `observe` and `name` |
| Its API cannot act either | Every `/api/v1/mesh` route is a `GET`; there is deliberately no endpoint to act on an observation |

It may observe, summarise, detect behaviour anomalies, and **recommend**
escalation. Recommending is where its authority ends — an operator acts
through the existing gated surfaces (AGENT-5). An advisory agent whose
recommendation restarts a worker is not advisory; it is a supervisor with a
confirmation dialog.

---

## Planning: a promotion by composition

`PlanningAgent` wraps the **same** C7 engine. `plan()` forwards its keyword
arguments unchanged and returns **the engine's own object** — no wrapping key,
no normalisation, no defaulting, no post-processing.

```python
def plan(self, **kwargs):
    ...
    return self._engine.plan(**kwargs)
```

The signature is `**kwargs` deliberately: restating the engine's parameter
list would be a second place for the contract to live, and therefore a second
place for it to drift.

**Why that matters.** The Orchestrator's planning stage just calls `.plan(...)`
on whatever it was given, so passing the agent where the engine used to go is
a drop-in — **zero orchestrator changes** — and the `execution_plan` finding it
appends is identical field for field (COMPAT-1).

Output equivalence is asserted across the engine's whole behavioural range
(no evidence, policy match, memory match, RAG causes, all sources, escalation,
low severity, no runbook), plus structurally: the agent returns the engine's
*same object*, and forwards every argument verbatim.

What the wrapper adds is standing, and only standing:

| Addition | Mechanism |
|---|---|
| Roster entry | `container.agent_roster` gains `planning` |
| Heartbeat | `heartbeat_tracker.record("planning")` (E7) |
| Metric | `agent_execution_time{agent="planning"}` (E11) |
| Trace span | `agent.planning`, nested in the investigation trace |

Both the metric and the heartbeat are recorded in a `finally` block, so a
raising engine still proves the agent ran: *"the planning agent was invoked and
failed"* must not read as *"the planning agent was never invoked"*. The
exception itself is never swallowed — a hidden planning failure would make the
plan silently absent from the investigation.

### Two timings, two questions

The Orchestrator's F5 stage timing (recorded under `execution_plan`) is
deliberately left alone. The two measure different things:

* `execution_plan` — *how long the planning STAGE of this investigation took*,
  persisted per incident for Timeline Replay;
* `planning` — *how long the planning AGENT has spent working*, aggregated
  across incidents in `/metrics`.

Neither replaces the other, and collapsing them would cost one of the two
questions an answer.

---

## Supervision: evidence, never invention

The Supervisor consumes **only telemetry that already existed**. No new
collector, no new table, no polling loop; the report is computed on read.

| Source | Used for |
|---|---|
| E7 `HeartbeatTracker` | per-agent liveness — the same tracker `/health` reads |
| E11 `agent_execution_time` | per-agent execution counts, read from the collector object rather than by re-scraping `/metrics` |
| E11 `action_success_total` / `action_failure_total` | action-outcome anomalies |
| E1 `agent_roster` | which agents this process actually constructed |
| D3/E11 observability summary | measured participation, reused not recomputed (ENG-6) |

### Heartbeat semantics are not uniform, and the report says so

| Scope | Agents | An old heartbeat means |
|---|---|---|
| `thread_worker` | `monitor`, `ingestion` | **stale** — the thread died or is wedged. A real fault. |
| `request_scoped` | `planning`, `supervisor`, `report`, `rag`, `kpi` | **idle** — the platform has been quiet. Not a fault. |

Reporting a request-scoped agent as stale would flag a healthy idle system as
broken. `/health` applies the same distinction: the two F6 agents are reported
there **informationally** and never degrade platform status.

### Detected anomalies

Every one is grounded in an observed number, and every one discloses the
**affected agent**, the **evidence** (with its source), a plain **reason**, and
a **recommended escalation**:

| Kind | Severity | Grounded in |
|---|---|---|
| `stale_heartbeat` | critical | heartbeat age vs `HEARTBEAT_STALE_SECONDS` |
| `no_heartbeat` | warning | a thread worker on the roster that never reported |
| `silent_agent` | warning | zero executions across N recorded investigations |
| `zero_participation` | info | a measured participation rate of 0 despite being consulted |
| `action_failure_rate` | warning / critical | failures > 50% / ≥ 90% of dispatches |
| `empty_roster` | critical | a roster of zero — and it admits it cannot tell "none constructed" from "unreadable" |

`silent_agent` fires only when there is a measured baseline to compare
against: with zero investigations, silence is correct behaviour rather than an
anomaly.

### Mesh health is computed or withheld — never fabricated

Three components, each included **only** when its inputs are genuinely
observable:

| Component | Included when | Evidence |
|---|---|---|
| `heartbeat_health` | the roster has thread workers | live / total heartbeats |
| `agent_activity` | investigations have been recorded | roster agents with ≥1 execution |
| `investigation_health` | the observability summary is available | D3/E11's own `overall_ai_health` |

The score is the **unweighted mean of the components present**, and the report
states that formula plus which components fed it. Equal weighting is stated
rather than tuned: any weighting would be an assertion about relative
importance that the evidence does not support (PHIL-1).

A component with unobservable inputs is **omitted, never defaulted to zero** —
a monitor-disabled deployment is not unhealthy for having nothing to
heartbeat. With no component computable, `available` is `false` with the reason
instead of a placeholder number.

---

## API

| Endpoint | Purpose | RBAC |
|---|---|---|
| `GET /api/v1/mesh/health` | full report: per-agent state, anomalies, score + formula | `logs:view` |
| `GET /api/v1/mesh/roster` | the E1 roster with each member's observed state | `logs:view` |
| `GET /api/v1/mesh/issues` | anomalies alone, filterable by severity | `logs:view` |

Graded by the observability tier — mesh oversight is operational telemetry,
the same material `/observability` and the audit log already expose, reachable
by the auditor role without granting anything actionable.

One RBAC entry covers the prefix because the router has no actionable surface.
The F6 suite asserts the absence of any non-`GET` route, so that stays true.

**Disabled is reported, not hidden.** With `SUPERVISOR_AGENT_ENABLED=false` the
endpoints return `supervisor_enabled: false` with the reason — using the *same
key set* a live report uses, so a console renders one code path and cannot
mistake "oversight is off" for "the mesh is healthy".

```bash
curl "http://localhost:8080/api/v1/mesh/health"
```

---

## Console

* **Agent Mesh** (Dashboard / Welcome) gains a `supervisor` node, and the
  planning node is relabelled **Planning Agent**. The Supervisor's live state
  comes from its own report — not from incident findings, because it observes
  the mesh rather than contributing to an investigation and therefore writes no
  finding. When the report was not fetched, the node says so rather than
  borrowing another agent's activity.
* **Agent Observatory** gains a *Mesh Health* panel rendering the Supervisor's
  report: the score with its formula, the roster with per-agent heartbeat /
  execution / participation state, and each anomaly with the metrics behind it.
  The panel contains **no action control** — asserted by test.

---

## Configuration

| Setting | Default | Meaning |
|---|---|---|
| `PLANNING_AGENT_ENABLED` | `true` | Wrap the C7 engine as a roster agent. Planning output is unchanged either way. |
| `SUPERVISOR_AGENT_ENABLED` | `true` | Construct the Supervisor and serve `/api/v1/mesh`. |

Both default **on**, the opposite of the F1–F4 intelligence flags, and
deliberately: neither can change an outcome. `PlanningAgent` forwards to the
same engine and returns its result unmodified, so there is no behaviour for a
default-off flag to protect; `SupervisorAgent` only reads telemetry and touches
no investigation path at all.

---

## Rollback

Set either flag to `false` and restart.

* `PLANNING_AGENT_ENABLED=false` — the bare C7 engine goes back into the
  Orchestrator's planning slot, byte-identically, and `planning` leaves the
  roster.
* `SUPERVISOR_AGENT_ENABLED=false` — `supervisor` leaves the roster and the
  mesh endpoints report that oversight is disabled.

No schema changed, no migration was added, and no investigation path was
altered in either state.

---

## Standing limits (stated, not worked around)

* **The Supervisor observes on read, not continuously.** There is no
  supervision loop, so a heartbeat that goes stale between two reads is
  noticed at the next read. A loop would need the E7 worker posture and a
  polling interval — new monitoring infrastructure this phase deliberately
  declines to add.
* **Anomaly detection is threshold-based, not learned.** The rules are
  deterministic and each cites its number, which makes them explainable but
  also blunt: a genuinely unusual-but-under-threshold pattern is not detected.
  Feeding F2's calibration machinery into supervision would be a different
  phase.
* **Participation is measured only for the five evidence sources D3/E11
  scores.** Other roster members report "not measured per incident" rather
  than a fabricated contribution figure.
* **The Supervisor cannot distinguish deliberate configuration from a fault.**
  A `silent_agent` may be misconfigured or intentionally unused; the report
  says so in the recommendation instead of guessing.
* **`/metrics` labels for the two new agents are process-scoped.** They reset on
  restart, like every other Prometheus counter here — the Supervisor reports
  what this process has observed, not lifetime totals.
