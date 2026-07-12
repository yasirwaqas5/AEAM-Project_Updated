"""
aeam/api/ingest.py

Enterprise Ingress API (Phase B1.2 — Ingress API + Async Job System).

Accepts uploaded files, validates them, stores the original bytes via the
existing content-addressable BlobStore (Phase B1.1), and creates an
IngestionJob row for the background worker to pick up. Returns 202 Accepted
immediately — this endpoint does NOT parse, chunk, embed, or index anything;
that happens later, off the request thread, once a real JobProcessor exists.

Rules enforced:
- All state access via request.app.state.container (existing convention,
  matching trigger.py / system.py / retrieval_debug.py).
- No agent calls, no Orchestrator calls, no RAG calls.
- BlobStore.put() is idempotent — re-uploading identical bytes never
  duplicates storage. This layer additionally avoids creating a duplicate
  *job* for content that already has an in-flight job.
- Public within the existing security posture (no new auth model introduced;
  matches trigger.py's current access level).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse

from aeam.ingestion.validation import IngestValidationError, validate_upload
from aeam.registry.models import (
    AssetStatus,
    Dataset,
    Document,
    IngestionJob,
    JobStatus,
    JobType,
    ParentType,
    Source,
    SourceKind,
    SourceStatus,
    Version,
)
from aeam.registry.repositories import (
    DatasetRepository,
    DocumentRepository,
    IngestionJobRepository,
    SourceRepository,
    VersionRepository,
)

# Structured formats become first-class datasets (schema + metric columns);
# every other supported format is registered as a retrievable document.
# Categories come from aeam.ingestion.validation.SUPPORTED_EXTENSIONS values.
_STRUCTURED_CATEGORIES: frozenset[str] = frozenset({"csv", "excel"})

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/ingest", tags=["Ingest"])

_DEFAULT_UPLOAD_SOURCE_NAME = "Manual Upload"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_or_create_upload_source(source_repo: SourceRepository) -> str:
    """
    Return the source_id of the canonical 'Manual Upload' Source, creating it
    on first use.

    Phase B1.2 has no connectors yet — every direct upload is attributed to
    this one bootstrap Source (kind=upload) so ``ingestion_jobs.source_id``
    is always populated. Later connector phases add real Sources without
    touching this bootstrap.
    """
    for existing in source_repo.list_by_kind(SourceKind.UPLOAD):
        if existing.name == _DEFAULT_UPLOAD_SOURCE_NAME:
            return existing.source_id
    return source_repo.create(
        Source(name=_DEFAULT_UPLOAD_SOURCE_NAME, kind=SourceKind.UPLOAD, status=SourceStatus.ACTIVE)
    )


def _get_or_create_document(
    doc_repo: DocumentRepository,
    version_repo: VersionRepository,
    *,
    source_id: str,
    filename: str | None,
    category: str,
    content_hash: str,
    blob_uri: str,
) -> tuple[str, bool]:
    """
    Return ``(doc_id, created)`` for the document backing this upload.

    Content-addressed dedup at the document level: identical bytes (same
    ``content_hash``) always map to the same Document, so re-uploading a file
    never creates a duplicate document — it reuses the existing one (whatever
    its status), and the processor decides whether any work is needed.

    A brand-new document is created ``pending`` together with its first active
    Version (``version=1``), which records the BlobStore URI of the original.
    The background worker/processor advances it to ``processing`` → ``indexed``.
    """
    existing = doc_repo.get_by_content_hash(content_hash)
    if existing is not None:
        return existing.doc_id, False

    doc_id = doc_repo.create(
        Document(
            title=filename or "untitled",
            source_id=source_id,
            origin_path=filename,
            doc_type=category,
            content_hash=content_hash,
            status=AssetStatus.PENDING,
        )
    )
    version_repo.create(
        Version(
            parent_type=ParentType.DOCUMENT,
            parent_id=doc_id,
            version=1,
            content_hash=content_hash,
            blob_ref=blob_uri,
            is_active=True,
        )
    )
    return doc_id, True


def _get_or_create_dataset(
    dataset_repo: DatasetRepository,
    version_repo: VersionRepository,
    *,
    source_id: str,
    filename: str | None,
    content_hash: str,
    blob_uri: str,
) -> tuple[str, bool]:
    """
    Return ``(dataset_id, created)`` for the dataset backing a structured upload.

    Content-addressed dedup at the dataset level: the ``datasets`` table has no
    ``content_hash`` column (by B1.1 design), so dedup keys off the active
    Version's ``content_hash`` (indexed) — identical bytes reuse the existing
    dataset rather than creating a duplicate.

    A brand-new dataset is created ``pending`` with its first active Version
    (``version=1``) recording the BlobStore URI of the original. The background
    worker/processor infers its schema and advances it ``processing`` →
    ``indexed``. ``dataset.name`` is the filename — the processor derives the
    format (csv/excel) from its extension.
    """
    existing = version_repo.find_active_by_content_hash(ParentType.DATASET, content_hash)
    if existing is not None:
        return existing.parent_id, False

    dataset_id = dataset_repo.create(
        Dataset(
            name=filename or "untitled",
            source_id=source_id,
            status=AssetStatus.PENDING,
        )
    )
    version_repo.create(
        Version(
            parent_type=ParentType.DATASET,
            parent_id=dataset_id,
            version=1,
            content_hash=content_hash,
            blob_ref=blob_uri,
            is_active=True,
        )
    )
    return dataset_id, True


def _iso(value: Any) -> str | None:
    """
    Normalise a timestamp field to an ISO-8601 string for JSON responses.

    ``DatabaseClient.fetch_one``/``fetch_all`` return driver-native values:
    PostgreSQL/psycopg2 gives back real ``datetime`` objects for TIMESTAMP
    columns, while SQLite gives back the ISO string exactly as it was written
    (SQLite has no native timestamp type). Both must round-trip through JSON.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _job_to_dict(job: IngestionJob) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "source_id": job.source_id,
        "job_type": job.job_type,
        "status": job.status,
        "progress": job.progress,
        "stage": job.stage,
        "error": job.error,
        "content_hash": job.content_hash,
        "parent_type": job.parent_type,
        "parent_id": job.parent_id,
        "created_at": _iso(job.created_at),
        "updated_at": _iso(job.updated_at),
    }


