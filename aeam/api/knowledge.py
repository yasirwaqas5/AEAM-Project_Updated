"""
aeam/api/knowledge.py

Knowledge Center API (Phases B1.6 + full Enterprise Knowledge Center pass).

Exposes the existing Enterprise Data Layer registries (B1.1-B1.5) — Documents,
Datasets, Schemas, Versions — over a thin REST surface, so the Enterprise UI
Shell's Knowledge Center page can display and manage what has already been
ingested. Ingestion (upload, job polling) is NOT re-exposed here: it already
has a full API in ``aeam.api.ingest`` — reused as-is, never duplicated.

Rules enforced (mirrors every other API module in this package):
- All state access via request.app.state.container.
- No new repository methods and no ingestion/extraction logic is
  re-implemented here — every query, blob read, extraction, and Qdrant call
  below composes an object that already exists elsewhere
  (``DocumentRepository``/``DatasetRepository``/``SchemaRepository``/
  ``VersionRepository``/``SourceRepository``/``IngestionJobRepository``,
  ``BlobStore``, ``extract_text``/``read_primary_table``, the shared
  ``qdrant_client``). "Search" is a basic in-memory substring filter over an
  already-fetched list — not a new SQL query.
- DELETE defaults to REGISTRY-ONLY (unchanged from B1.6: byte-identical
  behaviour when ``purge`` is omitted). Passing ``?purge=true`` additionally
  removes the document's Qdrant vectors (via the version's already-stored
  ``chunk_ids``) and the underlying BlobStore bytes — but only when no other
  live document/dataset still references that content hash (BlobStore is
  content-addressed and deduplicated; see ``_content_hash_still_referenced``).
- Re-index reuses the existing, UNMODIFIED ``IngestionWorker`` /
  ``RoutingJobProcessor`` / ``Document``/``DatasetIngestJobProcessor``: it
  only resets the asset's status to ``pending`` and queues a new
  ``IngestionJob`` — the exact same processing path a fresh upload takes.
  No processor branches on ``job_type``, so this needed zero ingestion
  changes (chunking is deterministic and Qdrant upsert is idempotent, so
  re-processing identical bytes safely overwrites the same vector points).
- Preview reuses ``extract_text``/``read_primary_table`` directly against
  the already-stored blob bytes — read-only, no new parsing logic.

Phase E12 (Knowledge, Policy & Memory Governance) adds two things here and
nothing else:

- **Policy lifecycle reads** under ``/policies`` — list and filter extracted
  policies by their new ``status``. Read-only, mapped to ``documents:search``
  like the rest of this router.
- **A privileged curation namespace** under ``/curate`` — policy status
  transitions and Enterprise Memory expunge/correct. These are the only
  WRITE operations this phase introduces, and they are deliberately grouped
  under one path prefix so ``_ENDPOINT_RBAC_MAP`` can guard the whole
  namespace with ``admin:config`` in a single, longest-prefix-first entry
  (SEC-7: curation is privileged). Every one of them writes an audit record
  through the SAME :class:`~aeam.security.audit_logger.AuditLogger` the
  security middleware already uses (MEM-4: who, why, when) — no second audit
  mechanism.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from aeam.ingestion.extraction import ExtractionError, UnsupportedCategoryError, extract_text
from aeam.ingestion.schema_inference import SchemaInferenceError, read_primary_table
from aeam.ingestion.validation import SUPPORTED_EXTENSIONS
from aeam.registry.models import (
    AssetStatus, Dataset, Document, IngestionJob, JobStatus, JobType, ParentType,
    PolicyStatus, Schema, SemanticDocType, Version,
)
from aeam.registry.repositories import (
    DatasetRepository,
    DocumentRepository,
    IngestionJobRepository,
    PolicyRepository,
    SchemaRepository,
    SourceRepository,
    VersionRepository,
)

# Preview payload size caps — keep responses small; this is a peek, not a
# document viewer. No new business logic: just a slice of what extract_text /
# read_primary_table already returned.
_PREVIEW_TEXT_CHARS = 4000
_PREVIEW_ROW_LIMIT = 20

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/knowledge", tags=["Knowledge"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _iso(value: Any) -> str | None:
    """Normalise a timestamp field to an ISO-8601 string for JSON responses (mirrors ingest.py)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _source_name_map(source_repo: SourceRepository) -> dict[str, str]:
    """
    Fetch every source once and build a ``source_id -> name`` map.

    A single query shared across an entire list response, avoiding an N+1
    lookup per row. Degrades to an empty map on any failure so a Sources-table
    hiccup never breaks Documents/Datasets listing.
    """
    try:
        return {s.source_id: s.name for s in source_repo.list_all()}
    except Exception as exc:  # noqa: BLE001
        logger.warning("_source_name_map | failed, degrading to empty map: %s", exc)
        return {}


def _infer_file_type(name: str | None) -> str | None:
    """Best-effort file type from a name's extension, reusing the existing category vocabulary."""
    if not name or "." not in name:
        return None
    ext = name.rsplit(".", 1)[-1].strip().lower()
    return SUPPORTED_EXTENSIONS.get(ext)


def _matches(needle: str, *haystacks: str | None) -> bool:
    return any(needle in (h or "").lower() for h in haystacks)


