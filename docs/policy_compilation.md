# Policy Compilation, Validation & the Policy Agent

**Phase F3.** This document is the operator's guide to turning an extracted
policy into an enforced detection rule: what compiles, what gets validated,
what a human must sign off on, and how to roll it back.

Companion documents: [`docs/KNOWLEDGE_GOVERNANCE.md`](KNOWLEDGE_GOVERNANCE.md)
(the E12 policy lifecycle this phase builds on),
[`docs/adaptive_learning.md`](adaptive_learning.md) (F2's advisory-agent
precedent, followed here for the rule-proposal lifecycle).

---

## 1. The gap this closes

C2 extracts policies from documents; C3 matches them as advisory evidence;
E12 governs their lifecycle. None of that could turn a policy into
*enforced* detection behavior — the only path from "a document says sales
drops over 20% require escalation" to the platform actually watching for
that was an operator hand-editing `aeam/config/detection_rules.yaml`.
Nothing checked whether the policy corpus, accumulated from many documents
over time, even agreed with itself.

F3 adds two static, deterministic tools and one governed lifecycle:

- **The Rule Compiler** turns a policy into a candidate override for one of
  `RuleEngine`'s three curated domains (`sales`, `complaints`, `inventory`).
- **The Policy Validator** checks the corpus for internal contradictions.
- **Compiled rule proposals** are how a candidate becomes enforced — never
  automatically, always through a recorded human approval.

Nothing here calls an LLM. Compilation and validation are both pure,
deterministic functions over already-extracted policy fields — reproducible,
inspectable, and never a source of fabricated enforcement (RAG-7/MOD-4).

---

## 2. Why compilation only targets three domains

`RuleEngine` hardcodes exactly three metric domains, each with its own
Python evaluator reading specific named keys from its config
(`sales.daily_drop_percent`, `sales.absolute_minimum`,
`complaints.daily_increase_threshold`, `inventory.critical_threshold`,
`inventory.low_stock_threshold`). A compiled rule for any other domain would
need a fourth evaluator — a second rule-evaluation code path, which ENG-6
forbids ("one rule engine").

So a policy compiles only when:

1. Its `related_metrics` names one of `sales`/`complaints`/`inventory`.
2. Its condition wording matches that domain's known rule shape (a
   percentage drop or an absolute floor for sales; a percentage increase
   for complaints; an absolute floor, critical or low-stock, for
   inventory).
3. A single unambiguous number can be extracted from its threshold text.

Anything else is reported as **not compilable**, with the exact missing
signal named — never guessed at, never silently dropped.

```
GET /api/v1/knowledge/policies/{policy_id}/compile
```

Preview-only. Never persists anything.

---

## 3. Validation — static conflict analysis

```
GET /api/v1/knowledge/policies/conflicts
```

Runs over every currently *matchable* (non-retired) policy and reports
three kinds of finding:

| Type | Meaning |
|---|---|
| `threshold_collision` | Two or more policies compile to the SAME domain+rule_key with DIFFERENT values. Only one can ever be the adopted value — this must be resolved by an operator before either is approved. |
| `unreachable` | Same situation, but the values are IDENTICAL — a harmless but fully redundant duplicate. |
| `action_conflict` | Two policies share a metric and disagree on `approval_required` for what reads as the same trigger — contradictory escalation behavior. |

Computed fresh on every call (no caching, mirroring how
`PolicyRegistry` itself loads policies fresh per investigation), so the
report can never go stale between a policy edit and the next read.

---

## 4. The rule lifecycle

```
proposed ──► approved ──► (later, optionally) retired
    │
    └──► rejected
```

**Proposing** compiles a policy and persists the candidate. It changes
nothing about detection — a proposed rule is not enforced.

```
POST /api/v1/knowledge/curate/rules/{policy_id}/propose
```

**Deciding** records a human verdict. Approval does not itself change any
running process's behaviour — see §5.

```
POST /api/v1/knowledge/curate/rules/{rule_id}/decide
{"verdict": "approved", "reviewer_id": "...", "note": "..."}
```

A decided rule is terminal: re-deciding is refused (409), matching E9's
verdict contract exactly.

**Retiring** withdraws a previously approved rule — the named rollback path.

```
POST /api/v1/knowledge/curate/rules/{rule_id}/retire
{"reason": "threshold revised elsewhere"}
```

Only an approved rule can be retired (409 otherwise); the row is never
deleted, so "why was this ever adopted, and who withdrew it" stays
answerable forever.

**Reading:**

```
GET /api/v1/knowledge/rules?status=approved
```

---

## 5. Why approval is "restart-applied" — and why that's not a shortcut

An approved rule's override reaches `RuleEngine` only at the next container
construction, when `aeam/main.py` reads
`PolicyAgent.active_overrides()` and passes the result into
`RuleEngine(overrides=...)`.

