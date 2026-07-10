"""
aeam/agents/rag/retrieval_debug.py

Retrieval observability / explainability (developer-only debug tracing).

Given a free-text query, replays the SAME production retrieval components —
query expansion, dense (Qdrant) search, BM25 search, RRF fusion,
cross-encoder reranking, evidence diversity — stage by stage, using the real
objects wired in ``main.py``, and records per-stage timing plus full
per-chunk provenance so a developer can see exactly why a chunk survived (or
was dropped by) each stage.

This module does NOT modify or reimplement the retrieval pipeline. It only
ORCHESTRATES calls to the already-frozen production components:
- :class:`~aeam.agents.rag.retrieval_pipeline.RetrievalPipeline` (dense)
- :class:`~aeam.agents.rag.hybrid_retrieval.BM25Index` (lexical)
- :class:`~aeam.agents.rag.hybrid_retrieval.HybridRetrievalPipeline` (dense+BM25+RRF)
- :class:`~aeam.agents.rag.multi_query_retrieval.MultiQueryRetrievalPipeline` (expansion+cross-query RRF)
- :class:`~aeam.agents.rag.reranker.CrossEncoderReranker` (rerank)
- :class:`~aeam.agents.rag.evidence_diversity.EvidenceDiversityFilter` (dedup/diversity)

Query expansion is LLM-backed and therefore non-deterministic across
independent calls. If each stage re-triggered its own expansion, different
stages of one trace could silently use different query variants. This module
solves that with :class:`_CachingExpanderWrapper` — a per-``trace()``-call,
throwaway wrapper that memoizes ``expand()`` so exactly one real LLM call
happens per trace, and every downstream stage sees the identical variant
list. It wraps the SAME shared production ``QueryExpansionAgent`` instance
(never mutates it, never monkey-patches its methods) so concurrent debug
traces and live traffic cannot interfere with each other.
"""

from __future__ import annotations

import time
from typing import Any

from aeam.agents.rag.evidence_diversity import (
    DEFAULT_CANDIDATE_MULTIPLIER as _DIVERSITY_MULTIPLIER,
    DEFAULT_MIN_CANDIDATES as _DIVERSITY_MIN_CANDIDATES,
)
from aeam.agents.rag.multi_query_retrieval import MultiQueryRetrievalPipeline
from aeam.monitoring.logging_config import get_logger

logger = get_logger(__name__, agent="rag")

DEFAULT_RAW_CANDIDATE_POOL: int = 20

# The per-chunk fields every stage list in the debug response exposes,
# regardless of which upstream fields a given chunk happens to carry.
_SUMMARY_FIELDS: tuple[str, ...] = (
    "chunk_id", "source", "text_preview", "similarity",
    "dense_similarity", "bm25_score", "hybrid_rrf_score", "rrf_score",
    "rerank_score", "originating_query", "query_index", "query_text", "final_rank",
)


class _CachingExpanderWrapper:
    """
    Per-``trace()``-call memoizing wrapper around a real ``QueryExpansionAgent``.

    Duck-types the one method :class:`~aeam.agents.rag.multi_query_retrieval.MultiQueryRetrievalPipeline`
    calls (``expand``), so a throwaway ``MultiQueryRetrievalPipeline`` built
    around this wrapper behaves identically to the production one for a
    single query, while guaranteeing exactly one real LLM call — never
    mutates or monkey-patches the shared production expander.
    """

    def __init__(self, real_expander: Any) -> None:
        self._real = real_expander
        self._cache: dict[str, list[str]] = {}
        self.call_count: int = 0

    def expand(self, query: str) -> list[str]:
        if query not in self._cache:
            self.call_count += 1
            self._cache[query] = self._real.expand(query)
        return self._cache[query]