def _content_hash_still_referenced(
    container: Any,
    content_hash: str,
    *,
    exclude_doc_id: str | None = None,
    exclude_dataset_id: str | None = None,
) -> bool:
    """
    ``True`` if any OTHER live document or dataset still references
    ``content_hash`` — BlobStore is content-addressed and deduplicated (B1.1),
    so identical bytes uploaded twice share one blob across two separate
    Document/Dataset rows. Purge-delete must never remove a blob another
    asset still needs.

    Uses ONLY existing repository methods (no new query added).

    Deliberately does NOT use ``DocumentRepository.get_by_content_hash`` —
    that method returns a single (first) match, which is correct for its
    original upload-dedup purpose but insufficient here: if the FIRST
    matching row happens to be the very document being deleted, a second
    document sharing the same hash would be invisible to it. Scans
    ``list_all()`` instead (already-existing, unmodified method) so every
    document sharing the hash is found, not just one.

    ``VersionRepository.find_active_by_content_hash`` similarly returns a
    single match — but a dataset delete must exclude ITS OWN active version
    from the check (it always "finds itself" otherwise, since that row still
    exists at check time), so the match's ``parent_id`` is compared against
    ``exclude_dataset_id`` rather than treating any match as a positive.
    Dataset-to-dataset hash collisions across DIFFERENT datasets cannot
    occur — ``_get_or_create_dataset`` in ``aeam.api.ingest`` already dedups
    by reusing the existing dataset for identical bytes — so this is only
    ever excluding self, never masking a real second dataset.
    """
    docs = DocumentRepository(container.db).list_all()
    if any(d.content_hash == content_hash and d.doc_id != exclude_doc_id for d in docs):
        return True

    other_dataset_version = VersionRepository(container.db).find_active_by_content_hash(
        ParentType.DATASET, content_hash
    )
    if other_dataset_version is not None and other_dataset_version.parent_id != exclude_dataset_id:
        return True

    return False


def _queue_reindex_job(
    container: Any, *, parent_type: str, parent_id: str, source_id: str | None, content_hash: str,
) -> str:
    """
    Reset-and-requeue: creates a new ``IngestionJob(job_type=REINDEX)`` for an
    already-registered asset, reusing ``IngestionJobRepository`` exactly as
    ``aeam.api.ingest``'s upload handler does. The existing, UNMODIFIED
    ``IngestionWorker``/``RoutingJobProcessor`` picks it up and reprocesses
    the asset through the same path a fresh upload takes.
    """
    job_repo = IngestionJobRepository(container.db)
    return job_repo.create(IngestionJob(
        job_type=JobType.REINDEX,
        source_id=source_id,
        parent_type=parent_type,
        parent_id=parent_id,
        status=JobStatus.QUEUED,
        progress=0,
        stage="queued for re-index",
        content_hash=content_hash,
    ))


def _preview_unavailable(reason: str, detail: str) -> dict[str, Any]:
    """Shape for an expected 'can't preview this' outcome — 200, not an error."""
    return {"available": False, "reason": reason, "detail": detail}


# ---------------------------------------------------------------------------
# Row -> dict projections
# ---------------------------------------------------------------------------

def _version_to_dict(version: Version) -> dict[str, Any]:
    return {
        "version_id": version.version_id,
        "parent_type": version.parent_type,
        "parent_id": version.parent_id,
        "version": version.version,
        "content_hash": version.content_hash,
        "blob_ref": version.blob_ref,
        "chunk_ids": version.chunk_ids,
        "chunk_count": len(version.chunk_ids or []),
        "created_at": _iso(version.created_at),
        "created_by": version.created_by,
        "is_active": bool(version.is_active),
    }


def _schema_to_dict(schema: Schema) -> dict[str, Any]:
    return {
        "schema_id": schema.schema_id,
        "object_name": schema.object_name,
        "source_id": schema.source_id,
        "columns": schema.columns,
        "relationships": schema.relationships,
        "discovered_at": _iso(schema.discovered_at),
    }


def _document_to_dict(
    doc: Document,
    source_names: dict[str, str],
    active_version: Version | None = None,
) -> dict[str, Any]:
    return {
        "doc_id": doc.doc_id,
        "title": doc.title,
        "source_id": doc.source_id,
        "source_name": source_names.get(doc.source_id) if doc.source_id else None,
        "origin_path": doc.origin_path,
        "file_type": doc.doc_type,
        "doc_type": doc.doc_type,
        # Phase E12 (MOD-4): the DECLARED semantic type, exposed separately
        # from the format above. Without this the console cannot tell a
        # classified document from an unclassified one and would report every
        # document as "not declared" regardless of what was actually stored.
        # ``None`` when nothing was declared — which is the honest answer, not
        # a fallback to the format.
        "semantic_type": getattr(doc, "semantic_type", None),
        "current_version": doc.current_version,
        "content_hash": doc.content_hash,
        "chunk_count": doc.chunk_count,
        "embedding_status": doc.status,  # B1.3: "indexed" means embedded into Qdrant
        "status": doc.status,
        "review_by": _iso(doc.review_by),
        "language": doc.language,
        "created_at": _iso(doc.created_at),
        "updated_at": _iso(doc.updated_at),
        "active_version": _version_to_dict(active_version) if active_version else None,
    }


def _dataset_to_dict(
    ds: Dataset,
    source_names: dict[str, str],
    schema: Schema | None = None,
    active_version: Version | None = None,
) -> dict[str, Any]:
    return {
        "dataset_id": ds.dataset_id,
        "name": ds.name,
        "source_id": ds.source_id,
        "source_name": source_names.get(ds.source_id) if ds.source_id else None,
        "file_type": _infer_file_type(ds.name),
        "schema_id": ds.schema_id,
        "row_count": ds.row_count,
        "metric_columns": ds.metric_columns,
        "refresh_schedule": ds.refresh_schedule,
        "processing_status": ds.status,  # B1.4: "indexed" means schema inferred + registered
        "status": ds.status,
        "last_ingested_at": _iso(ds.last_ingested_at),
        "created_at": _iso(ds.created_at),
        "schema": _schema_to_dict(schema) if schema else None,
        "active_version": _version_to_dict(active_version) if active_version else None,
    }


def _validate_status(status: str | None) -> None:
    if status is not None and status not in AssetStatus.ALL:
        raise HTTPException(
            status_code=422,
            detail=f"invalid status {status!r}. Must be one of {sorted(AssetStatus.ALL)}.",
        )


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------

