"""
aeam/tests/test_phase7_diversity.py

Phase 7.4 tests — Evidence Diversity Filter (post-rerank).

Self-contained pure-logic tests (no network/model needed — the filter
operates on plain dicts). They verify:
- Jaccard similarity math.
- Near-duplicate detection and removal (highest-ranked representative kept).
- Per-document cap ("prefer document diversity").
- Section-window cap ("prefer section diversity" via chunk_index proximity).
- Backfill: never returns fewer than top_k while non-duplicate candidates
  remain, even if that means relaxing document/section preferences.
- Drop-in pipeline contract, chunk_id/text/metadata/similarity preservation.
- Measured diversity (# distinct documents), duplicate reduction, and
  Recall@K / MRR / nDCG before vs after the filter.
"""

from __future__ import annotations

import math

import pytest

from aeam.agents.rag.evidence_diversity import (
    EvidenceDiversityFilter,
    EvidenceDiversityPipeline,
    jaccard_similarity,
)


def _chunk(cid, text, source="doc1", chunk_index=0, sim=0.9, rerank_score=None):
    d = {
        "chunk_id": cid,
        "text": text,
        "metadata": {"source": source, "chunk_index": chunk_index},
        "similarity": sim,
    }
    if rerank_score is not None:
        d["rerank_score"] = rerank_score
    return d


class FakeReranked:
    """Fake inner (reranker) pipeline returning a fixed, already-ranked list."""

    def __init__(self, results, collection="aeam_documents"):
        self._results = results
        self._collection = collection
        self.similarity_threshold = 0.5
        self.last_top_k = None

    @property
    def collection(self):
        return self._collection

    def search(self, query, filter_criteria=None, top_k=5):
        self.last_top_k = top_k
        return [dict(r) for r in self._results[:top_k]]


# ---------------------------------------------------------------------------
# jaccard_similarity
# ---------------------------------------------------------------------------

def test_jaccard_identical_sets():
    s = {"a", "b", "c"}
    assert jaccard_similarity(s, s) == 1.0

def test_jaccard_disjoint_sets():
    assert jaccard_similarity({"a"}, {"b"}) == 0.0

def test_jaccard_partial_overlap():
    assert jaccard_similarity({"a", "b"}, {"b", "c"}) == pytest.approx(1 / 3)

def test_jaccard_both_empty():
    assert jaccard_similarity(set(), set()) == 0.0


# ---------------------------------------------------------------------------
# EvidenceDiversityFilter — near-duplicate removal
# ---------------------------------------------------------------------------

def test_filter_removes_near_duplicate_keeps_highest_ranked():
    cands = [
        _chunk("c1", "database replication lag increased read query latency", source="d1"),
        _chunk("c2", "database replication lag increased read latency query", source="d2"),  # near-dup of c1
        _chunk("c3", "cpu saturation from a runaway process", source="d3"),
    ]
    f = EvidenceDiversityFilter(similarity_threshold=0.8, max_chunks_per_document=5)
    out = f.filter(cands, top_k=3)
    ids = [c["chunk_id"] for c in out]
    assert "c1" in ids            # highest-ranked representative kept (requirement #2)
    assert "c2" not in ids        # redundant duplicate removed (requirement #3)
    assert "c3" in ids


def test_filter_dissimilar_chunks_all_kept():
    cands = [
        _chunk("c1", "database replication lag", source="d1"),
        _chunk("c2", "cpu saturation runaway process", source="d2"),
        _chunk("c3", "memory exhaustion oom kill", source="d3"),
    ]
    f = EvidenceDiversityFilter(similarity_threshold=0.9, max_chunks_per_document=5)
    out = f.filter(cands, top_k=3)
    assert {c["chunk_id"] for c in out} == {"c1", "c2", "c3"}


def test_filter_threshold_boundary_respected():
    # Two chunks with exactly 50% token overlap.
    cands = [
        _chunk("c1", "alpha beta gamma delta", source="d1"),
        _chunk("c2", "alpha beta epsilon zeta", source="d2"),  # 2/6 = 0.333 overlap
    ]
    loose = EvidenceDiversityFilter(similarity_threshold=0.9, max_chunks_per_document=5)
    out = loose.filter(cands, top_k=2)
    assert len(out) == 2   # below threshold -> not a duplicate, both kept


# ---------------------------------------------------------------------------
# Document diversity preference
# ---------------------------------------------------------------------------

def test_filter_caps_chunks_per_document():
    cands = [
        _chunk("c1", "alpha topic one", source="docA", chunk_index=0),
        _chunk("c2", "beta topic two", source="docA", chunk_index=10),
        _chunk("c3", "gamma topic three", source="docA", chunk_index=20),
        _chunk("c4", "delta topic four", source="docB", chunk_index=0),
    ]
    f = EvidenceDiversityFilter(similarity_threshold=0.99, max_chunks_per_document=2)
    # top_k=3 (< total candidates) so the cap's effect is observable without
    # backfill being forced to reach into the capped document to hit top_k.
    out = f.filter(cands, top_k=3)
    doc_a_count = sum(1 for c in out if c["metadata"]["source"] == "docA")
    assert doc_a_count <= 2
    assert any(c["metadata"]["source"] == "docB" for c in out)   # doc diversity achieved
    assert "c3" not in {c["chunk_id"] for c in out}              # 3rd docA chunk excluded by the cap


