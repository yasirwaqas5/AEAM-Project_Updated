"""
aeam/tests/test_phase7_hybrid.py

Phase 7.1 tests — Hybrid retrieval (BM25 + dense) with Reciprocal Rank Fusion.

Fully self-contained: BM25 is pure Python and the dense retriever is faked
with a deterministic ranking, so these tests need no Qdrant, embeddings, or
network. They verify:
- tokenisation,
- BM25 lexical ranking,
- RRF fusion math + agreement bonus + stability,
- HybridRetrievalPipeline drop-in contract and preserved evidence schema,
- a measured recall improvement of hybrid over dense-only.
"""

from __future__ import annotations

from aeam.agents.rag.hybrid_retrieval import (
    BM25Index,
    HybridRetrievalPipeline,
    reciprocal_rank_fusion,
    tokenize,
)


# ---------------------------------------------------------------------------
# Corpus + fake dense pipeline
# ---------------------------------------------------------------------------

CORPUS: list[dict] = [
    {"chunk_id": "c1", "text": "payment gateway timeout during checkout caused a revenue drop", "metadata": {"source": "runbook_sales"}},
    {"chunk_id": "c2", "text": "database replication lag increased read query latency", "metadata": {"source": "runbook_db"}},
    {"chunk_id": "c3", "text": "kubernetes pod eviction under memory pressure triggered an OOM kill", "metadata": {"source": "runbook_k8s"}},
    {"chunk_id": "c4", "text": "marketing email campaign bounce rate was unusually high", "metadata": {"source": "runbook_mkt"}},
    {"chunk_id": "c5", "text": "disk IOPS saturation slowed write throughput on the primary volume", "metadata": {"source": "runbook_disk"}},
    {"chunk_id": "c6", "text": "cpu saturation from a runaway process caused elevated latency", "metadata": {"source": "runbook_cpu"}},
    {"chunk_id": "c7", "text": "cache eviction storm increased the redis miss rate", "metadata": {"source": "runbook_cache"}},
]

_TEXT_BY_ID = {c["chunk_id"]: c for c in CORPUS}


class FakeDensePipeline:
    """
    Deterministic stand-in for the dense RetrievalPipeline.

    Returns a caller-supplied ranking per query, mimicking an embedding model
    that under-weights certain exact lexical terms. Emits the same result
    schema (chunk_id/text/metadata/similarity) and exposes the attributes
    RAGAgent reads.
    """

    def __init__(self, ranking: dict[str, list[str]], collection: str = "aeam_documents"):
        self._ranking = ranking
        self._collection = collection
        self.similarity_threshold = 0.5

    @property
    def collection(self) -> str:
        return self._collection

    def search(self, query, filter_criteria=None, top_k=5):
        ids = self._ranking.get(query, [])
        out = []
        # Descending, plausible cosine scores starting comfortably above threshold.
        for rank, cid in enumerate(ids[:top_k]):
            doc = _TEXT_BY_ID[cid]
            out.append({
                "chunk_id": cid,
                "text": doc["text"],
                "metadata": dict(doc["metadata"]),
                "similarity": round(0.9 - 0.05 * rank, 6),
            })
        return out


def build_bm25() -> BM25Index:
    idx = BM25Index()
    idx.build(CORPUS)
    return idx


# ---------------------------------------------------------------------------
# Tokenisation
# ---------------------------------------------------------------------------

def test_tokenize_lowercases_splits_and_drops_stopwords():
    toks = tokenize("The Payment-Gateway TIMEOUT, during checkout!")
    assert "payment" in toks and "gateway" in toks and "timeout" in toks
    assert "the" not in toks           # stopword
    assert "checkout" in toks
    assert all(len(t) > 1 for t in toks)


def test_tokenize_empty():
    assert tokenize("") == []
    assert tokenize("   ") == []


# ---------------------------------------------------------------------------
# BM25
# ---------------------------------------------------------------------------

