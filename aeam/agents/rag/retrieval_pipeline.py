"""
aeam/agents/rag/retrieval_pipeline.py

Online similarity search pipeline for the AEAM RAG system.

Encodes a natural-language query, searches Qdrant using cosine similarity,
applies a minimum similarity threshold, and returns the top-k results as
structured dicts.

Phase 4 constraints strictly enforced:
- No LLM calls.
- No decision logic.
- No database writes.
- Qdrant only (official qdrant-client).
- Similarity threshold: >= 0.5
- Default top_k: 5
- Embedding: all-MiniLM-L6-v2 (dim=384, cosine)
"""

from __future__ import annotations

import logging
from aeam.monitoring.logging_config import get_logger
from typing import Any

from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels

from aeam.integrations.embedding_service import EmbeddingService

logger = get_logger(__name__, agent="rag")

# Phase 4 spec defaults.
DEFAULT_TOP_K: int = 5
SIMILARITY_THRESHOLD: float = 0.5


class RetrievalPipeline:
    """
    Online similarity search pipeline over a Qdrant collection.

    Accepts a natural-language query string, encodes it to a 384-dimensional
    vector using the injected :class:`~aeam.integrations.embedding_service.EmbeddingService`,
    queries Qdrant with cosine similarity, filters results below the similarity
    threshold, and returns the top-k results as plain dicts.

    This class:
    - Makes no LLM calls.
    - Makes no decisions.
    - Writes nothing to any database.
    - Is stateless between calls.

    Args:
        embedding_service:  Pre-loaded embedding model wrapper.
        qdrant_client:      Connected :class:`qdrant_client.QdrantClient`.
        collection:         Qdrant collection to search.
                            Defaults to ``"aeam_documents"``.
        similarity_threshold: Minimum cosine similarity score to include in
                            results. Defaults to ``0.5`` (empirically validated
                            against the 300/50 chunk corpus; see retrieval
                            threshold benchmark).
        default_top_k:      Maximum number of results when the caller does not
                            supply ``top_k``. Defaults to ``5`` (Phase 4 spec).

    Raises:
        ValueError: If ``embedding_service`` or ``qdrant_client`` is ``None``,
                    ``collection`` is empty, ``similarity_threshold`` is
                    outside (0, 1], or ``default_top_k`` < 1.
        RuntimeError: If Qdrant is not reachable at startup.
    """

    def __init__(
        self,
        embedding_service: EmbeddingService,
        qdrant_client: QdrantClient,
        collection: str = "aeam_documents",
        similarity_threshold: float = SIMILARITY_THRESHOLD,
        default_top_k: int = DEFAULT_TOP_K,
    ) -> None:
        """
        Initialise the retrieval pipeline.

        Args:
            embedding_service:    Loaded embedding service. Must not be None.
            qdrant_client:        Connected Qdrant client. Must not be None.
            collection:           Target Qdrant collection name.
            similarity_threshold: Minimum similarity score (0 < threshold <= 1).
            default_top_k:        Default result limit when not overridden per call.

        Raises:
            ValueError: On invalid arguments.
            RuntimeError: If Qdrant connection fails (e.g., server not running).
        """
        if embedding_service is None:
            raise ValueError("embedding_service must not be None.")
        if qdrant_client is None:
            raise ValueError("qdrant_client must not be None.")
        if not collection or not collection.strip():
            raise ValueError("collection must be a non-empty string.")
        if not (0 < similarity_threshold <= 1.0):
            raise ValueError(
                f"similarity_threshold must be in (0, 1]. Got: {similarity_threshold}."
            )
        if default_top_k < 1:
            raise ValueError(f"default_top_k must be >= 1. Got: {default_top_k}.")

        self._embed: EmbeddingService = embedding_service
        self._qdrant: QdrantClient = qdrant_client
        self._collection: str = collection.strip()
        self._threshold: float = similarity_threshold
        self._default_top_k: int = default_top_k

        # 🔐 Qdrant connection guard – fail early if Qdrant is unreachable.
        try:
            self._qdrant.get_collections()
            logger.info("RetrievalPipeline | Qdrant connection verified.")
        except Exception as exc:
            raise RuntimeError(
                f"Qdrant connection failed at startup: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        filter_criteria: dict[str, Any] | None = None,
        top_k: int = DEFAULT_TOP_K,
    ) -> list[dict[str, Any]]:
        """
        Encode ``query`` and retrieve the most similar chunks from Qdrant.

        Steps:
        1. Validate and encode ``query`` to a 384-dim vector.
        2. Build an optional Qdrant payload filter from ``filter_criteria``.
        3. Query Qdrant with cosine similarity and ``score_threshold`` >= 0.5.
        4. Map each hit to the standardised result schema.
        5. Return results sorted by similarity descending, capped at ``top_k``.

        Args:
            query:            Natural-language search string. Must not be empty.
            filter_criteria:  Optional dict of payload field exact-match filters
                              to apply server-side in Qdrant (e.g.
                              ``{"doc_type": "incident_report"}``).
                              All conditions are combined with AND.
                              Pass ``None`` to search without filtering.
            top_k:            Maximum number of results to return.
                              Must be >= 1. Defaults to ``5``.

        Returns:
            List of result dicts, ordered by similarity descending::

                [
                    {
                        "chunk_id":   str,    # chunk's SHA-256 chunk_id from payload
                        "text":       str,    # chunk text
                        "metadata":   dict,   # all payload fields except "text"
                        "similarity": float,  # cosine similarity score (0–1)
                    },
                    ...
                ]

            Empty list if no results meet the similarity threshold, the
            collection does not exist, or ``query`` produces no matches.

        Raises:
            ValueError: If ``query`` is empty or whitespace-only, or
                        ``top_k`` < 1.
            Exception:  Propagates Qdrant client errors without wrapping.

        Example::

            results = pipeline.search(
                query="memory leak payment service",
                filter_criteria={"doc_type": "incident_report"},
                top_k=3,
            )
            # [
            #   {"chunk_id": "a3f...", "text": "...", "metadata": {...}, "similarity": 0.91},
            #   {"chunk_id": "b7c...", "text": "...", "metadata": {...}, "similarity": 0.83},
            # ]
        """
        if not query or not query.strip():
            raise ValueError("query must be a non-empty string.")
        if top_k < 1:
            raise ValueError(f"top_k must be >= 1. Got: {top_k}.")

        # Step 1: encode query.
        query_vector: list[float] = self._embed.encode_text(query.strip())

        logger.debug(
            "RetrievalPipeline.search | query=%r | top_k=%d | threshold=%.2f",
            query, top_k, self._threshold,
        )

        # Step 2: build optional payload filter.
        qdrant_filter: qmodels.Filter | None = self._build_filter(filter_criteria)

        # Step 3: query Qdrant.
        try:
            hits = self._qdrant.query_points(
                collection_name=self._collection,
                query=query_vector,
                limit=top_k,
                score_threshold=self._threshold,
                query_filter=qdrant_filter,
                with_payload=True,
            ).points
        except Exception as exc:
            logger.error(
                "RetrievalPipeline.search failed | collection=%r | error=%s",
                self._collection, exc,
            )
            raise

        # Step 4: map all hits to result dicts.
        raw_results = [self._hit_to_result(hit) for hit in hits]

        # 🔐 Similarity sanity guard – filter out any result with invalid similarity.
        filtered = []
        for res in raw_results:
            sim = res.get("similarity")
            if (
                isinstance(sim, (int, float))
                and 0.0 <= sim <= 1.0
                and sim >= self._threshold
            ):
                filtered.append(res)
            else:
                logger.debug(
                    "RetrievalPipeline | discarding result with invalid similarity=%r",
                    sim,
                )

        logger.info(
            "RetrievalPipeline.search | returned=%d / filtered=%d | threshold=%.2f",
            len(filtered), len(raw_results), self._threshold,
        )

        return filtered

    def search_by_vector(
        self,
        query_vector: list[float],
        filter_criteria: dict[str, Any] | None = None,
        top_k: int = DEFAULT_TOP_K,
    ) -> list[dict[str, Any]]:
        """
        Search Qdrant with a pre-computed query vector.

        Identical to :meth:`search` but skips the embedding step. Useful when
        the caller already holds a vector (e.g. from a cached embedding).

        Args:
            query_vector:    Pre-computed 384-dim embedding vector.
            filter_criteria: Optional payload filter dict (same semantics as
                             :meth:`search`).
            top_k:           Maximum results to return. Defaults to ``5``.

        Returns:
            Same result schema as :meth:`search`.

        Raises:
            ValueError: If ``query_vector`` is empty or ``top_k`` < 1.
            Exception:  Propagates Qdrant client errors.
        """
        if not query_vector:
            raise ValueError("query_vector must be a non-empty list of floats.")
        if top_k < 1:
            raise ValueError(f"top_k must be >= 1. Got: {top_k}.")

        qdrant_filter = self._build_filter(filter_criteria)

        try:
            hits = self._qdrant.query_points(
                collection_name=self._collection,
                query=query_vector,
                limit=top_k,
                score_threshold=self._threshold,
                query_filter=qdrant_filter,
                with_payload=True,
            ).points
        except Exception as exc:
            logger.error(
                "RetrievalPipeline.search_by_vector failed | error=%s", exc
            )
            raise

        raw_results = [self._hit_to_result(hit) for hit in hits]

        # 🔐 Similarity guard – same as in search
        filtered = []
        for res in raw_results:
            sim = res.get("similarity")
            if (
                isinstance(sim, (int, float))
                and 0.0 <= sim <= 1.0
                and sim >= self._threshold
            ):
                filtered.append(res)

        logger.info(
            "RetrievalPipeline.search_by_vector | returned=%d / filtered=%d",
            len(filtered), len(raw_results),
        )

        return filtered

    @property
    def collection(self) -> str:
        """The Qdrant collection this pipeline searches."""
        return self._collection

    @property
    def similarity_threshold(self) -> float:
        """Minimum similarity score applied to all searches."""
        return self._threshold

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_filter(
        filter_criteria: dict[str, Any] | None,
    ) -> qmodels.Filter | None:
        """
        Convert a flat ``filter_criteria`` dict into a Qdrant ``Filter`` object.

        Each key-value pair becomes a ``FieldCondition`` with a ``MatchValue``
        check (exact match). All conditions are combined with ``must`` (AND).

        Args:
            filter_criteria: Dict of payload field → expected value pairs.
                             Pass ``None`` or an empty dict to apply no filter.

        Returns:
            A :class:`qdrant_client.http.models.Filter` instance, or ``None``
            if no criteria are provided.

        Example::

            _build_filter({"doc_type": "incident_report", "source": "ops"})
            # → Filter(must=[
            #       FieldCondition(key="doc_type", match=MatchValue(value="incident_report")),
            #       FieldCondition(key="source",   match=MatchValue(value="ops")),
            #   ])
        """
        if not filter_criteria:
            return None

        conditions: list[qmodels.FieldCondition] = [
            qmodels.FieldCondition(
                key=field,
                match=qmodels.MatchValue(value=value),
            )
            for field, value in filter_criteria.items()
        ]

        return qmodels.Filter(must=conditions)

    @staticmethod
    def _hit_to_result(hit: Any) -> dict[str, Any]:
        """
        Map a Qdrant ``ScoredPoint`` to the standardised result schema.

        Result schema::

            {
                "chunk_id":   str,
                "text":       str,
                "metadata":   dict,
                "similarity": float,
            }

        ``chunk_id`` is read from the payload (set during ingestion); if absent,
        the Qdrant point UUID is used as a fallback.
        ``metadata`` contains all payload fields except ``"text"``.

        Args:
            hit: A :class:`qdrant_client.http.models.ScoredPoint`.

        Returns:
            Standardised result dict.
        """
        payload: dict[str, Any] = dict(hit.payload or {})

        text: str = payload.pop("text", "")
        chunk_id: str = payload.pop("chunk_id", str(hit.id))

        return {
            "chunk_id":   chunk_id,
            "text":       text,
            "metadata":   payload,
            "similarity": round(float(hit.score), 6),
        }

    def __repr__(self) -> str:
        return (
            f"RetrievalPipeline("
            f"collection={self._collection!r}, "
            f"threshold={self._threshold}, "
            f"default_top_k={self._default_top_k})"
        )