# Knowledge, Policy & Memory Governance

**Phase E12 (MEM-4, MEM-6, RAG-7, MOD-4, COMPAT-6, SEC-7).** How AEAM's three
knowledge stores — extracted policies, organizational memory, and ingested
documents — are curated, corrected, and quality-measured.

Before this phase all three were write-once in practice: an extracted policy
matched investigations forever until someone deleted the row by hand, a
memory recorded from a wrong root cause kept surfacing as evidence with no
correction path, an uploaded runbook could never be recognised as
authoritative, and retrieval quality had no regression gate at all.

---

## 1. Policy lifecycle

### The vocabulary

`aeam/registry/models.py :: PolicyStatus` — mirrored on the frontend in
`frontend/src/lib/governance.js` as a documented lockstep pair, pinned by
tests on both runtimes.

| Status | Matches new investigations? | Meaning |
|---|---|---|
| `active` | **yes** | In force. Can be cited as advisory evidence. |
| `pending_review` | **yes** | Queued for a governance decision. Deliberately still matches — a review backlog must never silently degrade investigation quality. |
| `retired` | **no** | Withdrawn from force. Never matches a new investigation. |

### Why retired policies are retained, not deleted

A retired policy's row stays in the `policies` table. Incidents that already
cited it must remain explainable: deleting the row would leave historical
evidence trails pointing at nothing. Retirement changes the *future*, never
the record of what already happened.

### Backward compatibility (COMPAT-6)

`status` defaults to `active`, migration `0005_knowledge_governance` backfills
every existing row to `active`, and `Policy.from_row` treats a `NULL` status
as `active`. The result: **adopting the lifecycle changes no investigation's
policy matching** until an operator deliberately transitions something. A
policy is never ambiguous — `PolicyRegistry` is never left guessing whether an
unstatused row is in force.

### Where the filter lives

`PolicyRegistry.match_for_incident` loads via `PolicyRepository.list_matchable()`,
which filters **in SQL**. A large retired corpus therefore costs nothing per
investigation, and the guarantee is structural rather than a Python filter
someone could later forget to apply.

### Retiring a policy

**Console:** Knowledge Center → open a document → **Policies** tab → *Change*
on the policy → choose the status → give a reason → Apply.

**API:**
```bash
curl -sS -X POST '<host>/api/v1/knowledge/curate/policies/<policy_id>/status' \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"status": "retired", "reason": "superseded by the 2026 handbook"}'
```

A reason is **mandatory**. An unexplained governance change is exactly what
this lifecycle exists to prevent, so a blank reason is a 422, not a silent
success.

The response reports `matches_new_investigations`, so a caller never has to
re-derive the rule.

---

## 2. Memory curation (MEM-4)

Two operations on `aeam_incident_memories`, both requiring **who** and
**why**.

### Withdraw (expunge)

Permanently removes an incident's memory from recall. Future investigations
stop surfacing it as evidence. The **incident record itself is untouched** —
only its organizational-memory entry is withdrawn.

```bash
curl -sS -X POST '<host>/api/v1/knowledge/curate/memory/expunge' \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"incident_id": "<id>", "reason": "root cause analysis was incorrect"}'
```

### Correct

Rewrites the memory with corrected field values. Correctable fields:
`event_type`, `metric`, `severity`, `root_cause`, `confidence`,
`investigation_status`, `recommended_actions`, `executed_actions`,
`chunk_ids`, `timestamp`.

```bash
curl -sS -X POST '<host>/api/v1/knowledge/curate/memory/correct' \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"incident_id": "<id>",
       "corrections": {"root_cause": "missing composite index on orders"},
       "reason": "reanalysed after the post-mortem"}'
```

**Why correction is expunge-then-rewrite, not a payload patch.** A memory's
embedding is derived from the same text as its metadata. Patching
`root_cause` in the Qdrant payload without re-embedding would produce an
entry that *recalls on its old, wrong wording* while *displaying* the
corrected one — a subtler dishonesty than not correcting at all. So the stale
vector is withdrawn first, then the corrected memory is written and
re-embedded. If the rewrite fails after the withdrawal, the API says so
explicitly and tells you to re-run; it never reports a success it did not
achieve.