This is deliberately the **same trade-off** Phase D4's Enterprise
Configuration Engine already documents and the platform already accepts
(MOD-6) — not a new one invented for F3. The alternative, live-reloading a
mutable threshold on every `evaluate()` call, was rejected for a concrete
reason: `POST /api/v1/trigger` and MonitorAgent's cycles can run on any of
Cloud Run's up-to-5 instances, and a per-request DB read to check for a
newly-approved override would put a network round trip on the hottest path
in the system for a value that changes rarely. Restart-applied is honest,
cheap, and consistent with how the rest of the platform already treats
infrequently-changed configuration.

Every API response that approves or retires a rule states this explicitly:

```json
{"effective": "next restart — the composition root loads adopted overrides at startup"}
```

### Rolling back

Two levers:

1. **Retire the specific rule** — `POST .../retire`. Restores the domain's
   YAML-configured default for that key on next restart.
2. **Nothing to disable platform-wide** — F3 has no master flag, because a
   deployment that has never proposed a rule already behaves exactly as
   before F3 shipped. Zero approved rows produces an empty overrides dict,
   which `RuleEngine` treats as byte-identical to no `overrides` parameter
   at all.

---

## 6. What the Policy Agent cannot do

Mirrors the F2 Learning Agent's advisory boundary exactly (AGENT-5). It can
compile, propose, decide, retire, and validate. **It has no method that
applies an override to a running `RuleEngine`.** The only bridge from
governed state to actual enforcement is the composition root reading
`active_overrides()` at startup — the absence of an "apply"/"enact" method
on the agent class is the enforcement, verified by
`test_agent_has_no_method_that_applies_an_override`.

---

## 7. Tier-3 extraction

C2's extraction prompt is written for flat, sentence-shaped policies ("if X
then Y"). Fed a table — the shape a PDF's extracted text degrades tables
into most often — that prompt has no instruction telling the model rows
belong together, so it merges every row into one vague policy, keeps only
the first, or misses the table entirely.

```python
PolicyExtractor(llm_service=...).extract_tabular(text, chunk_ids=..., chunk_metadata=...)
```

Same LLM boundary, same guardrails (`sanitize_input`/`validate_output`),
same JSON parser, same chunk attribution as Tier-1/2's `extract()` — the
only difference is a prompt that explicitly instructs the model to treat
each table row or conditional branch as its own policy, linked to its
siblings by a shared `table_group` id and its own `table_row` index. Both
fields are simply absent for a policy that did not come from a
table/nested-conditional block.

Measured on a three-row severity/threshold/action table fixture: Tier-1/2
recovers 1 merged policy; Tier-3 recovers all 3, correctly linked and
ordered (`aeam/tests/test_phase_f3_policy.py::test_tier3_recovers_every_table_row_that_tier1_2_merges`).

---

## 8. Authorization

| Surface | Permission | Why |
|---|---|---|
| `GET .../compile`, `.../conflicts`, `GET /rules` | `documents:search` | Reads reachable by analyst/operator/admin/readonly, same tier as every other policy read |
| `POST .../propose`, `.../decide`, `.../retire` | `admin:config` | Configuration-writing surfaces are the strictest tier (SEC-7) |

Read and write paths deliberately live under different URL prefixes
(`/api/v1/knowledge/policies/...` and `/api/v1/knowledge/rules` for reads;
`/api/v1/knowledge/curate/rules/...` for writes) so `_ENDPOINT_RBAC_MAP`'s
path-only matching can never confuse one for the other — the same F2
lesson (an auditor must never be able to approve anything) applied here.

Every propose/decide/retire call writes an audit record through the same
`AuditLogger` every other curation write in this file already uses
(MEM-4/SEC-6).

---

## 9. Standing limits

- **Compilation covers exactly three domains and five rule shapes.**
  A policy about any other metric, or with wording outside the recognized
  patterns, is honestly reported as not compilable — it is not a
  comprehensive natural-language rule generator, and is not meant to be.
- **A collision between two approved rules resolves to the most recently
  decided one**, deterministically, with a logged warning naming the
  superseded rule. The validator should have caught this before both were
  approved, but nothing in the API prevents an operator from approving both
  anyway.
- **No per-domain, per-severity compilation.** One override per
  `(domain, rule_key)` — a policy that wants different thresholds for
  different severities is out of scope for this phase's compiler.
- **Tier-3 extraction quality depends entirely on the LLM's ability to
  recognize table/conditional structure in degraded PDF-extracted text.**
  No fallback parser attempts to recover a table structurally (e.g. via
  layout analysis); Tier-3 is a better prompt over the same text extraction
  Tier-1/2 already has.
