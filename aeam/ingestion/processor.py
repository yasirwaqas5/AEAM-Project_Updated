"""
aeam/ingestion/processor.py

Real ingestion job processor (Phase B1.3 — Ingestion Processing Pipeline).

Replaces the Phase B1.2 :class:`~aeam.ingestion.worker.PlaceholderJobProcessor`.
For each claimed :class:`~aeam.registry.models.IngestionJob` it runs the
end-to-end path that turns a stored upload into retrievable knowledge, reusing
existing components and adding no parallel infrastructure::

    blob_store.get(content_hash)                    # B1.1 storage
      -> extract_text(bytes, category)              # B1.3 (the only new capability)
      -> IngestionPipeline.ingest_document(text)    # existing RAG pipeline: chunk/embed/index -> Qdrant
      -> DocumentRepository / VersionRepository      # B1.1 registry rows
      -> job.parent = document, worker marks DONE

The Document + active Version rows are created at upload time (ingress API); this
processor drives the Document through its designed lifecycle and finalises it::

    documents.status:   pending -> processing -> indexed        (or -> error)
    ingestion_jobs:     (worker: validating) -> extracting -> indexing -> (worker: done)
                                                     \\-> raise -> (worker: failed)

It makes NO retrieval calls, NO LLM calls, and NO Orchestrator calls, and it
does not re-implement chunking, embedding, or Qdrant access — those are
delegated to the already-built IngestionPipeline. It is a plain
:class:`~aeam.ingestion.worker.JobProcessor`, injected into the unchanged
:class:`~aeam.ingestion.worker.IngestionWorker`.
"""

from __future__ import annotations

import logging
from typing import Any

from aeam.agents.rag.ingestion_pipeline import IngestionPipeline
from aeam.ingestion.extraction import ExtractionError, extract_text
from aeam.integrations.database import DatabaseClient
from aeam.registry.models import AssetStatus, IngestionJob, JobStatus, ParentType, _now_iso
from aeam.registry.repositories import DocumentRepository, VersionRepository
from aeam.registry.repositories import IngestionJobRepository
from aeam.storage.blob_store import BlobStore

logger = logging.getLogger(__name__)


class ProcessingError(Exception):
    """
    Raised for a structural problem processing a job (e.g. its document row is
    missing) — as opposed to an extraction failure. Carries a stable ``reason``
    so the recorded job error is greppable.
    """

    def __init__(self, reason: str, detail: str) -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(detail)