@router.get("/documents", summary="List registered documents")
def list_documents(
    request: Request,
    status: str | None = Query(default=None, description="Filter by AssetStatus."),
    q: str | None = Query(default=None, description="Basic case-insensitive search over title/origin_path."),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0, description="Phase E6: rows to skip for pagination."),
) -> JSONResponse:
    """List documents, optionally filtered by status and/or a basic text search, newest first.

    Phase E6: ``offset`` paginates; the total registered-document count is
    returned in the ``X-Total-Count`` header. Defaults are unchanged
    (limit 200, offset 0) so existing callers are unaffected.
    """
    _validate_status(status)
    container = request.app.state.container
    doc_repo = DocumentRepository(container.db)

    # Bounded read (index-friendly); status filter path is already bounded
    # by its own query. offset only meaningful on the unfiltered list path.
    docs = doc_repo.list_by_status(status) if status else doc_repo.list_all(limit=limit, offset=offset)
    if q and q.strip():
        needle = q.strip().lower()
        docs = [d for d in docs if _matches(needle, d.title, d.origin_path)]
    docs.sort(key=lambda d: d.created_at or "", reverse=True)

    headers: dict[str, str] = {}
    try:
        headers["X-Total-Count"] = str(doc_repo.total())
    except Exception:  # noqa: BLE001 — header is advisory, never fail the read
        pass

    source_names = _source_name_map(SourceRepository(container.db))
    return JSONResponse(
        status_code=200,
        content=[_document_to_dict(d, source_names) for d in docs],
        headers=headers,
    )


@router.get("/documents/{doc_id}", summary="Get one document, with its active version")
def get_document(request: Request, doc_id: str) -> JSONResponse:
    container = request.app.state.container
    doc_repo = DocumentRepository(container.db)
    doc = doc_repo.get(doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail=f"No document with id {doc_id!r}.")

    active_version = VersionRepository(container.db).get_active(ParentType.DOCUMENT, doc_id)
    source_names = _source_name_map(SourceRepository(container.db))
    return JSONResponse(
        status_code=200, content=_document_to_dict(doc, source_names, active_version=active_version)
    )


@router.delete("/documents/{doc_id}", summary="Delete a document")
def delete_document(
    request: Request,
    doc_id: str,
    purge: bool = Query(default=False, description="Also purge Qdrant vectors and BlobStore bytes."),
) -> JSONResponse:
    """
    Delete a document's registry row and its version rows.

    By default REGISTRY-ONLY (unchanged from B1.6: identical behaviour to
    omitting ``purge``). With ``purge=true``, additionally deletes the
    document's Qdrant vector points (via each version's stored ``chunk_ids``)
    and its BlobStore bytes — unless another live document/dataset still
    references the same content hash (BlobStore dedup; see
    ``_content_hash_still_referenced``), in which case the blob is kept and
    ``blob_purged`` reports ``false``. A Qdrant/BlobStore failure during purge
    is logged and does not abort the registry delete — the response's
    ``vectors_purged``/``blob_purged`` flags report what actually happened.
    """
    container = request.app.state.container
    doc_repo = DocumentRepository(container.db)
    doc = doc_repo.get(doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail=f"No document with id {doc_id!r}.")

    version_repo = VersionRepository(container.db)
    versions = version_repo.list_for_parent(ParentType.DOCUMENT, doc_id)

    vectors_purged = False
    blob_purged = False
    if purge:
        chunk_ids = [cid for v in versions for cid in (v.chunk_ids or [])]
        qdrant_client = getattr(container, "qdrant_client", None)
        ingestion_pipeline = getattr(container, "ingestion_pipeline", None)
        if chunk_ids and qdrant_client is not None and ingestion_pipeline is not None:
            try:
                qdrant_client.delete(
                    collection_name=ingestion_pipeline.collection, points_selector=chunk_ids,
                )
                vectors_purged = True
            except Exception as exc:  # noqa: BLE001
                logger.error("delete_document | doc_id=%s | Qdrant purge failed: %s", doc_id, exc)

        blob_store = getattr(container, "blob_store", None)
        if blob_store is not None and doc.content_hash:
            if _content_hash_still_referenced(container, doc.content_hash, exclude_doc_id=doc_id):
                logger.info(
                    "delete_document | doc_id=%s | blob kept — content_hash still referenced", doc_id,
                )
            else:
                try:
                    blob_purged = blob_store.delete(doc.content_hash)
                except Exception as exc:  # noqa: BLE001
                    logger.error("delete_document | doc_id=%s | BlobStore purge failed: %s", doc_id, exc)

    for v in versions:
        version_repo.delete(v.version_id)
    doc_repo.delete(doc_id)

    logger.info(
        "delete_document | doc_id=%s | versions_deleted=%d | purge=%s | vectors_purged=%s | blob_purged=%s",
        doc_id, len(versions), purge, vectors_purged, blob_purged,
    )
    return JSONResponse(status_code=200, content={
        "deleted": True, "doc_id": doc_id, "versions_deleted": len(versions),
        "vectors_purged": vectors_purged, "blob_purged": blob_purged,
    })


# ---------------------------------------------------------------------------
# Datasets
# ---------------------------------------------------------------------------

@router.get("/datasets", summary="List registered datasets")
def list_datasets(
    request: Request,
    status: str | None = Query(default=None, description="Filter by AssetStatus."),
    q: str | None = Query(default=None, description="Basic case-insensitive search over name."),
    limit: int = Query(default=200, ge=1, le=1000),
) -> JSONResponse:
    """List datasets, optionally filtered by status and/or a basic text search, newest first."""
    _validate_status(status)
    container = request.app.state.container
    dataset_repo = DatasetRepository(container.db)

    datasets = dataset_repo.list_all(limit=limit)
    if status:
        datasets = [d for d in datasets if d.status == status]
    if q and q.strip():
        needle = q.strip().lower()
        datasets = [d for d in datasets if _matches(needle, d.name)]
    datasets.sort(key=lambda d: d.created_at or "", reverse=True)

    source_names = _source_name_map(SourceRepository(container.db))
    return JSONResponse(status_code=200, content=[_dataset_to_dict(d, source_names) for d in datasets])