def test_filter_backfills_when_document_cap_would_shrink_result():
    # Only one document exists -> cap alone would return < top_k; backfill kicks in.
    cands = [
        _chunk("c1", "alpha topic one", source="docA", chunk_index=0),
        _chunk("c2", "beta topic two", source="docA", chunk_index=10),
        _chunk("c3", "gamma topic three", source="docA", chunk_index=20),
    ]
    f = EvidenceDiversityFilter(similarity_threshold=0.99, max_chunks_per_document=1)
    out = f.filter(cands, top_k=3)
    assert len(out) == 3   # never short-changes the caller when candidates exist
    assert any(c["diversity_backfilled"] for c in out)


# ---------------------------------------------------------------------------
# Section diversity preference (chunk_index proximity)
# ---------------------------------------------------------------------------

def test_filter_avoids_neighboring_chunk_regions():
    cands = [
        _chunk("c1", "alpha topic one", source="docA", chunk_index=5),
        _chunk("c2", "beta topic two", source="docA", chunk_index=6),   # adjacent -> same "section"
        _chunk("c3", "gamma topic three", source="docA", chunk_index=40),  # far away -> different section
    ]
    f = EvidenceDiversityFilter(similarity_threshold=0.99, max_chunks_per_document=5)
    out = f.filter(cands, top_k=3)
    ids = {c["chunk_id"] for c in out if not c["diversity_backfilled"]}
    assert "c1" in ids
    assert "c2" not in ids   # excluded by section window in the strict pass
    assert "c3" in ids
    assert len(out) == 3     # backfill restores c2 so top_k is still satisfied


def test_filter_missing_chunk_index_fails_open():
    cands = [
        _chunk("c1", "alpha", source="docA"),
        {**_chunk("c2", "beta", source="docA"), "metadata": {"source": "docA"}},  # no chunk_index
    ]
    f = EvidenceDiversityFilter(similarity_threshold=0.99, max_chunks_per_document=5)
    out = f.filter(cands, top_k=2)
    assert len(out) == 2   # missing chunk_index never blocks inclusion


# ---------------------------------------------------------------------------
# Preserves chunk_id / text / metadata / similarity (citations)
# ---------------------------------------------------------------------------

def test_filter_preserves_all_existing_fields():
    cands = [_chunk("c1", "alpha beta gamma", source="d1", sim=0.77, rerank_score=4.2)]
    f = EvidenceDiversityFilter()
    out = f.filter(cands, top_k=1)
    c = out[0]
    assert c["chunk_id"] == "c1"
    assert c["text"] == "alpha beta gamma"
    assert c["metadata"] == {"source": "d1", "chunk_index": 0}
    assert c["similarity"] == 0.77
    assert c["rerank_score"] == 4.2


def test_filter_top_k_and_empty_input():
    f = EvidenceDiversityFilter()
    assert f.filter([], top_k=5) == []
    with pytest.raises(ValueError):
        f.filter([_chunk("c1", "x")], top_k=0)


def test_filter_invalid_construction_params():
    with pytest.raises(ValueError):
        EvidenceDiversityFilter(similarity_threshold=0.0)
    with pytest.raises(ValueError):
        EvidenceDiversityFilter(similarity_threshold=1.5)
    with pytest.raises(ValueError):
        EvidenceDiversityFilter(max_chunks_per_document=0)


# ---------------------------------------------------------------------------
# EvidenceDiversityPipeline — drop-in contract
# ---------------------------------------------------------------------------

def test_pipeline_is_drop_in():
    inner = FakeReranked([_chunk("c1", "x")])
    pipe = EvidenceDiversityPipeline(inner, EvidenceDiversityFilter())
    assert pipe.similarity_threshold == 0.5
    assert pipe.collection == "aeam_documents"
    out = pipe.search(query="q", top_k=1)
    assert isinstance(out, list)


def test_pipeline_requests_candidate_pool_larger_than_top_k():
    inner = FakeReranked([_chunk(f"c{i}", f"text {i}", source=f"d{i}") for i in range(20)])
    pipe = EvidenceDiversityPipeline(inner, EvidenceDiversityFilter(), candidate_multiplier=3, min_candidates=10)
    pipe.search(query="q", top_k=3)
    assert inner.last_top_k == max(3 * 3, 10)   # == 10


def test_pipeline_empty_query_raises():
    pipe = EvidenceDiversityPipeline(FakeReranked([]), EvidenceDiversityFilter())
    for bad in ("", "  "):
        with pytest.raises(ValueError):
            pipe.search(query=bad, top_k=3)


def test_pipeline_no_candidates_returns_empty():
    pipe = EvidenceDiversityPipeline(FakeReranked([]), EvidenceDiversityFilter())
    assert pipe.search(query="q", top_k=5) == []


# ---------------------------------------------------------------------------
# Measured: diversity, duplicate reduction, Recall@K / MRR / nDCG
# ---------------------------------------------------------------------------

