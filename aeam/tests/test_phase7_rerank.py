"""
aeam/tests/test_phase7_rerank.py

Phase 7.2 tests — Cross-encoder reranking after hybrid retrieval.

Self-contained: the cross-encoder is stubbed with a deterministic model, so
these tests need no model download, no Qdrant, and no network. They verify:
- reranker reorders by score and preserves the evidence schema (+rerank_score),
- chunk IDs / citations are unchanged,
- the RerankingRetrievalPipeline drop-in contract (fetch top_n → return top_k,
  delegated attributes),
- graceful fallback on init failure (RuntimeError) and query-time failure,
- a measured quality improvement (Recall@K, MRR, nDCG) of reranked over the
  pre-rerank (hybrid) order.
"""

from __future__ import annotations

import math
from unittest.mock import patch

import pytest

from aeam.agents.rag.reranker import CrossEncoderReranker, RerankingRetrievalPipeline


# ---------------------------------------------------------------------------
# Deterministic stub cross-encoder + fake inner pipeline
# ---------------------------------------------------------------------------

class StubCrossEncoder:
    """Deterministic stand-in for sentence_transformers.CrossEncoder.

    Scores each [query, passage] pair from a caller-supplied map keyed on a
    substring found in the passage. Mimics a cross-encoder returning higher
    scores for more relevant passages.
    """

    def __init__(self, score_map: dict[str, float]):
        self._score_map = score_map

    def predict(self, pairs):
        scores = []
        for _query, passage in pairs:
            s = 0.0
            for key, val in self._score_map.items():
                if key in passage:
                    s = val
                    break
            scores.append(s)
        return scores


class FailingCrossEncoder:
    def predict(self, pairs):
        raise RuntimeError("model exploded at predict time")


class FakeInner:
    """Fake hybrid/dense pipeline returning a fixed candidate list (in order)."""

    def __init__(self, candidates: list[dict], collection="aeam_documents"):
        self._candidates = candidates
        self._collection = collection
        self.similarity_threshold = 0.5
        self.last_top_k = None

    @property
    def collection(self):
        return self._collection

    def search(self, query, filter_criteria=None, top_k=5):
        self.last_top_k = top_k
        return [dict(c) for c in self._candidates[:top_k]]


def _chunk(cid, text, sim=0.7):
    return {
        "chunk_id": cid,
        "text": text,
        "metadata": {"source": f"src_{cid}"},
        "similarity": sim,
        "rrf_score": 0.01,
        "retrieval_sources": ["dense", "bm25"],
    }


# ---------------------------------------------------------------------------
# CrossEncoderReranker
# ---------------------------------------------------------------------------

def test_reranker_reorders_by_score():
    cands = [
        _chunk("c1", "disk iops saturation"),
        _chunk("c2", "redis cache eviction miss rate"),
        _chunk("c3", "cpu runaway process"),
    ]
    stub = StubCrossEncoder({"redis": 9.0, "cpu": 3.0, "disk": 1.0})
    r = CrossEncoderReranker(model=stub)
    out = r.rerank("redis cache problem", cands, top_k=3)
    assert [c["chunk_id"] for c in out] == ["c2", "c3", "c1"]
    assert out[0]["rerank_score"] == 9.0


def test_reranker_preserves_schema_and_adds_score():
    cands = [_chunk("c1", "redis cache eviction")]
    r = CrossEncoderReranker(model=StubCrossEncoder({"redis": 5.0}))
    out = r.rerank("redis", cands, top_k=1)
    c = out[0]
    # every existing key preserved (citations/validation/evidence depend on these)
    assert c["chunk_id"] == "c1"
    assert c["text"] == "redis cache eviction"
    assert c["metadata"] == {"source": "src_c1"}
    assert c["similarity"] == 0.7
    assert c["rrf_score"] == 0.01
    assert c["retrieval_sources"] == ["dense", "bm25"]
    # rerank score added
    assert c["rerank_score"] == 5.0


