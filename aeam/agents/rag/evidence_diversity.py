"""
aeam/agents/rag/evidence_diversity.py

Evidence diversity filtering for the AEAM RAG system (Phase 7.4).

Runs AFTER cross-encoder reranking. Top-K reranked results often cluster
around a single document (or a single narrative section within a document),
because near-duplicate/overlapping chunks score similarly. This stage removes
redundant evidence and spreads the final Top-K across more documents/sections
— WITHOUT touching ranking, retrieval, or any earlier stage.

Design constraints (Phase 7.4):
- Operates ONLY on the already-reranked candidate list — no new retrieval.
- Composition only: the reranker (or whatever inner pipeline) is used
  unchanged via its existing ``search(query, filter_criteria, top_k)``
  contract. This stage is itself a drop-in with the same contract.
- Near-duplicate detection reuses the existing token-overlap machinery
  (:func:`aeam.agents.rag.hybrid_retrieval.tokenize`, Phase 7.1) — pure
  Python, no new dependency, no re-embedding round-trip.
- ``chunk_id`` / ``text`` / ``metadata`` / ``similarity`` and every field
  earlier stages attached are preserved untouched on every surviving result;
  this stage only SELECTS a subset (and reorders via backfill), it never
  mutates a chunk's content. Citations, chunk IDs, validator grounding, and
  confidence calculation are therefore unaffected.
- Document/section diversity are PREFERENCES (best-effort): if enforcing them
  would return fewer than ``top_k`` results while more candidates exist, a
  second pass backfills from rank order so the caller never gets short-changed.
  Near-duplicate removal is the one HARD rule (requirement #3: "remove
  redundant evidence") — duplicates are never backfilled.
"""

from __future__ import annotations

from typing import Any

from aeam.agents.rag.hybrid_retrieval import tokenize
from aeam.monitoring.logging_config import get_logger

logger = get_logger(__name__, agent="rag")

DEFAULT_SIMILARITY_THRESHOLD: float = 0.8
DEFAULT_MAX_CHUNKS_PER_DOCUMENT: int = 2
DEFAULT_SECTION_WINDOW: int = 1
DEFAULT_CANDIDATE_MULTIPLIER: int = 3
DEFAULT_MIN_CANDIDATES: int = 10


def jaccard_similarity(tokens_a: set[str], tokens_b: set[str]) -> float:
    """
    Jaccard similarity between two token sets: ``|A ∩ B| / |A ∪ B|``.

    Returns ``0.0`` if both sets are empty (no basis for comparison — treated
    as dissimilar, never falsely flagged as a duplicate).

    Args:
        tokens_a: First token set.
        tokens_b: Second token set.

    Returns:
        Float in ``[0.0, 1.0]``.
    """
    if not tokens_a and not tokens_b:
        return 0.0
    union = tokens_a | tokens_b
    if not union:
        return 0.0
    return len(tokens_a & tokens_b) / len(union)


# ---------------------------------------------------------------------------
# Diversity filter (pure logic — testable without any pipeline wiring)
# ---------------------------------------------------------------------------