def _recall_at_k(ids, rel, k): return len(set(ids[:k]) & rel) / len(rel)
def _mrr(ids, rel):
    for i, c in enumerate(ids, 1):
        if c in rel:
            return 1.0 / i
    return 0.0
def _ndcg_at_k(ids, rel, k):
    dcg = sum((1.0 if c in rel else 0.0) / math.log2(i + 1) for i, c in enumerate(ids[:k], 1))
    ideal = sum(1.0 / math.log2(i + 1) for i in range(1, min(len(rel), k) + 1))
    return dcg / ideal if ideal > 0 else 0.0


def test_diversity_reduces_duplicates_and_broadens_documents():
    """
    Scenario mirroring the stated problem: reranked Top-5 is dominated by
    near-duplicate/neighbouring chunks from ONE document, burying a
    genuinely relevant chunk from a different document at rank 6.

    Note: d1b..d1e are exact word-order permutations of d1a's 7 tokens, so
    their Jaccard similarity to d1a is 1.0 — real, unambiguous duplicates.
    With only one non-duplicate docA chunk (d1a) and one docB chunk (rel)
    in this 6-candidate pool, the filter correctly returns 2 results, not 5:
    requirement #3 ("remove redundant evidence") is a hard rule here — the
    filter never pads a shrunken result back up with duplicates just to hit
    top_k. (Document/section preferences DO backfill; true duplicates never do.)
    """
    k = 5
    reranked_order = [
        _chunk("d1a", "database replication lag increased read query latency", source="docA", chunk_index=3, sim=0.91),
        _chunk("d1b", "database replication lag increased read latency query", source="docA", chunk_index=4, sim=0.90),  # near-dup + neighbor of d1a
        _chunk("d1c", "replication lag increased read query latency database", source="docA", chunk_index=5, sim=0.89),  # near-dup + neighbor
        _chunk("d1d", "database replication increased latency read query lag", source="docA", chunk_index=6, sim=0.88),  # near-dup + neighbor
        _chunk("d1e", "lag replication database latency query read increased", source="docA", chunk_index=7, sim=0.87),  # near-dup + neighbor
        _chunk("rel", "connection pool exhaustion caused replication delay", source="docB", chunk_index=0, sim=0.60),   # relevant, different doc
    ]
    relevant = {"rel"}

    # BEFORE: raw reranked Top-5 (no diversity filter) — 5 near-duplicates, 1 document.
    before_ids = [c["chunk_id"] for c in reranked_order[:k]]
    before_docs = {c["metadata"]["source"] for c in reranked_order[:k]}
    duplicate_count_before = sum(1 for cid in before_ids if cid != "d1a")  # d1b..d1e

    # AFTER: diversity filter over the same reranked pool.
    inner = FakeReranked(reranked_order)
    f = EvidenceDiversityFilter(similarity_threshold=0.75, max_chunks_per_document=2)
    pipe = EvidenceDiversityPipeline(inner, f, candidate_multiplier=1, min_candidates=len(reranked_order))
    after = pipe.search(query="database replication issue", top_k=k)
    after_ids = [c["chunk_id"] for c in after]
    after_docs = {c["metadata"]["source"] for c in after}
    duplicate_count_after = sum(1 for cid in after_ids if cid != "d1a" and cid in {"d1b", "d1c", "d1d", "d1e"})

    print(f"\n[diversity] documents: before={len(before_docs)} after={len(after_docs)}")
    print(f"[diversity] duplicates in result: before={duplicate_count_before} after={duplicate_count_after}")
    print(f"[diversity] 'rel' chunk present: before={'rel' in before_ids} after={'rel' in after_ids}")
    print(f"[diversity] result size: before={len(before_ids)} after={len(after_ids)} "
          f"(smaller-but-clean is correct here — see docstring)")

    # Duplicate reduction.
    assert duplicate_count_before == 4
    assert duplicate_count_after == 0
    # Document diversity broadened.
    assert len(after_docs) > len(before_docs)
    assert "docB" in after_docs and "docB" not in before_docs
    # The buried relevant chunk is recovered once duplicates are cleared out.
    assert "rel" not in before_ids
    assert "rel" in after_ids

    before_metrics = (_recall_at_k(before_ids, relevant, k), _mrr(before_ids, relevant), _ndcg_at_k(before_ids, relevant, k))
    after_metrics = (_recall_at_k(after_ids, relevant, k), _mrr(after_ids, relevant), _ndcg_at_k(after_ids, relevant, k))
    print(f"[reranked-only] Recall@{k}={before_metrics[0]:.3f} MRR={before_metrics[1]:.3f} nDCG@{k}={before_metrics[2]:.3f}")
    print(f"[+diversity   ] Recall@{k}={after_metrics[0]:.3f} MRR={after_metrics[1]:.3f} nDCG@{k}={after_metrics[2]:.3f}")

    assert before_metrics[0] == 0.0
    assert after_metrics[0] == 1.0
    assert after_metrics[1] > before_metrics[1]
    assert after_metrics[2] > before_metrics[2]
