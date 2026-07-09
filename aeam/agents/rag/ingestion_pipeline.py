"""
aeam/agents/rag/ingestion_pipeline.py

Offline ingestion pipeline for unstructured documents in the AEAM RAG system.

Orchestrates the full offline path: text → chunks → embeddings → Qdrant.
This module is strictly a data preparation utility:
- No LLM calls.
- No anomaly detection.
- No decision logic.
- No Orchestrator interaction.

Dependencies are injected; the pipeline itself is stateless between calls.

Phase 4 constraints enforced:
- Vector dimension:  384  (all-MiniLM-L6-v2)
- Distance metric:   cosine
- Vector DB:         Qdrant via official qdrant-client
"""

from __future__ import annotations

import logging
from aeam.monitoring.logging_config import get_logger
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels
from qdrant_client.http.exceptions import UnexpectedResponse

from aeam.agents.rag.chunking import TextChunker
from aeam.integrations.embedding_service import EmbeddingService

logger = get_logger(__name__, agent="rag")

# Phase 4 spec constants.
VECTOR_DIMENSION: int = 384
DISTANCE_METRIC: qmodels.Distance = qmodels.Distance.COSINE

# Required metadata fields for every ingested document.
_REQUIRED_METADATA_FIELDS: frozenset[str] = frozenset(
    {"source", "date", "doc_type"}
)