class DocumentIngestJobProcessor:
    """
    Processes ``ingest`` jobs for uploaded documents.

    Args:
        blob_store:          Content-addressable store holding the original bytes
                             (``container.blob_store``).
        ingestion_pipeline:  The already-constructed RAG
                             :class:`~aeam.agents.rag.ingestion_pipeline.IngestionPipeline`
                             (same instance used at startup — no second embedding
                             model load, no second Qdrant client).
        db:                  Shared :class:`~aeam.integrations.database.DatabaseClient`,
                             used to build the document/version repositories.

    Raises:
        ValueError: If any dependency is ``None``.
    """

    def __init__(
        self,
        blob_store: BlobStore,
        ingestion_pipeline: IngestionPipeline,
        db: DatabaseClient,
    ) -> None:
        if blob_store is None:
            raise ValueError("blob_store must not be None.")
        if ingestion_pipeline is None:
            raise ValueError("ingestion_pipeline must not be None.")
        if db is None:
            raise ValueError("db must not be None.")
        self._blob_store = blob_store
        self._pipeline = ingestion_pipeline
        self._doc_repo = DocumentRepository(db)
        self._version_repo = VersionRepository(db)

    # ------------------------------------------------------------------
    # JobProcessor protocol
    # ------------------------------------------------------------------

    def __call__(self, job: IngestionJob, job_repo: IngestionJobRepository) -> None:
        """
        Process one claimed job. Returns normally on success (worker marks the
        job DONE); raises on failure (worker marks it FAILED) after flagging the
        document as ``error``.
        """
        doc = self._load_document(job)

        # Idempotent no-op: identical bytes were already fully indexed (e.g. a
        # re-upload). The blob and job were deduplicated upstream; complete the
        # job without re-embedding.
        if doc.status == AssetStatus.INDEXED:
            logger.info(
                "DocumentIngestJobProcessor | job_id=%s | doc_id=%s already indexed — "
                "deduplicated, no re-embedding", job.job_id, doc.doc_id,
            )
            job_repo.update_progress(
                job.job_id, progress=100, stage="content already indexed — deduplicated",
            )
            return

        try:
            self._process(job, job_repo, doc)
        except Exception:
            # Keep the document consistent with the failed job before the worker
            # records the failure. set_status also bumps updated_at.
            self._doc_repo.set_status(doc.doc_id, AssetStatus.ERROR)
            raise

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _process(self, job: IngestionJob, job_repo: IngestionJobRepository, doc: Any) -> None:
        category = doc.doc_type or ""
        version = self._version_repo.get_active(ParentType.DOCUMENT, doc.doc_id)
        version_id = version.version_id if version else None

        # --- EXTRACTING -------------------------------------------------
        job_repo.update_progress(
            job.job_id, status=JobStatus.EXTRACTING, progress=25,
            stage=f"extracting text ({category or 'unknown'})",
        )
        self._doc_repo.set_status(doc.doc_id, AssetStatus.PROCESSING)

        data = self._blob_store.get(job.content_hash)
        try:
            extracted = extract_text(data, category=category, filename=doc.origin_path)
        except ExtractionError:
            # Propagate as-is: its stable `reason`/`detail` become the job error.
            raise

        # --- INDEXING (delegated to the existing RAG IngestionPipeline) --
        job_repo.update_progress(
            job.job_id, status=JobStatus.INDEXING, progress=60,
            stage="chunking, embedding, indexing",
        )

        metadata: dict[str, Any] = {
            # Required by IngestionPipeline.ingest_document.
            "source": doc.origin_path or doc.title or "upload",
            "date": doc.created_at,
            "doc_type": category or "document",
            # Traceability + future filtered delete/reindex by document.
            "doc_id": doc.doc_id,
            "version_id": version_id,
            "job_id": job.job_id,
            "title": doc.title,
            "content_hash": job.content_hash,
        }
        result = self._pipeline.ingest_document(text=extracted.text, metadata=metadata)
        chunk_ids: list[str] = list(result.get("chunk_ids", []))
        chunk_count = len(chunk_ids)

        # --- Finalise registry rows -------------------------------------
        if version_id is not None:
            # Store the Qdrant point IDs so this version can be cleanly deleted
            # or reindexed later.
            self._version_repo.update(version_id, {"chunk_ids": chunk_ids})

        self._doc_repo.update(doc.doc_id, {
            "status": AssetStatus.INDEXED,
            "chunk_count": chunk_count,
            "current_version": 1,
            "updated_at": _now_iso(),
        })

        job_repo.update_progress(
            job.job_id, progress=95,
            stage=f"indexed {chunk_count} chunk(s) into '{result.get('collection')}'",
        )
        logger.info(
            "DocumentIngestJobProcessor | job_id=%s | doc_id=%s | category=%s | "
            "chunks=%d | detail=%s",
            job.job_id, doc.doc_id, category, chunk_count, extracted.detail,
        )

    def _load_document(self, job: IngestionJob) -> Any:
        if job.parent_type != ParentType.DOCUMENT or not job.parent_id:
            raise ProcessingError(
                "missing_document_link",
                f"Job {job.job_id} is not linked to a document "
                f"(parent_type={job.parent_type!r}, parent_id={job.parent_id!r}).",
            )
        doc = self._doc_repo.get(job.parent_id)
        if doc is None:
            raise ProcessingError(
                "document_not_found",
                f"Job {job.job_id} references document {job.parent_id!r}, which does not exist.",
            )
        return doc

    def __repr__(self) -> str:
        return f"DocumentIngestJobProcessor(collection={self._pipeline.collection!r})"
