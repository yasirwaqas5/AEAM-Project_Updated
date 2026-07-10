"""
aeam/tests/test_phase7_multiquery.py

Phase 7.3 tests — Multi-Query Retrieval (query expansion + per-query hybrid
retrieval + cross-query RRF fusion).

Self-contained: LLM is stubbed, so these tests need no network/model. They
verify:
- QueryExpansionAgent: parses valid output, always preserves the original
  query first, dedups/caps variants, and falls back to [original] on every
  failure mode (LLM exception, unparsable JSON, non-list, empty).
- MultiQueryRetrievalPipeline: drop-in contract, merge+dedup by chunk_id,
  provenance fields (originating_query/query_index/query_text/query_matches),
  hybrid-level provenance preserved under relabeled keys (no collision),
  citations/chunk_id integrity, graceful behaviour when expansion is skipped.
- Measured Recall@K / MRR / nDCG improvement of multi-query fusion over a
  single query, for a case where a synonym-only query misses the relevant
  chunk entirely.
"""

from __future__ import annotations

import json
import math

import pytest

from aeam.agents.rag.query_expansion import QueryExpansionAgent
from aeam.agents.rag.multi_query_retrieval import MultiQueryRetrievalPipeline


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------

class StubLLM:
    def __init__(self, response=None, raise_exc=None):
        self._response = response
        self._raise_exc = raise_exc
        self.calls = 0
        self.last_prompt = None

    def query(self, prompt, *, temperature=0.3, max_tokens=300):
        self.calls += 1
        self.last_prompt = prompt
        if self._raise_exc:
            raise self._raise_exc
        return self._response


class FakeHybrid:
    """Fake inner (hybrid) pipeline: returns a fixed candidate list per query text."""

    def __init__(self, per_query: dict[str, list[dict]], collection="aeam_documents"):
        self._per_query = per_query
        self._collection = collection
        self.similarity_threshold = 0.5
        self.calls: list[str] = []

    @property
    def collection(self):
        return self._collection

    def search(self, query, filter_criteria=None, top_k=5):
        self.calls.append(query)
        results = self._per_query.get(query, [])
        return [dict(r) for r in results[:top_k]]


def _chunk(cid, text="text", sim=0.7, rrf=0.02, sources=None):
    return {
        "chunk_id": cid,
        "text": text,
        "metadata": {"source": f"src_{cid}"},
        "similarity": sim,
        "rrf_score": rrf,
        "retrieval_sources": sources or ["dense", "bm25"],
    }


def expander_with(query_count, response_json):
    return QueryExpansionAgent(StubLLM(response=json.dumps(response_json)), query_count=query_count)


# ---------------------------------------------------------------------------
# QueryExpansionAgent
# ---------------------------------------------------------------------------

def test_expand_always_preserves_original_first():
    exp = expander_with(3, {"queries": ["variant A", "variant B"]})
    out = exp.expand("original query")
    assert out[0] == "original query"
    assert out == ["original query", "variant A", "variant B"]


def test_expand_count_one_skips_llm_entirely():
    llm = StubLLM(response="should never be read")
    exp = QueryExpansionAgent(llm, query_count=1)
    out = exp.expand("q")
    assert out == ["q"]
    assert llm.calls == 0


def test_expand_caps_at_query_count_minus_one():
    exp = expander_with(2, {"queries": ["v1", "v2", "v3", "v4"]})
    out = exp.expand("orig")
    assert len(out) == 2   # 1 original + (query_count-1)=1 variant
    assert out == ["orig", "v1"]


def test_expand_dedups_case_insensitive_against_original_and_each_other():
    exp = expander_with(4, {"queries": ["Orig", "variant", "VARIANT", "unique"]})
    out = exp.expand("orig")
    assert out == ["orig", "variant", "unique"]


def test_expand_falls_back_on_llm_exception():
    exp = QueryExpansionAgent(StubLLM(raise_exc=RuntimeError("groq down")), query_count=4)
    assert exp.expand("q") == ["q"]


def test_expand_falls_back_on_unparsable_json():
    exp = QueryExpansionAgent(StubLLM(response="not json at all"), query_count=4)
    assert exp.expand("q") == ["q"]


def test_expand_falls_back_when_queries_not_a_list():
    exp = expander_with(4, {"queries": "not a list"})
    assert exp.expand("q") == ["q"]