def test_bm25_ranks_exact_lexical_match_first():
    idx = build_bm25()
    results = idx.search("payment gateway checkout timeout", top_k=3)
    assert results, "BM25 returned no results for a clearly matching query"
    assert results[0]["chunk_id"] == "c1"
    assert results[0]["bm25_score"] > 0.0
    # schema
    assert set(results[0].keys()) >= {"chunk_id", "text", "metadata", "bm25_score"}


def test_bm25_empty_query_or_index():
    idx = build_bm25()
    assert idx.search("", top_k=5) == []
    assert idx.search("the and of", top_k=5) == []   # all stopwords
    empty = BM25Index()
    empty.build([])
    assert empty.search("payment", top_k=5) == []
    assert empty.size == 0


def test_bm25_rare_term_outranks_common_term():
    idx = build_bm25()
    # "redis" appears in exactly one doc (c7) -> high idf -> should win.
    results = idx.search("redis latency", top_k=3)
    assert results[0]["chunk_id"] == "c7"


# ---------------------------------------------------------------------------
# Reciprocal Rank Fusion
# ---------------------------------------------------------------------------

def test_rrf_pure_math_two_lists():
    dense = [{"chunk_id": "a"}, {"chunk_id": "b"}]
    bm25 = [{"chunk_id": "b"}, {"chunk_id": "c"}]
    fused = reciprocal_rank_fusion([dense, bm25], k=60, source_names=["dense", "bm25"])
    scores = {e["chunk_id"]: e["rrf_score"] for e in fused}
    # b is rank1 in bm25 and rank2 in dense -> 1/61 + 1/62 (highest)
    assert abs(scores["b"] - (1/61 + 1/62)) < 1e-9
    assert abs(scores["a"] - (1/61)) < 1e-9        # dense rank1 only
    assert abs(scores["c"] - (1/62)) < 1e-9        # bm25 rank2 only
    assert fused[0]["chunk_id"] == "b"             # agreement wins


def test_rrf_records_provenance_and_preserves_fields():
    dense = [{"chunk_id": "x", "text": "t", "metadata": {"s": 1}, "similarity": 0.8}]
    bm25 = [{"chunk_id": "x", "text": "t", "metadata": {"s": 1}, "bm25_score": 4.2}]
    fused = reciprocal_rank_fusion([dense, bm25], source_names=["dense", "bm25"])
    e = fused[0]
    assert e["retrieval_sources"] == ["dense", "bm25"]
    assert e["dense_rank"] == 1 and e["bm25_rank"] == 1
    assert e["similarity"] == 0.8 and e["bm25_score"] == 4.2   # both preserved


def test_rrf_is_deterministically_ordered():
    # Two chunks with identical single-list rank -> tie broken by chunk_id desc.
    lst = [{"chunk_id": "aaa"}], [{"chunk_id": "zzz"}]
    fused = reciprocal_rank_fusion(list(lst))
    assert [e["chunk_id"] for e in fused] == ["zzz", "aaa"]


# ---------------------------------------------------------------------------
# HybridRetrievalPipeline — drop-in contract + evidence schema
# ---------------------------------------------------------------------------

def test_hybrid_is_drop_in_for_retrieval_pipeline():
    dense = FakeDensePipeline({"q": ["c2", "c3"]})
    hybrid = HybridRetrievalPipeline(dense, build_bm25())
    # Attributes RAGAgent reads via getattr.
    assert hybrid.similarity_threshold == 0.5
    assert hybrid.collection == "aeam_documents"
    # Same call shape as RetrievalPipeline.search.
    out = hybrid.search(query="database replication lag", top_k=3)
    assert isinstance(out, list) and len(out) <= 3


