"""
aeam/ingestion/worker.py

Background async job worker (Phase B1.2 — Ingress API + Async Job System).

Polls IngestionJobRepository for QUEUED jobs and drives them through the
state machine, dispatching each claimed job to an injected JobProcessor
callable. Deliberately performs NO parsing, chunking, embedding, OCR, or
Qdrant indexing itself — only state transitions. A real processor
(extraction/chunking/embedding pipeline) replaces the injected callable in a
later phase; this worker loop never changes as a result.
"""

from __future__ import annotations

import logging
import threading
from typing import Protocol

from aeam.registry.models import IngestionJob, JobStatus
from aeam.registry.repositories import IngestionJobRepository

logger = logging.getLogger(__name__)


class JobProcessor(Protocol):
    """
    Callable that performs the actual work for one claimed IngestionJob.

    Implementations may report fine-grained progress via
    ``job_repo.update_progress(job.job_id, progress=..., stage=...)`` as they
    work, and must return normally on success or raise on failure — the
    worker translates the outcome into ``DONE`` / ``FAILED`` and never
    inspects a return value.
    """

    def __call__(self, job: IngestionJob, job_repo: IngestionJobRepository) -> None:
        ...


class PlaceholderJobProcessor:
    """
    Phase B1.2 stand-in processor.

    Proves the async job infrastructure end-to-end (Queued -> Running ->
    Completed) WITHOUT parsing, chunking, embedding, OCR, or Qdrant indexing —
    all explicitly out of scope for this phase. Always succeeds immediately.
    """

    def __call__(self, job: IngestionJob, job_repo: IngestionJobRepository) -> None:
        logger.info(
            "PlaceholderJobProcessor | job_id=%s | job_type=%s | no real "
            "processing implemented yet (Phase B1.2 infrastructure-only)",
            job.job_id, job.job_type,
        )
        job_repo.update_progress(
            job.job_id,
            progress=100,
            stage="placeholder — no extraction/embedding implemented yet (Phase B1.2)",
        )


class IngestionWorker:
    """
    Background worker draining QUEUED ingestion jobs.

    Runs a poll loop intended as the target of its own daemon thread — the
    exact pattern already used for ``MonitorAgent`` in ``main.py``
    (``threading.Thread(target=worker.start, daemon=True)``); no new
    queue/worker framework is introduced.

    State machine, mapped onto the existing (Phase B1.1) JobStatus vocabulary
    plus the one additive ``CANCELLED`` terminal state::

        QUEUED --(worker claims)--> VALIDATING --> DONE
                                               \\--> FAILED
        QUEUED --(operator cancels via API)--> CANCELLED   (never claimed)

    A job already moved to CANCELLED by the API is simply never returned by
    ``next_queued()`` again. A processor exception is caught, logged, and
    recorded as a FAILED job with a structured error string — it never kills
    the worker thread or the poll loop.

    Args:
        job_repo:      Repository used to poll for and update jobs.
        processor:     Callable invoked for each claimed job. Defaults to
                       :class:`PlaceholderJobProcessor`.
        poll_interval: Seconds to sleep between polls when the queue is empty.

    Raises:
        ValueError: If ``job_repo`` is ``None`` or ``poll_interval`` <= 0.
    """

    def __init__(
        self,
        job_repo: IngestionJobRepository,
        processor: JobProcessor | None = None,
        poll_interval: float = 2.0,
    ) -> None:
        if job_repo is None:
            raise ValueError("job_repo must not be None.")
        if poll_interval <= 0:
            raise ValueError(f"poll_interval must be > 0. Got: {poll_interval}.")
        self._job_repo = job_repo
        self._processor = processor or PlaceholderJobProcessor()
        self._poll_interval = poll_interval
        self._stop_event = threading.Event()
        self._processed_count = 0
        self._failed_count = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """
        Run the poll loop until :meth:`stop` is called. Blocking — the
        intended target of a daemon thread, never called directly on the
        request thread.
        """
        logger.info("IngestionWorker started | poll_interval=%.1fs", self._poll_interval)
        while not self._stop_event.is_set():
            try:
                self._run_cycle()
            except Exception as exc:  # noqa: BLE001
                # A cycle-level failure (e.g. a transient DB error) must never
                # kill the worker thread — log and keep polling.
                logger.error("IngestionWorker cycle error: %s", exc, exc_info=True)
            self._stop_event.wait(self._poll_interval)
        logger.info("IngestionWorker stopped.")

    def stop(self) -> None:
        """Signal the poll loop to exit after its current cycle (graceful shutdown)."""
        self._stop_event.set()

    def run_once(self) -> bool:
        """
        Claim and process at most one queued job, synchronously.

        Exposed for tests and manual draining; :meth:`start`'s loop calls
        this repeatedly.

        Returns:
            ``True`` if a job was claimed (regardless of success/failure),
            ``False`` if the queue was empty.
        """
        return self._run_cycle()

    @property
    def processed_count(self) -> int:
        """Jobs that reached DONE since this worker instance started."""
        return self._processed_count

    @property
    def failed_count(self) -> int:
        """Jobs that reached FAILED since this worker instance started."""
        return self._failed_count

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _run_cycle(self) -> bool:
        job = self._job_repo.next_queued()
        if job is None:
            return False

        logger.info(
            "IngestionWorker | claimed job_id=%s | job_type=%s | content_hash=%s",
            job.job_id, job.job_type, job.content_hash,
        )
        self._job_repo.update_progress(
            job.job_id, status=JobStatus.VALIDATING, progress=10, stage="worker claimed job",
        )

        try:
            self._processor(job, self._job_repo)
        except Exception as exc:  # noqa: BLE001
            logger.error("IngestionWorker | job_id=%s failed: %s", job.job_id, exc, exc_info=True)
            self._job_repo.update_progress(job.job_id, status=JobStatus.FAILED, error=str(exc))
            self._failed_count += 1
            return True

        self._job_repo.update_progress(job.job_id, status=JobStatus.DONE, progress=100)
        self._processed_count += 1
        logger.info("IngestionWorker | job_id=%s completed", job.job_id)
        return True

    def __repr__(self) -> str:
        return (
            f"IngestionWorker(processed={self._processed_count}, "
            f"failed={self._failed_count}, poll_interval={self._poll_interval})"
        )
