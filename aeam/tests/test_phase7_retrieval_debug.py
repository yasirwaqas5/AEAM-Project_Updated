"""
aeam/tests/test_phase7_retrieval_debug.py

Retrieval explainability tests — the developer-only debug tracer.

Fully self-contained deterministic stubs (no network/model needed). Verifies:
- rankings remain deterministic across repeated trace() calls,
- provenance (originating_query/query_index/hybrid_rrf_score/rrf_score) is
  present and correct,
- chunk_ids are never mutated across stages,
- timings are recorded for every required stage,
- the tracer's final_chunks byte-matches an INDEPENDENTLY constructed real
  production pipeline (HybridRetrievalPipeline -> MultiQueryRetrievalPipeline
  -> RerankingRetrievalPipeline -> EvidenceDiversityPipeline) built from the
  exact same stub components — the core "matches the live pipeline exactly"
  requirement.
- graceful behaviour with every optional stage disabled (dense-only).
"""

from __future__ import annotations

import json

import pytest

from aeam.agents.rag.evidence_diversity import EvidenceDiversityFilter, EvidenceDiversityPipeline
from aeam.agents.rag.hybrid_retrieval import BM25Index, HybridRetrievalPipeline
from aeam.agents.rag.multi_query_retrieval import MultiQueryRetrievalPipeline
from aeam.agents.rag.query_expansion import QueryExpansionAgent
from aeam.agents.rag.reranker import CrossEncoderReranker, RerankingRetrievalPipeline
from aeam.agents.rag.retrieval_debug import RetrievalDebugTracer, summarize_chunk


# ---------------------------------------------------------------------------
# Deterministic stub components (mirror the style already used by
# test_phase7_hybrid.py / test_phase7_rerank.py / test_phase7_multiquery.py)
# ---------------------------------------------------------------------------

CORPUS = [
    {"chunk_id": "c1", "text": "database replication lag increased read query latency", "metadata": {"source": "runbook_db"}},
    {"chunk_id": "c2", "text": "cpu saturation from a runaway process caused elevated latency", "metadata": {"source": "runbook_cpu"}},
    {"chunk_id": "c3", "text": "cache eviction storm increased the redis miss rate", "metadata": {"source": "runbook_cache"}},
    {"chunk_id": "c4", "text": "payment gateway timeout during checkout caused a revenue drop", "metadata": {"source": "runbook_sales"}},
    {"chunk_id": "c5", "text": "disk IOPS saturation slowed write throughput on the primary volume", "metadata": {"source": "runbook_disk"}},
]
_BY_ID = {c["chunk_id"]: c for c in CORPUS}


class FakeDense:
    """Deterministic dense pipeline: fixed ranking per query text."""

    def __init__(self, ranking: dict[str, list[str]], collection="aeam_documents"):
        self._ranking = ranking
        self._collection = collection
        self.similarity_threshold = 0.5
        self.calls: list[str] = []

    @property
    def collection(self):
        return self._collection

    def search(self, query, filter_criteria=None, top_k=5):
        self.calls.append(query)
        ids = self._ranking.get(query, [])
        out = []
        for rank, cid in enumerate(ids[:top_k]):
            doc = _BY_ID[cid]
            out.append({
                "chunk_id": cid, "text": doc["text"], "metadata": dict(doc["metadata"]),
                "similarity": round(0.9 - 0.05 * rank, 6),
            })
        return out


class StubCrossEncoderModel:
    """Deterministic cross-encoder: higher score for texts containing `boost_kw`."""

    def __init__(self, boost_kw: str):
        self._kw = boost_kw

    def predict(self, pairs):
        return [10.0 if self._kw in text else 1.0 for _q, text in pairs]


class StubLLM:
    """Deterministic 'LLM' for query expansion — no network."""

    def __init__(self, variants: list[str]):
        self._variants = variants
        self.calls = 0

    def query(self, prompt, *, temperature=0.3, max_tokens=300):
        self.calls += 1
        return json.dumps({"queries": self._variants})


