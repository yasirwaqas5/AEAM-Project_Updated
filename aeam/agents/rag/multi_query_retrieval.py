"""
aeam/agents/rag/multi_query_retrieval.py

Multi-Query Retrieval for the AEAM RAG system (Phase 7.3).

Expands the caller's query into several diverse variants (via
:class:`~aeam.agents.rag.query_expansion.QueryExpansionAgent`), retrieves each
through the existing hybrid pipeline unchanged, merges/deduplicates the
per-query candidate lists by ``chunk_id`` using Reciprocal Rank Fusion (the
same :func:`~aeam.agents.rag.hybrid_retrieval.reciprocal_rank_fusion` Phase 7.1
already introduced — reused, not duplicated), and attaches query-provenance
metadata.

Design constraints (Phase 7.3):
- Composition only: the inner pipeline (hybrid) is used unchanged, via its
  existing ``search(query, filter_criteria, top_k)`` contract.
- The public ``search(query, filter_criteria, top_k)`` contract and the
  ``similarity_threshold`` / ``collection`` attributes are preserved, so this
  is a drop-in inner pipeline for :class:`~aeam.agents.rag.reranker.RerankingRetrievalPipeline`,
  exactly like the hybrid pipeline was a drop-in for the dense one.
- Every existing evidence key is preserved (``chunk_id`` / ``text`` /
  ``metadata`` / ``similarity`` and the hybrid stage's own RRF provenance,
  relabelled — see below — so citations, chunk IDs and validation grounding
  are unaffected).
- Merging is fully deduplicated by ``chunk_id`` — a chunk retrieved under two
  query variants appears once, with combined provenance.
"""

from __future__ import annotations

from typing import Any

from aeam.agents.rag.hybrid_retrieval import DEFAULT_RRF_K, reciprocal_rank_fusion
from aeam.agents.rag.query_expansion import QueryExpansionAgent
from aeam.monitoring.logging_config import get_logger

logger = get_logger(__name__, agent="rag")

DEFAULT_CANDIDATE_MULTIPLIER: int = 3
DEFAULT_MIN_CANDIDATES: int = 15