@router.get("/datasets/{dataset_id}", summary="Get one dataset, with its schema and active version")
def get_dataset(request: Request, dataset_id: str) -> JSONResponse:
    container = request.app.state.container
    dataset_repo = DatasetRepository(container.db)
    ds = dataset_repo.get(dataset_id)
    if ds is None:
        raise HTTPException(status_code=404, detail=f"No dataset with id {dataset_id!r}.")

    schema = SchemaRepository(container.db).get(ds.schema_id) if ds.schema_id else None
    active_version = VersionRepository(container.db).get_active(ParentType.DATASET, dataset_id)
    source_names = _source_name_map(SourceRepository(container.db))
    return JSONResponse(
        status_code=200,
        content=_dataset_to_dict(ds, source_names, schema=schema, active_version=active_version),
    )


@router.delete("/datasets/{dataset_id}", summary="Delete a dataset")
def delete_dataset(
    request: Request,
    dataset_id: str,
    purge: bool = Query(default=False, description="Also purge BlobStore bytes."),
) -> JSONResponse:
    """
    Delete a dataset's registry row, its schema row, and its version rows.

    By default REGISTRY-ONLY (unchanged from B1.6). With ``purge=true``,
    additionally deletes the BlobStore bytes for each version's content
    hash — unless another live document still references that hash (see
    ``_content_hash_still_referenced``; dataset-to-dataset collisions cannot
    occur, upload-time dedup already prevents them). Datasets are never
    indexed into Qdrant (B1.4 registers structure, not prose), so there is no
    vector cleanup here — unlike documents. A dataset's schema is 1:1-owned
    by it in the current design, so cascading its deletion alongside the
    dataset is safe.
    """
    container = request.app.state.container
    dataset_repo = DatasetRepository(container.db)
    ds = dataset_repo.get(dataset_id)
    if ds is None:
        raise HTTPException(status_code=404, detail=f"No dataset with id {dataset_id!r}.")

    version_repo = VersionRepository(container.db)
    versions = version_repo.list_for_parent(ParentType.DATASET, dataset_id)

    blob_purged = False
    if purge:
        blob_store = getattr(container, "blob_store", None)
        content_hashes = {v.content_hash for v in versions if v.content_hash}
        if blob_store is not None:
            for content_hash in content_hashes:
                if _content_hash_still_referenced(
                    container, content_hash, exclude_dataset_id=dataset_id
                ):
                    logger.info(
                        "delete_dataset | dataset_id=%s | blob kept — content_hash still referenced",
                        dataset_id,
                    )
                    continue
                try:
                    blob_purged = blob_store.delete(content_hash) or blob_purged
                except Exception as exc:  # noqa: BLE001
                    logger.error("delete_dataset | dataset_id=%s | BlobStore purge failed: %s", dataset_id, exc)

    for v in versions:
        version_repo.delete(v.version_id)

    schema_deleted = False
    if ds.schema_id:
        SchemaRepository(container.db).delete(ds.schema_id)
        schema_deleted = True

    dataset_repo.delete(dataset_id)

    logger.info(
        "delete_dataset | dataset_id=%s | versions_deleted=%d | schema_deleted=%s | purge=%s | blob_purged=%s",
        dataset_id, len(versions), schema_deleted, purge, blob_purged,
    )
    return JSONResponse(status_code=200, content={
        "deleted": True, "dataset_id": dataset_id,
        "versions_deleted": len(versions), "schema_deleted": schema_deleted, "blob_purged": blob_purged,
    })


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

@router.get("/schemas", summary="List registered schemas")
def list_schemas(request: Request, limit: int = Query(default=200, ge=1, le=1000)) -> JSONResponse:
    container = request.app.state.container
    schemas = SchemaRepository(container.db).list_all(limit=limit)
    schemas.sort(key=lambda s: s.discovered_at or "", reverse=True)
    return JSONResponse(status_code=200, content=[_schema_to_dict(s) for s in schemas])


@router.get("/schemas/{schema_id}", summary="Get one schema")
def get_schema(request: Request, schema_id: str) -> JSONResponse:
    container = request.app.state.container
    schema = SchemaRepository(container.db).get(schema_id)
    if schema is None:
        raise HTTPException(status_code=404, detail=f"No schema with id {schema_id!r}.")
    return JSONResponse(status_code=200, content=_schema_to_dict(schema))


@router.delete("/schemas/{schema_id}", summary="Delete a schema (registry row only)")
def delete_schema(request: Request, schema_id: str) -> JSONResponse:
    """Delete a schema's registry row directly. Not wired to any frontend control yet."""
    container = request.app.state.container
    schema_repo = SchemaRepository(container.db)
    if schema_repo.get(schema_id) is None:
        raise HTTPException(status_code=404, detail=f"No schema with id {schema_id!r}.")
    schema_repo.delete(schema_id)
    logger.info("delete_schema | schema_id=%s", schema_id)
    return JSONResponse(status_code=200, content={"deleted": True, "schema_id": schema_id})


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------

@router.get("/versions", summary="List versions for a document or dataset")
def list_versions(
    request: Request,
    parent_type: str = Query(..., description="'document' or 'dataset'."),
    parent_id: str = Query(..., description="The owning document/dataset id."),
) -> JSONResponse:
    if parent_type not in ParentType.ALL:
        raise HTTPException(
            status_code=422,
            detail=f"invalid parent_type {parent_type!r}. Must be one of {sorted(ParentType.ALL)}.",
        )
    container = request.app.state.container
    versions = VersionRepository(container.db).list_for_parent(parent_type, parent_id)
    versions.sort(key=lambda v: v.version, reverse=True)
    return JSONResponse(status_code=200, content=[_version_to_dict(v) for v in versions])


@router.get("/versions/{version_id}", summary="Get one version")
def get_version(request: Request, version_id: str) -> JSONResponse:
    container = request.app.state.container
    version = VersionRepository(container.db).get(version_id)
    if version is None:
        raise HTTPException(status_code=404, detail=f"No version with id {version_id!r}.")
    return JSONResponse(status_code=200, content=_version_to_dict(version))


