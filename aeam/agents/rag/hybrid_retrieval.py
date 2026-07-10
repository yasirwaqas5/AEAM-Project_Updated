"""
aeam/agents/rag/hybrid_retrieval.py

Hybrid retrieval for the AEAM RAG system (Phase 7.1).

Adds a lexical BM25 retrieval source alongside the existing dense Qdrant
vector retrieval and fuses the two ranked lists with Reciprocal Rank Fusion
(RRF). The existing :class:`~aeam.agents.rag.retrieval_pipeline.RetrievalPipeline`
is used unchanged, by composition — this module never modifies it.

Design constraints (Phase 7.1):
- Dense vector retrieval stays exactly as-is (wrapped, not edited).
- BM25 is pure standard-library Python — no rank_bm25, no numpy, no framework
  (no LangChain / Haystack / LlamaIndex).
- The public ``search(query, filter_criteria, top_k)`` contract and the
  ``similarity_threshold`` / ``collection`` attributes match RetrievalPipeline
  exactly, so :class:`~aeam.agents.rag.rag_agent.RAGAgent` is a drop-in caller.
- Every result preserves the existing evidence schema
  (``chunk_id`` / ``text`` / ``metadata`` / ``similarity``) so citations,
  validation (chunk_id grounding), and the Evidence UI keep working. Hybrid
  provenance is added as *extra* keys, never by removing existing ones.
"""

from __future__ import annotations

import math
import re
from typing import Any

from aeam.monitoring.logging_config import get_logger

logger = get_logger(__name__, agent="rag")

# ---------------------------------------------------------------------------
# Tokenisation
# ---------------------------------------------------------------------------

# Split on any run of non-alphanumeric characters. Deterministic and
# dependency-free — the same tokeniser is applied to documents and queries.
_TOKEN_RE: re.Pattern[str] = re.compile(r"[^a-z0-9]+")

# Minimal, domain-neutral stopword set. Kept small on purpose: over-aggressive
# stopword removal hurts short SRE queries ("high cpu", "db latency").
_STOPWORDS: frozenset[str] = frozenset({
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "is", "are",
    "was", "were", "be", "been", "with", "at", "by", "it", "its", "this",
    "that", "as", "from", "but",
})

# RRF fusion constant (Cormack et al., 2009). 60 is the canonical default.
DEFAULT_RRF_K: int = 60


def tokenize(text: str) -> list[str]:
    """
    Lowercase, split on non-alphanumeric runs, drop stopwords and 1-char tokens.

    Args:
        text: Arbitrary input string (document chunk or query).

    Returns:
        List of normalised token strings (may be empty).
    """
    if not text:
        return []
    tokens = _TOKEN_RE.split(text.lower())
    return [t for t in tokens if len(t) > 1 and t not in _STOPWORDS]


# ---------------------------------------------------------------------------
# BM25 lexical index (Okapi BM25, pure Python)
# ---------------------------------------------------------------------------