def test_reranker_truncates_to_top_k():
    cands = [_chunk(f"c{i}", f"text {i}") for i in range(10)]
    r = CrossEncoderReranker(model=StubCrossEncoder({"text": 1.0}))
    assert len(r.rerank("q", cands, top_k=3)) == 3


def test_reranker_empty_candidates():
    r = CrossEncoderReranker(model=StubCrossEncoder({}))
    assert r.rerank("q", [], top_k=5) == []


def test_reranker_init_failure_raises_runtimeerror():
    # Simulate the real CrossEncoder failing to load (offline / bad model id).
    with patch("sentence_transformers.CrossEncoder", side_effect=OSError("no such model")):
        with pytest.raises(RuntimeError):
            CrossEncoderReranker(model_name="bogus/model")


# ---------------------------------------------------------------------------
# RerankingRetrievalPipeline — drop-in contract
# ---------------------------------------------------------------------------

def test_pipeline_is_drop_in():
    inner = FakeInner([_chunk("c1", "x")])
    r = CrossEncoderReranker(model=StubCrossEncoder({"x": 1.0}))
    pipe = RerankingRetrievalPipeline(inner, r, rerank_top_n=20)
    assert pipe.similarity_threshold == 0.5
    assert pipe.collection == "aeam_documents"
    out = pipe.search(query="q", top_k=1)
    assert isinstance(out, list)


def test_pipeline_fetches_top_n_then_returns_top_k():
    cands = [_chunk(f"c{i}", f"redis {i}" if i == 7 else f"other {i}") for i in range(30)]
    inner = FakeInner(cands)
    r = CrossEncoderReranker(model=StubCrossEncoder({"redis": 9.0, "other": 1.0}))
    pipe = RerankingRetrievalPipeline(inner, r, rerank_top_n=20)
    out = pipe.search(query="redis", top_k=5)
    # inner was asked for the candidate pool (>= rerank_top_n), not just top_k
    assert inner.last_top_k == 20
    assert len(out) == 5
    # the single 'redis' chunk (c7) is promoted to the top by reranking
    assert out[0]["chunk_id"] == "c7"


def test_pipeline_empty_query_raises():
    inner = FakeInner([_chunk("c1", "x")])
    pipe = RerankingRetrievalPipeline(inner, CrossEncoderReranker(model=StubCrossEncoder({})))
    for bad in ("", "  "):
        with pytest.raises(ValueError):
            pipe.search(query=bad, top_k=3)


def test_pipeline_no_candidates_returns_empty():
    inner = FakeInner([])
    pipe = RerankingRetrievalPipeline(inner, CrossEncoderReranker(model=StubCrossEncoder({})))
    assert pipe.search(query="q", top_k=5) == []


def test_pipeline_query_time_fallback_to_hybrid_order():
    # Reranker throws at predict time -> pipeline returns the inner order, unbroken.
    cands = [_chunk("c1", "a"), _chunk("c2", "b"), _chunk("c3", "c")]
    inner = FakeInner(cands)
    pipe = RerankingRetrievalPipeline(inner, CrossEncoderReranker(model=FailingCrossEncoder()))
    out = pipe.search(query="q", top_k=2)
    assert [c["chunk_id"] for c in out] == ["c1", "c2"]   # inner (hybrid) order preserved
    # chunk_ids intact, no rerank_score forced on a failed rerank
    assert all("chunk_id" in c for c in out)


# ---------------------------------------------------------------------------
# Measured quality: hybrid order vs reranked order (Recall@K, MRR, nDCG)
# ---------------------------------------------------------------------------

def _recall_at_k(ranked_ids, relevant, k):
    return len(set(ranked_ids[:k]) & relevant) / len(relevant)

def _mrr(ranked_ids, relevant):
    for i, cid in enumerate(ranked_ids, start=1):
        if cid in relevant:
            return 1.0 / i
    return 0.0