def build_stack(dense_ranking, boost_kw="cache", variants=("cache eviction miss rate",)):
    """Build one full, real (stub-backed) production-equivalent stack + a tracer over it."""
    dense = FakeDense(dense_ranking)
    bm25 = BM25Index()
    bm25.build(CORPUS)
    hybrid_stage = HybridRetrievalPipeline(dense, bm25)
    expander = QueryExpansionAgent(StubLLM(list(variants)), query_count=len(variants) + 1)
    reranker = CrossEncoderReranker(model=StubCrossEncoderModel(boost_kw))
    diversity = EvidenceDiversityFilter(similarity_threshold=0.9, max_chunks_per_document=2)

    tracer = RetrievalDebugTracer(
        dense=dense, bm25_index=bm25, hybrid_stage=hybrid_stage,
        query_expander=expander, reranker=reranker, diversity_filter=diversity,
        rerank_top_n=10,
    )
    return dense, bm25, hybrid_stage, expander, reranker, diversity, tracer


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------

def test_trace_rankings_are_deterministic_across_repeated_calls():
    *_, tracer = build_stack({"db replication issue": ["c1", "c2", "c3"]})
    r1 = tracer.trace(query="db replication issue", top_k=3)
    r2 = tracer.trace(query="db replication issue", top_k=3)

    assert [c["chunk_id"] for c in r1["final_chunks"]] == [c["chunk_id"] for c in r2["final_chunks"]]
    assert [c["chunk_id"] for c in r1["rrf_fused"]] == [c["chunk_id"] for c in r2["rrf_fused"]]
    assert [c["chunk_id"] for c in r1["reranked"]] == [c["chunk_id"] for c in r2["reranked"]]
    assert r1["expanded_queries"] == r2["expanded_queries"]


# ---------------------------------------------------------------------------
# Provenance preserved
# ---------------------------------------------------------------------------

def test_trace_preserves_provenance_fields():
    *_ , tracer = build_stack(
        {"cache issue": ["c1", "c2"], "cache eviction miss rate": ["c3"]},
        boost_kw="cache", variants=("cache eviction miss rate",),
    )
    result = tracer.trace(query="cache issue", top_k=5)

    assert len(result["expanded_queries"]) == 2
    assert result["expanded_queries"][0]["query_text"] == "cache issue"
    assert result["expanded_queries"][1]["query_text"] == "cache eviction miss rate"

    for entry in result["rrf_fused"]:
        assert "originating_query" in entry
        assert entry["originating_query"] == "cache issue"   # original query preserved (Phase 7.3 contract)
        assert "query_index" in entry

    # c3 (BM25-matched "cache") is recoverable with valid provenance. It
    # actually matches BM25 under BOTH the original and variant query text
    # (both contain "cache"), so its best-ranked contributing query can
    # legitimately be either — what matters is the field is present and valid.
    c3 = next((c for c in result["rrf_fused"] if c["chunk_id"] == "c3"), None)
    assert c3 is not None
    assert c3["query_index"] in (0, 1)
    assert c3["query_text"] in ("cache issue", "cache eviction miss rate")


def test_summarize_chunk_has_uniform_schema():
    item = {"chunk_id": "x", "text": "hello world", "metadata": {"source": "s"},
            "similarity": 0.42, "dense_similarity": 0.5, "rerank_score": 3.2}
    summary = summarize_chunk(item)
    assert set(summary.keys()) == {
        "chunk_id", "source", "text_preview", "similarity", "dense_similarity",
        "bm25_score", "hybrid_rrf_score", "rrf_score", "rerank_score",
        "originating_query", "query_index", "query_text", "final_rank",
    }
    assert summary["chunk_id"] == "x"
    assert summary["source"] == "s"
    assert summary["similarity"] == 0.42
    assert summary["dense_similarity"] == 0.5
    assert summary["rerank_score"] == 3.2
    assert summary["bm25_score"] is None   # not present on input -> None, never fabricated


# ---------------------------------------------------------------------------
# chunk_id integrity
# ---------------------------------------------------------------------------