@router.delete("/versions/{version_id}", summary="Delete a version (registry row only)")
def delete_version(request: Request, version_id: str) -> JSONResponse:
    """Delete a version's registry row directly. Not wired to any frontend control yet."""
    container = request.app.state.container
    version_repo = VersionRepository(container.db)
    if version_repo.get(version_id) is None:
        raise HTTPException(status_code=404, detail=f"No version with id {version_id!r}.")
    version_repo.delete(version_id)
    logger.info("delete_version | version_id=%s", version_id)
    return JSONResponse(status_code=200, content={"deleted": True, "version_id": version_id})


# ---------------------------------------------------------------------------
# Actions — re-index, preview
# ---------------------------------------------------------------------------

@router.post("/documents/{doc_id}/reindex", summary="Re-queue a document for re-processing")
def reindex_document(request: Request, doc_id: str) -> JSONResponse:
    """
    Reset a document to ``pending`` and queue a new ``ingest`` worker pass over
    its already-stored content hash. See :func:`_queue_reindex_job` — this
    reuses the existing worker/processor unchanged.
    """
    container = request.app.state.container
    doc_repo = DocumentRepository(container.db)
    doc = doc_repo.get(doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail=f"No document with id {doc_id!r}.")

    active_version = VersionRepository(container.db).get_active(ParentType.DOCUMENT, doc_id)
    content_hash = (active_version.content_hash if active_version else None) or doc.content_hash
    if not content_hash:
        raise HTTPException(
            status_code=409,
            detail=f"Document {doc_id!r} has no recorded content hash to re-index.",
        )

    doc_repo.set_status(doc_id, AssetStatus.PENDING)
    job_id = _queue_reindex_job(
        container, parent_type=ParentType.DOCUMENT, parent_id=doc_id,
        source_id=doc.source_id, content_hash=content_hash,
    )
    logger.info("reindex_document | doc_id=%s | job_id=%s", doc_id, job_id)
    return JSONResponse(
        status_code=202,
        content={"job_id": job_id, "doc_id": doc_id, "parent_type": ParentType.DOCUMENT, "status": JobStatus.QUEUED},
    )


@router.post("/datasets/{dataset_id}/reindex", summary="Re-queue a dataset for re-processing")
def reindex_dataset(request: Request, dataset_id: str) -> JSONResponse:
    """Reset a dataset to ``pending`` and queue a new worker pass — see :func:`reindex_document`."""
    container = request.app.state.container
    dataset_repo = DatasetRepository(container.db)
    ds = dataset_repo.get(dataset_id)
    if ds is None:
        raise HTTPException(status_code=404, detail=f"No dataset with id {dataset_id!r}.")

    active_version = VersionRepository(container.db).get_active(ParentType.DATASET, dataset_id)
    content_hash = active_version.content_hash if active_version else None
    if not content_hash:
        raise HTTPException(
            status_code=409,
            detail=f"Dataset {dataset_id!r} has no recorded content hash to re-index.",
        )

    dataset_repo.set_status(dataset_id, AssetStatus.PENDING)
    job_id = _queue_reindex_job(
        container, parent_type=ParentType.DATASET, parent_id=dataset_id,
        source_id=ds.source_id, content_hash=content_hash,
    )
    logger.info("reindex_dataset | dataset_id=%s | job_id=%s", dataset_id, job_id)
    return JSONResponse(
        status_code=202,
        content={"job_id": job_id, "dataset_id": dataset_id, "parent_type": ParentType.DATASET, "status": JobStatus.QUEUED},
    )


@router.get("/documents/{doc_id}/preview", summary="Preview extracted text for a document")
def preview_document(request: Request, doc_id: str) -> JSONResponse:
    """
    Read the document's stored blob and run it through the existing
    ``extract_text`` (same function the ingestion processor uses), returning
    a truncated snippet. Always ``200`` — an expected "can't preview this"
    outcome (e.g. a scanned PDF with no text layer, or a Tier-3 deferred
    category) is reported via ``available: false``, not an HTTP error.
    """
    container = request.app.state.container
    doc = DocumentRepository(container.db).get(doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail=f"No document with id {doc_id!r}.")

    active_version = VersionRepository(container.db).get_active(ParentType.DOCUMENT, doc_id)
    content_hash = (active_version.content_hash if active_version else None) or doc.content_hash
    if not content_hash:
        return JSONResponse(
            status_code=200,
            content=_preview_unavailable("no_content", "No version/content_hash recorded for this document."),
        )

    blob_store = getattr(container, "blob_store", None)
    if blob_store is None:
        return JSONResponse(
            status_code=200, content=_preview_unavailable("blob_store_unavailable", "BlobStore is not configured."),
        )
    try:
        data = blob_store.get(content_hash)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(status_code=200, content=_preview_unavailable("blob_not_found", str(exc)))

    try:
        result = extract_text(data, category=doc.doc_type or "", filename=doc.origin_path)
    except (ExtractionError, UnsupportedCategoryError) as exc:
        return JSONResponse(status_code=200, content=_preview_unavailable(exc.reason, exc.detail))

    text = result.text
    return JSONResponse(status_code=200, content={
        "available": True,
        "text": text[:_PREVIEW_TEXT_CHARS],
        "truncated": len(text) > _PREVIEW_TEXT_CHARS,
        "char_count": len(text),
        "detail": result.detail,
    })


