# The Business Graph — operator guide

**Phase F4 — Correlation Intelligence & Business Graph.**

Before this phase, correlation was per-incident and disposable. C4's
`CrossDatasetAnalyzer` compared the incident's metric against every other
*currently-activated* dataset, pairwise, from scratch, and the answer was
discarded at finalize. Two consequences followed: correlation could never
compound (the twentieth incident on `checkout_latency` started as ignorant
as the first), and nobody could ask "what is connected to checkout
latency?" outside an investigation.

The business graph makes those relationships durable and queryable.

---

## What it is

A typed, weighted, evidence-grounded relationship model over two tables:

| Table | Holds |
|---|---|
| `graph_nodes` | one row per entity: metric, dataset, service, policy, incident |
| `graph_edges` | one row per relationship, with confidence, observation count, and evidence pointers |

**Node types** (closed vocabulary — every one maps to a record the platform
already stores):

| Type | Grounded in |
|---|---|
| `metric` | a measure column `DatasetIntelligenceService` discovered, or an `incidents.metric` value |
| `dataset` | a `datasets` registry row |
| `service` | a `sources` registry row — the upstream system of record a dataset was ingested from |
| `policy` | a `policies` registry row (C2/C3 extraction) |
| `incident` | an `incidents` row |

`service` means exactly "a registered upstream source system", stated
plainly rather than left to imply a service mesh the platform has no
evidence of.

---

## Edge grounding — the rule that makes this honest

**Every edge originates from an existing record.** Four derivation rules,
each implemented exactly once in `aeam/intelligence/business_graph.py`:

| Edge type | Evidence read | Confidence |
|---|---|---|
| `derived_from` | dataset schema (metric → dataset), source registry (dataset → service) | `1.0` — recorded fact |
| `governed_by` | `policies.related_metrics` | `1.0` — recorded fact |
| `correlates_with` | persisted `cross_dataset` findings from past investigations | mean \|Pearson r\| observed |
| `co_occurred_in_incident` | `incidents.metric` plus the metrics that incident's investigation cited | `1.0` — recorded fact |

There is no similarity heuristic, no name-fuzzing, no transitive inference,
and no LLM anywhere in the graph modules. **When evidence is absent or too
thin, no edge is created** — the graph is simply smaller, which is the
honest outcome.

Concretely, the negative cases are as load-bearing as the positive ones:

* a policy whose *prose* mentions "sales" but which declares no
  `related_metrics` produces **no** `governed_by` edge;
* a correlation weaker than `GRAPH_MIN_CORRELATION` (default `0.7`, C4's
  own reporting threshold) produces **no** `correlates_with` edge;
* a retired policy (E12 lifecycle) stops governing anything;
* a dataset with no schema yet contributes its own node but no metric
  edges — the platform genuinely does not know what it measures.

### Why correlation compounds

C4 measures a pair once per incident and forgets it. The graph keeps every
observation: an edge's `confidence` is the **mean** \|r\| across all of
them and `observation_count` records how many. A pair seen once at 0.71
and a pair seen twenty times at 0.9 are visibly different claims, and the
edge's `evidence` carries the incident ids that produced them.

---

## The advisory contract

The graph is an **advisory evidence source**, exactly like Enterprise
Memory, the Policy Registry, and C4 itself (AGENT-5).

* It appends a `graph` finding to the investigation and nothing else.
* It never overrides `RuleEngine`, `StatisticalDetector`, `KPIAgent`, or
  `ForecastAgent`. The graph modules do not import them — a regression
  test asserts this, so the capability cannot appear later by accident.
* `GraphCorrelationEngine` has **no write method at all**. An investigation
  cannot mutate the graph it just read, so the graph can never grow from
  its own output.
* The graph is rebuilt only by an explicit, privileged, audited call. There
  is no startup build, no timer, and no agent deciding on its own that the
  graph should change.

---

## Bounded queries (E6)

Traversal is breadth-first with **four simultaneous budgets** — depth,
visited nodes, traversed edges, and edges read per hop — and it reports
which one stopped it.

There is no recursive traversal: the frontier is an explicit queue and each
hop is a single `LIMIT`-ed SQL query, so expanding a hub node with fifty
thousand edges costs one bounded read rather than fifty thousand rows.

Hard ceilings live in code (`business_graph.py`) and are applied *after*
any caller-supplied value, so neither a request parameter nor a
misconfigured setting can produce an unbounded traversal:

| Ceiling | Value |
|---|---|
| `MAX_DEPTH_CEILING` | 5 |
| `MAX_NODES_CEILING` | 1000 |
| `MAX_EDGES_CEILING` | 5000 |
| `EDGES_PER_HOP_CEILING` | 500 |

A truncated traversal is **labelled as truncated** with the reason
(`edge_budget_exhausted` / `node_budget_exhausted` / `depth_budget_reached`)
rather than presented as a complete neighbourhood. Ordering is
`confidence DESC, observation_count DESC, edge_id ASC`, so a truncated
answer keeps the strongest evidence and is identical on every run — an
operator can reproduce exactly what an investigation saw.

---

## Explainability

No graph conclusion is opaque. Every relationship reported discloses:

* the ordered **traversal path** of node keys walked to reach it;
* every **contributing edge**, with its type, direction, confidence,
  observation count, and evidence pointers;
* the **traversal depth** it was found at;
* the **path confidence** — the *product* of the traversed edges'
  confidences, so a two-hop relationship through two 0.8 edges reads as
  0.64, not as 0.8;