**Correction provenance travels with the memory.** A corrected entry carries
`corrected`, `corrected_by`, `corrected_at`, `correction_reason`, and
`fields_corrected` in its own metadata, so a future investigation that
recalls it can see it was corrected, by whom, and why. A corrected memory
that looked identical to an original one would hide exactly the fact an
auditor needs.

**Correcting a memory that does not exist** raises a 404 rather than creating
one. Fabricating organizational memory through a correction endpoint would be
the worst possible failure mode here.

**Console:** Memory Center → *Curate* on any recall row → Correct or
Withdraw.

---

## 3. Semantic document typing (MOD-4 / RAG-7)

### The defect this fixes

The upload path stored the detected **file format** (`markdown`, `pdf`) in
`Document.doc_type`. Retrieval's `BusinessRelevanceScorer` reads a payload key
also called `doc_type` to decide whether a chunk came from authoritative,
actionable material. The two meanings collided: an uploaded runbook arrived
labelled `"markdown"` and could **never** earn the authoritative-source
bonus, no matter how obviously it was a runbook.

### The fix

`Document.semantic_type` is a new, separate column carrying the **declared**
semantic type. Retrieval prefers it and falls back to the format when nothing
was declared, so pre-E12 documents behave exactly as before (COMPAT-1). The
format is still stored, and now also travels to retrieval under its own
`format` key — neither field has to stand in for the other any more.

### Declarable types

`runbook`, `sre_runbook`, `incident_report`, `post_mortem`, `policy`, `wiki`,
`api_doc`, `reference`.

The first four are **authoritative** — they earn the retrieval bonus. That
set is a lockstep pair with `DEFAULT_ACTIONABLE_DOC_TYPES` in
`advanced_retrieval.py`; a test asserts every authoritative type is also
declarable, so a declaration can never silently do nothing.

### Declaring at upload

```bash
curl -sS -X POST '<host>/api/v1/ingest/' \
  -H "Authorization: Bearer $TOKEN" \
  -F 'file=@db-runbook.md' -F 'doc_type=runbook'
```

An unrecognised value is a 422, not a stored typo — `runbok` would otherwise
persist and silently fail to earn the very bonus the declaration exists to
grant.

### Declaring afterwards

A corpus ingested before this phase has no declarations, and re-uploading
every file just to classify it would be absurd:

```bash
curl -sS -X POST \
  '<host>/api/v1/knowledge/curate/documents/<doc_id>/semantic-type?semantic_type=runbook' \
  -H "Authorization: Bearer $TOKEN"
```

**This does not take effect immediately, and the response says so.** The
chunks already in Qdrant carry the old `doc_type` in their payload until the
document is re-processed. Re-index to apply it now:

```bash
curl -sS -X POST '<host>/api/v1/knowledge/documents/<doc_id>/reindex' \
  -H "Authorization: Bearer $TOKEN"
```

**Console:** Knowledge Center → open a document → **Overview** → *Semantic
Type* panel.

### The reason is attached (RAG-7)

When the bonus applies, the ranked result carries a human-readable reason
naming the type **and where it came from** — `declared at upload` versus
`derived from the document's stored type`. Both legitimately earn the bonus,
but an explanation that hid which one applied would leave an operator unable
to distinguish a classified corpus from a coincidentally-named one.

---

## 4. Retrieval evaluation methodology

### The problem

Corpus drift is invisible. A chunking change, an embedding-model swap, or an
accidentally-deleted document can quietly degrade what investigations
retrieve — and every pre-E12 test would still pass, because they all assert
on *structure* rather than on whether the right evidence came back.

### The harness

- **Golden set:** `aeam/tests/fixtures/retrieval_golden_set.json` — queries
  paired with the evidence that must come back, each with a stated rationale.
- **Harness:** `aeam/tests/retrieval_eval.py` — test infrastructure, not
  runtime code. Nothing outside the test tree imports it.
- **Gate:** `aeam/tests/test_phase_e12_knowledge_governance.py` runs it in
  gating CI.

### Metrics, with declared semantics

| Metric | Definition | Catches |
|---|---|---|
| `recall@k` | Fraction of a case's expected chunks appearing anywhere in the top-k. | Evidence disappearing entirely — a deleted document, a broken filter. |
| `MRR` | Mean reciprocal rank of the **first** expected chunk. | Ranking collapse — the right evidence is still found but buried, which recall alone cannot see. |