class BM25Index:
    """
    In-memory Okapi BM25 lexical index over a fixed chunk corpus.

    The corpus mirrors the dense Qdrant collection: each document is a chunk
    with ``chunk_id`` / ``text`` / ``metadata`` — the exact shape
    :meth:`RetrievalPipeline._hit_to_result` produces — so fused results are
    schema-compatible with dense results.

    Scoring uses the standard Okapi BM25 formula::

        score(q, d) = Σ_t IDF(t) * ( f(t,d) * (k1 + 1) )
                                   / ( f(t,d) + k1 * (1 - b + b * |d| / avgdl) )

    with ``IDF(t) = ln( 1 + (N - n(t) + 0.5) / (n(t) + 0.5) )`` (the
    non-negative BM25+ idf variant, so common terms never contribute a
    negative score).

    Args:
        k1: Term-frequency saturation (typical 1.2–2.0). Default 1.5.
        b:  Length-normalisation strength in [0, 1]. Default 0.75.
    """

    def __init__(self, k1: float = 1.5, b: float = 0.75) -> None:
        self._k1 = float(k1)
        self._b = float(b)
        self._docs: list[dict[str, Any]] = []          # {chunk_id, text, metadata}
        self._doc_tokens: list[list[str]] = []          # per-doc token lists
        self._doc_freqs: list[dict[str, int]] = []      # per-doc term frequencies
        self._doc_len: list[int] = []                   # per-doc token counts
        self._df: dict[str, int] = {}                   # document frequency per term
        self._idf: dict[str, float] = {}                # cached idf per term
        self._avgdl: float = 0.0
        self._built: bool = False

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def build(self, documents: list[dict[str, Any]]) -> None:
        """
        Build the index from a list of chunk dicts.

        Args:
            documents: List of ``{"chunk_id": str, "text": str,
                       "metadata": dict}`` dicts. Documents with empty text
                       are indexed (so counts stay consistent) but simply never
                       match a query.
        """
        self._docs = []
        self._doc_tokens = []
        self._doc_freqs = []
        self._doc_len = []
        self._df = {}

        for doc in documents:
            text = str(doc.get("text", "") or "")
            tokens = tokenize(text)
            freqs: dict[str, int] = {}
            for tok in tokens:
                freqs[tok] = freqs.get(tok, 0) + 1

            self._docs.append({
                "chunk_id": doc.get("chunk_id"),
                "text": text,
                "metadata": doc.get("metadata", {}) or {},
            })
            self._doc_tokens.append(tokens)
            self._doc_freqs.append(freqs)
            self._doc_len.append(len(tokens))

            for term in freqs:
                self._df[term] = self._df.get(term, 0) + 1

        n = len(self._docs)
        self._avgdl = (sum(self._doc_len) / n) if n else 0.0

        # Pre-compute idf for every term once.
        self._idf = {}
        for term, df in self._df.items():
            self._idf[term] = math.log(1.0 + (n - df + 0.5) / (df + 0.5))

        self._built = True
        logger.info(
            "BM25Index.build | docs=%d | vocab=%d | avgdl=%.1f",
            n, len(self._df), self._avgdl,
        )

    @classmethod
    def from_qdrant(
        cls,
        qdrant_client: Any,
        collection: str,
        k1: float = 1.5,
        b: float = 0.75,
        batch_size: int = 256,
    ) -> "BM25Index":
        """
        Build a BM25 index by scrolling every point in a Qdrant collection.

        Reads the same payloads the dense pipeline stores (``text`` /
        ``chunk_id`` / metadata), so the lexical and dense views share one
        source of truth. Never raises on an empty or missing collection — it
        returns an empty index (which simply contributes nothing to fusion).

        Args:
            qdrant_client: Connected ``QdrantClient``.
            collection:    Collection name to scroll.
            k1, b:         BM25 hyperparameters.
            batch_size:    Scroll page size.

        Returns:
            A built :class:`BM25Index`.
        """
        index = cls(k1=k1, b=b)
        documents: list[dict[str, Any]] = []
        offset: Any = None

        try:
            while True:
                points, offset = qdrant_client.scroll(
                    collection_name=collection,
                    limit=batch_size,
                    offset=offset,
                    with_payload=True,
                    with_vectors=False,
                )
                for point in points:
                    payload: dict[str, Any] = dict(getattr(point, "payload", None) or {})
                    text = payload.pop("text", "")
                    chunk_id = payload.pop("chunk_id", str(getattr(point, "id", "")))
                    documents.append({
                        "chunk_id": chunk_id,
                        "text": text,
                        "metadata": payload,
                    })
                if offset is None:
                    break
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "BM25Index.from_qdrant | scroll failed for collection=%r: %s "
                "| building index from %d docs collected so far",
                collection, exc, len(documents),
            )

        index.build(documents)
        return index

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(self, query: str, top_k: int) -> list[dict[str, Any]]:
        """
        Return the ``top_k`` chunks scored highest by BM25 for ``query``.

        Args:
            query: Natural-language query string.
            top_k: Maximum number of results (>= 1).

        Returns:
            Ranked list (best first) of dicts::

                {"chunk_id", "text", "metadata", "bm25_score"}

            Empty list if the index is empty, the query has no usable tokens,
            or nothing scored above zero.
        """
        if not self._built or not self._docs or top_k < 1:
            return []

        q_tokens = tokenize(query)
        if not q_tokens:
            return []
        # Unique query terms — repeating a term in the query must not double-count.
        q_terms = set(q_tokens)

        scored: list[tuple[float, int]] = []
        for i, freqs in enumerate(self._doc_freqs):
            dl = self._doc_len[i]
            if dl == 0:
                continue
            score = 0.0
            for term in q_terms:
                f = freqs.get(term)
                if not f:
                    continue
                idf = self._idf.get(term, 0.0)
                denom = f + self._k1 * (1.0 - self._b + self._b * dl / (self._avgdl or 1.0))
                score += idf * (f * (self._k1 + 1.0)) / denom
            if score > 0.0:
                scored.append((score, i))

        scored.sort(key=lambda x: x[0], reverse=True)

        results: list[dict[str, Any]] = []
        for score, i in scored[:top_k]:
            doc = self._docs[i]
            results.append({
                "chunk_id": doc["chunk_id"],
                "text": doc["text"],
                "metadata": doc["metadata"],
                "bm25_score": round(float(score), 6),
            })
        return results

    @property
    def size(self) -> int:
        """Number of indexed documents."""
        return len(self._docs)

    def __repr__(self) -> str:
        return f"BM25Index(docs={self.size}, k1={self._k1}, b={self._b})"