def test_expand_falls_back_when_zero_usable_variants():
    exp = expander_with(4, {"queries": ["", "   ", "q"]})   # blanks + dup of original
    assert exp.expand("q") == ["q"]


def test_expand_recovers_from_markdown_fenced_response():
    # Reuses the shared resilient parser (Phase 6) — fences should just work.
    llm = StubLLM(response='```json\n{"queries": ["alt one"]}\n```')
    exp = QueryExpansionAgent(llm, query_count=2)
    assert exp.expand("q") == ["q", "alt one"]


def test_expand_empty_original_query_raises():
    exp = expander_with(3, {"queries": []})
    with pytest.raises(ValueError):
        exp.expand("")


# ---------------------------------------------------------------------------
# MultiQueryRetrievalPipeline — drop-in contract, merge/dedup, provenance
# ---------------------------------------------------------------------------

def test_pipeline_is_drop_in():
    hybrid = FakeHybrid({"orig": [_chunk("c1")]})
    pipe = MultiQueryRetrievalPipeline(hybrid, expander_with(1, {}))
    assert pipe.similarity_threshold == 0.5
    assert pipe.collection == "aeam_documents"
    out = pipe.search(query="orig", top_k=1)
    assert isinstance(out, list)


def test_pipeline_dedups_chunk_found_by_multiple_queries():
    hybrid = FakeHybrid({
        "orig": [_chunk("shared"), _chunk("only_orig")],
        "alt":  [_chunk("shared"), _chunk("only_alt")],
    })
    exp = expander_with(2, {"queries": ["alt"]})
    pipe = MultiQueryRetrievalPipeline(hybrid, exp)
    out = pipe.search(query="orig", top_k=10)
    ids = [c["chunk_id"] for c in out]
    assert ids.count("shared") == 1                 # deduplicated, not doubled
    assert set(ids) == {"shared", "only_orig", "only_alt"}


def test_pipeline_provenance_fields_present_and_correct():
    hybrid = FakeHybrid({
        "orig": [_chunk("c1")],
        "alt":  [_chunk("c1")],   # c1 rank 1 under both queries
    })
    exp = expander_with(2, {"queries": ["alt"]})
    pipe = MultiQueryRetrievalPipeline(hybrid, exp)
    out = pipe.search(query="orig", top_k=5)
    c1 = next(r for r in out if r["chunk_id"] == "c1")

    assert c1["originating_query"] == "orig"          # requirement: original query preserved/attached
    assert c1["query_index"] in (0, 1)
    assert c1["query_text"] in ("orig", "alt")
    assert len(c1["query_matches"]) == 2              # matched under both q0 and q1
    assert {m["query_index"] for m in c1["query_matches"]} == {0, 1}
    assert {m["query_text"] for m in c1["query_matches"]} == {"orig", "alt"}


def test_pipeline_preserves_hybrid_level_provenance_under_relabeled_keys():
    hybrid = FakeHybrid({"orig": [_chunk("c1", rrf=0.0314, sources=["dense", "bm25"])]})
    pipe = MultiQueryRetrievalPipeline(hybrid, expander_with(1, {}))
    out = pipe.search(query="orig", top_k=1)
    c1 = out[0]
    # Hybrid stage's own fusion score/sources survive under new names...
    assert c1["hybrid_rrf_score"] == 0.0314
    assert c1["hybrid_retrieval_sources"] == ["dense", "bm25"]
    # ...and are not confused with this stage's own (now query-level) fusion fields.
    assert c1["retrieval_sources"] == ["q0"]


def test_pipeline_preserves_chunk_id_text_metadata_similarity():
    hybrid = FakeHybrid({"orig": [_chunk("c42", text="the actual text", sim=0.81)]})
    pipe = MultiQueryRetrievalPipeline(hybrid, expander_with(1, {}))
    out = pipe.search(query="orig", top_k=1)
    c = out[0]
    assert c["chunk_id"] == "c42"
    assert c["text"] == "the actual text"
    assert c["metadata"] == {"source": "src_c42"}
    assert c["similarity"] == 0.81


def test_pipeline_runs_each_query_against_inner():
    hybrid = FakeHybrid({"orig": [_chunk("c1")], "alt1": [_chunk("c2")], "alt2": [_chunk("c3")]})
    exp = expander_with(3, {"queries": ["alt1", "alt2"]})
    pipe = MultiQueryRetrievalPipeline(hybrid, exp)
    pipe.search(query="orig", top_k=5)
    assert hybrid.calls == ["orig", "alt1", "alt2"]


