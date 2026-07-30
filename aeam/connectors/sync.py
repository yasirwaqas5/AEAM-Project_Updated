"""
aeam/connectors/sync.py

The connector synchronization engine (Phase F7).

The single place that drives a connector into the platform. It knows the
connector *contract* and nothing about any connector — there is no
per-connector branch here, and adding a ninth connector requires no change to
this file.

What a sync run does
--------------------
1. authenticate through the connector (credentials via ``SecretManager`` only);
2. list upstream artifacts, passing the last cursor so upstream can filter;
3. for each artifact, compare against its recorded ``connector_artifacts``
   row and **skip it entirely** when nothing changed;
4. for the rest, fetch bytes and hand them to the **existing**
   :class:`~aeam.ingestion.submission.IngestionSubmitter`;
5. record provenance and advance the cursor.

Step 4 is the whole architectural point. Connector content does not travel a
connector path — it travels the upload path. Same validator, same
``BlobStore``, same content-addressed dedup, same ``Document`` row, same
``JobType.INGEST`` job, same ``IngestionWorker``, same
``DocumentIngestJobProcessor``, same chunker, same embeddings, same Qdrant
collection. After ingestion there is nothing left that distinguishes a
SharePoint page from an uploaded PDF except its provenance row, which is
exactly the intent (ENG-6, TECH-2).

Idempotency
-----------
Re-running a sync with no upstream change performs no download, creates no
document, no embedding, no metadata row, and no ingestion job. Three
independent layers guarantee it, and any one of them would be sufficient:

* **the change signature** — an upstream hash/version/timestamp identical to
  the recorded one short-circuits before ``fetch_artifact`` is even called;
* **the content hash** — when upstream exposes no signature, the bytes are
  fetched and hashed, and an identical hash short-circuits before submission;
* **the existing dedup** — if a submission does happen, identical bytes reuse
  the same blob, the same in-flight job, the same ``Document``, and the
  processor no-ops on an already-``indexed`` document.

Failure isolation
-----------------
Every connector's run is wrapped. A connector that raises, hangs on a bad
config, or returns garbage produces a FAILED run record and nothing else: the
next connector still syncs, ingestion still runs, KPI collection still runs,
retrieval still works, and startup is unaffected. :meth:`sync_all` isolates
per connector; :meth:`sync_source` isolates per artifact, so one poisoned
document does not abandon the other ninety-nine.

Nothing here runs on a timer. Sync is triggered explicitly through
``POST /api/v1/connectors/sync/{source_id}``, matching the repo's existing
posture (the APScheduler stub was removed in E1 and autonomous polling is
deferred) — and, more importantly, keeping connector work off the ingestion
worker's thread, which is what makes the isolation above real.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from aeam.connectors.base import (
    ConnectorArtifactRef,
    EnterpriseConnector,
    content_hash_of,
    sanitize_error,
)
from aeam.ingestion.validation import IngestValidationError
from aeam.registry.models import (
    ConnectorArtifact,
    ConnectorCapability,
    ConnectorSyncRun,
    SourceStatus,
    SyncRunStatus,
    _now_iso,
)
from aeam.registry.repositories import (
    ConnectorArtifactRepository,
    ConnectorSyncRunRepository,
    SourceRepository,
)

logger = logging.getLogger(__name__)

#: Default cap on artifacts one run will process. A bound rather than a
#: preference: an unbounded first sync against a 200k-document SharePoint
#: tenant would hold a request open indefinitely and flood the ingestion
#: queue. Reaching it is reported as ``truncated``, and the next run continues
#: from the advanced cursor — so a large corpus arrives over several runs
#: instead of one that never finishes.
DEFAULT_MAX_ARTIFACTS_PER_RUN: int = 500


@dataclass
class SyncOutcome:
    """One connector's sync result — what the API returns and health reads."""

    source_id: str
    connector: str
    status: str
    listed: int = 0
    changed: int = 0
    processed: int = 0
    skipped: int = 0
    failed: int = 0
    duration_seconds: float = 0.0
    error: str | None = None
    run_id: str | None = None
    cursor_from: str | None = None
    cursor_to: str | None = None
    #: True when the run hit ``max_artifacts`` and more remain upstream.
    truncated: bool = False
    #: Per-artifact failures, each with its own credential-free reason, so one
    #: bad artifact is diagnosable without re-running the sync.
    artifact_errors: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "connector": self.connector,
            "status": self.status,
            "listed_count": self.listed,
            "changed_count": self.changed,
            "processed_count": self.processed,
            "skipped_count": self.skipped,
            "failed_count": self.failed,
            "duration_seconds": round(self.duration_seconds, 4),
            "error": self.error,
            "run_id": self.run_id,
            "cursor_from": self.cursor_from,
            "cursor_to": self.cursor_to,
            "truncated": self.truncated,
            "artifact_errors": list(self.artifact_errors),
        }