# ---------------------------------------------------------------------------
# Reciprocal Rank Fusion
# ---------------------------------------------------------------------------


def reciprocal_rank_fusion(
    ranked_lists: list[list[dict[str, Any]]],
    k: int = DEFAULT_RRF_K,
    source_names: list[str] | None = None,
) -> list[dict[str, Any]]:
    """
    Fuse several ranked result lists into one, using Reciprocal Rank Fusion.

    RRF is rank-based, not score-based, so it fuses heterogeneous scorers
    (cosine similarity vs BM25) without any score normalisation::

        rrf_score(d) = Σ_r  1 / (k + rank_r(d))

    where ``rank_r(d)`` is the 1-based position of document ``d`` in list ``r``
    (a document absent from a list contributes nothing for that list).

    Documents are identified by ``chunk_id``. The returned dict for each
    document preserves the original chunk fields (``chunk_id`` / ``text`` /
    ``metadata`` and any per-source score such as ``similarity`` /
    ``bm25_score``) and adds fusion provenance:

        - ``rrf_score``        — fused score (higher is better).
        - ``retrieval_sources``— names of the lists that contained the doc.
        - ``<source>_rank``    — 1-based rank within each contributing list.

    Args:
        ranked_lists: List of ranked lists (each best-first). Items must carry
                      a ``chunk_id``.
        k:            RRF constant (default 60).
        source_names: Optional names parallel to ``ranked_lists`` used for the
                      ``retrieval_sources`` and ``<source>_rank`` keys.
                      Defaults to ``["source0", "source1", ...]``.

    Returns:
        Fused list sorted by ``rrf_score`` descending. Ties broken
        deterministically by ``chunk_id`` so output is stable.
    """
    if source_names is None:
        source_names = [f"source{i}" for i in range(len(ranked_lists))]

    fused: dict[str, dict[str, Any]] = {}

    for list_idx, ranked in enumerate(ranked_lists):
        src = source_names[list_idx]
        for rank, item in enumerate(ranked, start=1):
            cid = item.get("chunk_id")
            if cid is None:
                continue
            contribution = 1.0 / (k + rank)

            entry = fused.get(cid)
            if entry is None:
                # First time we see this chunk — seed from its source dict so
                # we preserve text/metadata/per-source score untouched.
                entry = dict(item)
                entry["rrf_score"] = 0.0
                entry["retrieval_sources"] = []
                fused[cid] = entry
            else:
                # Merge any fields this source has that the first one lacked
                # (e.g. bm25_score on a chunk first seen from the dense list).
                for key, value in item.items():
                    if key not in entry:
                        entry[key] = value

            entry["rrf_score"] += contribution
            entry["retrieval_sources"].append(src)
            entry[f"{src}_rank"] = rank

    results = list(fused.values())
    results.sort(key=lambda e: (e["rrf_score"], str(e.get("chunk_id"))), reverse=True)
    return results


# ---------------------------------------------------------------------------
# Hybrid retrieval pipeline (drop-in wrapper)
# ---------------------------------------------------------------------------