@router.get("/documents/{doc_id}/policies", summary="Structured policies extracted from a document (Phase C2)")
def get_document_policies(request: Request, doc_id: str) -> JSONResponse:
    """
    List the structured business policies the Policy Intelligence Engine
    extracted from this document (see
    aeam.intelligence.policy_extraction.PolicyExtractor, invoked from
    aeam.ingestion.processor.DocumentIngestJobProcessor right after this
    document was chunked/embedded/indexed).

    Always ``200`` for an existing document — a document with no recognizable
    policy reports an empty ``policies`` list, matching the same "expected
    outcome, not a server error" convention as ``/preview``. Policies are
    read-only here: this phase only extracts and persists them (no
    RuleEngine integration, no execution).
    """
    container = request.app.state.container
    doc = DocumentRepository(container.db).get(doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail=f"No document with id {doc_id!r}.")

    policies = PolicyRepository(container.db).list_by_document(doc_id)
    return JSONResponse(status_code=200, content={
        "doc_id": doc_id,
        "count": len(policies),
        "policies": [_policy_to_dict(p) for p in policies],
    })


@router.get("/datasets/{dataset_id}/preview", summary="Preview rows for a dataset")
def preview_dataset(request: Request, dataset_id: str) -> JSONResponse:
    """
    Read the dataset's stored blob and run it through the existing
    ``read_primary_table`` (same function the ingestion processor uses),
    returning the first few rows. Always ``200`` — see :func:`preview_document`
    for why an unreadable/unsupported file is reported via ``available: false``.
    """
    container = request.app.state.container
    ds = DatasetRepository(container.db).get(dataset_id)
    if ds is None:
        raise HTTPException(status_code=404, detail=f"No dataset with id {dataset_id!r}.")

    active_version = VersionRepository(container.db).get_active(ParentType.DATASET, dataset_id)
    content_hash = active_version.content_hash if active_version else None
    if not content_hash:
        return JSONResponse(
            status_code=200,
            content=_preview_unavailable("no_content", "No version/content_hash recorded for this dataset."),
        )

    blob_store = getattr(container, "blob_store", None)
    if blob_store is None:
        return JSONResponse(
            status_code=200, content=_preview_unavailable("blob_store_unavailable", "BlobStore is not configured."),
        )
    try:
        data = blob_store.get(content_hash)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(status_code=200, content=_preview_unavailable("blob_not_found", str(exc)))

    category = _infer_file_type(ds.name) or ""
    try:
        df, detail = read_primary_table(data, category, object_name=ds.name or "data")
    except SchemaInferenceError as exc:
        return JSONResponse(status_code=200, content=_preview_unavailable(exc.reason, exc.detail))

    preview_df = df.head(_PREVIEW_ROW_LIMIT)
    # pandas' own to_json() correctly handles numpy scalar types and NaN ->
    # null; safer than hand-rolling type conversion for arbitrary dataset
    # contents.
    rows = json.loads(preview_df.to_json(orient="records", date_format="iso"))
    return JSONResponse(status_code=200, content={
        "available": True,
        "columns": [str(c) for c in preview_df.columns],
        "rows": rows,
        "total_rows": int(len(df)),
        "previewed_rows": int(len(preview_df)),
        "detail": detail,
    })


# ===========================================================================
# Phase E12 — Policy lifecycle (read) + privileged curation (write)
# ===========================================================================
#
# Everything below composes objects that already exist: PolicyRepository's
# new lifecycle methods, EnterpriseMemoryEngine's new curation methods, and
# the AuditLogger the security middleware already writes through. No second
# policy store, no second memory client, no second audit mechanism.


class PolicyStatusRequest(BaseModel):
    """A policy lifecycle transition (Phase E12, COMPAT-6 / SEC-7)."""

    status: str = Field(
        description=(
            "Target lifecycle status: 'active', 'pending_review', or 'retired'. "
            "A RETIRED policy never matches a new investigation."
        ),
    )
    reason: str = Field(
        min_length=1,
        description=(
            "Why the transition is being made. REQUIRED — an unexplained "
            "governance change is exactly what this lifecycle exists to prevent."
        ),
    )


class MemoryExpungeRequest(BaseModel):
    """Withdraw one incident's memory from recall (Phase E12, MEM-4)."""

    incident_id: str = Field(min_length=1, description="Incident whose memory is withdrawn.")
    reason: str = Field(
        min_length=1,
        description="Why it is being withdrawn. REQUIRED by MEM-4; recorded verbatim.",
    )


class MemoryCorrectionRequest(BaseModel):
    """Correct one incident's memory in place (Phase E12, MEM-4)."""

    incident_id: str = Field(min_length=1, description="Incident whose memory is corrected.")
    corrections: dict[str, Any] = Field(
        description=(
            "Field overrides, e.g. {'root_cause': '...'}. Correctable fields: "
            "event_type, metric, severity, root_cause, confidence, "
            "investigation_status, recommended_actions, executed_actions, "
            "chunk_ids, timestamp."
        ),
    )
    reason: str = Field(
        min_length=1,
        description="Why the correction is being made. REQUIRED by MEM-4; recorded verbatim.",
    )


def _policy_to_dict(p: Any) -> dict[str, Any]:
    """Serialise one Policy, including its Phase E12 lifecycle fields."""
    status = getattr(p, "status", PolicyStatus.ACTIVE) or PolicyStatus.ACTIVE
    return {
        "policy_id": p.policy_id,
        "doc_id": p.doc_id,
        "source_document": p.source_document,
        "source_chunk": p.source_chunk,
        "raw_text": p.raw_text,
        "business_rule": p.business_rule,
        "condition": p.condition,
        "threshold": p.threshold,
        "actions": p.actions,
        "escalation_rule": p.escalation_rule,
        "approval_required": p.approval_required,
        "department": p.department,
        "role": p.role,
        "time_constraint": p.time_constraint,
        "priority": p.priority,
        "related_metrics": p.related_metrics,
        "extracted_at": _iso(p.extracted_at),
        # --- Phase E12 lifecycle (COMPAT-6: 'active' for every pre-E12 row) ---
        "status": status,
        "status_changed_at": _iso(getattr(p, "status_changed_at", None)),
        "status_changed_by": getattr(p, "status_changed_by", None),
        "status_reason": getattr(p, "status_reason", None),
        # Stated inline so a console never has to re-derive the rule that
        # decides whether this policy can still influence an investigation.
        "matchable": status in PolicyStatus.MATCHABLE,
    }