class ConnectorSyncEngine:
    """
    Drives connectors into the existing ingestion pipeline.

    Args:
        db:            The shared ``DatabaseClient``.
        submitter:     The shared
                       :class:`~aeam.ingestion.submission.IngestionSubmitter`
                       — the ONLY way this engine puts content into the
                       platform. Required, so there is no code path here that
                       could bypass it.
        registry:      The :class:`~aeam.connectors.registry.ConnectorRegistry`
                       that builds a connector for a ``sources`` row.
        max_artifacts: Per-run artifact cap.

    Raises:
        ValueError: If any required dependency is ``None``.
    """

    def __init__(
        self,
        db: Any,
        submitter: Any,
        registry: Any,
        max_artifacts: int = DEFAULT_MAX_ARTIFACTS_PER_RUN,
    ) -> None:
        if db is None:
            raise ValueError("db must not be None.")
        if submitter is None:
            raise ValueError(
                "submitter must not be None. The sync engine has no ingestion path "
                "of its own — it submits through the existing IngestionSubmitter."
            )
        if registry is None:
            raise ValueError("registry must not be None.")
        self._db = db
        self._submitter = submitter
        self._registry = registry
        self._max_artifacts = max(1, int(max_artifacts))
        self._sources = SourceRepository(db)
        self._artifacts = ConnectorArtifactRepository(db)
        self._runs = ConnectorSyncRunRepository(db)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def sync_all(self, triggered_by: str | None = None) -> list[dict[str, Any]]:
        """
        Sync every enabled, connector-kind source.

        Each connector is isolated: an exception from one is caught, recorded
        against that connector, and the loop continues. A deployment with one
        misconfigured connector still syncs the other seven.
        """
        outcomes: list[dict[str, Any]] = []
        for source in self._connector_sources():
            outcomes.append(self.sync_source(source.source_id, triggered_by=triggered_by))
        return outcomes

    def sync_source(self, source_id: str, triggered_by: str | None = None) -> dict[str, Any]:
        """
        Sync one connector.

        Never raises. Every failure — unknown source, disabled connector,
        missing configuration, rejected credentials, unreachable upstream, a
        connector implementation bug — becomes a FAILED run record and a
        returned outcome. That is what "a connector failure never blocks
        anything" means concretely: the caller always gets an answer.
        """
        source = self._sources.get(source_id)
        if source is None:
            return SyncOutcome(
                source_id=source_id, connector="unknown", status=SyncRunStatus.FAILED,
                error=f"No source with id {source_id!r} is registered.",
            ).as_dict()

        connector: EnterpriseConnector | None = None
        started = time.time()
        run_id: str | None = None
        cursor_from = source.last_synced_at

        try:
            connector = self._registry.build(source)
            if connector is None:
                return SyncOutcome(
                    source_id=source_id, connector=source.kind, status=SyncRunStatus.FAILED,
                    error=(
                        f"Connector kind {source.kind!r} is not enabled or has no "
                        "implementation registered. Nothing was synced."
                    ),
                ).as_dict()

            run_id = self._runs.create(ConnectorSyncRun(
                source_id=source_id,
                connector=source.kind,
                status=SyncRunStatus.RUNNING,
                cursor_from=cursor_from,
                triggered_by=triggered_by,
            ))

            outcome = self._run(connector, source, cursor_from, run_id)
            outcome.duration_seconds = time.time() - started
            outcome.run_id = run_id

            self._runs.finish(
                run_id,
                status=outcome.status,
                duration_seconds=outcome.duration_seconds,
                listed_count=outcome.listed,
                changed_count=outcome.changed,
                processed_count=outcome.processed,
                skipped_count=outcome.skipped,
                failed_count=outcome.failed,
                error=outcome.error,
                cursor_to=outcome.cursor_to,
            )
            if outcome.status in SyncRunStatus.ADVANCES_CURSOR:
                self._sources.update(source_id, {
                    "last_synced_at": outcome.cursor_to,
                    "status": SourceStatus.ACTIVE,
                })
            else:
                # The source's own status records the connector's last known
                # state, so an operator listing sources sees the failure
                # without opening the run ledger.
                self._sources.update(source_id, {"status": SourceStatus.ERROR})
            return outcome.as_dict()

        except Exception as exc:  # noqa: BLE001
            # The isolation boundary. Anything at all that escapes the run
            # lands here, is sanitised, and is recorded — never propagated.
            #
            # The sanitiser is resolved defensively: a connector broken badly
            # enough to fail here may also be broken enough to lack a working
            # `sanitize`, and an AttributeError raised INSIDE the isolation
            # handler would defeat the whole point of having one.
            reason = self._sanitize_with(connector, exc)
            logger.error(
                "connector sync failed | source_id=%s | kind=%s | %s",
                source_id, source.kind, reason,
            )
            duration = time.time() - started
            if run_id is not None:
                try:
                    self._runs.finish(
                        run_id, status=SyncRunStatus.FAILED,
                        duration_seconds=duration, error=reason,
                    )
                except Exception as record_exc:  # noqa: BLE001
                    logger.warning("connector sync | run record failed: %s", record_exc)
            try:
                self._sources.update(source_id, {"status": SourceStatus.ERROR})
            except Exception:  # noqa: BLE001
                pass
            return SyncOutcome(
                source_id=source_id, connector=source.kind, status=SyncRunStatus.FAILED,
                duration_seconds=duration, error=reason, run_id=run_id,
                cursor_from=cursor_from,
            ).as_dict()
        finally:
            if connector is not None:
                try:
                    connector.close()
                except Exception as close_exc:  # noqa: BLE001
                    logger.warning("connector sync | close failed: %s", close_exc)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _sanitize_with(connector: Any, message: Any) -> str:
        """Sanitise ``message`` using the connector's scrubber when it has one.

        Falls back to the module-level :func:`sanitize_error` — which scrubs
        nothing, because a connector that cannot be asked has resolved no
        secrets this engine knows about. The fallback exists so the isolation
        handler itself can never raise; a credential cannot leak through it,
        since a connector that never resolved a secret has none to leak.
        """
        scrubber = getattr(connector, "sanitize", None)
        if callable(scrubber):
            try:
                return str(scrubber(message))
            except Exception:  # noqa: BLE001
                pass
        return sanitize_error(message)

    def _connector_sources(self) -> list[Any]:
        """Every registered source whose kind has a connector implementation.

        Reads through the registry so "which kinds are syncable" has one
        definition. A source of kind ``upload`` or ``gsheet`` is skipped
        silently — those are not connectors, and reporting them as failed
        connectors would be noise.
        """
        try:
            return [
                source for source in self._sources.list_all()
                if self._registry.is_connector_kind(source.kind)
            ]
        except Exception as exc:  # noqa: BLE001
            logger.error("connector sync | source listing failed: %s", exc)
            return []

    def _run(
        self,
        connector: EnterpriseConnector,
        source: Any,
        cursor_from: str | None,
        run_id: str,
    ) -> SyncOutcome:
        """Execute one connector's sync. Raises only for whole-run failures;
        per-artifact failures are collected and reported."""
        outcome = SyncOutcome(
            source_id=source.source_id, connector=source.kind,
            status=SyncRunStatus.RUNNING, cursor_from=cursor_from,
        )

        configured, config_reason = connector.validate_config()
        if not configured:
            outcome.status = SyncRunStatus.FAILED
            outcome.error = config_reason
            return outcome

        if not connector.authenticate():
            outcome.status = SyncRunStatus.FAILED
            outcome.error = connector.sanitize(
                connector.health().get("auth_error")
                or "Authentication failed and the connector gave no reason."
            )
            return outcome

        # A METRICS connector has no artifacts to walk: its rows reach the
        # platform through CompositeKPISource on MonitorAgent's own cycle, not
        # through a sync run. The run still exists and still records success,
        # because "we verified this connector is reachable and authenticated"
        # is exactly what an operator wants a metrics-connector sync to mean.
        if connector.capability == ConnectorCapability.METRICS:
            outcome.status = SyncRunStatus.SUCCEEDED
            outcome.cursor_to = _now_iso()
            probe = connector.fetch_rows(str(source.config.get("selector") or source.name or ""))
            outcome.listed = len(probe)
            logger.info(
                "connector sync | source_id=%s | kind=%s | metrics connector verified | "
                "probe_rows=%d", source.source_id, source.kind, outcome.listed,
            )
            return outcome

        refs = connector.list_artifacts(since=cursor_from) or []
        outcome.listed = len(refs)
        if len(refs) > self._max_artifacts:
            outcome.truncated = True
            refs = refs[: self._max_artifacts]

        for ref in refs:
            try:
                self._sync_artifact(connector, source, ref, outcome)
            except Exception as exc:  # noqa: BLE001
                # Per-artifact isolation: one poisoned document must not
                # abandon the rest of the run.
                outcome.failed += 1
                reason = self._sanitize_with(connector, exc)
                outcome.artifact_errors.append({
                    "external_id": ref.external_id, "title": ref.title, "reason": reason,
                })
                logger.warning(
                    "connector sync | artifact failed | source_id=%s | external_id=%s | %s",
                    source.source_id, ref.external_id, reason,
                )

        outcome.cursor_to = _now_iso()
        if outcome.failed == 0:
            outcome.status = SyncRunStatus.SUCCEEDED
        elif outcome.processed > 0 or outcome.skipped > 0:
            # Some artifacts landed. Neither "succeeded" nor "failed" is true,
            # and PARTIAL is the only honest answer.
            outcome.status = SyncRunStatus.PARTIAL
            outcome.error = f"{outcome.failed} of {outcome.listed} artifact(s) failed."
        else:
            outcome.status = SyncRunStatus.FAILED
            outcome.error = f"All {outcome.failed} listed artifact(s) failed."

        logger.info(
            "connector sync | source_id=%s | kind=%s | status=%s | listed=%d changed=%d "
            "processed=%d skipped=%d failed=%d",
            source.source_id, source.kind, outcome.status, outcome.listed,
            outcome.changed, outcome.processed, outcome.skipped, outcome.failed,
        )
        return outcome

    def _sync_artifact(
        self,
        connector: EnterpriseConnector,
        source: Any,
        ref: ConnectorArtifactRef,
        outcome: SyncOutcome,
    ) -> None:
        """
        Sync one artifact, doing as little work as its change state allows.

        Three exits, cheapest first:

        1. **upstream says unchanged** — the recorded change signature matches,
           so nothing is downloaded. This is where incremental sync earns its
           keep.
        2. **bytes are unchanged** — upstream exposed no signature, so the
           bytes were fetched, but their hash matches what we ingested last
           time. No submission, no re-embedding.
        3. **changed (or new)** — submitted through the existing pipeline, and
           the provenance row is updated.
        """
        recorded = self._artifacts.get_by_external_id(source.source_id, ref.external_id)
        signature = ref.change_signature()
        recorded_signature = (recorded.extra or {}).get("change_signature") if recorded else None

        # Exit 1 — upstream itself told us nothing changed.
        if recorded is not None and signature is not None and signature == recorded_signature:
            self._artifacts.record_skipped(recorded.artifact_id, recorded.skip_count)
            outcome.skipped += 1
            return

        outcome.changed += 1
        data = connector.fetch_artifact(ref)
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError(
                f"fetch_artifact must return bytes; {type(connector).__name__} returned "
                f"{type(data).__name__}."
            )
        data = bytes(data)
        fetched_hash = content_hash_of(data)

        # Exit 2 — the bytes are byte-for-byte what we already ingested.
        if recorded is not None and recorded.source_content_hash == fetched_hash:
            self._artifacts.record_skipped(recorded.artifact_id, recorded.skip_count)
            outcome.skipped += 1
            # `changed` counted an upstream-reported change that turned out not
            # to be one (a touched timestamp). Correct the count rather than
            # leaving a figure the numbers cannot justify.
            outcome.changed -= 1
            return

        # Exit 3 — real content, into the EXISTING ingestion pipeline.
        try:
            result = self._submitter.submit(
                data,
                filename=ref.title,
                content_type=ref.content_type,
                source_id=source.source_id,
                semantic_type=ref.semantic_type,
            )
        except IngestValidationError as exc:
            # An artifact the platform cannot ingest (unsupported format,
            # oversized) is a SKIPPED artifact with a stated reason, not a run
            # failure: a SharePoint library containing one .exe must not stop
            # its other documents from arriving.
            outcome.skipped += 1
            outcome.changed -= 1
            outcome.artifact_errors.append({
                "external_id": ref.external_id,
                "title": ref.title,
                "reason": f"not ingestible: {exc.reason} — {exc.detail}",
                "skipped": True,
            })
            logger.info(
                "connector sync | artifact not ingestible | source_id=%s | external_id=%s | %s",
                source.source_id, ref.external_id, exc.reason,
            )
            return

        self._artifacts.record_ingested(
            ConnectorArtifact(
                source_id=source.source_id,
                external_id=ref.external_id,
                connector=source.kind,
                source_type=ref.source_type,
                title=ref.title,
                source_url=ref.source_url,
                source_timestamp=ref.source_timestamp,
                source_version=ref.source_version,
                semantic_type=ref.semantic_type,
                extra={
                    # Persisted so the next run's exit-1 comparison works. This
                    # is the whole mechanism behind "repeated sync does no work".
                    "change_signature": signature,
                    "size_bytes": ref.size_bytes,
                    "upstream_metadata": ref.metadata or {},
                    "duplicate_of_content": result.duplicate_of_content,
                },
            ),
            content_hash=fetched_hash,
            parent_type=result.parent_type,
            parent_id=result.parent_id,
            job_id=result.job_id,
        )
        outcome.processed += 1

    def __repr__(self) -> str:
        return f"ConnectorSyncEngine(max_artifacts={self._max_artifacts})"
