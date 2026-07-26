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
    IncidentApproval,
    IngestionJob,
    JobStatus,
    ParentType,
    Policy,
    PolicyStatus,
    ReviewVerdict,
    Schema,
    Source,
    SourceStatus,
    Verdict,
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

    def list_all(self, limit: int | None = None, offset: int = 0) -> list[Any]:
        # Phase E6: ``offset`` is additive — default 0 keeps every existing
        # caller byte-identical. Only applied alongside a ``limit`` (a bare
        # OFFSET without LIMIT is meaningless in SQL and unsupported on some
        # dialects), so unbounded ``list_all()`` is unchanged.
        query = f"SELECT * FROM {self.table}"
        params: dict[str, Any] = {}
        if limit is not None:
            query += " LIMIT :limit OFFSET :offset"
            params["limit"] = limit
            params["offset"] = max(0, int(offset))
        return [self.model_cls.from_row(r) for r in self._db.fetch_all(query, params)]

    def total(self) -> int:
        """Row count for pagination — alias of :meth:`count` with a clearer name."""
        return self.count()

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

    def find_active_by_content_hash(
        self, parent_type: str, content_hash: str
    ) -> Version | None:
        """
        Return the active version of the given parent type whose original bytes
        hash to ``content_hash``, if any.

        Enables content-addressed dedup for parents that carry no own
        ``content_hash`` column (e.g. ``datasets``): the version records the
        hash of the original file, so an identical re-upload maps back to the
        same parent. ``versions.content_hash`` is indexed (idx_versions_content_hash).
        """
        rows = self._query(
            "parent_type = :pt AND content_hash = :h AND is_active = :active",
            {"pt": parent_type, "h": content_hash, "active": True},
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

    def find_active_by_content_hash(self, content_hash: str) -> IngestionJob | None:
        """
        Return a non-terminal job already tracking this content hash, if any.

        Used by the ingress API to avoid creating a duplicate job when the
        same file bytes are uploaded again while an earlier job for them is
        still in flight — the blob itself is already deduplicated by
        BlobStore; this extends the same "never duplicate" intent to jobs.
        """
        terminal = tuple(JobStatus.TERMINAL)
        placeholders = ", ".join(f":t{i}" for i in range(len(terminal)))
        params: dict[str, Any] = {"h": content_hash}
        params.update({f"t{i}": v for i, v in enumerate(terminal)})
        rows = self._db.fetch_all(
            f"SELECT * FROM ingestion_jobs WHERE content_hash = :h "
            f"AND status NOT IN ({placeholders}) "
            f"ORDER BY created_at DESC LIMIT 1",
            params,
        )
        return self.model_cls.from_row(rows[0]) if rows else None

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


class PolicyRepository(BaseRepository):
    table, pk, model_cls = "policies", "policy_id", Policy

    def list_by_document(self, doc_id: str) -> list[Policy]:
        return self._query("doc_id = :doc_id", {"doc_id": doc_id})

    # ---- Phase E12: policy lifecycle (COMPAT-6, SEC-7) -----------------

    def list_matchable(self) -> list[Policy]:
        """
        Every policy eligible to match an investigation.

        Excludes RETIRED policies at the DATABASE, not in Python, so a large
        retired corpus costs nothing per investigation. Rows written before
        this phase have ``status`` NULL and are treated as matchable — that
        is the behaviour they already had, and the E12 migration backfills
        them to ``'active'`` anyway (COMPAT-6).
        """
        placeholders = ", ".join(f":s{i}" for i in range(len(PolicyStatus.MATCHABLE)))
        params = {f"s{i}": s for i, s in enumerate(sorted(PolicyStatus.MATCHABLE))}
        return self._query(
            f"status IS NULL OR status IN ({placeholders})", params
        )

    def list_by_status(self, status: str) -> list[Policy]:
        """Policies in exactly ``status`` (the Knowledge Center's filter)."""
        return self._query("status = :s", {"s": status})

    def set_status(
        self,
        policy_id: str,
        status: str,
        *,
        changed_by: str | None = None,
        reason: str | None = None,
    ) -> None:
        """
        Transition one policy's lifecycle status, recording attribution.

        ``changed_by``/``reason`` are stored verbatim so the row itself
        answers "who retired this policy, and why" without a join to the
        audit trail — the audit record written alongside by the API layer is
        the tamper-evident copy, this is the queryable one.

        Raises:
            ValueError: If ``status`` is not a member of
                        :data:`~aeam.registry.models.PolicyStatus.ALL`.
        """
        if status not in PolicyStatus.ALL:
            raise ValueError(
                f"invalid policy status {status!r}; must be one of {sorted(PolicyStatus.ALL)}."
            )
        self.update(policy_id, {
            "status": status,
            "status_changed_at": _now_iso(),
            "status_changed_by": changed_by,
            "status_reason": reason,
        })

    def count_by_status(self) -> dict[str, int]:
        """``{status: count}`` across the whole policy corpus, aggregated in SQL."""
        rows = self._db.fetch_all(
            "SELECT status, COUNT(*) AS n FROM policies GROUP BY status", {}
        )
        counts: dict[str, int] = {}
        for row in rows:
            # NULL status (pre-E12 rows) reports under 'active', matching how
            # from_row() and list_matchable() already treat it.
            key = row.get("status") or PolicyStatus.ACTIVE
            counts[key] = counts.get(key, 0) + int(row.get("n") or 0)
        return counts


# ---------------------------------------------------------------------------
# Phase E9 — Human-in-the-Loop Enforcement
# ---------------------------------------------------------------------------

class IncidentApprovalRepository(BaseRepository):
    """
    Persistence for the approval requirement attached to ONE incident.

    Same thin-CRUD contract as every repository above: no workflow logic
    lives here (that is :class:`~aeam.governance.human_review.HumanReviewService`),
    only queries the review API and the Orchestrator need.
    """
    table, pk, model_cls = "incident_approvals", "approval_id", IncidentApproval

    def get_by_incident(self, incident_id: str) -> IncidentApproval | None:
        """
        The approval record for ``incident_id``, or ``None`` when the
        incident never required approval (including every incident that
        predates Phase E9 — absence means "no gate", never "denied").
        """
        rows = self._db.fetch_all(
            "SELECT * FROM incident_approvals WHERE incident_id = :iid "
            "ORDER BY created_at ASC",
            {"iid": incident_id},
        )
        return self.model_cls.from_row(rows[0]) if rows else None

    def list_by_status(
        self, status: str, limit: int | None = None, offset: int = 0,
    ) -> list[IncidentApproval]:
        """Approvals in ``status``, oldest first (the review queue's order)."""
        query = (
            "SELECT * FROM incident_approvals WHERE status = :s "
            "ORDER BY created_at ASC"
        )
        params: dict[str, Any] = {"s": status}
        if limit is not None:
            query += " LIMIT :limit OFFSET :offset"
            params["limit"] = limit
            params["offset"] = max(0, int(offset))
        return [self.model_cls.from_row(r) for r in self._db.fetch_all(query, params)]

    def count_by_status(self, status: str) -> int:
        row = self._db.fetch_one(
            "SELECT COUNT(*) AS n FROM incident_approvals WHERE status = :s",
            {"s": status},
        )
        return int(row["n"]) if row and row.get("n") is not None else 0

    def save_progress(
        self,
        approval_id: str,
        *,
        status: str | None = None,
        current_tier: int | None = None,
        executed_actions: list[str] | None = None,
        skipped_actions: list[dict[str, Any]] | None = None,
    ) -> None:
        """
        Partial update of the mutable columns, always refreshing
        ``updated_at`` (mirrors :meth:`IngestionJobRepository.update_progress`).

        ``pending_actions`` is deliberately NOT updatable: it records what
        was withheld at finalization, and rewriting it would destroy the
        guarantee that an approval executes exactly those steps.
        """
        fields: dict[str, Any] = {"updated_at": _now_iso()}
        if status is not None:
            fields["status"] = status
        if current_tier is not None:
            fields["current_tier"] = current_tier
        if executed_actions is not None:
            fields["executed_actions"] = executed_actions
        if skipped_actions is not None:
            fields["skipped_actions"] = skipped_actions
        self.update(approval_id, fields)


class ReviewVerdictRepository(BaseRepository):
    """Append-only persistence for human review verdicts (Phase E9)."""
    table, pk, model_cls = "review_verdicts", "verdict_id", ReviewVerdict

    def list_for_approval(self, approval_id: str) -> list[ReviewVerdict]:
        """Every verdict cast against ``approval_id``, in chain order."""
        rows = self._db.fetch_all(
            "SELECT * FROM review_verdicts WHERE approval_id = :aid "
            "ORDER BY tier ASC, created_at ASC",
            {"aid": approval_id},
        )
        return [self.model_cls.from_row(r) for r in rows]

    def list_for_incident(self, incident_id: str) -> list[ReviewVerdict]:
        rows = self._db.fetch_all(
            "SELECT * FROM review_verdicts WHERE incident_id = :iid "
            "ORDER BY tier ASC, created_at ASC",
            {"iid": incident_id},
        )
        return [self.model_cls.from_row(r) for r in rows]

    def get_for_tier(self, approval_id: str, tier: int) -> ReviewVerdict | None:
        """
        The chain-advancing/halting verdict already recorded at ``tier``,
        if any. ``changes_requested`` / ``escalated`` verdicts are ignored
        here: they leave the tier open, so a later approval at the same
        tier is legitimate, not a duplicate.
        """
        rows = self._db.fetch_all(
            "SELECT * FROM review_verdicts WHERE approval_id = :aid AND tier = :t "
            "ORDER BY created_at ASC",
            {"aid": approval_id, "t": int(tier)},
        )
        for row in rows:
            model = self.model_cls.from_row(row)
            if model.verdict in Verdict.ADVANCING or model.verdict in Verdict.HALTING:
                return model
        return None

    def find_advancing_by_reviewer(
        self, approval_id: str, reviewer_id: str,
    ) -> ReviewVerdict | None:
        """
        An approval this principal has ALREADY cast against this chain.

        Two purposes, one query: it makes a repeated approve request
        idempotent (the second call finds the first and changes nothing),
        and it stops one principal from satisfying several tiers of a
        chain that exists precisely to require several people.
        """
        rows = self._db.fetch_all(
            "SELECT * FROM review_verdicts WHERE approval_id = :aid "
            "AND reviewer_id = :rid ORDER BY created_at ASC",
            {"aid": approval_id, "rid": reviewer_id},
        )
        for row in rows:
            model = self.model_cls.from_row(row)
            if model.verdict in Verdict.ADVANCING:
                return model
        return None

    def list_recent(
        self, limit: int | None = None, offset: int = 0,
        incident_id: str | None = None,
    ) -> list[ReviewVerdict]:
        """Verdict history, newest first (what the review workspace shows)."""
        query = "SELECT * FROM review_verdicts"
        params: dict[str, Any] = {}
        if incident_id:
            query += " WHERE incident_id = :iid"
            params["iid"] = incident_id
        query += " ORDER BY created_at DESC"
        if limit is not None:
            query += " LIMIT :limit OFFSET :offset"
            params["limit"] = limit
            params["offset"] = max(0, int(offset))
        return [self.model_cls.from_row(r) for r in self._db.fetch_all(query, params)]

    def count_all(self, incident_id: str | None = None) -> int:
        if incident_id:
            row = self._db.fetch_one(
                "SELECT COUNT(*) AS n FROM review_verdicts WHERE incident_id = :iid",
                {"iid": incident_id},
            )
        else:
            row = self._db.fetch_one("SELECT COUNT(*) AS n FROM review_verdicts")
        return int(row["n"]) if row and row.get("n") is not None else 0
