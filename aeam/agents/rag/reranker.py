"""
aeam/agents/rag/reranker.py

Cross-encoder reranking for the AEAM RAG system (Phase 7.2).

Adds a second-stage reranker AFTER hybrid retrieval (dense + BM25 + RRF).
A lightweight SentenceTransformers ``CrossEncoder`` re-scores each fused
candidate against the query and reorders them, replacing rank-fusion order
(which never saw the two together) with true query-document relevance.

Design constraints (Phase 7.2):
- Reranks ONLY the already-fused candidate list — retrieval is untouched.
- Composition, not modification: the hybrid/dense pipeline is wrapped, never
  edited. The public ``search(query, filter_criteria, top_k)`` contract and the
  ``similarity_threshold`` / ``collection`` attributes are preserved, so
  :class:`~aeam.agents.rag.rag_agent.RAGAgent` remains a drop-in caller.
- Every result keeps its existing evidence schema (``chunk_id`` / ``text`` /
  ``metadata`` / ``similarity`` and any RRF provenance); a ``rerank_score`` key
  is ADDED. No existing key is removed → citations, chunk IDs, validation and
  confidence calculation are all unaffected.
- Two graceful fallbacks: a model that fails to initialize is handled by the
  caller (main.py keeps the hybrid pipeline); a ``predict`` failure at query
  time falls back to the inner (hybrid) ordering for that query.
- No LangChain / Haystack / LlamaIndex. Only sentence-transformers, which is
  already a project dependency (the EmbeddingService uses it).
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from aeam.monitoring.logging_config import get_logger

logger = get_logger(__name__, agent="rag")

# Canonical lightweight cross-encoder reranker.
DEFAULT_RERANK_MODEL: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
DEFAULT_RERANK_TOP_N: int = 20


@runtime_checkable
class CrossEncoderModel(Protocol):
    """Structural type for the subset of ``sentence_transformers.CrossEncoder`` used here."""

    def predict(self, sentence_pairs: list[list[str]], **kwargs: Any) -> Any:
        """Return one relevance score per ``[query, passage]`` pair."""
        ...


# ---------------------------------------------------------------------------
# Cross-encoder reranker
# ---------------------------------------------------------------------------


class CrossEncoderReranker:
    """
    Re-scores and reorders candidate chunks with a cross-encoder.

    Args:
        model_name:  HuggingFace cross-encoder id. Ignored if ``model`` is given.
        max_length:  Max sequence length for the cross-encoder.
        model:       Pre-constructed model implementing :class:`CrossEncoderModel`
                     (used by tests to inject a deterministic stub; production
                     leaves this ``None`` so the real model is loaded).

    Raises:
        RuntimeError: If the real model cannot be imported or loaded. The caller
                      is expected to catch this and fall back to hybrid
                      retrieval (Phase 7.2 requirement #13).
    """

    def __init__(
        self,
        model_name: str = DEFAULT_RERANK_MODEL,
        max_length: int = 512,
        model: CrossEncoderModel | None = None,
    ) -> None:
        self._model_name = model_name
        if model is not None:
            self._model: CrossEncoderModel = model
            logger.info("CrossEncoderReranker | using injected model.")
            return
        try:
            from sentence_transformers import CrossEncoder
            self._model = CrossEncoder(model_name, max_length=max_length)
            logger.info("CrossEncoderReranker | loaded model=%s", model_name)
        except Exception as exc:  # noqa: BLE001
            # Surface as RuntimeError so the composition root can fall back.
            raise RuntimeError(
                f"CrossEncoder init failed for {model_name!r}: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def rerank(
        self,
        query: str,
        candidates: list[dict[str, Any]],
        top_k: int,
    ) -> list[dict[str, Any]]:
        """
        Re-score ``candidates`` against ``query`` and return the top ``top_k``.

        Each returned dict is a shallow copy of the input candidate with a
        ``rerank_score`` (float) added; all existing keys — including
        ``chunk_id`` (citations), ``text``, ``metadata``, ``similarity`` and any
        RRF provenance — are preserved untouched.

        Args:
            query:      The search query.
            candidates: Fused candidate chunks (must carry ``chunk_id`` / ``text``).
            top_k:      Number of results to keep after reranking (>= 1).

        Returns:
            Reranked list (best first), length ``min(top_k, len(candidates))``.
            Ordering is deterministic (ties broken by ``chunk_id``).

        Raises:
            Exception: Propagates a model ``predict`` failure so the wrapping
                       pipeline can decide to fall back. (The pipeline catches it.)
        """
        if not candidates:
            return []
        if top_k < 1:
            raise ValueError(f"top_k must be >= 1. Got: {top_k}.")

        pairs: list[list[str]] = [[query, str(c.get("text", "") or "")] for c in candidates]
        raw_scores = self._model.predict(pairs)

        reranked: list[dict[str, Any]] = []
        for cand, score in zip(candidates, raw_scores):
            item = dict(cand)                       # preserve every existing key
            item["rerank_score"] = round(float(score), 6)
            reranked.append(item)

        reranked.sort(
            key=lambda x: (x["rerank_score"], str(x.get("chunk_id"))),
            reverse=True,
        )
        return reranked[:top_k]

    @property
    def model_name(self) -> str:
        return self._model_name

    def __repr__(self) -> str:
        return f"CrossEncoderReranker(model={self._model_name!r})"


# ---------------------------------------------------------------------------
# Reranking retrieval pipeline (drop-in wrapper)
# ---------------------------------------------------------------------------


class RerankingRetrievalPipeline:
    """
    Retrieve-then-rerank pipeline — a drop-in for
    :class:`~aeam.agents.rag.retrieval_pipeline.RetrievalPipeline` /
    :class:`~aeam.agents.rag.hybrid_retrieval.HybridRetrievalPipeline`.

    Fetches a larger candidate pool (``rerank_top_n``) from the inner pipeline,
    reranks it with a :class:`CrossEncoderReranker`, and returns the caller's
    requested ``top_k``. The inner pipeline (hybrid or dense) is used unchanged.

    Args:
        inner_pipeline: Any object exposing ``search(query, filter_criteria,
                        top_k)`` (typically the HybridRetrievalPipeline).
        reranker:       A constructed :class:`CrossEncoderReranker`.
        rerank_top_n:   Candidate-pool size fed to the reranker. Default 20.
    """

    def __init__(
        self,
        inner_pipeline: Any,
        reranker: CrossEncoderReranker,
        rerank_top_n: int = DEFAULT_RERANK_TOP_N,
    ) -> None:
        if inner_pipeline is None:
            raise ValueError("inner_pipeline must not be None.")
        if reranker is None:
            raise ValueError("reranker must not be None.")
        self._inner = inner_pipeline
        self._reranker = reranker
        self._rerank_top_n = max(1, int(rerank_top_n))

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
        Retrieve ``rerank_top_n`` fused candidates, rerank, return ``top_k``.

        Args:
            query:           Natural-language query. Must be non-empty.
            filter_criteria: Forwarded to the inner pipeline unchanged.
            top_k:           Final number of results (>= 1).

        Returns:
            Reranked results (best first). Each preserves the existing evidence
            schema and adds ``rerank_score``. If reranking fails at query time,
            the inner (hybrid) ordering is returned instead — the search never
            raises on a model error.

        Raises:
            ValueError: If ``query`` is empty/whitespace or ``top_k`` < 1.
        """
        if not query or not query.strip():
            raise ValueError("query must be a non-empty string.")
        if top_k < 1:
            raise ValueError(f"top_k must be >= 1. Got: {top_k}.")

        candidate_n = max(self._rerank_top_n, top_k)
        candidates = self._inner.search(
            query=query, filter_criteria=filter_criteria, top_k=candidate_n,
        )
        if not candidates:
            return []

        try:
            reranked = self._reranker.rerank(query, candidates, top_k=top_k)
            logger.info(
                "RerankingRetrievalPipeline.search | candidates=%d | returned=%d | "
                "query=%r", len(candidates), len(reranked), query,
            )
            return reranked
        except Exception as exc:  # noqa: BLE001
            # Query-time fallback: never break the investigation on a model error.
            logger.error(
                "Reranker failed at query time (%s) — falling back to hybrid order.",
                exc,
            )
            return candidates[:top_k]

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
        return (
            f"RerankingRetrievalPipeline(inner={self._inner!r}, "
            f"reranker={self._reranker!r}, rerank_top_n={self._rerank_top_n})"
        )