def _require_curation_enabled(container: Any) -> None:
    """
    Enforce the phase's documented rollback posture.

    ``KNOWLEDGE_CURATION_ENABLED=false`` disables every curation WRITE while
    leaving all reads working — 503 rather than 404, because the capability
    exists and is deliberately switched off, which is a different fact from
    "no such endpoint".
    """
    settings = getattr(container, "settings", None)
    if settings is not None and not getattr(settings, "KNOWLEDGE_CURATION_ENABLED", True):
        raise HTTPException(
            status_code=503,
            detail=(
                "Knowledge curation is disabled in this deployment "
                "(KNOWLEDGE_CURATION_ENABLED=false). Read endpoints are unaffected."
            ),
        )


def _acting_principal(request: Request) -> tuple[str, str]:
    """
    Return ``(principal, attribution_source)`` for a curation write.

    Mirrors the Phase E9 review router's attribution contract exactly: the
    principal comes from the verified JWT when the security middleware
    established one, and is otherwise tagged so the audit trail never implies
    a cryptographically-established identity it does not have. (The only way
    to reach a curation endpoint without a verified principal is
    ENVIRONMENT=development, where the middleware bypasses everything.)
    """
    user_id = getattr(request.state, "user_id", None)
    if user_id and user_id != "anonymous":
        return str(user_id), "jwt"
    return "unauthenticated-development", "development-bypass"


def _write_curation_audit(
    request: Request,
    *,
    action: str,
    endpoint: str,
    detail: dict[str, Any],
) -> None:
    """
    Record one curation act in the durable audit trail (MEM-4 / SEC-7).

    Uses the SAME AuditLogger instance the security middleware writes
    through, so a curation record lands in the same tamper-evident,
    hash-carrying ``audit_logs`` table as every other audited action and is
    queryable through the Phase E11 audit surface. An audit-write failure is
    logged but never fails the request, exactly as the middleware's own audit
    write does not — the curation already happened, and reporting failure
    would be a worse lie than a missing audit line.
    """
    audit = getattr(request.app.state, "audit_logger", None)
    if audit is None:
        logger.warning(
            "curation audit SKIPPED (no AuditLogger on app.state) | action=%s | detail=%s",
            action, detail,
        )
        return
    try:
        audit.log({
            "user_id": detail.get("actor", "unknown"),
            "action": action,
            "endpoint": endpoint,
            "status_code": 200,
            **detail,
        })
    except Exception as exc:  # noqa: BLE001
        logger.error("curation audit write FAILED | action=%s | error=%s", action, exc)