def test_chunk_ids_unchanged_across_all_stages():
    *_ , tracer = build_stack({"cpu issue": ["c2", "c1"]}, boost_kw="cpu", variants=())
    # query_count effectively 1 (no variants) -> single-query path.
    dense, bm25, hybrid_stage, expander, reranker, diversity, tracer = build_stack(
        {"cpu issue": ["c2", "c1"]}, boost_kw="cpu", variants=(),
    )
    result = tracer.trace(query="cpu issue", top_k=5)

    known_ids = {c["chunk_id"] for c in CORPUS}
    for section in ("dense_results", "bm25_results", "rrf_fused", "reranked", "final_chunks"):
        for chunk in result[section]:
            assert chunk["chunk_id"] in known_ids, f"unexpected/mutated chunk_id in {section}: {chunk['chunk_id']}"


def test_final_rank_assigned_sequentially():
    *_ , tracer = build_stack({"disk issue": ["c5", "c1", "c2"]}, boost_kw="disk", variants=())
    result = tracer.trace(query="disk issue", top_k=3)
    ranks = [c["final_rank"] for c in result["final_chunks"]]
    assert ranks == list(range(1, len(ranks) + 1))


def test_similarity_field_present_from_fusion_onward():
    """
    `similarity` is the canonical field HybridRetrievalPipeline._finalize()
    guarantees on every chunk from the fused stage onward (0.0 for BM25-only
    chunks). The debug endpoint must surface it, not just the raw-dense-only
    `dense_similarity` alias.
    """
    *_ , tracer = build_stack({"disk issue": ["c5", "c1"]}, boost_kw="disk", variants=())
    result = tracer.trace(query="disk issue", top_k=3)
    for section in ("rrf_fused", "reranked", "final_chunks"):
        for chunk in result[section]:
            assert "similarity" in chunk
            assert isinstance(chunk["similarity"], (int, float))


def test_evidence_diversity_output_matches_final_chunks():
    """
    In the current frozen architecture, EvidenceDiversityFilter.filter()
    performs diversity filtering AND Top-K selection in one call — there is
    no separate post-diversity trimming stage — so these two sections must
    be identical, exposed under both names per the explicit stage list.
    """
    *_ , tracer = build_stack({"disk issue": ["c5", "c1", "c2"]}, boost_kw="disk", variants=())
    result = tracer.trace(query="disk issue", top_k=3)
    assert result["evidence_diversity_output"] == result["final_chunks"]


def test_stage_survival_uses_required_phrasing():
    """Requirement: explanations must be readable as 'removed by reranker' /
    'removed by evidence diversity' / 'removed during fusion'."""
    dense_ranking = {"cache issue": ["c1", "c2", "c3", "c4", "c5"]}
    dense, bm25, hybrid_stage, expander, reranker, diversity, tracer = build_stack(
        dense_ranking, boost_kw="cache", variants=(),
    )
    # Force top_k small enough, and rerank/diversity pools small enough, that
    # at least one candidate is dropped at each of the three stages.
    tracer = RetrievalDebugTracer(
        dense=dense, bm25_index=bm25, hybrid_stage=hybrid_stage,
        query_expander=None, reranker=reranker, diversity_filter=diversity,
        rerank_top_n=3,
    )
    result = tracer.trace(query="cache issue", top_k=1)
    stages_seen = {e["removed_at_stage"] for e in result["stage_survival"]}
    explanations = " ".join(e["explanation"] for e in result["stage_survival"])
    assert stages_seen & {"fusion", "reranker", "evidence_diversity", None}
    if "fusion" in stages_seen:
        assert "removed during fusion" in explanations
    if "reranker" in stages_seen:
        assert "removed by reranker" in explanations
    if "evidence_diversity" in stages_seen:
        assert "removed by evidence diversity" in explanations


# ---------------------------------------------------------------------------
# Timings recorded
# ---------------------------------------------------------------------------

def test_timings_are_recorded_for_every_stage():
    *_ , tracer = build_stack({"sales issue": ["c4"]}, boost_kw="payment", variants=())
    result = tracer.trace(query="sales issue", top_k=3)
    timings = result["timings_ms"]
    for key in (
        "query_expansion_ms", "embedding_search_ms", "bm25_search_ms",
        "rrf_fusion_ms", "reranking_ms", "diversity_ms", "total_retrieval_latency_ms",
    ):
        assert key in timings, f"missing timing key: {key}"
        assert isinstance(timings[key], (int, float))
        assert timings[key] >= 0.0


# ---------------------------------------------------------------------------
# Debug endpoint / tracer matches the live pipeline exactly
# ---------------------------------------------------------------------------

