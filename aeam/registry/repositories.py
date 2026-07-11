"""
aeam/registry/repositories.py

Repository layer for the Enterprise Data Layer registries (Phase B1.1).

Thin persistence classes — one per registry table — built entirely on top of
the existing :class:`~aeam.integrations.database.DatabaseClient`. They perform
ONLY CRUD and query operations and map rows to/from the dataclasses in
:mod:`aeam.registry.models`. No ingestion, no classification, no lifecycle
orchestration — that logic belongs to later B1 phases.

Table and primary-key names are hardcoded per repository (never user input);
update column names are validated to ``[A-Za-z0-9_]`` before use.
"""

from __future__ import annotations

import json
from typing import Any

from aeam.integrations.database import DatabaseClient
from aeam.registry.models import (
    AssetStatus,
    Dataset,
    Document,
    IngestionJob,
    JobStatus,
    ParentType,
    Schema,
    Source,
    SourceStatus,
    Version,
    _now_iso,
)


def _validate_ident(name: str) -> None:
    """Guard identifiers that cannot be parameterised (column names)."""
    if not name or not all(c.isalnum() or c == "_" for c in name):
        raise ValueError(f"invalid column identifier: {name!r}")


class BaseRepository:
    """
    Generic CRUD over one registry table using the shared DatabaseClient.

    Subclasses set ``table``, ``pk`` and ``model_cls``; everything below is
    parameterised. JSON columns are handled by the models' ``to_row`` /
    ``from_row`` (writes) and, for partial updates, by :meth:`update`.
    """

    table: str = ""
    pk: str = ""
    model_cls: Any = None

    def __init__(self, db: DatabaseClient) -> None:
        self._db = db

    # ---- create / read ------------------------------------------------
    def create(self, model: Any) -> str:
        """Insert ``model`` and return its primary key."""
        return self._db.insert(self.table, model.to_row(), returning_column=self.pk)

    def get(self, id_: str) -> Any | None:
        row = self._db.fetch_one(
            f"SELECT * FROM {self.table} WHERE {self.pk} = :id", {"id": id_}
        )
        return self.model_cls.from_row(row) if row else None

    def list_all(self, limit: int | None = None) -> list[Any]:
        query = f"SELECT * FROM {self.table}"
        params: dict[str, Any] = {}
        if limit is not None:
            query += " LIMIT :limit"
            params["limit"] = limit
        return [self.model_cls.from_row(r) for r in self._db.fetch_all(query, params)]

    def _query(self, where: str, params: dict[str, Any]) -> list[Any]:
        return [
            self.model_cls.from_row(r)
            for r in self._db.fetch_all(
                f"SELECT * FROM {self.table} WHERE {where}", params
            )
        ]

    def count(self) -> int:
        row = self._db.fetch_one(f"SELECT COUNT(*) AS n FROM {self.table}")
        return int(row["n"]) if row and row.get("n") is not None else 0

    # ---- update / delete ----------------------------------------------
    def update(self, id_: str, fields: dict[str, Any]) -> None:
        """Partial update by primary key. dict/list values are JSON-encoded."""
        if not fields:
            return
        safe: dict[str, Any] = {}
        for key, value in fields.items():
            _validate_ident(key)
            safe[key] = json.dumps(value) if isinstance(value, (dict, list)) else value
        set_clause = ", ".join(f"{k} = :{k}" for k in safe)
        safe["_pk_value"] = id_
        self._db.execute(
            f"UPDATE {self.table} SET {set_clause} WHERE {self.pk} = :_pk_value", safe
        )

    def delete(self, id_: str) -> None:
        self._db.execute(f"DELETE FROM {self.table} WHERE {self.pk} = :id", {"id": id_})


# ---------------------------------------------------------------------------
# Concrete repositories
# ---------------------------------------------------------------------------

class SourceRepository(BaseRepository):
    table, pk, model_cls = "sources", "source_id", Source

    def list_by_kind(self, kind: str) -> list[Source]:
        return self._query("kind = :kind", {"kind": kind})

    def list_active(self) -> list[Source]:
        return self._query("status = :status", {"status": SourceStatus.ACTIVE})


class DocumentRepository(BaseRepository):
    table, pk, model_cls = "documents", "doc_id", Document

    def get_by_content_hash(self, content_hash: str) -> Document | None:
        rows = self._query("content_hash = :h", {"h": content_hash})
        return rows[0] if rows else None

    def list_by_source(self, source_id: str) -> list[Document]:
        return self._query("source_id = :sid", {"sid": source_id})

    def list_by_status(self, status: str) -> list[Document]:
        return self._query("status = :s", {"s": status})

    def set_status(self, doc_id: str, status: str) -> None:
        self.update(doc_id, {"status": status, "updated_at": _now_iso()})


class DatasetRepository(BaseRepository):
    table, pk, model_cls = "datasets", "dataset_id", Dataset

    def list_by_source(self, source_id: str) -> list[Dataset]:
        return self._query("source_id = :sid", {"sid": source_id})

    def set_status(self, dataset_id: str, status: str) -> None:
        self.update(dataset_id, {"status": status})


class SchemaRepository(BaseRepository):
    table, pk, model_cls = "schemas", "schema_id", Schema

    def list_by_source(self, source_id: str) -> list[Schema]:
        return self._query("source_id = :sid", {"sid": source_id})


class VersionRepository(BaseRepository):
    table, pk, model_cls = "versions", "version_id", Version

    def list_for_parent(self, parent_type: str, parent_id: str) -> list[Version]:
        return self._query(
            "parent_type = :pt AND parent_id = :pid",
            {"pt": parent_type, "pid": parent_id},
        )

    def get_active(self, parent_type: str, parent_id: str) -> Version | None:
        rows = self._query(
            "parent_type = :pt AND parent_id = :pid AND is_active = :active",
            {"pt": parent_type, "pid": parent_id, "active": True},
        )
        return rows[0] if rows else None

    def deactivate_all(self, parent_type: str, parent_id: str) -> None:
        self._db.execute(
            "UPDATE versions SET is_active = :inactive "
            "WHERE parent_type = :pt AND parent_id = :pid",
            {"inactive": False, "pt": parent_type, "pid": parent_id},
        )


class IngestionJobRepository(BaseRepository):
    table, pk, model_cls = "ingestion_jobs", "job_id", IngestionJob

    def list_by_status(self, status: str) -> list[IngestionJob]:
        return self._query("status = :s", {"s": status})

    def next_queued(self) -> IngestionJob | None:
        rows = [
            self.model_cls.from_row(r)
            for r in self._db.fetch_all(
                "SELECT * FROM ingestion_jobs WHERE status = :s "
                "ORDER BY created_at ASC LIMIT 1",
                {"s": JobStatus.QUEUED},
            )
        ]
        return rows[0] if rows else None

    def update_progress(
        self,
        job_id: str,
        *,
        status: str | None = None,
        progress: int | None = None,
        stage: str | None = None,
        error: str | None = None,
    ) -> None:
        fields: dict[str, Any] = {"updated_at": _now_iso()}
        if status is not None:
            fields["status"] = status
        if progress is not None:
            fields["progress"] = progress
        if stage is not None:
            fields["stage"] = stage
        if error is not None:
            fields["error"] = error
        self.update(job_id, fields)