class HybridRetrievalPipeline:
    """
    Dense + BM25 hybrid retrieval with RRF fusion — a drop-in for
    :class:`~aeam.agents.rag.retrieval_pipeline.RetrievalPipeline`.

    Composes (never edits) the existing dense pipeline with a
    :class:`BM25Index`. Exposes the identical ``search`` signature and the
    ``similarity_threshold`` / ``collection`` attributes that
    :class:`~aeam.agents.rag.rag_agent.RAGAgent` reads, so no caller changes.

    Args:
        dense_pipeline:       The existing dense ``RetrievalPipeline`` (unchanged).
        bm25_index:           A built :class:`BM25Index` over the same corpus.
        rrf_k:                RRF constant. Default 60.
        candidate_multiplier: Each retriever fetches ``max(top_k *
                              candidate_multiplier, min_candidates)`` candidates
                              before fusion, so fusion can promote a chunk that
                              neither retriever ranked in its own top_k.
        min_candidates:       Lower bound on the per-retriever candidate pool.
    """

    def __init__(
        self,
        dense_pipeline: Any,
        bm25_index: BM25Index,
        rrf_k: int = DEFAULT_RRF_K,
        candidate_multiplier: int = 4,
        min_candidates: int = 20,
    ) -> None:
        if dense_pipeline is None:
            raise ValueError("dense_pipeline must not be None.")
        if bm25_index is None:
            raise ValueError("bm25_index must not be None.")
        self._dense = dense_pipeline
        self._bm25 = bm25_index
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
        Retrieve, fuse (RRF), and return the top-k hybrid results.

        The dense list is produced by the unchanged dense pipeline (still
        subject to its cosine ``similarity_threshold``); the BM25 list adds
        lexical recall for chunks the dense threshold would have dropped.

        ``filter_criteria`` is forwarded to the dense pipeline only (server-side
        Qdrant filtering). The BM25 index is unfiltered; in the current RAG
        flow ``RAGAgent`` never passes a filter, so this has no production
        effect — documented here for completeness.

        Args:
            query:           Natural-language query. Must be non-empty.
            filter_criteria: Optional dense-side payload filter (forwarded to
                             the dense pipeline).
            top_k:           Maximum fused results to return. Must be >= 1.

        Returns:
            Fused results (best first). Each preserves the existing evidence
            schema — ``chunk_id`` / ``text`` / ``metadata`` / ``similarity`` —
            and adds ``rrf_score``, ``retrieval_sources``, ``dense_rank`` /
            ``bm25_rank``, and ``dense_similarity`` / ``bm25_score`` provenance.

        Raises:
            ValueError: If ``query`` is empty/whitespace or ``top_k`` < 1.
        """
        if not query or not query.strip():
            raise ValueError("query must be a non-empty string.")
        if top_k < 1:
            raise ValueError(f"top_k must be >= 1. Got: {top_k}.")

        candidate_k = max(top_k * self._candidate_multiplier, self._min_candidates)

        # Source 1: dense vector retrieval (existing behaviour, unchanged).
        dense_results = self._dense.search(
            query=query, filter_criteria=filter_criteria, top_k=candidate_k,
        )
        # Preserve the dense cosine under its own key before fusion renames.
        for r in dense_results:
            r.setdefault("dense_similarity", r.get("similarity"))

        # Source 2: BM25 lexical retrieval (new).
        bm25_results = self._bm25.search(query=query, top_k=candidate_k)

        # Fuse by RRF (rank-based; no score normalisation needed).
        fused = reciprocal_rank_fusion(
            [dense_results, bm25_results],
            k=self._rrf_k,
            source_names=["dense", "bm25"],
        )

        top = fused[:top_k]
        top = [self._finalize(entry) for entry in top]

        logger.info(
            "HybridRetrievalPipeline.search | dense=%d | bm25=%d | fused=%d | "
            "returned=%d | query=%r",
            len(dense_results), len(bm25_results), len(fused), len(top), query,
        )
        return top

    # ------------------------------------------------------------------
    # Drop-in compatibility surface (read by RAGAgent / reporting)
    # ------------------------------------------------------------------

    @property
    def similarity_threshold(self) -> float:
        """Delegates to the dense pipeline (reported in RAG findings)."""
        return getattr(self._dense, "similarity_threshold", 0.0)

    @property
    def collection(self) -> str:
        """Delegates to the dense pipeline's collection name."""
        return getattr(self._dense, "collection", "")

    @property
    def bm25_size(self) -> int:
        """Number of documents in the lexical index (diagnostics)."""
        return self._bm25.size

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _finalize(entry: dict[str, Any]) -> dict[str, Any]:
        """
        Normalise a fused entry to the evidence schema RAGAgent expects.

        Guarantees ``similarity`` is a valid float:
        - dense-sourced chunks keep their original cosine similarity;
        - BM25-only chunks (no cosine) get ``similarity = 0.0`` — their
          relevance is carried honestly by ``bm25_score`` / ``bm25_rank`` and
          the ``rrf_score`` that drives the ordering.

        All fusion provenance keys are retained; no existing key is removed.
        """
        sim = entry.get("similarity")
        if not isinstance(sim, (int, float)):
            # BM25-only chunk: no cosine similarity exists.
            entry["similarity"] = 0.0
        # Ensure the provenance keys always exist for downstream consumers.
        entry.setdefault("dense_similarity", None)
        entry.setdefault("bm25_score", None)
        entry.setdefault("dense_rank", None)
        entry.setdefault("bm25_rank", None)
        entry["rrf_score"] = round(float(entry.get("rrf_score", 0.0)), 8)
        return entry

    def __repr__(self) -> str:
        return (
            f"HybridRetrievalPipeline(dense={self._dense!r}, "
            f"bm25_docs={self._bm25.size}, rrf_k={self._rrf_k})"
        )
