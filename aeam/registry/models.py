"""
aeam/registry/models.py

Shared domain models for enterprise assets (Phase B1.1 — Storage Foundation).

Plain ``@dataclass`` objects — one per registry table — that repositories map
rows to and from. They carry NO business workflow: only field definitions,
sensible defaults, a ``to_row()`` (insert-ready dict) and a ``from_row()``
(DB-dict → model, decoding JSON columns). Designed for extension: every model
has an ``extra`` JSON escape hatch so later phases can attach fields without a
schema migration, and status/kind vocabularies are centralised below.

No ORM, no framework — these compose with the existing DatabaseClient.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any


# ---------------------------------------------------------------------------
# Controlled vocabularies (string constants — DB/JSON friendly, no enum coupling)
# ---------------------------------------------------------------------------

class SourceKind:
    UPLOAD = "upload"
    CONFLUENCE = "confluence"
    SHAREPOINT = "sharepoint"
    S3 = "s3"
    AZURE_BLOB = "azure_blob"
    DATABASE = "database"
    REST = "rest"
    GSHEET = "gsheet"
    ALL = {UPLOAD, CONFLUENCE, SHAREPOINT, S3, AZURE_BLOB, DATABASE, REST, GSHEET}


class SourceStatus:
    ACTIVE = "active"
    ERROR = "error"
    DISABLED = "disabled"
    ALL = {ACTIVE, ERROR, DISABLED}


class AssetStatus:
    """Lifecycle shared by documents and datasets."""
    PENDING = "pending"
    PROCESSING = "processing"
    INDEXED = "indexed"
    STALE = "stale"
    ARCHIVED = "archived"
    DELETED = "deleted"
    ERROR = "error"
    ALL = {PENDING, PROCESSING, INDEXED, STALE, ARCHIVED, DELETED, ERROR}


class JobType:
    INGEST = "ingest"
    REINDEX = "reindex"
    DELETE = "delete"
    SYNC = "sync"
    ALL = {INGEST, REINDEX, DELETE, SYNC}


class JobStatus:
    QUEUED = "queued"
    VALIDATING = "validating"
    EXTRACTING = "extracting"
    INDEXING = "indexing"
    DONE = "done"
    FAILED = "failed"
    # Phase B1.2: distinct terminal state for an operator-cancelled job — never
    # was an error, so must not be conflated with FAILED. Purely additive: no
    # existing value renamed or removed, no schema change (status is TEXT).
    CANCELLED = "cancelled"
    ALL = {QUEUED, VALIDATING, EXTRACTING, INDEXING, DONE, FAILED, CANCELLED}
    #: Statuses from which a job can no longer transition.
    TERMINAL = {DONE, FAILED, CANCELLED}


class ParentType:
    DOCUMENT = "document"
    DATASET = "dataset"
    SOURCE_SYNC = "source_sync"
    ALL = {DOCUMENT, DATASET, SOURCE_SYNC}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    """UTC now as an ISO-8601 string (matches how DatabaseClient serialises datetimes)."""
    return datetime.now(tz=timezone.utc).isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


def _decode_json(value: Any, default: Any) -> Any:
    """
    Normalise a JSON column read from the DB.

    PostgreSQL JSONB returns dict/list already; SQLite returns a JSON string.
    Returns ``default`` for NULL/blank/unparseable values so callers never crash.
    """
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return default
        try:
            return json.loads(s)
        except (ValueError, TypeError):
            return default
    return default


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------

@dataclass
class _Asset:
    """Common serialisation behaviour for every registry model."""

    def to_row(self) -> dict[str, Any]:
        """
        Insert-ready column→value mapping.

        Dict/list values (JSON columns) are passed through — the existing
        DatabaseClient.insert() serialises them to JSON. ``extra`` is always
        emitted as a JSON object.
        """
        row = asdict(self)
        return row

    @staticmethod
    def _base_from_row(cls, row: dict[str, Any], json_fields: tuple[str, ...]) -> Any:
        """Shared row→model construction that decodes the named JSON columns."""
        data = dict(row)
        for f in json_fields:
            if f in data:
                data[f] = _decode_json(data[f], {} if f in ("config", "columns", "relationships", "metric_columns", "extra") else [])
        # Drop any DB columns the dataclass doesn't declare (forward-compat).
        allowed = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in allowed})


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

@dataclass
class Source(_Asset):
    name: str = ""
    kind: str = SourceKind.UPLOAD
    source_id: str = field(default_factory=_new_id)
    config: dict[str, Any] = field(default_factory=dict)
    secret_ref: str | None = None
    status: str = SourceStatus.ACTIVE
    sync_schedule: str | None = None
    last_synced_at: str | None = None
    created_at: str = field(default_factory=_now_iso)
    created_by: str | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Source":
        return _Asset._base_from_row(cls, row, ("config",))


@dataclass
class Document(_Asset):
    title: str = ""
    doc_id: str = field(default_factory=_new_id)
    source_id: str | None = None
    origin_path: str | None = None
    doc_type: str | None = None
    current_version: int = 1
    content_hash: str | None = None
    chunk_count: int = 0
    status: str = AssetStatus.PENDING
    review_by: str | None = None
    language: str | None = None
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Document":
        return _Asset._base_from_row(cls, row, ())


@dataclass
class Dataset(_Asset):
    name: str = ""
    dataset_id: str = field(default_factory=_new_id)
    source_id: str | None = None
    schema_id: str | None = None
    row_count: int = 0
    metric_columns: list[str] = field(default_factory=list)
    refresh_schedule: str | None = None
    status: str = AssetStatus.PENDING
    last_ingested_at: str | None = None
    created_at: str = field(default_factory=_now_iso)

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Dataset":
        return _Asset._base_from_row(cls, row, ("metric_columns",))


@dataclass
class Schema(_Asset):
    object_name: str = ""
    schema_id: str = field(default_factory=_new_id)
    source_id: str | None = None
    columns: list[dict[str, Any]] = field(default_factory=list)
    relationships: list[dict[str, Any]] = field(default_factory=list)
    discovered_at: str = field(default_factory=_now_iso)

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Schema":
        return _Asset._base_from_row(cls, row, ("columns", "relationships"))


@dataclass
class Version(_Asset):
    parent_type: str = ParentType.DOCUMENT
    parent_id: str = ""
    version: int = 1
    version_id: str = field(default_factory=_new_id)
    content_hash: str | None = None
    blob_ref: str | None = None
    chunk_ids: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=_now_iso)
    created_by: str | None = None
    is_active: bool = True

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Version":
        return _Asset._base_from_row(cls, row, ("chunk_ids",))


@dataclass
class IngestionJob(_Asset):
    job_type: str = JobType.INGEST
    job_id: str = field(default_factory=_new_id)
    source_id: str | None = None
    parent_type: str | None = None
    parent_id: str | None = None
    status: str = JobStatus.QUEUED
    progress: int = 0
    stage: str | None = None
    error: str | None = None
    content_hash: str | None = None
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "IngestionJob":
        return _Asset._base_from_row(cls, row, ())
