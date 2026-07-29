# Adaptive Learning & Confidence Recalibration

**Phase F2.** This document is the operator's guide to the feedback loop:
what the platform learns from, what it does with what it learns, what it is
structurally prevented from doing, and how to turn any of it off.

Companion documents: [`docs/human_in_the_loop.md`](human_in_the_loop.md)
(the E9 verdicts that are this loop's primary signal),
[`docs/ai_governance.md`](ai_governance.md),
[`docs/KNOWLEDGE_GOVERNANCE.md`](KNOWLEDGE_GOVERNANCE.md).

---

## 1. The problem

AEAM has always produced a confidence number, and until F2 that number
meant nothing checkable. A "0.8" should resolve correctly about 80% of the
time; nothing verified that it did. That is not a cosmetic gap — it is what
made confidence unusable for the thing operators most want it for: setting
an automation threshold. You cannot say "auto-execute above 0.9" without
knowing what 0.9 is worth.

Measured on the platform's own history, stated confidence was substantially
**overconfident** at every level. That is the expected shape: confidence is
assembled additively from independent components (detector agreement,
deviation magnitude, persistence, history depth — see the F1 KPI Agent),
each adding a little, with nothing checking the total against reality.

---

## 2. What the loop learns from

Two sources of labeled signal, both read-only over records the platform
already keeps.

| Signal | Meaning | Priority |
|---|---|---|
| E9 verdict `approved` | The analysis was correct | Wins over status |
| E9 verdict `rejected` | The analysis was not correct | Wins over status |
| `investigation_status = RESOLVED` | The platform reached a real answer | Fallback |
| `investigation_status = FAILED` | It did not | Fallback |

A human verdict outranks a derived status deliberately: a reviewer who
rejected an analysis has said something the status vocabulary cannot
express.

**Deliberately excluded:**

- `changes_requested` and `escalated` verdicts — the review is still in
  motion, and treating either as a negative would punish confidence for a
  decision nobody has made yet.
- `investigation_status = ESCALATED` — escalation means a human was asked,
  **not** that the analysis was wrong. Scoring it as a failure would train
  the platform to be under-confident precisely on the incidents that matter
  most.
- Incidents with no usable confidence. These are counted and reported, never
  defaulted to a number nobody predicted.

Every exclusion is counted and shown in the recalibration result, because
"we trained on 200 of 1,000 incidents" is something the operator approving
a calibration needs to see.

---

## 3. What it computes

**Isotonic regression** (pool-adjacent-violators) fits a monotone mapping
from stated confidence to observed resolution rate.

Isotonic rather than Platt scaling because Platt assumes the miscalibration
is sigmoidal, and there is no reason to expect AEAM's additive confidence to
misbehave in that shape. Isotonic assumes only **monotonicity** — higher
stated confidence should not mean lower actual resolution — which is the one
property the score genuinely has to have. It is implemented in stdlib rather
than pulling scikit-learn into the finalize path, matching the
constitutional precedent that implemented BM25 in stdlib (TECH-1/TECH-2).

**Quality is measured two ways, and both must hold:**

- **ECE** (expected calibration error) — the sample-weighted mean distance
  between each bucket's stated confidence and its observed rate. Literally
  the distance of the reliability curve from the diagonal, which is what the
  F2 acceptance criterion asks to be reduced.
- **Brier score** — a proper scoring rule. ECE alone can be gamed by a model
  that predicts the base rate for everything: perfectly calibrated, entirely
  uninformative. Requiring Brier not to regress rules that out.

**Everything is measured on a held-out split the fit never saw.** Measuring
on training data reports a near-zero ECE for any mapping and proves nothing.
The split is deterministic, not random — a governance decision that cannot
be re-run and reproduced is not auditable.

### Recorded baseline

| | Before | After | Change |
|---|---|---|---|
| Held-out ECE | 0.2149 | **0.0958** | **−55.4%** |
| Held-out Brier | 0.2807 | **0.2253** | −19.8% |

Measured on 400 samples (266 training / 134 held out) from the fixture in
`aeam/tests/fixtures/calibration_baseline.json`, asserted in CI by
`aeam/tests/test_phase_f2_learning.py`. A live run against real platform
history reproduced the shape: ECE 0.1948 → 0.0808.

---

## 4. What it refuses to do

A calibration is **not adopted** unless it earns it. Each refusal is a
normal, reported outcome — not an error:

| Condition | Why |
|---|---|
| Fewer than `LEARNING_MIN_TRAINING_SAMPLES` (60) labeled samples | Isotonic on 30 points reproduces the training set and generalises to nothing |
| Every training outcome the same class | The mapping collapses all confidences to a constant — "perfectly calibrated" on that data and catastrophic in production |
| Held-out ECE improvement below `LEARNING_MIN_ECE_IMPROVEMENT` (0.01) | That is noise, not learning |
| Train or holdout split empty | Nothing to measure against |

PHIL-1: calibration is measured, never asserted.

---

## 5. What the Learning Agent cannot do

Two boundaries are structural, not conventional.

**It never mutates history (MEM-2).** Every read is a `SELECT`. The agent
has no code path that updates an incident, a verdict, or a finding.
`test_learning_run_mutates_no_historical_row` proves it by hashing every row
of every historical table before and after a run; a live run over real
PostgreSQL history mutated zero rows across eight tables.

**It never changes a threshold (AGENT-5).** It may *propose* that an
automation threshold move, with the measurement that motivated it attached.
The proposal sits `pending` until a human records a verdict. There is
deliberately **no method on the class that applies a proposal** — an
advisory agent that can enact its own advice is not advisory, and the
absence of the method is the enforcement.

Approving a proposal records that an authorized human agrees the threshold
should move. **Moving it is a separate deployment-configuration act**, and
the approval record is the authorization for it. The API says so in its own
response (`"applied": false`) rather than leaving an operator to assume.

---

## 6. Operating it

### Review what a recalibration would do, without committing

```bash
curl -X POST http://<host>/api/v1/learning/recalibrate \
  -H "Content-Type: application/json" -d '{"dry_run": true}'
```

Returns the full measurement — ECE and Brier before and after, sample
counts, exclusion reasons — and persists nothing. A governance surface that
can only be exercised by committing to its result is one nobody exercises.

### Adopt it

```bash
curl -X POST http://<host>/api/v1/learning/recalibrate \
  -H "Content-Type: application/json" -d '{}'
```

### Inspect what is live

```bash
curl http://<host>/api/v1/learning/state
```

Reports `enabled` (is the flag on) separately from `active` (does a
calibration exist). A stored calibration on a deployment with the flag off
is **not in force**, and the endpoint says so — reporting it as active would
misdescribe what every incident's confidence currently means.

### Roll back

Two independent levers, in increasing order of bluntness:

1. **Restore a previous version** — `POST /api/v1/learning/restore` with
   `{"version": N}`. Creates no mapping and re-measures nothing; it
   re-points `active` at a version whose measurements are already on
   record, which is what makes it safe to do under pressure.
2. **Disable calibration entirely** — set `LEARNING_CALIBRATION_ENABLED=false`
   and redeploy. Confidence reverts to raw **exactly**, verified live: the
   persisted confidence equals the raw value and the disclosure states the
   feature is disabled.

Superseding never deletes. Every version ever adopted stays in
`calibration_models`, so the mapping that produced a historical incident's
calibrated confidence remains inspectable forever (COMPAT-7).

---

## 7. What an operator sees

Both values, always. The console renders the calibrated confidence with a
note carrying the raw value it was adjusted from and the magnitude of the
adjustment:

> Calibrated (v1) — raw 90% ▼ 35 pts

When calibration was **not** applied, the note says so with the reason on
hover rather than being omitted — "this confidence is raw, because …" is
information someone setting an automation threshold needs.

An incident recorded before F2 carries no calibration data at all and
renders exactly as it always did (COMPAT-1).

### Drift metrics

Published through the same Prometheus pipeline as everything else (OBS-1):

| Metric | Meaning |
|---|---|
| `calibration_ece{stage="raw"\|"calibrated"}` | Held-out ECE before and after. Both published — the gap between them *is* the learning, and publishing only the post-calibration number would make a useless calibration indistinguishable from a good one. |
| `calibration_version` | Active version. **0 means no calibration is active** — distinct from version 1, and worth alerting on: it catches a deployment that believes it is calibrated and is not. |
| `calibration_samples{split="training"\|"holdout"}` | Sample counts. An ECE of 0.02 over 80 held-out samples and one over 8,000 are very different claims. |

These update when a recalibration runs, not continuously. They are a
statement about that run, and the metric documentation says so.

---

## 8. Authorization

| Surface | Permission | Why |
|---|---|---|
| `GET /learning/state`, `/history`, `/proposals` | `logs:view` | An auditor must be able to inspect calibration state and the proposal ledger |
| `POST /learning/recalibrate`, `/restore`, `/decisions/{id}` | `admin:config` | Recalibrating changes what every subsequent incident's confidence means; deciding a proposal is a governance act (SEC-7) |

Read and write paths deliberately share no URL prefix. `_ENDPOINT_RBAC_MAP`
grades on path alone, not method, so nesting a read under a write prefix (or
the reverse) would let one be graded as the other — which is exactly how an
auditor ends up able to approve a proposal. Anything else added under
`/api/v1/learning` falls through to `admin:config`, so a new write surface
is guarded by default rather than by remembering to map it.

Every recalibration, restore and proposal decision is audit-logged with the
acting principal and how that identity was attributed.

---

## 9. Standing limits

- **Calibration is global, not per-metric or per-severity.** One mapping
  covers every incident. A metric whose confidence is systematically
  different from the platform average is not separately corrected. Doing so
  would need per-segment sample volumes the platform does not yet produce.
- **Approved proposals are not applied automatically**, by design (§5). An
  operator must make the configuration change.
- **Recalibration is operator-triggered, not scheduled.** There is no
  background job; a deployment that never calls the endpoint never learns.
  Scheduling it would mean a mapping changing without anyone deciding it
  should.
- **The loop cannot learn from incidents nobody reviewed and that never
  reached a terminal status.** Escalated-and-forgotten incidents contribute
  nothing, which is correct but does mean coverage depends on operational
  discipline.