* the **budget** the traversal ran under, without which "nothing else was
  found" cannot be judged.

---

## Deterministic evolution

A build is a pure function of the database's current contents.

* Node and edge primary keys are **UUID5 hashes of their natural keys**,
  not random UUID4s. Rebuilding from unchanged evidence produces
  byte-identical rows, so "the graph changed" always means "the evidence
  changed".
* Correlation strength is **recomputed from the complete observation set**
  on every build rather than incremented, so rebuilds converge instead of
  drifting upward.
* `first_seen_at` survives a rebuild; `last_seen_at` advances.
* Rows not re-confirmed by a build are **retired** — this is how an edge
  whose grounding evidence disappeared leaves the graph.

**Concurrency (ARCH-8).** Deterministic ids mean two builders racing on the
same edge compute the same primary key, so the database's uniqueness
constraint resolves the race rather than producing duplicates. The stale
sweep keys on `last_seen_at`, not on a build id, so a row another builder
just wrote is newer than this build's cutoff and survives — two concurrent
builds can never delete each other's work.

---

## The C4 upgrade

`CrossDatasetAnalyzer` gained one optional constructor argument,
`graph_store`, and one capability: before scanning, it asks the graph which
metrics are already known to relate to the incident's metric. Only
`correlates_with` and `co_occurred_in_incident` edges are followed —
`derived_from` and `governed_by` describe structure and governance, which
say nothing about whether two signals move together.

A candidate dataset whose measure appears in that set is recognised as a
known relative (`relation: "graph_correlates_with"`, with the traversal
attached) instead of being filed under the generic `activated_dataset`
catch-all and dropped when it happens to look normal.

**With `graph_store=None` — the default and the flag-off state — the
analyzer is byte-identical to Phase C4**: same queries, same comparisons,
same result keys, same values. The `graph_aware` and `graph_known_metrics`
keys appear only when a store is wired.

---

## API

| Endpoint | Purpose | RBAC |
|---|---|---|
| `GET /api/v1/graph/stats` | node/edge counts by type, vocabularies, budget defaults | `documents:search` |
| `GET /api/v1/graph/nodes` | bounded node search | `documents:search` |
| `GET /api/v1/graph/neighborhood` | bounded traversal from one node | `documents:search` |
| `POST /api/v1/graph/build` | deterministic rebuild from current evidence | `admin:config` |

Reads map to `documents:search` because the graph is derived from the same
governed knowledge that grant already covers. The build is `admin:config`
(SEC-7): it changes what every subsequent investigation's advisory finding
says, which is platform-wide state.

Anything else added under `/api/v1/graph` later falls through to
`admin:config` by default rather than by someone remembering to map it.

`GET /stats` returns `enabled` alongside the counts, so a console can
distinguish "the graph is off" from "the graph is on but empty".

Example — what relates to `checkout_latency`:

```bash
curl "http://localhost:8080/api/v1/graph/neighborhood?metric=checkout_latency&max_depth=2"
```

Rebuild after ingesting new datasets, policies, or accumulating incidents:

```bash
curl -X POST http://localhost:8080/api/v1/graph/build -H 'Content-Type: application/json' -d '{}'
```

---

## Configuration

| Setting | Default | Meaning |
|---|---|---|
| `BUSINESS_GRAPH_ENABLED` | `false` | Whether the graph participates in an **investigation**. Read-only API surfaces work regardless. |
| `GRAPH_MAX_DEPTH` | `2` | Traversal hops. One hop is what C4 already sees; the second is the compounding F4 adds. |
| `GRAPH_MAX_NODES` | `100` | Node budget per traversal. |
| `GRAPH_MAX_EDGES` | `300` | Edge budget per traversal, across all hops. |
| `GRAPH_MIN_EDGE_CONFIDENCE` | `0.0` | Skip weaker edges. Zero traverses everything (all edges are already grounded). |
| `GRAPH_BUILD_INCIDENT_LIMIT` | `5000` | Incidents read per build, most recent first. |
| `GRAPH_MIN_CORRELATION` | `0.7` | Minimum \|r\| before a `correlates_with` edge exists. |

---

## Rollback

Set `BUSINESS_GRAPH_ENABLED=false` and restart. No graph finding is
appended, `CrossDatasetAnalyzer` runs its exact Phase C4 pairwise path, and
the graph tables sit inert. The tables are purely additive and can be left
in place; dropping them is only necessary if the storage is wanted back.

A graph that has drifted from the evidence is corrected by running a build,
not by editing rows: the build is the only writer, and it recomputes
everything from the records.

---

## Standing limits (stated, not worked around)

* **The graph is only as current as its last build.** Nothing rebuilds it
  automatically — that is the "no autonomous graph mutation" guarantee, and
  the cost of it is that a metric introduced since the last build has no
  node. The finding says so explicitly (`available: false`, with the
  reason) rather than reporting an empty neighbourhood.
* **`correlates_with` edges can only exist where C4 already ran.** The
  graph re-reads measurements; it never re-measures a series. A pair that
  no investigation ever compared has no edge, however related it may be.
* **Two hops is the default reach.** Deeper relationships exist in the data
  but are not surfaced by default, because path confidence decays
  multiplicatively and a four-hop 0.8 chain is a 0.41 claim dressed as a
  connection.
* **`service` nodes describe ingestion provenance, not runtime topology.**
  A dataset's source system is what the platform has evidence of; a service
  dependency graph is not.
