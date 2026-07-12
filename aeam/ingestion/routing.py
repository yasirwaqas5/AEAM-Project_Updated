"""
aeam/ingestion/routing.py

Routing job processor (Phase B1.4 — Dataset & Schema Registration).

The :class:`~aeam.ingestion.worker.IngestionWorker` drives a single injected
:data:`~aeam.ingestion.worker.JobProcessor`. B1.4 introduces a second kind of
work (structured datasets) alongside B1.3's documents, so this thin router
dispatches each claimed job to the right sub-processor by ``job.parent_type``::

    parent_type == 'dataset'  -> DatasetIngestJobProcessor   (B1.4)
    otherwise (document/None) -> DocumentIngestJobProcessor  (B1.3)

The worker, the document processor, and the dataset processor are all reused
unchanged — no worker/queue framework is modified, and no processor is
replaced.
"""

from __future__ import annotations

import logging

from aeam.ingestion.worker import JobProcessor
from aeam.registry.models import IngestionJob, ParentType
from aeam.registry.repositories import IngestionJobRepository

logger = logging.getLogger(__name__)


class RoutingJobProcessor:
    """
    Dispatch a claimed ingestion job to a sub-processor by parent type.

    Args:
        document_processor: Handles jobs whose parent is a ``document``
                            (and the default for any unrecognised parent type).
        dataset_processor:  Handles jobs whose parent is a ``dataset``.

    Raises:
        ValueError: If either processor is ``None``.
    """

    def __init__(
        self,
        document_processor: JobProcessor,
        dataset_processor: JobProcessor,
    ) -> None:
        if document_processor is None:
            raise ValueError("document_processor must not be None.")
        if dataset_processor is None:
            raise ValueError("dataset_processor must not be None.")
        self._document_processor = document_processor
        self._dataset_processor = dataset_processor

    def __call__(self, job: IngestionJob, job_repo: IngestionJobRepository) -> None:
        if job.parent_type == ParentType.DATASET:
            logger.debug("RoutingJobProcessor | job_id=%s -> dataset processor", job.job_id)
            return self._dataset_processor(job, job_repo)
        # Documents are the default: covers ParentType.DOCUMENT and any legacy
        # job that predates typed parents.
        logger.debug("RoutingJobProcessor | job_id=%s -> document processor", job.job_id)
        return self._document_processor(job, job_repo)

    def __repr__(self) -> str:
        return (
            f"RoutingJobProcessor(document={self._document_processor!r}, "
            f"dataset={self._dataset_processor!r})"
        )
