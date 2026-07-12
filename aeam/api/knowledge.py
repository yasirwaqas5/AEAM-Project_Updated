"""
aeam/api/knowledge.py

Knowledge Center API (Phase B1.6).

Exposes the existing Enterprise Data Layer registries (B1.1-B1.5) — Documents,
Datasets, Schemas, Versions — over a thin read-mostly REST surface, so the
Enterprise UI Shell's Knowledge Center page can display what has already been
ingested. Ingestion Jobs are NOT re-exposed here: they already have a full
API (list/get/cancel/retry) in ``aeam.api.ingest`` — reusing that existing
endpoint instead of duplicating it.

Rules enforced (mirrors every other API module in this package):
- All state access via request.app.state.container.
- No ingestion, no RAG, no BlobStore, no Qdrant calls — pure registry reads.
- No new repository methods: every query here is exactly
  ``DocumentRepository``/``DatasetRepository``/``SchemaRepository``/
  ``VersionRepository``/``SourceRepository`` methods that already exist.
  "Search" is a basic in-memory substring filter over an already-fetched
  list — not a new SQL query.
- DELETE endpoints call ONLY the existing generic ``BaseRepository.delete()``
  (row delete). They are REGISTRY-ONLY: deleting a Document does not purge
  its Qdrant vectors, and deleting a Dataset/Document does not remove its
  BlobStore bytes. This phase does not modify RAG or BlobStore, so a true
  cascading purge is explicitly out of scope — see the module-level docstring
  on each delete handler. Not wired to any frontend control this phase.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from aeam.ingestion.validation import SUPPORTED_EXTENSIONS
from aeam.registry.models import AssetStatus, Dataset, Document, ParentType, Schema, Version
from aeam.registry.repositories import (
    DatasetRepository,
    DocumentRepository,
    SchemaRepository,
    SourceRepository,
    VersionRepository,
)

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
) -> JSONResponse:
    """List documents, optionally filtered by status and/or a basic text search, newest first."""
    _validate_status(status)
    container = request.app.state.container
    doc_repo = DocumentRepository(container.db)

    docs = doc_repo.list_by_status(status) if status else doc_repo.list_all(limit=limit)
    if q and q.strip():
        needle = q.strip().lower()
        docs = [d for d in docs if _matches(needle, d.title, d.origin_path)]
    docs.sort(key=lambda d: d.created_at or "", reverse=True)

    source_names = _source_name_map(SourceRepository(container.db))
    return JSONResponse(status_code=200, content=[_document_to_dict(d, source_names) for d in docs])


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


@router.delete("/documents/{doc_id}", summary="Delete a document (registry rows only)")
def delete_document(request: Request, doc_id: str) -> JSONResponse:
    """
    Delete a document's registry row and its version rows.

    REGISTRY-ONLY: this does not purge the document's Qdrant vectors or its
    BlobStore bytes — a true cascading purge would require calling into RAG/
    Qdrant and BlobStore, both explicitly out of scope for this phase. Not
    wired to any frontend control yet.
    """
    container = request.app.state.container
    doc_repo = DocumentRepository(container.db)
    doc = doc_repo.get(doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail=f"No document with id {doc_id!r}.")

    version_repo = VersionRepository(container.db)
    versions = version_repo.list_for_parent(ParentType.DOCUMENT, doc_id)
    for v in versions:
        version_repo.delete(v.version_id)
    doc_repo.delete(doc_id)

    logger.info("delete_document | doc_id=%s | versions_deleted=%d", doc_id, len(versions))
    return JSONResponse(
        status_code=200, content={"deleted": True, "doc_id": doc_id, "versions_deleted": len(versions)}
    )


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


@router.delete("/datasets/{dataset_id}", summary="Delete a dataset (registry rows only)")
def delete_dataset(request: Request, dataset_id: str) -> JSONResponse:
    """
    Delete a dataset's registry row, its schema row, and its version rows.

    REGISTRY-ONLY: this does not purge the dataset's BlobStore bytes.
    Datasets are never indexed into Qdrant (B1.4 registers structure, not
    prose), so there is no vector cleanup concern here — unlike documents.
    A dataset's schema is 1:1-owned by it in the current design (no other
    dataset ever references the same schema_id), so cascading its deletion
    alongside the dataset is safe. Not wired to any frontend control yet.
    """
    container = request.app.state.container
    dataset_repo = DatasetRepository(container.db)
    ds = dataset_repo.get(dataset_id)
    if ds is None:
        raise HTTPException(status_code=404, detail=f"No dataset with id {dataset_id!r}.")

    version_repo = VersionRepository(container.db)
    versions = version_repo.list_for_parent(ParentType.DATASET, dataset_id)
    for v in versions:
        version_repo.delete(v.version_id)

    schema_deleted = False
    if ds.schema_id:
        SchemaRepository(container.db).delete(ds.schema_id)
        schema_deleted = True

    dataset_repo.delete(dataset_id)

    logger.info(
        "delete_dataset | dataset_id=%s | versions_deleted=%d | schema_deleted=%s",
        dataset_id, len(versions), schema_deleted,
    )
    return JSONResponse(status_code=200, content={
        "deleted": True, "dataset_id": dataset_id,
        "versions_deleted": len(versions), "schema_deleted": schema_deleted,
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
