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

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse

from aeam.ingestion.submission import (
    STRUCTURED_CATEGORIES,
    IngestionSubmitter,
    get_or_create_dataset,
    get_or_create_document,
    get_or_create_upload_source,
)
from aeam.ingestion.validation import IngestValidationError, validate_upload
from aeam.registry.models import (
    AssetStatus,
    Dataset,
    Document,
    IngestionJob,
    JobStatus,
    JobType,
    ParentType,
    SemanticDocType,
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

# Phase F7: the routing rule, the get-or-create helpers, and the upload-source
# helper now live in aeam.ingestion.submission so that HTTP uploads and the
# eight connectors share ONE definition of each. Re-exported here (unchanged
# names) because this module's public surface is unchanged and other call
# sites/tests import them from here.
_STRUCTURED_CATEGORIES = STRUCTURED_CATEGORIES
_get_or_create_document = get_or_create_document
_get_or_create_dataset = get_or_create_dataset
_get_or_create_upload_source = get_or_create_upload_source

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/ingest", tags=["Ingest"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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
async def upload_file(
    request: Request,
    file: UploadFile = File(...),
    doc_type: str | None = Form(
        default=None,
        description=(
            "Phase E12 (MOD-4/RAG-7): OPTIONAL declared SEMANTIC document "
            "type — one of SemanticDocType.ALL (e.g. 'runbook', "
            "'incident_report', 'post_mortem'). Distinct from the file "
            "FORMAT, which is detected from the upload and stored separately. "
            "Declaring 'runbook' is what lets the document earn retrieval's "
            "authoritative-source bonus; omitting it preserves the exact "
            "pre-E12 behaviour."
        ),
    ),
) -> JSONResponse:
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

    # Phase E12: validate the DECLARED semantic type against the closed
    # vocabulary before anything is stored. An unrecognised value is rejected
    # rather than persisted, because a typo'd 'runbok' would silently fail to
    # earn the very bonus the declaration exists to grant.
    semantic_type: str | None = None
    if doc_type is not None and doc_type.strip():
        semantic_type = doc_type.strip().lower()
        if semantic_type not in SemanticDocType.ALL:
            raise HTTPException(
                status_code=422,
                detail={
                    "reason": "unsupported_doc_type",
                    "detail": (
                        f"doc_type {doc_type!r} is not a recognised semantic document "
                        f"type. Must be one of {sorted(SemanticDocType.ALL)}."
                    ),
                },
            )

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

    # Phase F7: the store/dedup/register/enqueue sequence now lives in the
    # shared IngestionSubmitter, so an uploaded document and a
    # connector-fetched document travel the SAME path and are therefore
    # indistinguishable afterwards. Behaviour here is unchanged: the same
    # steps, in the same order, with the same dedup rules.
    submitter = IngestionSubmitter(db=container.db, blob_store=container.blob_store)
    source_id = get_or_create_upload_source(SourceRepository(container.db))
    result = submitter.submit(
        data,
        filename=file.filename,
        content_type=file.content_type,
        source_id=source_id,
        semantic_type=semantic_type,
        category=category,
    )

    if result.duplicate_of_content:
        return JSONResponse(status_code=202, content={
            **_job_to_dict(result.job),
            "duplicate_of_content": True,
            "asset_created": False,
            **_asset_id_keys(result.parent_type, result.parent_id),
            "blob_uri": result.blob_uri,
            "filename": file.filename,
            "category": category,
        })

    parent_type, parent_id = result.parent_type, result.parent_id
    asset_created = result.asset_created
    created = result.job
    job_id = result.job_id

    return JSONResponse(status_code=202, content={
        **_job_to_dict(created),
        "duplicate_of_content": False,
        # False when identical bytes already had a registered document/dataset
        # (reused, no duplicate); True when this upload registered a new asset.
        "asset_created": asset_created,
        **_asset_id_keys(parent_type, parent_id),
        "blob_uri": result.blob_uri,
        "filename": file.filename,
        "category": category,
        # Phase E12: echo back what was DECLARED (None when nothing was), so
        # the caller can see whether the classification landed rather than
        # having to re-fetch the document to find out.
        "semantic_type": semantic_type,
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