def _dcg(ranked_ids, relevant, k):
    return sum((1.0 if cid in relevant else 0.0) / math.log2(i + 1)
               for i, cid in enumerate(ranked_ids[:k], start=1))

def _ndcg_at_k(ranked_ids, relevant, k):
    ideal = sum(1.0 / math.log2(i + 1) for i in range(1, min(len(relevant), k) + 1))
    return (_dcg(ranked_ids, relevant, k) / ideal) if ideal > 0 else 0.0


def test_reranking_improves_recall_mrr_ndcg():
    """
    Three queries where the fused (hybrid) order buries the relevant chunk.
    The (stubbed, deterministic) cross-encoder scores the relevant chunk
    highest, promoting it. Assert reranked Recall@3 / MRR / nDCG beat hybrid.

    Validates the reranking mechanism + metric plumbing; the real-model measure
    is in the live verification script.
    """
    k = 3

    # Each case: (query, hybrid-ordered candidates [relevant is last], relevant id,
    #             cross-encoder score_map that rewards the relevant passage)
    cases = [
        ("redis cache miss",
         [_chunk("d1", "disk saturation"), _chunk("d2", "cpu spike"),
          _chunk("d3", "memory pressure"), _chunk("rel", "redis cache eviction miss rate")],
         "rel", {"redis": 9.0}),
        ("payment checkout failure",
         [_chunk("d4", "email bounce"), _chunk("d5", "dns error"),
          _chunk("d6", "disk io"), _chunk("rel", "payment gateway checkout timeout")],
         "rel", {"payment": 9.0}),
        ("oom kill memory",
         [_chunk("d7", "cache warmup"), _chunk("d8", "tls handshake"),
          _chunk("d9", "queue backlog"), _chunk("rel", "memory pressure oom kill")],
         "rel", {"memory": 9.0}),
    ]

    hybrid_recall = hybrid_mrr = hybrid_ndcg = 0.0
    rerank_recall = rerank_mrr = rerank_ndcg = 0.0

    for query, cands, rel_id, score_map in cases:
        relevant = {rel_id}
        inner = FakeInner(cands)
        reranker = CrossEncoderReranker(model=StubCrossEncoder({**score_map, }))
        pipe = RerankingRetrievalPipeline(inner, reranker, rerank_top_n=20)

        hybrid_ids = [c["chunk_id"] for c in inner.search(query=query, top_k=len(cands))]
        rerank_ids = [c["chunk_id"] for c in pipe.search(query=query, top_k=len(cands))]

        hybrid_recall += _recall_at_k(hybrid_ids, relevant, k)
        hybrid_mrr += _mrr(hybrid_ids, relevant)
        hybrid_ndcg += _ndcg_at_k(hybrid_ids, relevant, k)

        rerank_recall += _recall_at_k(rerank_ids, relevant, k)
        rerank_mrr += _mrr(rerank_ids, relevant)
        rerank_ndcg += _ndcg_at_k(rerank_ids, relevant, k)

    n = len(cases)
    hybrid = (hybrid_recall / n, hybrid_mrr / n, hybrid_ndcg / n)
    rerank = (rerank_recall / n, rerank_mrr / n, rerank_ndcg / n)

    print(f"\n[hybrid ] Recall@{k}={hybrid[0]:.3f} MRR={hybrid[1]:.3f} nDCG@{k}={hybrid[2]:.3f}")
    print(f"[rerank ] Recall@{k}={rerank[0]:.3f} MRR={rerank[1]:.3f} nDCG@{k}={rerank[2]:.3f}")

    # Recall@3: relevant chunk was at rank 4 (missed) -> now rank 1 (hit).
    assert hybrid[0] == 0.0 and rerank[0] == 1.0
    # MRR and nDCG strictly improve.
    assert rerank[1] > hybrid[1]
    assert rerank[2] > hybrid[2]
