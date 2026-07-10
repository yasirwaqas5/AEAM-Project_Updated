# Retrieval Debugging & Explainability

Developer-only tooling for inspecting exactly what AEAM's RAG retrieval pipeline
does for a given query — which chunks each stage retrieved, why a chunk did or
didn't survive into the final evidence set, and how long each stage took.

This is observability tooling only. It never changes retrieval behavior and is
not called by `RAGAgent`, the Orchestrator, or any production code path.

## Pipeline this traces

```
Query
  ├─ Query Expansion (LLM, if RAG_MULTI_QUERY_ENABLED)
  ├─ Dense retrieval (Qdrant)
  ├─ BM25 retrieval (lexical)
  ├─ RRF fusion (dense + BM25, then across expanded queries)
  ├─ Cross-encoder reranking (if RAG_RERANK_ENABLED)
  └─ Evidence diversity filter (if RAG_DIVERSITY_ENABLED)
        ↓
   final_chunks  ← what RAGAgent actually receives
```

Every stage is individually toggleable via existing settings
(`RAG_HYBRID_ENABLED`, `RAG_MULTI_QUERY_ENABLED`, `RAG_RERANK_ENABLED`,
`RAG_DIVERSITY_ENABLED`) — the debug endpoint automatically reflects whichever
stages are actually active; disabled stages simply pass through unchanged
(e.g. with `RAG_RERANK_ENABLED=false`, the `reranked` section is identical to
`rrf_fused`).

## Endpoint

```
GET /api/v1/debug/retrieval?query=<text>&top_k=5
```

Disabled (returns `404`, not `403` — its existence is not disclosed) when
`ENVIRONMENT=production`. Available in `development`/`staging`/`test`.

```bash
curl -s "http://localhost:8080/api/v1/debug/retrieval/?query=database+replication+lag&top_k=5" | python -m json.tool
```

### Response shape

```jsonc
{
  "query": "database replication lag",
  "expanded_queries": [
    {"query_index": 0, "query_text": "database replication lag"},
    {"query_index": 1, "query_text": "database replication delay slow reads"}
  ],
  "dense_results":            [ {chunk...}, ... ],   // raw dense-only hits (informational)
  "bm25_results":             [ {chunk...}, ... ],   // raw BM25-only hits (informational)
  "rrf_fused":                [ {chunk...}, ... ],   // fused candidate pool feeding the reranker
  "reranked":                 [ {chunk...}, ... ],   // reranked candidate pool feeding diversity
  "evidence_diversity_output":[ {chunk...}, ... ],   // == final_chunks (see note below)
  "final_chunks":             [ {chunk...}, ... ],   // exactly what RAGAgent receives
  "timings_ms": {
    "query_expansion_ms": 812.4,
    "embedding_search_ms": 41.2,
    "bm25_search_ms": 3.1,
    "rrf_fusion_ms": 5.7,
    "reranking_ms": 340.8,
    "diversity_ms": 0.9,
    "total_retrieval_latency_ms": 1204.1
  },
  "stage_survival": [
    {
      "chunk_id": "a17801f6...",
      "in_dense": true, "in_bm25": true, "in_rrf_fused": true,
      "in_reranked": true, "in_final": true,
      "removed_at_stage": null,
      "explanation": "survived every stage — present in the final chunks returned to RAGAgent."
    },
    {
      "chunk_id": "94adc384...",
      "in_dense": true, "in_bm25": false, "in_rrf_fused": true,
      "in_reranked": true, "in_final": false,
      "removed_at_stage": "evidence_diversity",
      "explanation": "removed by evidence diversity — survived reranking but was dropped as a near-duplicate of a higher-ranked chunk, or for exceeding the per-document/section cap."
    }
  ]
}
```

Every chunk object in every stage list (`dense_results`, `bm25_results`,
`rrf_fused`, `reranked`, `evidence_diversity_output`, `final_chunks`) has the
same flat, predictable shape:

| Field | Meaning | Populated by |
|---|---|---|
| `chunk_id` | Stable identifier — **never changes across stages** | ingestion |
| `source` | Source document (`metadata.source`) | ingestion |
| `text_preview` | First 160 chars of the chunk text | — |
| `similarity` | Canonical similarity score (0.0 for BM25-only chunks) | hybrid stage (`_finalize`) |
| `dense_similarity` | Raw cosine similarity to the query (dense-only view) | dense stage |
| `bm25_score` | Okapi BM25 score | BM25 stage |
| `hybrid_rrf_score` | Dense+BM25 fusion score (per query variant) | hybrid stage |
| `rrf_score` | Cross-query fusion score (after multi-query fusion) | multi-query stage |
| `rerank_score` | Cross-encoder relevance score | rerank stage |
| `originating_query` | The **original** user/investigation query | multi-query stage |
| `query_index` / `query_text` | Which expanded query variant best matched this chunk | multi-query stage |
| `final_rank` | 1-based position in `final_chunks` (`null` elsewhere) | diversity stage |