def _asset_id_keys(parent_type: str | None, parent_id: str | None) -> dict[str, Any]:
    """
    Response keys identifying the registered asset.

    Always emits canonical ``asset_type``/``asset_id``; additionally emits the
    typed convenience key (``doc_id`` for documents — retained for B1.3
    backward compatibility — or ``dataset_id`` for datasets).
    """
    keys: dict[str, Any] = {"asset_type": parent_type, "asset_id": parent_id}
    if parent_type == ParentType.DOCUMENT:
        keys["doc_id"] = parent_id
    elif parent_type == ParentType.DATASET:
        keys["dataset_id"] = parent_id
    return keys


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------

@router.post(
    "/upload",
    status_code=202,
    summary="Upload a file and create an ingestion job",
    response_description="The created (or reused) ingestion job.",
)
async def upload_file(request: Request, file: UploadFile = File(...)) -> JSONResponse:
    """
    Validate, store, and register an uploaded file for later processing.

    Flow::

        UploadFile -> validate (name/size/extension/MIME)
                   -> BlobStore.put(bytes)               [content-addressed]
                   -> IngestionJobRepository.create(...)  [status=QUEUED]
                   -> 202 {job_id, status, ...}

    No parsing, chunking, embedding, or indexing happens here or as a result
    of this call in this phase — the created job sits QUEUED until the
    background :class:`~aeam.ingestion.worker.IngestionWorker` claims it.

    Returns:
        ``202`` — job created (or an existing in-flight job for identical
        content bytes was reused; see ``duplicate_of_content``).
        ``422`` — validation failure (missing/empty file, unsupported
        extension/MIME, or over the size limit).
    """
    container = request.app.state.container
    data = await file.read()

    try:
        category = validate_upload(file.filename, file.content_type, len(data))
    except IngestValidationError as exc:
        logger.warning(
            "upload_file | rejected | filename=%r | reason=%s | detail=%s",
            file.filename, exc.reason, exc.detail,
        )
        raise HTTPException(
            status_code=422, detail={"reason": exc.reason, "detail": exc.detail}
        ) from exc

    blob_ref = container.blob_store.put(data, content_type=file.content_type)

    job_repo = IngestionJobRepository(container.db)
    source_repo = SourceRepository(container.db)

    existing = job_repo.find_active_by_content_hash(blob_ref.content_hash)
    if existing is not None:
        logger.info(
            "upload_file | identical content already in flight — reusing "
            "job_id=%s | content_hash=%s",
            existing.job_id, blob_ref.content_hash,
        )
        return JSONResponse(status_code=202, content={
            **_job_to_dict(existing),
            "duplicate_of_content": True,
            "asset_created": False,
            **_asset_id_keys(existing.parent_type, existing.parent_id),
            "blob_uri": blob_ref.uri,
            "filename": file.filename,
            "category": category,
        })

    source_id = _get_or_create_upload_source(source_repo)
    version_repo = VersionRepository(container.db)

    # Structured formats (csv/excel) are registered as first-class datasets
    # (schema + metric columns); everything else becomes a retrievable document.
    if category in _STRUCTURED_CATEGORIES:
        parent_type = ParentType.DATASET
        parent_id, asset_created = _get_or_create_dataset(
            DatasetRepository(container.db),
            version_repo,
            source_id=source_id,
            filename=file.filename,
            content_hash=blob_ref.content_hash,
            blob_uri=blob_ref.uri,
        )
    else:
        parent_type = ParentType.DOCUMENT
        parent_id, asset_created = _get_or_create_document(
            DocumentRepository(container.db),
            version_repo,
            source_id=source_id,
            filename=file.filename,
            category=category,
            content_hash=blob_ref.content_hash,
            blob_uri=blob_ref.uri,
        )

    job = IngestionJob(
        job_type=JobType.INGEST,
        source_id=source_id,
        parent_type=parent_type,
        parent_id=parent_id,
        status=JobStatus.QUEUED,
        progress=0,
        stage=f"queued — {category} upload ({file.filename})",
        content_hash=blob_ref.content_hash,
    )
    job_id = job_repo.create(job)
    created = job_repo.get(job_id)

    logger.info(
        "upload_file | job_id=%s | %s=%s | created=%s | filename=%r | "
        "category=%s | size=%d | content_hash=%s",
        job_id, parent_type, parent_id, asset_created, file.filename, category,
        len(data), blob_ref.content_hash,
    )

    return JSONResponse(status_code=202, content={
        **_job_to_dict(created),
        "duplicate_of_content": False,
        # False when identical bytes already had a registered document/dataset
        # (reused, no duplicate); True when this upload registered a new asset.
        "asset_created": asset_created,
        **_asset_id_keys(parent_type, parent_id),
        "blob_uri": blob_ref.uri,
        "filename": file.filename,
        "category": category,
    })


