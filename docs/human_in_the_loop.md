# AEAM Human-in-the-Loop Governance (Phase E9)

The governance workflow guide required by `ROADMAP.md`'s "Documentation
updates" line for this phase: what the approval gate is, what it does and
does not withhold, how tiered approval works, and how to operate it.

---

## 1. What changed: `human_approval_required` is now enforced

Since Phase C7 the Enterprise Action Planning Engine has computed
`human_approval_required` for every incident. Nothing enforced it. The safe
runbook executed regardless, so the platform's only real protection was the
reversible-action catalog itself, and the Human Review workspace could offer
verdicts that went nowhere — it recorded them in browser session state and
said so.

`CONSTITUTION.md` AGENT-5 permits an unenforced flag **only** when it is
documented as advisory. Phase E9 takes the other option:

| | Before E9 | From E9 |
|---|---|---|
| `human_approval_required=True` | Advisory. Runbook ran anyway. | **Enforced.** Gated steps are withheld until the approval chain is satisfied. |
| A review verdict | Browser session state, lost on refresh. | A persisted, attributed row that survives restart and appears in the audit trail. |
| Who approved | Not recorded anywhere. | The acting principal (E3 identity), per tier, with how that identity was established. |
| Multi-party sign-off | Not expressible. | An ordered tier chain; every tier must approve, in order. |

The constitutional statement in `CONSTITUTION.md` AGENT-5 that describes the
flag as currently advisory records the pre-E9 state of the platform; the
enforced semantics that supersede it are the ones documented here and
asserted by `aeam/tests/test_phase_e9_human_review.py`.

---

## 2. What is gated, and what is deliberately not

The split lives in exactly one place — `NEVER_GATED_STEPS` in
`aeam/agents/orchestrator/runbooks.py` — so the Orchestrator and the review
API cannot disagree about it.

| Runbook step | Gated? | Why |
|---|---|---|
| `slack`, `marketing_slack` | **No** | Informing humans is never gated. Withholding the alert would suppress the message telling a reviewer an approval is waiting. |
| `jira` | **No** | Same: the ticket is how the organisation learns the incident exists. |
| `email` (report) | **No** | The report is the review packet; it is dispatched independently of the runbook, as it always was. |
| `diagnostics` | Yes | Captures a snapshot — changes system state. |
| `monitoring` | Yes | Raises a monitoring flag — changes system state. |
| `webhook`, `sheets` | Yes | Calls a third party / writes to an external sheet. |
| anything new | Yes (default) | An unclassified step is held for a human rather than executed on the assumption it is harmless. |

Every one of these actions was already safe and reversible — that is the
catalog's entry condition and has not changed. "Safe" simply is not the same
as "an operator is content for it to happen without being asked."

---

## 3. The approval chain

An incident's chain is an **ordered list of tiers**. Every tier must approve,
in order, before any withheld step executes. A single-tier chain — the
default — behaves exactly like a one-step approval (COMPAT-1/6).

Resolution precedence (`aeam/governance/human_review.py::resolve_approval_chain`),
strictest source of truth first:

1. **Policy-driven.** Roles named by the incident's own matched policies that
   both require approval (`approval_required`) and name a responsible `role`.
   The organisation's written policy outranks a deployment default. Multiple
   such policies form the chain in Policy Registry match order.
2. **Severity override.** `APPROVAL_TIER_CHAIN_OVERRIDES`, e.g.
   `CRITICAL:analyst,manager,risk;HIGH:analyst,manager`.
3. **Default chain.** `APPROVAL_TIER_CHAIN` (default `reviewer` — one tier).

A policy that requires approval but names no role contributes nothing to the
chain: there is no honest way to guess who it meant, so resolution falls
through to configuration. The gate still applies either way — an unresolvable
chain never releases execution.

### Verdict semantics

| Verdict | Effect on the chain | Executes anything? |
|---|---|---|
| `approved` | Advances one tier. On the **last** tier, the recorded pending steps execute. | Only on the last tier. |
| `rejected` | Halts permanently, at the tier and principal that rejected. | Never. Each withheld step is recorded as skipped, naming who halted it. |
| `changes_requested` | None — chain stays where it is. | No. |
| `escalated` | None — chain stays where it is. | No. |

`changes_requested` and `escalated` are recorded with full attribution but are
never silently converted into an approval or a rejection: they mean "not yet
/ not by me", and the record says exactly that.

### Idempotency and the one-principal rule

A repeated approval from the same principal is a **no-op** — the response
reports `idempotent: true` and nothing executes twice. The same rule stops
one person from clearing several tiers of a chain that exists precisely to
require several people. A verdict arriving after the chain terminated is
either idempotent (it repeats the terminal verdict) or a `409` naming the
state that actually holds — never silently ignored.

---

## 4. Endpoints and authorisation

