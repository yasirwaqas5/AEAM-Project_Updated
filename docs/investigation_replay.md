# Investigation & Timeline Replay — operator guide

**Phase F5 — Explainability Deepening.**

The findings model has always recorded every investigation stage in order, and
the console has always had a Replay page — but nothing reconstructed an
investigation *as a sequence*. The page derived its own narrative client-side
from an incident's summary fields, so it could drift from the record and could
not cite it. D1 explains the *final* plan; nothing explained the *unfolding*.

F5 closes that: any past investigation can be walked stage by stage and placed
against measured time, entirely from what was already persisted.

---

## The replay contract

**Replay reconstructs history. It never re-executes it.**

Replay reads one `incidents` row and returns a projection of it. It cannot
reach `RuleEngine`, `StatisticalDetector`, `KPIAgent`, `ForecastAgent`, the
business graph, `PolicyAgent`, `ActionAgent`, or any LLM — none is imported by
`aeam/intelligence/replay.py` or `aeam/api/replay.py`, so the guarantee is
enforced by the import graph rather than by reviewer vigilance, and a test
fails the moment one appears.

**Replay is strictly read-only.** It modifies no incident, finding, memory
record, audit entry, timestamp, or metric, and creates nothing (MEM-2).
Replaying an incident a thousand times leaves the database bit-identical. This
is asserted three ways:

1. every route under `/api/v1/replay` is a `GET` — there is no write surface;
2. the only SQL either module contains is one primary-key `SELECT` (asserted
   from the AST, so prose cannot mask a write);
3. a spy over every `DatabaseClient` write method records zero calls across a
   full replay, and the stored row compares equal afterwards.

---

## What a replay returns

`GET /api/v1/replay/{incident_id}` reconstructs the investigation:

* **stages** — in the order they were **recorded**, never re-sorted into a
  canonical pipeline order. Each carries:
  * `sequence` (its index in the persisted findings array) and `occurrence`
    (the investigation loop genuinely runs the decision, RAG, and evaluation
    stages once per depth, so those appear more than once);
  * `label`, `category` (`decision` / `evidence` / `planning` /
    `explainability` / `governance` / `actions`) and `introduced_in`;
  * `summary` — one deliberately dull factual line that counts and quotes
    persisted values and says nothing it cannot read off the entry;
  * `outputs` — the persisted payload, verbatim;
  * `duration` — measured, or an explicit statement that none was recorded;
  * `state_after` — the state the record itself had established by that step.
* **gaps** — catalog stages the record does not contain (below).
* **timeline** — the same stages against measured time (below).
* **replay_contract** — the read-only/no-re-execution declaration, inline.

An unrecognised stage — one recorded by a phase this build has no catalog
entry for — is **still replayed, in place, with its payload intact** and
flagged `recognised: false`. An audit reconstruction that silently discarded a
recorded stage would be worse than no reconstruction.

### State visible at each step

`state_after` is a strict fold over values the entries *themselves* carry: the
decision recorded at that depth, the evaluation's own verdict, the audit
summary's own status. Every value is tagged with the stage that supplied it
(`root_cause_source_stage`, `decision_source_stage`, …).

Per-step root causes and confidences were never persisted, so **none is
reconstructed**. The view grows only as the record supplies real values — the
first stage of a two-pass investigation shows the depth-1 decision and no root
cause, because that is what was true.

---

## Mixed history — honest gaps

Older incidents predate newer phases. Replay reports what is absent and never
fills it in (EXPL-3, COMPAT-1).

A pre-C7 incident has no `execution_plan` entry, so replay emits:

```
Execution Planning · introduced in Phase C7
No 'Execution Planning' entry is present in this incident's recorded findings.
This stage was introduced in Phase C7; an incident recorded before it — or one
where the engine was not wired — has no such entry. Replay does not
reconstruct the step.
```

The reason states what is **true** (no such entry is present) and gives the
introducing phase as context. It never claims to know *why*: the record does
not distinguish "predates C7" from "engine was unwired".

Stages that are conditional **by design** — `escalation`,
`llm_reasoning_error`, `human_approval` — are excluded from gaps. Their absence
means the condition did not arise, and calling that a gap would send an
auditor looking for records that should not exist.

Three further states are kept distinguishable, because collapsing them would
let a reader mistake a missing record for an empty one:

| State | Signal |
|---|---|
| The findings column could not be decoded | `findings_readable: false` |
| The incident genuinely recorded no stages | `findings_readable: true`, `total_stages: 0` |
| The incident does not exist | HTTP `404` |

---

## The timeline

`GET /api/v1/replay/{incident_id}/timeline` places the recorded stages against
time. Entry order is identical to the stage order — the two views are
projections of the same array, so they can never disagree.

**Every figure is a persisted measurement**, from one of three places:

| Figure | Source | Since |
|---|---|---|
| Anchor timestamp | `incidents.timestamp` | Phase 3 |
| Total investigation duration | `audit_summary.investigation_duration_seconds` | Phase E11 |
| Per-stage duration | `audit_summary.stage_durations` | Phase F5 |