@router.get("/policies", summary="List extracted policies with their lifecycle status (Phase E12)")
def list_policies(
    request: Request,
    status: str | None = Query(
        default=None,
        description="Filter by lifecycle status: 'active', 'pending_review', or 'retired'.",
    ),
    q: str | None = Query(
        default=None,
        description="Case-insensitive substring filter over business_rule/raw_text/source_document.",
    ),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> JSONResponse:
    """
    Return a bounded page of policies, newest-extracted first, with the
    lifecycle status the Knowledge Center renders and acts on.

    ``status_counts`` is aggregated in SQL so the console can render the
    lifecycle breakdown without fetching every row.
    """
    container = request.app.state.container
    policy_repo = PolicyRepository(container.db)

    if status is not None and status not in PolicyStatus.ALL:
        raise HTTPException(
            status_code=422,
            detail=f"invalid status {status!r}. Must be one of {sorted(PolicyStatus.ALL)}.",
        )

    policies = policy_repo.list_by_status(status) if status else policy_repo.list_all()

    if q:
        needle = q.strip().lower()
        policies = [
            p for p in policies
            if _matches(needle, p.business_rule, p.raw_text, p.source_document)
        ]

    policies.sort(key=lambda p: p.extracted_at or "", reverse=True)
    total = len(policies)
    page = policies[offset:offset + limit]

    return JSONResponse(
        status_code=200,
        content={
            "policies": [_policy_to_dict(p) for p in page],
            "count": len(page),
            "total": total,
            "limit": limit,
            "offset": offset,
            "status_counts": policy_repo.count_by_status(),
            "lifecycle": {
                "statuses": sorted(PolicyStatus.ALL),
                "matchable": sorted(PolicyStatus.MATCHABLE),
                "note": (
                    "A policy in a matchable status can be cited as advisory evidence by a "
                    "new investigation. A retired policy never can — but is retained, not "
                    "deleted, so historical incidents that cited it stay explainable."
                ),
            },
        },
    )


@router.post(
    "/curate/policies/{policy_id}/status",
    summary="Transition a policy's lifecycle status (Phase E12, privileged)",
)
def set_policy_status(
    request: Request, policy_id: str, body: PolicyStatusRequest,
) -> JSONResponse:
    """
    Move one policy through its lifecycle, with attribution.

    Retiring a policy takes it out of force immediately: the very next
    investigation's ``PolicyRegistry.match_for_incident`` call will not see
    it. The row is kept so incidents that already cited it remain
    explainable.

    Authorisation is the middleware's — ``/api/v1/knowledge/curate`` maps to
    ``admin:config``, so only the admin role reaches this at all.
    """
    container = request.app.state.container
    _require_curation_enabled(container)

    if body.status not in PolicyStatus.ALL:
        raise HTTPException(
            status_code=422,
            detail=f"invalid status {body.status!r}. Must be one of {sorted(PolicyStatus.ALL)}.",
        )

    policy_repo = PolicyRepository(container.db)
    policy = policy_repo.get(policy_id)
    if policy is None:
        raise HTTPException(status_code=404, detail=f"No policy with id {policy_id!r}.")

    previous_status = getattr(policy, "status", PolicyStatus.ACTIVE) or PolicyStatus.ACTIVE
    actor, attribution_source = _acting_principal(request)

    policy_repo.set_status(
        policy_id, body.status, changed_by=actor, reason=body.reason,
    )

    detail = {
        "policy_id": policy_id,
        "previous_status": previous_status,
        "new_status": body.status,
        "reason": body.reason,
        "actor": actor,
        "attribution_source": attribution_source,
    }
    _write_curation_audit(
        request,
        action="policy_status_changed",
        endpoint=f"/api/v1/knowledge/curate/policies/{policy_id}/status",
        detail=detail,
    )
    logger.info(
        "set_policy_status | policy_id=%s | %s -> %s | actor=%s | reason=%s",
        policy_id, previous_status, body.status, actor, body.reason,
    )

    updated = policy_repo.get(policy_id)
    return JSONResponse(status_code=200, content={
        **detail,
        "policy": _policy_to_dict(updated) if updated else None,
        "matches_new_investigations": body.status in PolicyStatus.MATCHABLE,
    })


@router.post(
    "/curate/memory/expunge",
    summary="Withdraw one incident's Enterprise Memory entry from recall (Phase E12, privileged)",
)
def expunge_memory(request: Request, body: MemoryExpungeRequest) -> JSONResponse:
    """
    Permanently remove an incident's memory so it stops surfacing as evidence.

    MEM-4's destructive correction path. ``reason`` is mandatory and the
    acting principal is taken from the verified session, so the resulting
    audit record answers who, why, and when without further lookup.
    """
    container = request.app.state.container
    _require_curation_enabled(container)

    memory = getattr(container, "enterprise_memory", None)
    if memory is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "Enterprise Memory is not configured in this deployment, so there is "
                "nothing to curate."
            ),
        )

    actor, attribution_source = _acting_principal(request)
    try:
        result = memory.expunge_incident_memory(
            body.incident_id, reason=body.reason, actor=actor,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    _write_curation_audit(
        request,
        action="memory_expunged",
        endpoint="/api/v1/knowledge/curate/memory/expunge",
        detail={
            "incident_id": body.incident_id,
            "reason": body.reason,
            "actor": actor,
            "attribution_source": attribution_source,
        },
    )
    return JSONResponse(status_code=200, content={
        **result, "attribution_source": attribution_source,
    })


@router.post(
    "/curate/memory/correct",
    summary="Correct one incident's Enterprise Memory entry (Phase E12, privileged)",
)
def correct_memory(request: Request, body: MemoryCorrectionRequest) -> JSONResponse:
    """
    Rewrite an incident's memory with corrected field values.

    MEM-4's non-destructive correction path. Implemented as
    expunge-then-rewrite so the memory's embedding is regenerated from the
    corrected text — a payload-only patch would leave the entry recalling on
    its old, wrong wording while displaying the corrected one.
    """
    container = request.app.state.container
    _require_curation_enabled(container)

    memory = getattr(container, "enterprise_memory", None)
    if memory is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "Enterprise Memory is not configured in this deployment, so there is "
                "nothing to curate."
            ),
        )

    actor, attribution_source = _acting_principal(request)
    try:
        result = memory.correct_incident_memory(
            body.incident_id, body.corrections, reason=body.reason, actor=actor,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    _write_curation_audit(
        request,
        action="memory_corrected",
        endpoint="/api/v1/knowledge/curate/memory/correct",
        detail={
            "incident_id": body.incident_id,
            "fields_corrected": result.get("fields_corrected"),
            "reason": body.reason,
            "actor": actor,
            "attribution_source": attribution_source,
        },
    )
    return JSONResponse(status_code=200, content={
        **result, "attribution_source": attribution_source,
    })


@router.post(
    "/curate/documents/{doc_id}/semantic-type",
    summary="Declare an already-ingested document's semantic type (Phase E12, privileged)",
)
def set_document_semantic_type(
    request: Request,
    doc_id: str,
    semantic_type: str = Query(
        description=(
            "Declared semantic type, e.g. 'runbook'. Distinct from the file "
            "format. Determines whether retrieval treats the document as an "
            "authoritative source."
        ),
    ),
) -> JSONResponse:
    """
    Classify an already-ingested document after the fact.

    The upload path accepts a declaration inline, but a corpus ingested
    before this phase has none — and re-uploading every file just to classify
    it would be absurd. Declaring the type here updates the registry row; the
    new type reaches retrieval on the document's next re-index, which the
    response states explicitly rather than implying an instant effect it does
    not have.
    """
    container = request.app.state.container
    _require_curation_enabled(container)

    normalised = (semantic_type or "").strip().lower()
    if normalised not in SemanticDocType.ALL:
        raise HTTPException(
            status_code=422,
            detail=(
                f"semantic_type {semantic_type!r} is not recognised. "
                f"Must be one of {sorted(SemanticDocType.ALL)}."
            ),
        )

    doc_repo = DocumentRepository(container.db)
    doc = doc_repo.get(doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail=f"No document with id {doc_id!r}.")

    previous = getattr(doc, "semantic_type", None)
    actor, attribution_source = _acting_principal(request)
    doc_repo.update(doc_id, {"semantic_type": normalised})

    _write_curation_audit(
        request,
        action="document_semantic_type_declared",
        endpoint=f"/api/v1/knowledge/curate/documents/{doc_id}/semantic-type",
        detail={
            "doc_id": doc_id,
            "previous_semantic_type": previous,
            "new_semantic_type": normalised,
            "actor": actor,
            "attribution_source": attribution_source,
        },
    )

    return JSONResponse(status_code=200, content={
        "doc_id": doc_id,
        "previous_semantic_type": previous,
        "semantic_type": normalised,
        "format": doc.doc_type,
        "actor": actor,
        "attribution_source": attribution_source,
        # Honest about WHEN this takes effect: the vectors already in Qdrant
        # carry the old doc_type until the document is re-processed.
        "takes_effect": (
            "on the document's next re-index — POST /api/v1/knowledge/documents/"
            f"{doc_id}/reindex applies it to the already-indexed chunks now."
        ),
        "authoritative_source": normalised in {
            "runbook", "sre_runbook", "incident_report", "post_mortem",
        },
    })