`stage_survival` entries also carry a machine-readable `removed_at_stage`
(`"fusion"` / `"reranker"` / `"evidence_diversity"` / `null` if it survived
everything) alongside the human-readable `explanation`.

A field is `null` when the stage that populates it didn't run (e.g.
`hybrid_rrf_score` is `null` for every chunk if `RAG_HYBRID_ENABLED=false`) or
hasn't been reached yet by that section (e.g. `final_rank` is `null` on
`dense_results`).

## Comparing retrieval quality across stages

The most useful comparison is usually **"what did stage N drop that stage N-1
had?"** — read the chunk_id sets between two adjacent sections:

```python
import requests

r = requests.get("http://localhost:8080/api/v1/debug/retrieval/",
                  params={"query": "cpu saturation runaway process", "top_k": 5}).json()

fused_ids    = {c["chunk_id"] for c in r["rrf_fused"]}
reranked_ids = {c["chunk_id"] for c in r["reranked"]}
final_ids    = {c["chunk_id"] for c in r["final_chunks"]}

print("dropped by reranking: ", fused_ids - reranked_ids)
print("dropped by diversity: ", reranked_ids - final_ids)
```

Or read `stage_survival` directly — it already computes this per chunk with a
human-readable `explanation`.

### Comparing WITH a stage vs WITHOUT it

Since every stage is gated by an existing settings flag, the most direct way
to measure a stage's actual contribution is to run the **same query** with
that flag off vs on and diff `final_chunks`:

```bash
# Baseline: dense + BM25 + RRF, no reranking, no diversity
RAG_RERANK_ENABLED=false RAG_DIVERSITY_ENABLED=false \
  uvicorn aeam.main:app --port 8081 &

curl -s "http://localhost:8081/api/v1/debug/retrieval/?query=auth+failure&top_k=5" \
  | python -c "import json,sys; print([c['chunk_id'] for c in json.load(sys.stdin)['final_chunks']])"

# Full stack
curl -s "http://localhost:8080/api/v1/debug/retrieval/?query=auth+failure&top_k=5" \
  | python -c "import json,sys; print([c['chunk_id'] for c in json.load(sys.stdin)['final_chunks']])"
```

A meaningful stage contribution shows up as: (a) different chunk_ids in
`final_chunks`, and/or (b) the same chunk_ids in a different order. No
difference means that stage had no effect for this particular query — which
is itself useful, honest information (see the Phase 7.4 completion report for
a real example: on AEAM's current single-document corpus, document-diversity
capping shows zero effect because there's only one document to diversify
against).

### Reading timings

`timings_ms` isolates each stage's own wall-clock cost. `query_expansion_ms`
dominates when multi-query is enabled (one real LLM call); `reranking_ms`
dominates otherwise (one cross-encoder forward pass per candidate). Use these
to decide whether a quality improvement from enabling a stage is worth its
latency cost for your use case.

## Implementation notes (why the trace is trustworthy)

The tracer (`aeam/agents/rag/retrieval_debug.py`) never reimplements RRF,
reranking, or diversity math — it calls the exact same classes/functions
`main.py` wires into production (`HybridRetrievalPipeline`,
`MultiQueryRetrievalPipeline`, `CrossEncoderReranker.rerank()`,
`EvidenceDiversityFilter.filter()`), using the same shared, live component
instances. The one subtlety: query expansion is LLM-backed and therefore
non-deterministic across independent calls, so the tracer computes it exactly
once per trace (via a short-lived, per-request caching wrapper) and threads
that fixed query list through every later stage — guaranteeing internal
consistency and exactly one LLM call per trace, without ever mutating the
shared production `QueryExpansionAgent` (safe under concurrent requests).

This is verified directly: `aeam/tests/test_phase7_retrieval_debug.py::test_final_chunks_matches_independently_built_real_pipeline`
builds a real production pipeline chain independently and asserts the
tracer's `final_chunks` are byte-identical to calling `.search()` on it
directly.