| Endpoint | RBAC grant | Notes |
|---|---|---|
| `GET /api/v1/review/queue` | `incidents:view` | Queue depth in `X-Total-Count`. Readable by auditor/analyst — seeing governance state is not releasing anything. |
| `GET /api/v1/review/verdicts` | `incidents:view` | Verdict history, newest first. |
| `GET /api/v1/review/incidents/{id}` | `actions:approve` | Approval record + full verdict chain. |
| `POST /api/v1/review/incidents/{id}/approve` | `actions:approve` | |
| `POST /api/v1/review/incidents/{id}/reject` | `actions:approve` | |
| `POST /api/v1/review/incidents/{id}/verdict` | `actions:approve` | The full four-verdict vocabulary. |

`actions:approve` is held only by `admin` in the E3 permission matrix — the
same grant that already guarded `/api/v1/actions/approve`. Casting a verdict
can release withheld execution, so it carries the strictest action grant;
this router adds no permission model of its own (ENG-6).

### Reviewer attribution

Every verdict records `attribution_source`:

| Value | Meaning |
|---|---|
| `jwt` | The principal came from a verified token (the E3 identity path). This is the only value a deployed instance should ever produce. |
| `request` | The principal came from the request body — reachable **only** when `ENVIRONMENT=development`, where `SecurityMiddleware` bypasses authentication entirely. |
| `unattributed` | No identity was available at all. |

A request body can never override an authenticated identity: when a verified
principal exists, the body's `reviewer_id` is ignored. The console renders
any non-`jwt` source visibly, so a verdict is never displayed as though it
carries an authority it does not have.

---

## 5. Configuration

| Setting | Default | Effect |
|---|---|---|
| `HUMAN_APPROVAL_ENFORCED` | `true` | `false` restores pre-E9 advisory behaviour exactly; the tables stay and go inert. This is the documented rollback switch. |
| `APPROVAL_TIER_CHAIN` | `reviewer` | Default ordered chain, comma-separated. |
| `APPROVAL_TIER_CHAIN_OVERRIDES` | unset | Per-severity chains: `CRITICAL:analyst,manager,risk;HIGH:analyst,manager`. |

These three are deliberately **not** registered in
`aeam/config/config_registry.py`. The D5 admin API can edit every field it
lists, and an approval gate that one API call can switch off is not a
governance control. Changing them is a deployment-time act — env var or
`deploy/cloudrun.yaml` — auditable in the deployment record rather than
silently at runtime.

### Rollback

Set `HUMAN_APPROVAL_ENFORCED=false` and restart. The Orchestrator's
finalization becomes byte-identical to pre-E9: gated steps execute
immediately, no approval rows are written, and the review workspace shows a
banner saying enforcement is off rather than implying verdicts are gating
something. Existing approval and verdict rows are untouched and remain
readable.

---

## 6. Data model

Two additive tables (migration `0004_human_review`), inert when enforcement
is off. No existing table is altered — MEM-2 holds, because a verdict is a
new record *about* an incident, never a mutation of the incident row.

- **`incident_approvals`** — one row per incident that actually required
  approval. Carries the ordered chain (`required_tiers`), how far through it
  the incident is (`current_tier`), and `pending_actions`: the exact
  ActionAgent calls that were withheld, with the parameters the Orchestrator
  had already built. An approval later executes *those* calls — never a
  re-derived or re-planned set. `pending_actions` is deliberately not
  updatable for that reason.
- **`review_verdicts`** — append-only. One row per (tier, principal), so a
  three-tier chain leaves three attributable rows, and a rejection names the
  tier and principal that halted it.

An incident that never required approval has no row in either table.
**Absence means "no gate", never "denied"** — which is exactly how every
incident predating this phase reads back (COMPAT-1), in the API, the
console, and the findings JSON alike.

The incident's own `findings` array also carries a `human_approval` entry
(the snapshot written at finalization: chain, withheld steps, why the gate
fired) so any incident view can show "gated" without a second request. The
*live* state — which tier the chain is on now, who has approved — is the
review API's answer, and the console reads it there rather than deriving it.

---

## 7. Operating the queue

1. An incident finalizes, its execution plan requires approval, and its gated
   steps are withheld. Slack/Jira/email still dispatch — that is how the
   organisation finds out.
2. The incident appears in **Human Review** (`GET /api/v1/review/queue`) with
   its chain, the tier it awaits, and exactly which steps are held.
3. A reviewer holding `actions:approve` casts a verdict. Non-final tiers
   advance the chain; the final approval executes the withheld steps through
   the **unchanged** `ActionAgent` and records what actually succeeded.
4. The verdict lands in `review_verdicts`, in the audit trail with the acting
   principal, and in the workspace's Verdict History.

Only actions ActionAgent confirmed as `SUCCESS` are reported executed — the
same "never claim an action ran unless it did" rule the Orchestrator has
always followed. If no ActionAgent is wired, an approval is still recorded
and every released step is honestly reported as skipped rather than silently
claimed.