# ---------------------------------------------------------------------------
# Job status API
# ---------------------------------------------------------------------------

@router.get("/jobs", summary="List ingestion jobs")
def list_jobs(
    request: Request,
    status: str | None = Query(default=None, description="Filter by job status."),
    limit: int = Query(default=100, ge=1, le=1000),
) -> JSONResponse:
    """List ingestion jobs, optionally filtered by status, newest-inclusive."""
    container = request.app.state.container
    job_repo = IngestionJobRepository(container.db)

    if status is not None:
        if status not in JobStatus.ALL:
            raise HTTPException(
                status_code=422,
                detail=f"invalid status {status!r}. Must be one of {sorted(JobStatus.ALL)}.",
            )
        jobs = job_repo.list_by_status(status)
    else:
        jobs = job_repo.list_all(limit=limit)

    return JSONResponse(status_code=200, content=[_job_to_dict(j) for j in jobs])


@router.get("/jobs/{job_id}", summary="Get one ingestion job")
def get_job(request: Request, job_id: str) -> JSONResponse:
    """Fetch a single ingestion job by id."""
    container = request.app.state.container
    job_repo = IngestionJobRepository(container.db)
    job = job_repo.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"No ingestion job with id {job_id!r}.")
    return JSONResponse(status_code=200, content=_job_to_dict(job))


@router.post("/jobs/{job_id}/cancel", summary="Cancel a queued ingestion job")
def cancel_job(request: Request, job_id: str) -> JSONResponse:
    """
    Cancel a job that has not yet been claimed by the worker.

    Only ``QUEUED`` jobs can be cancelled — once the worker has claimed a job
    (moved it to ``VALIDATING``) or it has reached a terminal state, this
    returns ``409``. Cancellation is a distinct terminal state from
    ``FAILED`` (see :data:`~aeam.registry.models.JobStatus.CANCELLED`) since
    no error occurred.
    """
    container = request.app.state.container
    job_repo = IngestionJobRepository(container.db)
    job = job_repo.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"No ingestion job with id {job_id!r}.")
    if job.status != JobStatus.QUEUED:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Job {job_id} cannot be cancelled from status '{job.status}' "
                f"(only a QUEUED job can be cancelled)."
            ),
        )
    job_repo.update_progress(job_id, status=JobStatus.CANCELLED, stage="cancelled by operator")
    updated = job_repo.get(job_id)
    logger.info("cancel_job | job_id=%s cancelled", job_id)
    return JSONResponse(status_code=200, content=_job_to_dict(updated))


@router.post("/jobs/{job_id}/retry", summary="Retry a failed ingestion job")
def retry_job(request: Request, job_id: str) -> JSONResponse:
    """
    Requeue a ``FAILED`` job for another attempt.

    Resets ``status`` to ``QUEUED``, ``progress`` to 0, clears ``error``, and
    updates ``stage`` — the background worker will pick it up on its next
    poll like any other queued job. Only ``FAILED`` jobs can be retried
    (``409`` otherwise).
    """
    container = request.app.state.container
    job_repo = IngestionJobRepository(container.db)
    job = job_repo.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"No ingestion job with id {job_id!r}.")
    if job.status != JobStatus.FAILED:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Job {job_id} cannot be retried from status '{job.status}' "
                f"(only a FAILED job can be retried)."
            ),
        )
    job_repo.update_progress(job_id, status=JobStatus.QUEUED, progress=0, stage="requeued for retry")
    job_repo.update(job_id, {"error": None})  # update_progress() only sets error when non-None
    updated = job_repo.get(job_id)
    logger.info("retry_job | job_id=%s requeued", job_id)
    return JSONResponse(status_code=200, content=_job_to_dict(updated))