class MultiQueryRetrievalPipeline:
    """
    Query-expansion + per-query hybrid retrieval + cross-query RRF fusion.

    A drop-in inner pipeline: exposes the same ``search`` contract and
    ``similarity_threshold`` / ``collection`` attributes as
    :class:`~aeam.agents.rag.hybrid_retrieval.HybridRetrievalPipeline`, so it
    slots directly between hybrid retrieval and the cross-encoder reranker.

    Args:
        inner_pipeline:       The existing retrieval pipeline each expanded
                              query is run through (typically
                              ``HybridRetrievalPipeline``, used unchanged).
        query_expansion_agent: Configured :class:`QueryExpansionAgent`.
        rrf_k:                RRF constant for the cross-query fusion pass.
        candidate_multiplier: Each expanded query fetches ``max(top_k *
                              candidate_multiplier, min_candidates)``
                              candidates from the inner pipeline before fusion.
        min_candidates:       Lower bound on the per-query candidate pool.
    """

    def __init__(
        self,
        inner_pipeline: Any,
        query_expansion_agent: QueryExpansionAgent,
        rrf_k: int = DEFAULT_RRF_K,
        candidate_multiplier: int = DEFAULT_CANDIDATE_MULTIPLIER,
        min_candidates: int = DEFAULT_MIN_CANDIDATES,
    ) -> None:
        if inner_pipeline is None:
            raise ValueError("inner_pipeline must not be None.")
        if query_expansion_agent is None:
            raise ValueError("query_expansion_agent must not be None.")
        self._inner = inner_pipeline
        self._expander = query_expansion_agent
        self._rrf_k = int(rrf_k)
        self._candidate_multiplier = max(1, int(candidate_multiplier))
        self._min_candidates = max(1, int(min_candidates))

    # ------------------------------------------------------------------
    # Public API — mirrors RetrievalPipeline.search
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        filter_criteria: dict[str, Any] | None = None,
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        """
        Expand ``query``, retrieve each variant, fuse, and return the top-k.

        Args:
            query:           Original query. Must be non-empty (preserved as
                             the first expanded query — requirement #2).
            filter_criteria: Forwarded unchanged to every per-query inner search.
            top_k:           Final number of fused results to return (>= 1).

        Returns:
            Fused results (best first), deduplicated by ``chunk_id``. Each
            preserves ``chunk_id`` / ``text`` / ``metadata`` / ``similarity``
            plus the hybrid stage's own provenance (relabelled
            ``hybrid_rrf_score`` / ``hybrid_retrieval_sources`` so it is never
            overwritten by this stage's cross-query fusion), and adds:

            - ``rrf_score``          — cross-query fusion score (best now = this stage's).
            - ``retrieval_sources``  — which query indices (``"q0"``, ``"q1"``, ...) matched.
            - ``query_matches``      — list of ``{query_index, query_text, rank}``
                                       for every contributing query variant.
            - ``originating_query``  — the ORIGINAL user/investigation query (requirement #2).
            - ``query_index``        — index of the best-ranked contributing variant.
            - ``query_text``         — that variant's exact text.

        Raises:
            ValueError: If ``query`` is empty/whitespace or ``top_k`` < 1.
        """
        if not query or not query.strip():
            raise ValueError("query must be a non-empty string.")
        if top_k < 1:
            raise ValueError(f"top_k must be >= 1. Got: {top_k}.")

        original = query.strip()
        queries = self._expander.expand(original)

        candidate_k = max(top_k * self._candidate_multiplier, self._min_candidates)

        per_query_lists: list[list[dict[str, Any]]] = []
        for q in queries:
            results = self._inner.search(query=q, filter_criteria=filter_criteria, top_k=candidate_k)
            # Relabel the hybrid stage's own RRF provenance so this stage's
            # cross-query fusion (which also writes rrf_score/retrieval_sources)
            # never silently overwrites it.
            relabeled = []
            for r in results:
                item = dict(r)
                if "rrf_score" in item:
                    item["hybrid_rrf_score"] = item.pop("rrf_score")
                if "retrieval_sources" in item:
                    item["hybrid_retrieval_sources"] = item.pop("retrieval_sources")
                relabeled.append(item)
            per_query_lists.append(relabeled)

        source_names = [f"q{i}" for i in range(len(queries))]
        fused = reciprocal_rank_fusion(per_query_lists, k=self._rrf_k, source_names=source_names)

        top = [
            self._attach_provenance(entry, original, queries, source_names)
            for entry in fused[:top_k]
        ]

        logger.info(
            "MultiQueryRetrievalPipeline.search | queries=%d | fused=%d | "
            "returned=%d | original=%r",
            len(queries), len(fused), len(top), original,
        )
        return top

    # ------------------------------------------------------------------
    # Drop-in compatibility surface (read by RAGAgent / reporting)
    # ------------------------------------------------------------------

    @property
    def similarity_threshold(self) -> float:
        return getattr(self._inner, "similarity_threshold", 0.0)

    @property
    def collection(self) -> str:
        return getattr(self._inner, "collection", "")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _attach_provenance(
        entry: dict[str, Any],
        original_query: str,
        queries: list[str],
        source_names: list[str],
    ) -> dict[str, Any]:
        """
        Derive requirement #13's provenance fields from RRF's per-source ranks.

        ``reciprocal_rank_fusion`` already records ``retrieval_sources``
        (which ``q{i}`` labels matched) and ``q{i}_rank`` (that variant's
        1-based rank) on every fused entry. This walks those to build:
        - ``query_matches``: full per-variant match list, sorted by rank.
        - the single best (lowest-rank) match's index/text, exposed as the
          flat ``query_index`` / ``query_text`` fields.
        - ``originating_query``: the original (unexpanded) query, constant.
        """
        matches: list[dict[str, Any]] = []
        for src in entry.get("retrieval_sources", []):
            rank = entry.get(f"{src}_rank")
            if rank is None:
                continue
            idx = source_names.index(src)
            matches.append({
                "query_index": idx,
                "query_text": queries[idx],
                "rank": rank,
            })
        matches.sort(key=lambda m: m["rank"])

        entry["query_matches"] = matches
        entry["originating_query"] = original_query
        if matches:
            entry["query_index"] = matches[0]["query_index"]
            entry["query_text"] = matches[0]["query_text"]
        else:
            # Defensive default — every fused entry originates from >=1 query
            # in normal operation, but never leave these keys missing.
            entry["query_index"] = 0
            entry["query_text"] = original_query

        return entry

    def __repr__(self) -> str:
        return (
            f"MultiQueryRetrievalPipeline(inner={self._inner!r}, "
            f"expander={self._expander!r})"
        )