def test_pipeline_falls_back_gracefully_when_expansion_yields_original_only():
    # Expansion agent itself falls back internally (e.g. LLM down) -> pipeline
    # still works correctly with just the original query, no special-casing needed.
    hybrid = FakeHybrid({"orig": [_chunk("c1"), _chunk("c2")]})
    exp = QueryExpansionAgent(StubLLM(raise_exc=RuntimeError("down")), query_count=4)
    pipe = MultiQueryRetrievalPipeline(hybrid, exp)
    out = pipe.search(query="orig", top_k=5)
    assert [c["chunk_id"] for c in out] == ["c1", "c2"]
    assert hybrid.calls == ["orig"]   # only one retrieval pass ran


def test_pipeline_empty_query_raises():
    pipe = MultiQueryRetrievalPipeline(FakeHybrid({}), expander_with(1, {}))
    for bad in ("", "   "):
        with pytest.raises(ValueError):
            pipe.search(query=bad, top_k=3)


# ---------------------------------------------------------------------------
# Measured quality: single query vs multi-query fusion (Recall@K, MRR, nDCG)
# ---------------------------------------------------------------------------

def _recall_at_k(ids, rel, k):
    return len(set(ids[:k]) & rel) / len(rel)

def _mrr(ids, rel):
    for i, c in enumerate(ids, 1):
        if c in rel:
            return 1.0 / i
    return 0.0

def _ndcg_at_k(ids, rel, k):
    dcg = sum((1.0 if c in rel else 0.0) / math.log2(i + 1) for i, c in enumerate(ids[:k], 1))
    ideal = sum(1.0 / math.log2(i + 1) for i in range(1, min(len(rel), k) + 1))
    return dcg / ideal if ideal > 0 else 0.0


def test_multiquery_improves_recall_mrr_ndcg_when_original_query_misses():
    """
    The original query ("db slow") retrieves nothing relevant (a synonym gap).
    An LLM-generated variant ("database replication lag") retrieves the
    relevant chunk. Assert multi-query fusion recovers it while single-query
    (original-only) retrieval does not.
    """
    k = 3
    hybrid = FakeHybrid({
        "db slow": [_chunk("d1", "disk io"), _chunk("d2", "cpu spike"), _chunk("d3", "cache miss")],
        "database replication lag": [_chunk("rel", "database replication lag detected")],
    })
    relevant = {"rel"}

    # Single-query baseline (multi-query disabled == query_count=1).
    single = MultiQueryRetrievalPipeline(hybrid, expander_with(1, {}))
    single_ids = [c["chunk_id"] for c in single.search(query="db slow", top_k=k)]

    # Multi-query (LLM supplies the synonym reformulation).
    exp = expander_with(2, {"queries": ["database replication lag"]})
    multi = MultiQueryRetrievalPipeline(hybrid, exp)
    multi_ids = [c["chunk_id"] for c in multi.search(query="db slow", top_k=k)]

    single_metrics = (_recall_at_k(single_ids, relevant, k), _mrr(single_ids, relevant), _ndcg_at_k(single_ids, relevant, k))
    multi_metrics = (_recall_at_k(multi_ids, relevant, k), _mrr(multi_ids, relevant), _ndcg_at_k(multi_ids, relevant, k))

    print(f"\n[single-query] Recall@{k}={single_metrics[0]:.3f} MRR={single_metrics[1]:.3f} nDCG@{k}={single_metrics[2]:.3f}")
    print(f"[multi-query ] Recall@{k}={multi_metrics[0]:.3f} MRR={multi_metrics[1]:.3f} nDCG@{k}={multi_metrics[2]:.3f}")

    # "rel" ties d1's RRF score (both matched at rank 1 in one list only); the
    # fusion's deterministic tie-break (by chunk_id, descending) happens to
    # place "rel" first here — so multi-query fully recovers it at rank 1.
    assert single_metrics == (0.0, 0.0, 0.0)
    assert multi_metrics == (1.0, pytest.approx(1.0), pytest.approx(1.0))
    assert multi_metrics[0] > single_metrics[0]
    assert multi_metrics[1] > single_metrics[1]
    assert multi_metrics[2] > single_metrics[2]