def summarize_chunk(item: dict[str, Any]) -> dict[str, Any]:
    """
    Flatten a retrieval-stage chunk dict to the canonical debug-response shape.

    Always returns the same key set (missing values are ``None``), so every
    stage list in the API response has a predictable, uniform shape. Never
    mutates ``item``; only reads from it. ``source`` is promoted out of
    ``metadata`` for direct accessibility.

    Args:
        item: A chunk dict from any retrieval stage.

    Returns:
        Flat dict with exactly the keys in :data:`_SUMMARY_FIELDS`.
    """
    metadata = item.get("metadata") or {}
    text = str(item.get("text", "") or "")
    return {
        "chunk_id": item.get("chunk_id"),
        "source": metadata.get("source"),
        "text_preview": text[:160],
        "similarity": item.get("similarity"),
        "dense_similarity": item.get("dense_similarity"),
        "bm25_score": item.get("bm25_score"),
        "hybrid_rrf_score": item.get("hybrid_rrf_score"),
        "rrf_score": item.get("rrf_score"),
        "rerank_score": item.get("rerank_score"),
        "originating_query": item.get("originating_query"),
        "query_index": item.get("query_index"),
        "query_text": item.get("query_text"),
        "final_rank": item.get("final_rank"),
    }


class RetrievalDebugTracer:
    """
    Stage-by-stage retrieval tracer built from the live, shared production components.

    Args:
        dense:          The live dense ``RetrievalPipeline`` (required).
        bm25_index:      The live ``BM25Index``, or ``None`` if hybrid retrieval is disabled.
        hybrid_stage:    The pipeline object production uses as the input to
                        multi-query/reranking — either the live
                        ``HybridRetrievalPipeline``, or ``dense`` itself if
                        hybrid retrieval is disabled. This is a real,
                        shared production object; the tracer never mutates it.
        query_expander:  The live ``QueryExpansionAgent``, or ``None`` if
                        multi-query is disabled.
        reranker:        The live ``CrossEncoderReranker``, or ``None`` if
                        reranking is disabled.
        diversity_filter: The live ``EvidenceDiversityFilter``, or ``None``
                        if diversity filtering is disabled.
        rerank_top_n:    Same value production's ``RerankingRetrievalPipeline``
                        uses (``settings.RAG_RERANK_TOP_N``) — reused here so
                        the fused-candidate pool size matches production exactly.
        raw_pool_size:   Candidate count for the informational raw
                        dense-only / BM25-only comparison sections (does not
                        affect ``final_chunks``).

    Raises:
        ValueError: If ``dense`` is ``None``.
    """

    def __init__(
        self,
        dense: Any,
        bm25_index: Any | None,
        hybrid_stage: Any,
        query_expander: Any | None,
        reranker: Any | None,
        diversity_filter: Any | None,
        rerank_top_n: int,
        raw_pool_size: int = DEFAULT_RAW_CANDIDATE_POOL,
    ) -> None:
        if dense is None:
            raise ValueError("dense must not be None.")
        if hybrid_stage is None:
            raise ValueError("hybrid_stage must not be None.")
        self._dense = dense
        self._bm25 = bm25_index
        self._hybrid_stage = hybrid_stage
        self._expander = query_expander
        self._reranker = reranker
        self._diversity = diversity_filter
        self._rerank_top_n = int(rerank_top_n)
        self._raw_pool_size = max(1, int(raw_pool_size))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def trace(self, query: str, top_k: int = 5) -> dict[str, Any]:
        """
        Trace retrieval for ``query`` and return a full stage-by-stage report.

        Returns:
            Dict with keys:

            - ``query``            — the input query (stripped).
            - ``expanded_queries`` — ``[{"query_index", "query_text"}, ...]``.
            - ``dense_results``    — raw dense-only hits (informational).
            - ``bm25_results``     — raw BM25-only hits (informational).
            - ``rrf_fused``        — the fused candidate pool feeding the
              reranker (via the REAL hybrid/multi-query pipeline).
            - ``reranked``         — the reranked candidate pool feeding
              diversity filtering (via the REAL cross-encoder).
            - ``final_chunks``     — exactly what ``RAGAgent`` receives
              (via the REAL diversity filter), each with ``final_rank`` set.
            - ``timings_ms``       — per-stage wall-clock milliseconds.
            - ``stage_survival``   — per-``chunk_id`` explanation of which
              stages it appeared in and why it was dropped, if it was.

            Every chunk in every stage list is normalised via
            :func:`summarize_chunk`.

        Raises:
            ValueError: If ``query`` is empty/whitespace or ``top_k`` < 1.
        """
        if not query or not query.strip():
            raise ValueError("query must be a non-empty string.")
        if top_k < 1:
            raise ValueError(f"top_k must be >= 1. Got: {top_k}.")

        query = query.strip()
        timings: dict[str, float] = {}
        t_total = time.perf_counter()

        # --- Stage 1: query expansion (exactly one real LLM call, cached
        # for the rest of this trace). ---
        t0 = time.perf_counter()
        cached_expander = _CachingExpanderWrapper(self._expander) if self._expander else None
        expanded = cached_expander.expand(query) if cached_expander else [query]
        timings["query_expansion_ms"] = _elapsed_ms(t0)

        # --- Stage 2: raw dense (informational — real object, real call). ---
        t0 = time.perf_counter()
        dense_results = self._dense.search(query=query, top_k=self._raw_pool_size)
        for r in dense_results:
            r.setdefault("dense_similarity", r.get("similarity"))
        timings["embedding_search_ms"] = _elapsed_ms(t0)

        # --- Stage 3: raw BM25 (informational — real object, real call). ---
        t0 = time.perf_counter()
        bm25_results = (
            self._bm25.search(query=query, top_k=self._raw_pool_size) if self._bm25 else []
        )
        timings["bm25_search_ms"] = _elapsed_ms(t0)

        # Candidate-pool sizes mirroring production's cascading defaults,
        # computed backward from the caller's requested top_k so the fused/
        # reranked pools are exactly what production would have used.
        pool_before_diversity = (
            max(top_k * _DIVERSITY_MULTIPLIER, _DIVERSITY_MIN_CANDIDATES)
            if self._diversity is not None else top_k
        )
        pool_before_rerank = (
            max(self._rerank_top_n, pool_before_diversity)
            if self._reranker is not None else pool_before_diversity
        )

        # --- Stage 4: RRF fusion — via the REAL hybrid/multi-query pipeline. ---
        t0 = time.perf_counter()
        if cached_expander is not None:
            # Fresh, throwaway wrapper instance around the SAME shared
            # hybrid_stage — identical behaviour to production's
            # MultiQueryRetrievalPipeline, scoped to this one trace so the
            # expansion cache never leaks across requests.
            mq_pipeline = MultiQueryRetrievalPipeline(self._hybrid_stage, cached_expander)
            rrf_fused = mq_pipeline.search(query=query, top_k=pool_before_rerank)
        else:
            rrf_fused = self._hybrid_stage.search(query=query, top_k=pool_before_rerank)
        timings["rrf_fusion_ms"] = _elapsed_ms(t0)

        # --- Stage 5: cross-encoder reranking — via the REAL reranker. ---
        t0 = time.perf_counter()
        if self._reranker is not None and rrf_fused:
            reranked = self._reranker.rerank(query, rrf_fused, top_k=pool_before_diversity)
        else:
            reranked = list(rrf_fused[:pool_before_diversity])
        timings["reranking_ms"] = _elapsed_ms(t0)

        # --- Stage 6: evidence diversity — via the REAL filter. ---
        t0 = time.perf_counter()
        if self._diversity is not None and reranked:
            final_chunks = self._diversity.filter(reranked, top_k=top_k)
        else:
            final_chunks = list(reranked[:top_k])
        timings["diversity_ms"] = _elapsed_ms(t0)

        for rank, item in enumerate(final_chunks, start=1):
            item["final_rank"] = rank

        timings["total_retrieval_latency_ms"] = _elapsed_ms(t_total)

        final_summarized = [summarize_chunk(r) for r in final_chunks]

        return {
            "query": query,
            "expanded_queries": [
                {"query_index": i, "query_text": q} for i, q in enumerate(expanded)
            ],
            "dense_results": [summarize_chunk(r) for r in dense_results],
            "bm25_results": [summarize_chunk(r) for r in bm25_results],
            "rrf_fused": [summarize_chunk(r) for r in rrf_fused],
            "reranked": [summarize_chunk(r) for r in reranked],
            # In the current frozen architecture, EvidenceDiversityFilter.filter()
            # performs diversity filtering AND the final Top-K selection in one
            # call (there is no separate post-diversity trimming stage) — so
            # "evidence_diversity_output" and "final_chunks" are the identical
            # list, exposed under both names for explicit stage naming.
            "evidence_diversity_output": final_summarized,
            "final_chunks": final_summarized,
            "timings_ms": timings,
            "stage_survival": _build_survival(
                dense_results, bm25_results, rrf_fused, reranked, final_chunks,
            ),
        }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _elapsed_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 3)