class IngestionPipeline:
    """
    Offline ingestion pipeline: text → chunks → embeddings → Qdrant.

    Accepts a raw document string and a metadata dict, chunks the text using
    :class:`~aeam.agents.rag.chunking.TextChunker`, generates 384-dimensional
    embeddings via :class:`~aeam.integrations.embedding_service.EmbeddingService`,
    and upserts each chunk as a Qdrant point with its full metadata payload.

    The target Qdrant collection is created automatically if it does not
    already exist (idempotent).

    This class:
    - Makes no LLM calls.
    - Performs no anomaly detection.
    - Makes no decisions.
    - Does not interact with the Orchestrator.

    Args:
        embedding_service: Pre-loaded :class:`~aeam.integrations.embedding_service.EmbeddingService`
                           instance (model loaded once externally).
        qdrant_client:     Connected :class:`qdrant_client.QdrantClient` instance.
        chunker:           :class:`~aeam.agents.rag.chunking.TextChunker` instance.
                           Defaults to ``TextChunker()`` (sentence strategy,
                           chunk_size=300, overlap=50).
        collection:        Qdrant collection name to ingest into.
                           Defaults to ``"aeam_documents"``.

    Raises:
        ValueError: If ``embedding_service`` or ``qdrant_client`` is ``None``.

    Example::

        from qdrant_client import QdrantClient
        from aeam.integrations.embedding_service import EmbeddingService
        from aeam.agents.rag.chunking import TextChunker
        from aeam.agents.rag.ingestion_pipeline import IngestionPipeline

        pipeline = IngestionPipeline(
            embedding_service=EmbeddingService(),
            qdrant_client=QdrantClient(url="http://localhost:6333"),
        )
        result = pipeline.ingest_document(
            text="The CPU spiked due to a runaway thread in the payment service.",
            metadata={
                "source": "post_mortem_2024_01",
                "date": "2024-01-15",
                "doc_type": "incident_report",
            },
        )
    """

    def __init__(
        self,
        embedding_service: EmbeddingService,
        qdrant_client: QdrantClient,
        chunker: TextChunker | None = None,
        collection: str = "aeam_documents",
    ) -> None:
        """
        Initialise the ingestion pipeline.

        Args:
            embedding_service: Loaded embedding model wrapper. Must not be None.
            qdrant_client:     Connected Qdrant client. Must not be None.
            chunker:           Text chunker. Defaults to
                               ``TextChunker(strategy="sentence", chunk_size=300,
                               overlap=50)`` if not provided.
            collection:        Target Qdrant collection name. Must not be empty.

        Raises:
            ValueError: If ``embedding_service`` or ``qdrant_client`` is None,
                        or if ``collection`` is empty / whitespace-only.
        """
        if embedding_service is None:
            raise ValueError("embedding_service must not be None.")
        if qdrant_client is None:
            raise ValueError("qdrant_client must not be None.")
        if not collection or not collection.strip():
            raise ValueError("collection must be a non-empty string.")

        self._embed: EmbeddingService = embedding_service
        self._qdrant: QdrantClient = qdrant_client
        self._chunker: TextChunker = chunker or TextChunker(
            chunk_size=300,
            overlap=50,
            strategy="sentence",
        )
        self._collection: str = collection.strip()

        # Ensure the target collection exists before any ingest call.
        self._ensure_collection()

    def _split_markdown_into_sections(self, text: str) -> list[tuple[str, str]]:
        """
        Split markdown text into sections based on '## ' headers.
        Returns a list of (section_title, section_text) tuples.
        If no '## ' headers are found, returns an empty list.
        """
        if not text.strip():
            return []
        # Split by lines starting with '## ' (markdown level 2 header)
        parts = re.split(r'\n## ', text)
        # The first part is the preamble (before any ## header)
        sections = []
        if parts[0].strip():
            # If there's preamble, treat it as a section with no title? We'll skip it for now.
            # Alternatively, we could use the first line as a title, but we'll ignore preamble.
            pass
        for part in parts[1:]:  # Skip preamble
            lines = part.splitlines()
            if not lines:
                continue
            # First line is the header
            header = lines[0].strip()
            # Rest is the content
            content = '\n'.join(lines[1:]).strip()
            if content:
                sections.append((header, content))
        return sections

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def ingest_document(
        self,
        text: str,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Chunk, embed, and upsert a document into Qdrant.

        Required metadata fields:
        - ``source``   — origin of the document (e.g. file path, report name).
        - ``date``     — document date as a string (ISO-8601 recommended).
        - ``doc_type`` — category of document (e.g. ``"incident_report"``).

        Steps:
        1. Validate ``metadata`` for required fields.
        2. Chunk ``text`` using the configured :class:`~aeam.agents.rag.chunking.TextChunker`.
        3. Encode all chunk texts in a single batch call to the embedding model.
        4. Build Qdrant ``PointStruct`` objects, each carrying:
           - A deterministic UUID point ID derived from the chunk's ``chunk_id``.
           - The 384-dim embedding vector.
           - A payload containing: ``text``, ``source``, ``date``,
             ``doc_type``, ``chunk_id``, ``chunk_index``, ``chunk_total``,
             ``ingested_at``, and all additional caller-supplied metadata.
        5. Batch-upsert all points into Qdrant.

        Args:
            text:     Raw document text. Must not be empty.
            metadata: Document-level metadata. Must include ``source``,
                      ``date``, and ``doc_type``. May include any additional
                      fields (e.g. ``incident_id``, ``author``).

        Returns:
            A summary dict::

                {
                    "collection":    str,   # target collection name
                    "chunks_total":  int,   # number of chunks created
                    "chunks_upserted": int, # number of points written to Qdrant
                    "doc_type":      str,
                    "source":        str,
                    "date":          str,
                }

        Raises:
            ValueError:  If ``text`` is empty, or required metadata fields are
                         missing.
            Exception:   Propagates any Qdrant client error without wrapping,
                         preserving the original traceback.

        Example::

            result = pipeline.ingest_document(
                text="Post-mortem: CPU spiked on web-01 at 14:32 UTC...",
                metadata={
                    "source":   "post_mortem_2024_01",
                    "date":     "2024-01-15",
                    "doc_type": "incident_report",
                    "author":   "ops-team",
                },
            )
            # result["chunks_upserted"] → 3
        """
        if not text or not text.strip():
            raise ValueError("text must be a non-empty string to ingest.")

        self._validate_metadata(metadata)

        ingested_at = datetime.now(tz=timezone.utc).isoformat()

        # Step 2: chunk.
        chunks = self._chunker.chunk_text(text=text, metadata=metadata)

        if not chunks:
            logger.warning(
                "ingest_document | text produced 0 chunks after splitting. "
                "source=%r", metadata.get("source"),
            )
            return {
                "collection": self._collection,
                "chunks_total": 0,
                "chunks_upserted": 0,
                "doc_type": metadata.get("doc_type"),
                "source": metadata.get("source"),
                "date": metadata.get("date"),
            }

        # Step 3: batch-encode all chunk texts.
        chunk_texts = [c["text"] for c in chunks]
        vectors: list[list[float]] = self._embed.encode_batch(chunk_texts)

        logger.info(
            "ingest_document | source=%r | doc_type=%r | chunks=%d",
            metadata.get("source"), metadata.get("doc_type"), len(chunks),
        )

        # Step 4: build Qdrant points.
        points: list[qmodels.PointStruct] = []

        for chunk, vector in zip(chunks, vectors):
            point_id = self._chunk_id_to_uuid(chunk["chunk_id"])

            payload: dict[str, Any] = {
                # Guaranteed fields from the spec.
                "text":        chunk["text"],
                "source":      metadata["source"],
                "date":        metadata["date"],
                "doc_type":    metadata["doc_type"],
                # Chunk-level positional fields.
                "chunk_id":    chunk["chunk_id"],
                "chunk_index": chunk["metadata"]["chunk_index"],
                "chunk_total": chunk["metadata"]["chunk_total"],
                # Audit field.
                "ingested_at": ingested_at,
            }

            # Merge all additional caller-supplied metadata (non-reserved keys).
            _reserved = {"text", "source", "date", "doc_type",
                         "chunk_id", "chunk_index", "chunk_total", "ingested_at"}
            for key, value in metadata.items():
                if key not in _reserved:
                    payload[key] = value

            points.append(
                qmodels.PointStruct(
                    id=point_id,
                    vector=vector,
                    payload=payload,
                )
            )

        # Step 5: batch upsert.
        self._qdrant.upsert(
            collection_name=self._collection,
            points=points,
        )

        logger.info(
            "ingest_document | upserted %d points -> collection=%r",
            len(points), self._collection,
        )

        return {
            "collection":      self._collection,
            "chunks_total":    len(chunks),
            "chunks_upserted": len(points),
            "doc_type":        metadata["doc_type"],
            "source":          metadata["source"],
            "date":            metadata["date"],
        }

    def ingest_batch(
        self,
        documents: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """
        Ingest a batch of documents, returning one result dict per document.

        Each entry in ``documents`` must be a dict with keys:
        - ``"text"``     — raw document text.
        - ``"metadata"`` — document-level metadata dict (see :meth:`ingest_document`).

        Documents are ingested sequentially. A failure on any single document
        logs the error and records it in the result with a ``"error"`` key,
        then continues processing the remaining documents.

        Args:
            documents: List of ``{"text": str, "metadata": dict}`` dicts.

        Returns:
            List of result dicts (same length as ``documents``). Failed entries
            contain ``{"error": str, ...}`` instead of chunk counts.

        Raises:
            ValueError: If ``documents`` is empty.

        Example::

            results = pipeline.ingest_batch([
                {"text": "...", "metadata": {"source": "r1", "date": "2024-01", "doc_type": "report"}},
                {"text": "...", "metadata": {"source": "r2", "date": "2024-02", "doc_type": "report"}},
            ])
        """
        if not documents:
            raise ValueError("documents must be a non-empty list.")

        results: list[dict[str, Any]] = []

        for i, doc in enumerate(documents):
            try:
                result = self.ingest_document(
                    text=doc.get("text", ""),
                    metadata=doc.get("metadata", {}),
                )
                results.append(result)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "ingest_batch | doc[%d] failed | error=%s", i, exc
                )
                results.append({
                    "collection": self._collection,
                    "error": str(exc),
                    "doc_index": i,
                    "source": doc.get("metadata", {}).get("source"),
                })

        return results

    @property
    def collection(self) -> str:
        """The Qdrant collection this pipeline ingests into."""
        return self._collection

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_collection(self) -> None:
        """
        Create the target Qdrant collection if it does not already exist.

        Uses ``COSINE`` distance and dimension ``384`` (Phase 4 spec).
        Idempotent: a ``409 Conflict`` response from Qdrant is silently
        ignored.

        Raises:
            Exception: Propagates any Qdrant error other than 409.
        """
        try:
            self._qdrant.create_collection(
                collection_name=self._collection,
                vectors_config=qmodels.VectorParams(
                    size=VECTOR_DIMENSION,
                    distance=DISTANCE_METRIC,
                ),
                optimizers_config=qmodels.OptimizersConfigDiff(
                    indexing_threshold=1,
                ),
            )
            logger.info(
                "IngestionPipeline | collection created: %r", self._collection
            )
        except UnexpectedResponse as exc:
            if exc.status_code == 409:
                self._qdrant.update_collection(
                    collection_name=self._collection,
                    optimizers_config=qmodels.OptimizersConfigDiff(
                        indexing_threshold=1,
                    ),
                )
                logger.debug(
                    "IngestionPipeline | collection already exists: %r",
                    self._collection,
                )
            else:
                logger.error(
                    "_ensure_collection failed | collection=%r | error=%s",
                    self._collection, exc,
                )
                raise

    @staticmethod
    def _validate_metadata(metadata: dict[str, Any]) -> None:
        """
        Raise ``ValueError`` if any required metadata field is absent or blank.

        Required fields: ``source``, ``date``, ``doc_type``.

        Args:
            metadata: Caller-supplied metadata dict.

        Raises:
            ValueError: Lists all missing or blank required fields.
        """
        missing: list[str] = []
        for field in sorted(_REQUIRED_METADATA_FIELDS):
            value = metadata.get(field)
            if value is None or (isinstance(value, str) and not value.strip()):
                missing.append(field)

        if missing:
            raise ValueError(
                f"ingest_document() requires the following metadata fields, "
                f"which are missing or blank: {missing}. "
                f"Received keys: {sorted(metadata.keys())}."
            )

    @staticmethod
    def _chunk_id_to_uuid(chunk_id: str) -> str:
        """
        Convert a hex SHA-256 ``chunk_id`` to a UUID v5 string for Qdrant.

        Qdrant point IDs must be unsigned integers or UUID strings. Since
        ``chunk_id`` is a 64-char hex digest, we derive a UUID v5 from it
        using the DNS namespace for stability.

        Args:
            chunk_id: 64-character hex SHA-256 string from TextChunker.

        Returns:
            UUID string (e.g. ``"550e8400-e29b-41d4-a716-446655440000"``).
        """
        return str(uuid.uuid5(uuid.NAMESPACE_DNS, chunk_id))

    def __repr__(self) -> str:
        return (
            f"IngestionPipeline("
            f"collection={self._collection!r}, "
            f"chunker={self._chunker!r}, "
            f"embed={self._embed!r})"
        )