class EvidenceDiversityFilter:
    """
    Selects a diverse Top-K subset from an already-ranked candidate list.

    Args:
        similarity_threshold:    Jaccard token-overlap at/above which two
                                 chunks are considered near-duplicates.
                                 Must be in (0, 1].
        max_chunks_per_document: Max chunks kept from the same
                                 ``metadata["source"]`` document (preference).
        section_window:          Two chunks from the same document with
                                 ``|chunk_index_a - chunk_index_b| <=`` this
                                 value are treated as the same "section"
                                 (neighbouring chunk regions) and only the
                                 higher-ranked one is kept (preference).
                                 Chunks missing ``chunk_index`` are never
                                 subject to this check (fails open).
    """

    def __init__(
        self,
        similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
        max_chunks_per_document: int = DEFAULT_MAX_CHUNKS_PER_DOCUMENT,
        section_window: int = DEFAULT_SECTION_WINDOW,
    ) -> None:
        if not (0.0 < similarity_threshold <= 1.0):
            raise ValueError(
                f"similarity_threshold must be in (0, 1]. Got: {similarity_threshold}."
            )
        if max_chunks_per_document < 1:
            raise ValueError(
                f"max_chunks_per_document must be >= 1. Got: {max_chunks_per_document}."
            )
        if section_window < 0:
            raise ValueError(f"section_window must be >= 0. Got: {section_window}.")
        self._threshold = float(similarity_threshold)
        self._max_per_doc = int(max_chunks_per_document)
        self._section_window = int(section_window)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def filter(self, candidates: list[dict[str, Any]], top_k: int) -> list[dict[str, Any]]:
        """
        Select up to ``top_k`` diverse, non-redundant candidates.

        Candidates are assumed already ranked best-first (e.g. by
        ``rerank_score``) — this method never re-scores or reorders within a
        tier, it only decides which candidates to keep, preserving the
        incoming relative order.

        Args:
            candidates: Ranked candidate list (each must carry ``chunk_id`` /
                       ``text``; ``metadata`` is optional but required for
                       document/section preferences to apply).
            top_k:      Maximum number of results to return (>= 1).

        Returns:
            Up to ``top_k`` candidates, each a shallow copy with diversity
            provenance added (``diversity_kept_reason``,
            ``diversity_backfilled``) — every original key is preserved.

        Raises:
            ValueError: If ``top_k`` < 1.
        """
        if top_k < 1:
            raise ValueError(f"top_k must be >= 1. Got: {top_k}.")
        if not candidates:
            return []

        kept: list[dict[str, Any]] = []
        kept_tokens: list[set[str]] = []
        doc_counts: dict[Any, int] = {}
        doc_indices: dict[Any, list[int]] = {}
        rejected_for_preference: list[dict[str, Any]] = []
        duplicates_removed = 0

        def _try_keep(cand: dict[str, Any], *, enforce_preferences: bool, backfilled: bool) -> bool:
            """
            Attempt to add ``cand`` to ``kept``. Duplicate detection ALWAYS
            runs (against the live, growing ``kept_tokens`` — including
            anything already added during backfill), so two mutually-redundant
            candidates can never both slip in just because they were rejected
            in different iterations before either was kept. Document/section
            preferences are skipped when ``enforce_preferences=False``
            (backfill pass) — those are soft preferences, not hard rules.
            Returns True if kept.
            """
            text = str(cand.get("text", "") or "")
            tokens = set(tokenize(text))

            dup_of = self._find_duplicate(tokens, kept_tokens, kept)
            if dup_of is not None:
                nonlocal duplicates_removed
                duplicates_removed += 1
                logger.debug(
                    "EvidenceDiversityFilter | dropping near-duplicate chunk_id=%s "
                    "(duplicate of %s)", cand.get("chunk_id"), dup_of,
                )
                return False  # hard rule — applies in every pass, never backfilled

            metadata = cand.get("metadata") or {}
            doc_id = metadata.get("source")
            chunk_index = metadata.get("chunk_index")

            if enforce_preferences and doc_id is not None:
                if doc_counts.get(doc_id, 0) >= self._max_per_doc:
                    return False
                if chunk_index is not None and self._too_close_to_kept_section(
                    doc_id, chunk_index, doc_indices,
                ):
                    return False

            item = dict(cand)
            item["diversity_kept_reason"] = "backfilled" if backfilled else "diverse"
            item["diversity_backfilled"] = backfilled
            kept.append(item)
            kept_tokens.append(tokens)
            if doc_id is not None:
                doc_counts[doc_id] = doc_counts.get(doc_id, 0) + 1
                if chunk_index is not None:
                    doc_indices.setdefault(doc_id, []).append(chunk_index)
            return True

        # --- Pass 1: strict — enforce dedup + document/section preferences.
        for cand in candidates:
            if len(kept) >= top_k:
                rejected_for_preference.append(cand)
                continue
            if not _try_keep(cand, enforce_preferences=True, backfilled=False):
                rejected_for_preference.append(cand)

        # --- Pass 2: backfill — never return fewer than top_k while
        # candidates remain, but duplicates stay excluded in every pass
        # (checked live against the growing kept set — see _try_keep).
        if len(kept) < top_k:
            for cand in rejected_for_preference:
                if len(kept) >= top_k:
                    break
                _try_keep(cand, enforce_preferences=False, backfilled=True)

        logger.info(
            "EvidenceDiversityFilter.filter | candidates=%d | kept=%d | "
            "duplicates_removed=%d | backfilled=%d",
            len(candidates), len(kept), duplicates_removed,
            sum(1 for k in kept if k["diversity_backfilled"]),
        )
        return kept

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _find_duplicate(
        self,
        tokens: set[str],
        kept_tokens: list[set[str]],
        kept: list[dict[str, Any]],
    ) -> Any:
        """Return the chunk_id of the first already-kept chunk this one duplicates, else None."""
        for other_tokens, other in zip(kept_tokens, kept):
            if jaccard_similarity(tokens, other_tokens) >= self._threshold:
                return other.get("chunk_id")
        return None

    def _too_close_to_kept_section(
        self,
        doc_id: Any,
        chunk_index: int,
        doc_indices: dict[Any, list[int]],
    ) -> bool:
        """True if ``chunk_index`` falls within section_window of any already-kept index for doc_id."""
        for kept_index in doc_indices.get(doc_id, []):
            try:
                if abs(int(chunk_index) - int(kept_index)) <= self._section_window:
                    return True
            except (TypeError, ValueError):
                continue
        return False

    def __repr__(self) -> str:
        return (
            f"EvidenceDiversityFilter(similarity_threshold={self._threshold}, "
            f"max_chunks_per_document={self._max_per_doc}, "
            f"section_window={self._section_window})"
        )