def test_hybrid_preserves_evidence_schema_and_adds_provenance():
    dense = FakeDensePipeline({"database replication lag": ["c2", "c3"]})
    hybrid = HybridRetrievalPipeline(dense, build_bm25())
    out = hybrid.search(query="database replication lag", top_k=5)
    assert out
    for r in out:
        # Existing evidence keys preserved (citations/validation depend on these).
        assert "chunk_id" in r and "text" in r and "metadata" in r
        assert isinstance(r["similarity"], (int, float))
        # Provenance added.
        assert "rrf_score" in r and "retrieval_sources" in r
        assert set(r["retrieval_sources"]) <= {"dense", "bm25"}


def test_hybrid_surfaces_bm25_only_chunk_with_valid_similarity():
    # Dense returns only c3/c6; the lexical answer c1 is BM25-only.
    dense = FakeDensePipeline({"checkout payment gateway": ["c3", "c6"]})
    hybrid = HybridRetrievalPipeline(dense, build_bm25())
    out = hybrid.search(query="checkout payment gateway", top_k=5)
    ids = [r["chunk_id"] for r in out]
    assert "c1" in ids                                   # lexical recall win
    c1 = next(r for r in out if r["chunk_id"] == "c1")
    assert c1["retrieval_sources"] == ["bm25"]
    assert isinstance(c1["similarity"], (int, float))    # never None/invalid
    assert c1["bm25_score"] and c1["bm25_score"] > 0.0


def test_hybrid_empty_query_raises():
    hybrid = HybridRetrievalPipeline(FakeDensePipeline({}), build_bm25())
    for bad in ("", "   "):
        try:
            hybrid.search(query=bad, top_k=3)
            assert False, "expected ValueError"
        except ValueError:
            pass


# ---------------------------------------------------------------------------
# Measured improvement: dense-only vs hybrid recall
# ---------------------------------------------------------------------------

def _recall_at_k(results: list[dict], golden: set[str], k: int) -> float:
    got = {r["chunk_id"] for r in results[:k]}
    return len(got & golden) / len(golden)


def test_vector_vs_hybrid_recall_improvement():
    """
    Scenario: three queries whose exact lexical answer is under-ranked by the
    (faked) dense retriever. BM25 catches each; RRF promotes them into top-k.
    Assert hybrid recall@3 strictly beats dense-only recall@3.
    """
    bm25 = build_bm25()
    k = 3

    # Faked dense rankings deliberately push the lexical golden chunk out of
    # the top-k (simulating embedding under-weighting of exact terms).
    cases = [
        # query, dense ranking (golden chunk missing/last), golden id
        ("checkout payment gateway revenue", ["c3", "c6", "c2", "c7"], "c1"),
        ("redis cache miss rate",            ["c2", "c6", "c3"],       "c7"),
        ("disk iops write throughput",       ["c6", "c2", "c3"],       "c5"),
    ]

    dense_recalls: list[float] = []
    hybrid_recalls: list[float] = []

    for query, dense_ranking, golden_id in cases:
        dense = FakeDensePipeline({query: dense_ranking})
        hybrid = HybridRetrievalPipeline(dense, bm25)

        dense_only = dense.search(query=query, top_k=k)
        hybrid_out = hybrid.search(query=query, top_k=k)

        golden = {golden_id}
        dense_recalls.append(_recall_at_k(dense_only, golden, k))
        hybrid_recalls.append(_recall_at_k(hybrid_out, golden, k))

    dense_avg = sum(dense_recalls) / len(dense_recalls)
    hybrid_avg = sum(hybrid_recalls) / len(hybrid_recalls)

    # Print the measured comparison (visible with `pytest -s`).
    print(f"\n[recall@{k}] dense-only={dense_avg:.3f}  hybrid={hybrid_avg:.3f}  "
          f"improvement=+{(hybrid_avg - dense_avg):.3f}")

    assert dense_avg == 0.0, "test premise: dense misses every golden chunk"
    assert hybrid_avg == 1.0, "hybrid should recover every golden chunk"
    assert hybrid_avg > dense_avg