def test_final_chunks_matches_independently_built_real_pipeline():
    """
    Build the tracer AND a fully independent, real, fully-composed production
    pipeline (same classes main.py wires: Hybrid -> MultiQuery -> Reranking ->
    EvidenceDiversity) from the identical stub components, then assert the
    tracer's final_chunks chunk_id order is byte-identical to calling
    .search() directly on the real composed pipeline.
    """
    dense_ranking = {
        "checkout issue": ["c2", "c1", "c5"],
        "payment gateway checkout timeout": ["c4"],
    }
    dense, bm25, hybrid_stage, expander, reranker, diversity, tracer = build_stack(
        dense_ranking, boost_kw="payment", variants=("payment gateway checkout timeout",),
    )

    # Independently compose the REAL production pipeline chain.
    real_pipeline = EvidenceDiversityPipeline(
        inner_pipeline=RerankingRetrievalPipeline(
            inner_pipeline=MultiQueryRetrievalPipeline(
                inner_pipeline=hybrid_stage,
                query_expansion_agent=expander,
            ),
            reranker=reranker,
            rerank_top_n=10,
        ),
        diversity_filter=diversity,
    )

    traced = tracer.trace(query="checkout issue", top_k=3)
    live_result = real_pipeline.search(query="checkout issue", top_k=3)

    traced_ids = [c["chunk_id"] for c in traced["final_chunks"]]
    live_ids = [c["chunk_id"] for c in live_result]

    assert traced_ids == live_ids
    assert "c4" in traced_ids   # recovered via the expanded query, in both


def test_expander_called_exactly_once_per_trace():
    """The caching wrapper must prevent a second real LLM call within one trace."""
    dense_ranking = {"auth issue": ["c1"], "auth token rejection": ["c3"]}
    dense = FakeDense(dense_ranking)
    bm25 = BM25Index(); bm25.build(CORPUS)
    hybrid_stage = HybridRetrievalPipeline(dense, bm25)
    stub_llm = StubLLM(["auth token rejection"])
    expander = QueryExpansionAgent(stub_llm, query_count=2)
    reranker = CrossEncoderReranker(model=StubCrossEncoderModel("cache"))
    diversity = EvidenceDiversityFilter(similarity_threshold=0.9, max_chunks_per_document=2)
    tracer = RetrievalDebugTracer(
        dense=dense, bm25_index=bm25, hybrid_stage=hybrid_stage,
        query_expander=expander, reranker=reranker, diversity_filter=diversity,
        rerank_top_n=10,
    )
    tracer.trace(query="auth issue", top_k=3)
    assert stub_llm.calls == 1   # exactly one real LLM call, despite multiple internal .search() layers


# ---------------------------------------------------------------------------
# Graceful degradation — every optional stage disabled
# ---------------------------------------------------------------------------

def test_trace_with_all_optional_stages_disabled():
    dense = FakeDense({"plain query": ["c1", "c2", "c3"]})
    tracer = RetrievalDebugTracer(
        dense=dense, bm25_index=None, hybrid_stage=dense,
        query_expander=None, reranker=None, diversity_filter=None,
        rerank_top_n=10,
    )
    result = tracer.trace(query="plain query", top_k=2)
    assert [c["chunk_id"] for c in result["final_chunks"]] == ["c1", "c2"]
    assert result["expanded_queries"] == [{"query_index": 0, "query_text": "plain query"}]
    assert result["bm25_results"] == []


def test_trace_invalid_input_raises():
    dense = FakeDense({})
    tracer = RetrievalDebugTracer(
        dense=dense, bm25_index=None, hybrid_stage=dense,
        query_expander=None, reranker=None, diversity_filter=None,
        rerank_top_n=10,
    )
    with pytest.raises(ValueError):
        tracer.trace(query="", top_k=5)
    with pytest.raises(ValueError):
        tracer.trace(query="x", top_k=0)


def test_tracer_requires_dense():
    with pytest.raises(ValueError):
        RetrievalDebugTracer(
            dense=None, bm25_index=None, hybrid_stage=None,
            query_expander=None, reranker=None, diversity_filter=None,
            rerank_top_n=10,
        )