Both thresholds must be cleared. The fixture declares them
(`min_recall_at_k`, `min_mrr`, `k`) so the gate is data, not code.

### Threshold policy

Set at the level the current pipeline clears **with margin**, not at the
level it barely reaches — a threshold pinned to today's exact score turns
every harmless ranking jitter into a red build. Raise them as retrieval
genuinely improves. **Never lower a threshold to make a failing change
pass**; that converts the gate into decoration.

### Three honesty rules the harness enforces

1. **A missing corpus is not a retrieval failure.** A case whose expected
   evidence is absent from the corpus under test is *skipped* and reported as
   skipped. Scoring it as a pass would hide a missing corpus; scoring it as a
   failure would blame the retriever for something it did not do.
2. **A crash is a failure, never a skip.** A retriever that raises is exactly
   the regression this harness must catch.
3. **An empty measurement never passes.** A run with zero scored cases returns
   `passed == False`. "We measured nothing" is not evidence of quality.

### Running it offline (gating CI)

The gating suite uses a deterministic in-process retriever over a fixture
corpus — no Qdrant, no embedding model download, no network. That is what
makes it safe to gate on.

### Running it against a live deployment

Use `tracer_retriever`, which adapts `RetrievalDebugTracer` to the harness's
retriever signature:

```python
from aeam.tests.retrieval_eval import evaluate_retrieval, tracer_retriever

report = evaluate_retrieval(tracer_retriever(container.rag_debug_tracer))
print(report.mean_recall_at_k, report.mrr, report.passed)
print(report.failure_summary())
```

The tracer already replays the real retrieval pipeline stage by stage, so
evaluating through it measures what an investigation would **actually**
receive — not a parallel code path that could drift from it. That is
precisely why it is the instrument.

---

## 5. Authorisation and rollback

### Curation is privileged (SEC-7)

Every write in this document lives under `/api/v1/knowledge/curate`, mapped
to `admin:config` by a single longest-prefix-first entry in
`_ENDPOINT_RBAC_MAP`. Grouping them under one namespace is deliberate: a
curation endpoint added later cannot accidentally land outside the guard by
being registered at a path nobody remembered to map.

Only the `admin` role holds `admin:config`. Reads
(`GET /api/v1/knowledge/policies`) stay on `documents:search`, so an analyst
can see governance state without being able to change it.

The console hides controls a session cannot use — a courtesy, not a security
boundary. The middleware is the only enforcement that matters and enforces
regardless.

### Every curation is audited (MEM-4)

Writes go through the **same** `AuditLogger` the security middleware uses, so
they land in the same hash-carrying `audit_logs` table and are queryable
through the Phase E11 audit surface. Actions:

| Action | Written by |
|---|---|
| `policy_status_changed` | Policy lifecycle transition |
| `memory_expunged` | Memory withdrawal |
| `memory_corrected` | Memory correction |
| `document_semantic_type_declared` | Semantic type declaration |

Query them:
```bash
curl -sS '<host>/api/v1/audit/entries?principal=alice@example.com' \
  -H "Authorization: Bearer $TOKEN"
```

Attribution follows the Phase E9 review-router contract exactly: the
principal comes from the verified JWT, and where no verified identity exists
(only possible under `ENVIRONMENT=development`, where the middleware bypasses
everything) the record is tagged `development-bypass` so the trail never
implies a cryptographic identity it does not have.

### Rollback posture

`KNOWLEDGE_CURATION_ENABLED=false` disables every curation **write** while
leaving all reads working. Those endpoints return **503, not 404** — the
capability exists and is deliberately switched off, which is a different fact
from "no such endpoint", and an operator debugging a permissions problem
deserves to be told which.

Policy status defaults preserve current matching, so the lifecycle itself
needs no flag. The evaluation suite is additive CI.

---

## 6. Retention posture (MEM-6)

Store-by-store retention ownership is declared in
[persistence_and_retention.md](persistence_and_retention.md). This phase adds
one clarification for the memory store: **retirement and expunction are
curation, not retention.** They are operator decisions about correctness, and
they are audited individually. Automated age-based retention of
`aeam_incident_memories` remains a declared posture without enforcement
tooling, exactly as Phase E5 recorded it.