Per-stage durations are new in F5: `Orchestrator._timed_stage` measures each
stage on the **existing** E11 `agent_execution_time` histogram and records the
same number per incident. Without them the only persisted figure would be one
total, and a per-stage timeline could only be produced by dividing it up — an
estimate, which the timeline contract forbids.

### What the timeline deliberately does not do

* **No wall-clock positions.** Per-stage start times were never persisted, and
  summing durations would place stages earlier than they actually ran (real
  investigations spend time between stages). The cumulative figure is labelled
  as a sum of measured stage time, not a clock position.
* **No zero-filling.** An unmeasured stage reports `duration_available: false`.
  A zero is a measurement; showing one for an unmeasured stage would invent the
  most misleading possible value.
* **No splitting an aggregate.** `stage_durations` is a per-stage **total**
  across every occurrence (the investigation loop can run a stage once per
  depth, and accumulating is what keeps the figures summing to the measured
  total). A measured number is therefore attributed to an individual step only
  when that step occurs once in the record; otherwise the aggregate is reported
  *as* an aggregate, with its occurrence count.
* **No distributing the remainder.** Where measured stage time falls short of
  the measured total, the difference is reported as `unattributed_seconds` —
  the investigation's uninstrumented work (event handling, state transitions,
  persistence). That gap is real information; scaling the stage figures up to
  hide it would make the timeline a fabrication.

---

## Bounded reads (E6)

An incident's findings array is unbounded in principle, so the reconstruction
pages over stages with a ceiling the caller cannot exceed:

| Bound | Value |
|---|---|
| `DEFAULT_STAGE_LIMIT` | 100 |
| `MAX_STAGE_LIMIT` | 500 |

`GET /{id}/stages` is the pagination path and returns `X-Total-Count` — the
same header contract `/api/v1/incidents` already uses, reused rather than
reinvented — so a paged client computes pages without a second endpoint. A
5,000-stage record returns a bounded page with `truncated: true` and the true
total.

---

## API

| Endpoint | Purpose | RBAC |
|---|---|---|
| `GET /api/v1/replay/catalog` | the canonical stage sequence and the phase each arrived in | `logs:view` |
| `GET /api/v1/replay/{id}` | full reconstruction + timeline | `logs:view` |
| `GET /api/v1/replay/{id}/stages` | stage sequence, paginated | `logs:view` |
| `GET /api/v1/replay/{id}/timeline` | timeline only | `logs:view` |

Replay is graded by the **audit** tier, not `incidents:view` (SEC-6): it
exposes an incident's complete decision trail, the same material the audit log
carries. An auditor must be able to walk an investigation, and nobody should
reach the full trail with only the incident-list grant.

One RBAC entry covers the whole prefix because the router has no write surface
at all. Should that ever change, the write would inherit `logs:view` rather
than a stricter tier — which is why the F5 suite asserts the absence of any
non-`GET` route under this prefix.

The catalog endpoint exists so the console can render a gap with the same
context the backend used to identify it, rather than hardcoding a second copy
of the stage list that could drift.

```bash
curl "http://localhost:8080/api/v1/replay/<incident_id>"
```

---

## Console

Replay Workspace now consumes `GET /api/v1/replay/{id}` as its data source.
Play / Pause / Next / Previous control **when** each already-recorded stage is
revealed; the sequence, labels, per-stage state, gaps, and durations all come
from the response. The page derives no narrative of its own, so what it shows
cannot disagree with the record.

`buildStages()` in `Timeline.jsx` is unchanged and still drives the pipeline
strip on the Investigation page and the Incidents timeline modal. It is a
glanceable display derivation from summary fields — **not** an audit
reconstruction, and it is no longer used by Replay.

---

## Rollback

Entirely additive and read-only. Removing the replay router and
`aeam/intelligence/replay.py` has zero data or behaviour consequences; no
schema changed and no investigation path was altered.

`stage_durations` is an additive key inside the existing `audit_summary`
finding. Every pre-F5 incident simply lacks it, every reader that ignores
unknown keys is unaffected, and the timeline reports its absence honestly.

---

## Standing limits (stated, not worked around)

* **No per-stage start times.** Only durations are measured, so no stage can be
  placed at a clock position. Persisting a start timestamp per stage would fix
  this, at the cost of a wider findings payload.
* **A repeated stage's time is an aggregate.** `stage_durations` accumulates
  across investigation depths, so a two-pass RAG stage reports one total rather
  than a figure per pass. Per-occurrence timing would need an occurrence-keyed
  duration map.
* **Unattributed time is not attributed.** The remainder between the measured
  total and the measured stages is disclosed, not explained. Narrowing it means
  instrumenting more of the path, not redistributing what is already measured.
* **Replay reflects what was recorded, not what happened.** If a stage ran but
  wrote no findings entry, replay cannot know it ran. The reconstruction is
  faithful to the audit trail, which is a different and weaker claim than being
  faithful to the execution — and it is the only claim the persisted record
  supports.
