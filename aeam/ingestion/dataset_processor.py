"""
aeam/ingestion/dataset_processor.py

Structured dataset job processor (Phase B1.4 — Dataset & Schema Registration).

The structured counterpart to
:class:`~aeam.ingestion.processor.DocumentIngestJobProcessor`. For a claimed
``ingest`` job whose parent is a ``dataset``, it profiles the uploaded tabular
file and registers its structure, reusing existing components and adding no
parallel infrastructure::

    blob_store.get(content_hash)                    # B1.1 storage
      -> read_primary_table + infer_schema          # B1.4 (the only new capability)
      -> SchemaRepository.create(Schema)             # B1.1 registry
      -> DatasetRepository.update(row_count,          # B1.1 registry
             metric_columns, schema_id, INDEXED)
      -> worker marks DONE

The Dataset + active Version rows are created at upload (ingress API); this
processor drives the Dataset through its lifecycle::

    datasets.status:  pending -> processing -> indexed        (or -> error)
    ingestion_jobs:   (worker: validating) -> extracting -> indexing -> (worker: done)

It makes NO Qdrant, LLM, or Orchestrator calls, and re-implements no parsing —
tabular reading + inference live in ``schema_inference``. Structured files are
registered as first-class datasets here rather than indexed as prose documents
(which was B1.3's deliberate stopgap). It is a plain
:class:`~aeam.ingestion.worker.JobProcessor`.
"""

from __future__ import annotations

import logging
from typing import Any

from aeam.ingestion.schema_inference import (
    SchemaInferenceError,
    infer_schema,
    read_primary_table,
)
from aeam.ingestion.validation import SUPPORTED_EXTENSIONS
from aeam.integrations.database import DatabaseClient
from aeam.registry.models import AssetStatus, IngestionJob, JobStatus, ParentType, Schema, _now_iso
from aeam.registry.repositories import (
    DatasetRepository,
    IngestionJobRepository,
    SchemaRepository,
    VersionRepository,
)
from aeam.storage.blob_store import BlobStore

logger = logging.getLogger(__name__)


class DatasetProcessingError(Exception):
    """Structural problem processing a dataset job (e.g. its row is missing)."""

    def __init__(self, reason: str, detail: str) -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(detail)


def _category_from_name(name: str | None) -> str | None:
    """Resolve a format category from a filename extension (e.g. 'x.csv' -> 'csv')."""
    if not name or "." not in name:
        return None
    ext = name.rsplit(".", 1)[-1].strip().lower()
    return SUPPORTED_EXTENSIONS.get(ext)


class DatasetIngestJobProcessor:
    """
    Processes ``ingest`` jobs whose parent is a ``dataset`` (CSV/Excel uploads).

    Args:
        blob_store: Content-addressable store holding the original bytes.
        db:         Shared DatabaseClient, used to build the dataset/schema/
                    version repositories.

    Raises:
        ValueError: If any dependency is ``None``.
    """

    def __init__(self, blob_store: BlobStore, db: DatabaseClient) -> None:
        if blob_store is None:
            raise ValueError("blob_store must not be None.")
        if db is None:
            raise ValueError("db must not be None.")
        self._blob_store = blob_store
        self._dataset_repo = DatasetRepository(db)
        self._schema_repo = SchemaRepository(db)
        self._version_repo = VersionRepository(db)

    # ------------------------------------------------------------------
    # JobProcessor protocol
    # ------------------------------------------------------------------

    def __call__(self, job: IngestionJob, job_repo: IngestionJobRepository) -> None:
        dataset = self._load_dataset(job)

        # Idempotent no-op: identical bytes already profiled (e.g. re-upload).
        if dataset.status == AssetStatus.INDEXED:
            logger.info(
                "DatasetIngestJobProcessor | job_id=%s | dataset_id=%s already indexed — "
                "deduplicated", job.job_id, dataset.dataset_id,
            )
            job_repo.update_progress(
                job.job_id, progress=100, stage="dataset already registered — deduplicated",
            )
            return

        try:
            self._process(job, job_repo, dataset)
        except Exception:
            self._dataset_repo.set_status(dataset.dataset_id, AssetStatus.ERROR)
            raise

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _process(self, job: IngestionJob, job_repo: IngestionJobRepository, dataset: Any) -> None:
        category = _category_from_name(dataset.name) or ""

        # --- EXTRACTING (read the tabular data) -------------------------
        job_repo.update_progress(
            job.job_id, status=JobStatus.EXTRACTING, progress=30,
            stage=f"reading tabular data ({category or 'auto'})",
        )
        self._dataset_repo.set_status(dataset.dataset_id, AssetStatus.PROCESSING)

        data = self._blob_store.get(job.content_hash)
        try:
            df, detail = read_primary_table(data, category, object_name=dataset.name or "data")
        except SchemaInferenceError:
            raise  # stable reason/detail -> job error

        # --- INDEXING (infer schema, register) --------------------------
        job_repo.update_progress(
            job.job_id, status=JobStatus.INDEXING, progress=65,
            stage="inferring schema & registering dataset",
        )
        schema_dict = infer_schema(df, object_name=dataset.name or "data")

        schema_id = self._schema_repo.create(Schema(
            object_name=schema_dict["object_name"],
            source_id=dataset.source_id,
            columns=schema_dict["columns"],
            relationships=[],  # FK/join inference deferred to a later phase
        ))

        self._dataset_repo.update(dataset.dataset_id, {
            "schema_id": schema_id,
            "row_count": schema_dict["row_count"],
            "metric_columns": schema_dict["metric_columns"],
            "status": AssetStatus.INDEXED,
            "last_ingested_at": _now_iso(),
        })

        n_cols = len(schema_dict["columns"])
        n_metrics = len(schema_dict["metric_columns"])
        job_repo.update_progress(
            job.job_id, progress=95,
            stage=(f"registered dataset: {schema_dict['row_count']} row(s), "
                   f"{n_cols} column(s), {n_metrics} metric(s)"),
        )
        logger.info(
            "DatasetIngestJobProcessor | job_id=%s | dataset_id=%s | schema_id=%s | "
            "rows=%d | columns=%d | metrics=%d | detail=%s",
            job.job_id, dataset.dataset_id, schema_id, schema_dict["row_count"],
            n_cols, n_metrics, detail,
        )

    def _load_dataset(self, job: IngestionJob) -> Any:
        if job.parent_type != ParentType.DATASET or not job.parent_id:
            raise DatasetProcessingError(
                "missing_dataset_link",
                f"Job {job.job_id} is not linked to a dataset "
                f"(parent_type={job.parent_type!r}, parent_id={job.parent_id!r}).",
            )
        dataset = self._dataset_repo.get(job.parent_id)
        if dataset is None:
            raise DatasetProcessingError(
                "dataset_not_found",
                f"Job {job.job_id} references dataset {job.parent_id!r}, which does not exist.",
            )
        return dataset

    def __repr__(self) -> str:
        return "DatasetIngestJobProcessor()"