# ---------------------------------------------------------------------------
# Drop-in pipeline wrapper
# ---------------------------------------------------------------------------


class EvidenceDiversityPipeline:
    """
    Drop-in retrieval-pipeline wrapper applying :class:`EvidenceDiversityFilter`
    to an inner pipeline's (already reranked) output.

    Args:
        inner_pipeline:       Pipeline to fetch a candidate pool from
                              (typically ``RerankingRetrievalPipeline``, used
                              unchanged).
        diversity_filter:     Configured :class:`EvidenceDiversityFilter`.
        candidate_multiplier: Fetches ``max(top_k * candidate_multiplier,
                              min_candidates)`` from the inner pipeline so
                              there is a real pool to diversify from.
        min_candidates:       Lower bound on the candidate pool.
    """

    def __init__(
        self,
        inner_pipeline: Any,
        diversity_filter: EvidenceDiversityFilter,
        candidate_multiplier: int = DEFAULT_CANDIDATE_MULTIPLIER,
        min_candidates: int = DEFAULT_MIN_CANDIDATES,
    ) -> None:
        if inner_pipeline is None:
            raise ValueError("inner_pipeline must not be None.")
        if diversity_filter is None:
            raise ValueError("diversity_filter must not be None.")
        self._inner = inner_pipeline
        self._filter = diversity_filter
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
        Fetch a reranked candidate pool from the inner pipeline and diversify it.

        Args:
            query:           Forwarded unchanged to the inner pipeline.
            filter_criteria: Forwarded unchanged to the inner pipeline.
            top_k:           Final number of results to return (>= 1).

        Returns:
            Up to ``top_k`` diverse results, in the inner pipeline's rank
            order minus redundant/over-represented candidates.

        Raises:
            ValueError: If ``query`` is empty/whitespace or ``top_k`` < 1.
        """
        if not query or not query.strip():
            raise ValueError("query must be a non-empty string.")
        if top_k < 1:
            raise ValueError(f"top_k must be >= 1. Got: {top_k}.")

        candidate_k = max(top_k * self._candidate_multiplier, self._min_candidates)
        candidates = self._inner.search(query=query, filter_criteria=filter_criteria, top_k=candidate_k)
        if not candidates:
            return []

        result = self._filter.filter(candidates, top_k=top_k)
        logger.info(
            "EvidenceDiversityPipeline.search | candidates=%d | returned=%d | query=%r",
            len(candidates), len(result), query,
        )
        return result

    # ------------------------------------------------------------------
    # Drop-in compatibility surface (read by RAGAgent / reporting)
    # ------------------------------------------------------------------

    @property
    def similarity_threshold(self) -> float:
        return getattr(self._inner, "similarity_threshold", 0.0)

    @property
    def collection(self) -> str:
        return getattr(self._inner, "collection", "")

    def __repr__(self) -> str:
        return f"EvidenceDiversityPipeline(inner={self._inner!r}, filter={self._filter!r})"