def _build_survival(
    dense_results: list[dict[str, Any]],
    bm25_results: list[dict[str, Any]],
    rrf_fused: list[dict[str, Any]],
    reranked: list[dict[str, Any]],
    final_chunks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Build a per-``chunk_id`` explanation of which stages it survived.

    Args:
        dense_results, bm25_results, rrf_fused, reranked, final_chunks:
            The raw (non-summarized) stage output lists from :meth:`RetrievalDebugTracer.trace`.

    Returns:
        List of ``{"chunk_id", "in_dense", "in_bm25", "in_rrf_fused",
        "in_reranked", "in_final", "explanation"}`` dicts, one per distinct
        ``chunk_id`` seen anywhere in the raw dense/BM25/fused stages.
    """
    dense_ids = {r.get("chunk_id") for r in dense_results}
    bm25_ids = {r.get("chunk_id") for r in bm25_results}
    fused_ids = {r.get("chunk_id") for r in rrf_fused}
    reranked_ids = {r.get("chunk_id") for r in reranked}
    final_ids = {r.get("chunk_id") for r in final_chunks}

    all_ids = dense_ids | bm25_ids | fused_ids
    explanations: list[dict[str, Any]] = []
    for cid in sorted(all_ids, key=lambda x: (x is None, str(x))):
        in_dense = cid in dense_ids
        in_bm25 = cid in bm25_ids
        in_fused = cid in fused_ids
        in_reranked = cid in reranked_ids
        in_final = cid in final_ids

        if not in_fused:
            removed_at_stage = "fusion"
            reason = (
                "removed during fusion — retrieved by dense/BM25 but outside "
                "the candidate pool fused for reranking (candidate-pool truncation)."
            )
        elif not in_reranked:
            removed_at_stage = "reranker"
            reason = (
                "removed by reranker — reached RRF fusion but was not kept in "
                "the cross-encoder's reranked pool."
            )
        elif not in_final:
            removed_at_stage = "evidence_diversity"
            reason = (
                "removed by evidence diversity — survived reranking but was "
                "dropped as a near-duplicate of a higher-ranked chunk, or for "
                "exceeding the per-document/section cap."
            )
        else:
            removed_at_stage = None
            reason = "survived every stage — present in the final chunks returned to RAGAgent."

        explanations.append({
            "chunk_id": cid,
            "in_dense": in_dense,
            "in_bm25": in_bm25,
            "in_rrf_fused": in_fused,
            "in_reranked": in_reranked,
            "in_final": in_final,
            "removed_at_stage": removed_at_stage,
            "explanation": reason,
        })
    return explanations
