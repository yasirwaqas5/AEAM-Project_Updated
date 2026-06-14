"""
aeam/integrations/vector_db.py

Vector database integration for the AEAM system.

Provides a unified vector storage and similarity search interface. When
``settings.VECTOR_DB_URL`` is configured the class is designed to accept
an external vector DB backend in future. When it is absent (or in any
degraded state) the class transparently falls back to an in-memory store
with manual cosine similarity computation.

Design rules:
- Never crashes startup.
- Never requires pinecone, qdrant, chromadb, faiss, or numpy.
- Cosine similarity implemented with stdlib ``math`` only.
- All public methods return safe empty/False values on failure.
- Secret values are never logged.

In-memory storage layout::

    self._documents = {
        document_id: {
            "embedding": list[float],
            "metadata":  dict,
        }
    }
"""

from __future__ import annotations

import logging
import math
from typing import Any

logger = logging.getLogger("aeam.integrations.vector_db")


class VectorDB:
    """
    Startup-safe vector storage with in-memory cosine similarity search.

    When ``settings.VECTOR_DB_URL`` is present, the class logs that an
    external backend is configured (future integration point). Regardless
    of external availability, an in-memory fallback store is always active
    so that the RAG pipeline and memory layer can operate without any
    external service.

    All mutations and searches are O(n) over the in-memory document dict.
    This is sufficient for demo and development workloads; swap for a real
    vector DB client (Qdrant, Weaviate, etc.) without changing the public
    interface.

    Args:
        settings:       Application settings. Read for ``VECTOR_DB_URL``.
        secret_manager: Optional secret manager (reserved for future
                        authenticated backends). Pass ``None`` for the
                        in-memory path.

    Example::

        db = VectorDB(settings=settings)
        db.initialize()

        db.upsert("doc-1", embedding=[0.1, 0.9, 0.3], metadata={"source": "report"})
        results = db.search(embedding=[0.1, 0.8, 0.3], top_k=3)
    """

    def __init__(
        self,
        settings: Any,
        secret_manager: Any = None,
    ) -> None:
        """
        Initialise VectorDB.

        Reads ``settings.VECTOR_DB_URL`` to determine whether an external
        backend is configured. In all cases the in-memory fallback store
        is set up and ready immediately.

        Args:
            settings:       Settings object with a ``VECTOR_DB_URL`` attribute.
            secret_manager: Reserved for future authenticated backends.
        """
        self._settings = settings
        self._secret_manager = secret_manager

        # In-memory document store — always available.
        self._documents: dict[str, dict[str, Any]] = {}

        self._vector_db_url: str = str(
            getattr(settings, "VECTOR_DB_URL", "") or ""
        ).strip()

        self._external_configured: bool = bool(self._vector_db_url)
        self._initialised: bool = False

        if self._external_configured:
            logger.info(
                "VectorDB | external backend configured at %s "
                "— using in-memory fallback (external client not yet wired).",
                self._vector_db_url,
            )
        else:
            logger.info(
                "VectorDB | VECTOR_DB_URL not set — using in-memory store."
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def initialize(self) -> bool:
        """
        Prepare the vector store for use.

        For the in-memory backend this is a lightweight no-op that marks
        the store as initialised. For future external backends this is the
        hook for connection setup and collection creation.

        Returns:
            ``True`` always — in-memory initialisation cannot fail.

        Example::

            success = db.initialize()
            assert success
        """
        try:
            self._documents.clear()
            self._initialised = True
            logger.info(
                "VectorDB.initialize | mode=%s | status=ready",
                "external(fallback)" if self._external_configured else "in-memory",
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("VectorDB.initialize | unexpected error: %s", exc)
            return False

    def upsert(
        self,
        document_id: str,
        embedding: list[float],
        metadata: dict[str, Any],
    ) -> bool:
        """
        Insert or update a document vector in the store.

        If ``document_id`` already exists its embedding and metadata are
        replaced atomically.

        Args:
            document_id: Unique string identifier for the document.
            embedding:   Dense vector as a list of floats.
            metadata:    Arbitrary key-value payload stored alongside the vector
                         (e.g. ``{"source": "report", "incident_id": "INC-42"}``).

        Returns:
            ``True`` on success; ``False`` on any failure.

        Example::

            db.upsert(
                document_id="chunk-001",
                embedding=[0.1, 0.9, 0.3],
                metadata={"source": "post_mortem", "date": "2025-01-15"},
            )
        """
        if not document_id or not document_id.strip():
            logger.warning("VectorDB.upsert | empty document_id — skipping.")
            return False

        if not embedding:
            logger.warning(
                "VectorDB.upsert | document_id=%r | empty embedding — skipping.",
                document_id,
            )
            return False

        try:
            self._documents[document_id] = {
                "embedding": list(embedding),
                "metadata":  dict(metadata) if metadata else {},
            }
            logger.debug(
                "VectorDB.upsert | document_id=%r | dim=%d | total_docs=%d",
                document_id, len(embedding), len(self._documents),
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "VectorDB.upsert | document_id=%r | error=%s", document_id, exc
            )
            return False

    def search(
        self,
        embedding: list[float],
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        """
        Return the ``top_k`` most similar documents to ``embedding``.

        Similarity is computed as cosine similarity using stdlib ``math``
        only — no numpy or external vector library is required.

        Args:
            embedding: Query vector as a list of floats. Must be the same
                       dimensionality as stored embeddings.
            top_k:     Maximum number of results to return. Defaults to ``5``.

        Returns:
            List of result dicts sorted by similarity descending::

                [
                    {
                        "document_id": str,
                        "score":       float,   # cosine similarity [0, 1]
                        "metadata":    dict,
                    },
                    ...
                ]

            Empty list when the store is empty, ``embedding`` is empty, or
            any error occurs.

        Example::

            results = db.search(embedding=[0.1, 0.8, 0.3], top_k=3)
            for r in results:
                print(r["document_id"], r["score"])
        """
        if not embedding:
            logger.warning("VectorDB.search | empty query embedding — returning [].")
            return []

        if not self._documents:
            logger.debug("VectorDB.search | store is empty — returning [].")
            return []

        if top_k < 1:
            logger.warning(
                "VectorDB.search | top_k=%d < 1 — returning [].", top_k
            )
            return []

        try:
            scored: list[dict[str, Any]] = []

            for doc_id, doc in self._documents.items():
                stored_vec: list[float] = doc["embedding"]
                score = self._cosine_similarity(embedding, stored_vec)
                if score is None:
                    continue
                scored.append({
                    "document_id": doc_id,
                    "score":       round(score, 6),
                    "metadata":    dict(doc["metadata"]),
                })

            scored.sort(key=lambda x: x["score"], reverse=True)
            results = scored[:top_k]

            logger.debug(
                "VectorDB.search | query_dim=%d | candidates=%d | returned=%d",
                len(embedding), len(scored), len(results),
            )
            return results

        except Exception as exc:  # noqa: BLE001
            logger.error("VectorDB.search | error=%s", exc)
            return []

    def delete(self, document_id: str) -> bool:
        """
        Remove a document from the store by ID.

        Args:
            document_id: ID of the document to remove.

        Returns:
            ``True`` if the document was found and deleted;
            ``False`` if it was not found or an error occurred.

        Example::

            removed = db.delete("chunk-001")
        """
        if not document_id or not document_id.strip():
            logger.warning("VectorDB.delete | empty document_id — skipping.")
            return False

        try:
            if document_id in self._documents:
                del self._documents[document_id]
                logger.debug(
                    "VectorDB.delete | document_id=%r | deleted | total_docs=%d",
                    document_id, len(self._documents),
                )
                return True

            logger.warning(
                "VectorDB.delete | document_id=%r | not found.", document_id
            )
            return False

        except Exception as exc:  # noqa: BLE001
            logger.error(
                "VectorDB.delete | document_id=%r | error=%s", document_id, exc
            )
            return False

    def count(self) -> int:
        """
        Return the number of documents currently in the store.

        Returns:
            Non-negative integer document count. Returns ``0`` on error.

        Example::

            print(f"{db.count()} documents indexed.")
        """
        try:
            n = len(self._documents)
            logger.debug("VectorDB.count | count=%d", n)
            return n
        except Exception as exc:  # noqa: BLE001
            logger.error("VectorDB.count | error=%s", exc)
            return 0

    def health_check(self) -> dict[str, str]:
        """
        Return the health status of the vector store.

        The store is ``"healthy"`` when the internal document dict is
        accessible. A ``"degraded"`` status is returned only if an
        unexpected error prevents the check.

        Returns:
            Dict with ``"service"`` and ``"status"`` keys::

                {"service": "vector_db", "status": "healthy"}

            or::

                {"service": "vector_db", "status": "degraded"}

        Example::

            status = db.health_check()
            assert status["status"] == "healthy"
        """
        try:
            _ = len(self._documents)
            mode = "external(fallback)" if self._external_configured else "in-memory"
            logger.debug("VectorDB.health_check | status=healthy | mode=%s", mode)
            return {"service": "vector_db", "status": "healthy"}
        except Exception as exc:  # noqa: BLE001
            logger.error("VectorDB.health_check | error=%s", exc)
            return {"service": "vector_db", "status": "degraded"}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _cosine_similarity(
        vec_a: list[float],
        vec_b: list[float],
    ) -> float | None:
        """
        Compute the cosine similarity between two vectors using ``math`` only.

        Returns ``None`` when similarity cannot be computed (e.g. dimension
        mismatch, zero-magnitude vector).

        Args:
            vec_a: First vector.
            vec_b: Second vector.

        Returns:
            Cosine similarity in ``[-1, 1]``, or ``None`` on invalid input.
        """
        if len(vec_a) != len(vec_b):
            return None

        dot: float = 0.0
        mag_a: float = 0.0
        mag_b: float = 0.0

        for a, b in zip(vec_a, vec_b):
            dot   += a * b
            mag_a += a * a
            mag_b += b * b

        mag_a = math.sqrt(mag_a)
        mag_b = math.sqrt(mag_b)

        if mag_a == 0.0 or mag_b == 0.0:
            return None

        return dot / (mag_a * mag_b)

    def __repr__(self) -> str:
        return (
            f"VectorDB("
            f"mode={'external(fallback)' if self._external_configured else 'in-memory'}, "
            f"docs={len(self._documents)}, "
            f"initialised={self._initialised})"
        )