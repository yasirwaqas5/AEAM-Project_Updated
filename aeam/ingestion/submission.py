"""
aeam/ingestion/submission.py

The single ingestion submission path (Phase F7).

Before this module, the sequence that turns bytes into an ingestible asset
lived inside ``aeam/api/ingest.py``'s ``upload_file`` handler:

    validate → BlobStore.put → in-flight dedup → get-or-create the
    Document/Dataset → enqueue an INGEST job

That was fine while HTTP upload was the only way content entered the
platform. F7 adds eight connectors, and each of them needs exactly that
sequence — so the choice was to extract it once or to write it nine times.
Writing it nine times would give nine places for the dedup rule to drift and
would make "a connector document is indistinguishable from an uploaded one"
a claim rather than a consequence.

**This is the only way content enters the pipeline.** ``upload_file``
delegates here; :class:`~aeam.connectors.sync.ConnectorSyncEngine` delegates
here. There is no second chunker, embedder, indexer, or job type: every
submission produces the same ``JobType.INGEST`` row that the unchanged
``IngestionWorker`` claims and the unchanged ``DocumentIngestJobProcessor``
processes (ENG-6, TECH-2).

What this module deliberately does NOT do
-----------------------------------------
* It does not chunk, embed, index, or extract — the existing processor owns
  all of that, and this module never touches it.
* It does not decide *when* content is submitted (that is the caller's job:
  an HTTP request, or a connector sync run).
* It raises no HTTP exception. Validation failures surface as
  :class:`~aeam.ingestion.validation.IngestValidationError`, which the API
  layer already translates to a 422 and a connector sync records as a
  skipped artifact. Putting HTTP semantics here would make the module
  unusable from a background sync.

Idempotency, inherited not reimplemented
----------------------------------------
Every dedup rule below is the one the upload path already had:

* **blob level** — content-addressed storage means identical bytes produce
  one blob;
* **job level** — ``find_active_by_content_hash`` reuses an in-flight job
  rather than queuing a second one;
* **asset level** — identical bytes map to the same ``Document`` (by
  ``content_hash``) or ``Dataset`` (by its active Version's hash);
* **processor level** — an already-``indexed`` document short-circuits
  without re-embedding.

Together these are what make a repeated connector sync produce no duplicate
document, embedding, or metadata: the connector does not need its own
idempotency mechanism, because submitting the same bytes twice is already a
no-op four times over.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from aeam.ingestion.validation import validate_upload
from aeam.registry.models import (
    AssetStatus,
    Dataset,
    Document,
    IngestionJob,
    JobStatus,
    JobType,
    ParentType,
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

logger = logging.getLogger(__name__)

#: Structured formats become first-class datasets (schema + metric columns);
#: every other supported format is registered as a retrievable document.
#: Categories come from ``aeam.ingestion.validation.SUPPORTED_EXTENSIONS``.
#: Moved here from ``aeam/api/ingest.py`` unchanged, and re-exported there so
#: the routing rule has exactly one definition.
STRUCTURED_CATEGORIES: frozenset[str] = frozenset({"csv", "excel"})

_DEFAULT_UPLOAD_SOURCE_NAME = "Manual Upload"


@dataclass
class SubmissionResult:
    """The outcome of one submission.

    Carries everything both callers need: the API turns it into a 202 body,
    and a connector sync turns it into a provenance row and a counter.
    """

    job: Any
    job_id: str
    parent_type: str
    parent_id: str
    content_hash: str
    blob_uri: str
    category: str
    #: True when identical bytes were already being ingested and that job was
    #: reused instead of a second one being queued.
    duplicate_of_content: bool = False
    #: True when this submission registered a NEW Document/Dataset; False when
    #: identical bytes already had one and it was reused.
    asset_created: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


def get_or_create_document(
    doc_repo: DocumentRepository,
    version_repo: VersionRepository,
    *,
    source_id: str,
    filename: str | None,
    category: str,
    content_hash: str,
    blob_uri: str,
    semantic_type: str | None = None,
) -> tuple[str, bool]:
    """
    Return ``(doc_id, created)`` for the document backing this submission.

    Content-addressed dedup at the document level: identical bytes (same
    ``content_hash``) always map to the same Document, so re-submitting a
    file never creates a duplicate document — it reuses the existing one
    (whatever its status), and the processor decides whether any work is
    needed.

    A brand-new document is created ``pending`` together with its first active
    Version (``version=1``), which records the BlobStore URI of the original.
    The background worker/processor advances it to ``processing`` →
    ``indexed``.

    Phase E12 (MOD-4/RAG-7): ``category`` is the FORMAT the validator detected
    and continues to be stored in ``doc_type``, unchanged. ``semantic_type``
    is the separately-DECLARED semantic type ("runbook", "incident_report",
    …) that retrieval's authoritative-source bonus actually needs. When an
    identical-bytes document already exists and this submission declares a
    semantic type the stored row lacks, the declaration is recorded on the
    existing row — re-submitting a file specifically to classify it is a
    reasonable thing to do, and silently discarding the declaration would be
    the same defect E12 fixed.
    """
    existing = doc_repo.get_by_content_hash(content_hash)
    if existing is not None:
        if semantic_type and not existing.semantic_type:
            doc_repo.update(existing.doc_id, {"semantic_type": semantic_type})
            logger.info(
                "submit | recorded declared semantic_type=%r on existing doc_id=%s",
                semantic_type, existing.doc_id,
            )
        return existing.doc_id, False

    doc_id = doc_repo.create(
        Document(
            title=filename or "untitled",
            source_id=source_id,
            origin_path=filename,
            doc_type=category,
            semantic_type=semantic_type,
            content_hash=content_hash,
            status=AssetStatus.PENDING,
        )
    )
    version_repo.create(
        Version(
            parent_type=ParentType.DOCUMENT,
            parent_id=doc_id,
            version=1,
            content_hash=content_hash,
            blob_ref=blob_uri,
            is_active=True,
        )
    )
    return doc_id, True


def get_or_create_dataset(
    dataset_repo: DatasetRepository,
    version_repo: VersionRepository,
    *,
    source_id: str,
    filename: str | None,
    content_hash: str,
    blob_uri: str,
) -> tuple[str, bool]:
    """
    Return ``(dataset_id, created)`` for the dataset backing a structured
    submission.

    Content-addressed dedup at the dataset level: the ``datasets`` table has
    no ``content_hash`` column (by B1.1 design), so dedup keys off the active
    Version's ``content_hash`` (indexed) — identical bytes reuse the existing
    dataset rather than creating a duplicate.

    A brand-new dataset is created ``pending`` with its first active Version
    (``version=1``) recording the BlobStore URI of the original. The
    background worker/processor infers its schema and advances it
    ``processing`` → ``indexed``. ``dataset.name`` is the filename — the
    processor derives the format (csv/excel) from its extension.
    """
    existing = version_repo.find_active_by_content_hash(ParentType.DATASET, content_hash)
    if existing is not None:
        return existing.parent_id, False

    dataset_id = dataset_repo.create(
        Dataset(
            name=filename or "untitled",
            source_id=source_id,
            status=AssetStatus.PENDING,
        )
    )
    version_repo.create(
        Version(
            parent_type=ParentType.DATASET,
            parent_id=dataset_id,
            version=1,
            content_hash=content_hash,
            blob_ref=blob_uri,
            is_active=True,
        )
    )
    return dataset_id, True


def get_or_create_upload_source(source_repo: SourceRepository) -> str:
    """
    Return the id of the singleton ``upload`` Source, creating it on first use.

    Every manual upload belongs to one logical source so ``documents``/
    ``datasets`` always have a valid ``source_id``. Connectors do NOT use this
    — each connector has its own ``sources`` row, which is what makes
    provenance answerable.
    """
    existing = source_repo.list_by_kind(SourceKind.UPLOAD)
    if existing:
        return existing[0].source_id
    return source_repo.create(
        Source(
            name=_DEFAULT_UPLOAD_SOURCE_NAME,
            kind=SourceKind.UPLOAD,
            status=SourceStatus.ACTIVE,
            config={},
        )
    )


class IngestionSubmitter:
    """
    Submits content into the existing ingestion pipeline.

    One instance can serve every caller: it holds only repositories and the
    blob store, all of which are already shared singletons.

    Args:
        db:         The shared ``DatabaseClient``.
        blob_store: The shared ``BlobStore`` (E4).

    Raises:
        ValueError: If either dependency is ``None``.
    """

    def __init__(self, db: Any, blob_store: Any) -> None:
        if db is None:
            raise ValueError("db must not be None.")
        if blob_store is None:
            raise ValueError("blob_store must not be None.")
        self._db = db
        self._blob_store = blob_store

    def submit(
        self,
        data: bytes,
        *,
        filename: str | None,
        content_type: str | None,
        source_id: str,
        semantic_type: str | None = None,
        category: str | None = None,
    ) -> SubmissionResult:
        """
        Put ``data`` into the pipeline and return what happened.

        Args:
            data:          The artifact's bytes.
            filename:      Name used for validation, the asset title, and
                           ``origin_path``. A connector supplies the upstream
                           artifact's name here.
            content_type:  MIME type, or ``None``.
            source_id:     The ``sources`` row this content belongs to — the
                           upload source for an upload, the connector's own
                           source row for connector content. This is what
                           makes provenance answerable later.
            semantic_type: Optional declared E12 semantic doc type. Callers
                           are expected to have validated it against
                           :class:`SemanticDocType` already; an unvalidated
                           value would be stored verbatim and silently fail
                           to earn retrieval's authoritative-source bonus.
            category:      Pre-computed format category. When ``None`` the
                           validator computes it, which is the normal path.

        Returns:
            A :class:`SubmissionResult`.

        Raises:
            IngestValidationError: If the artifact's metadata is not
                                   acceptable. Deliberately NOT translated to
                                   an HTTP error here — the API layer already
                                   does that, and a connector sync needs to
                                   record it as a skipped artifact instead.
        """
        resolved_category = category or validate_upload(filename, content_type, len(data))

        blob_ref = self._blob_store.put(data, content_type=content_type)
        job_repo = IngestionJobRepository(self._db)

        existing = job_repo.find_active_by_content_hash(blob_ref.content_hash)
        if existing is not None:
            logger.info(
                "submit | identical content already in flight — reusing job_id=%s | "
                "content_hash=%s", existing.job_id, blob_ref.content_hash,
            )
            return SubmissionResult(
                job=existing,
                job_id=existing.job_id,
                parent_type=existing.parent_type or "",
                parent_id=existing.parent_id or "",
                content_hash=blob_ref.content_hash,
                blob_uri=blob_ref.uri,
                category=resolved_category,
                duplicate_of_content=True,
                asset_created=False,
            )

        version_repo = VersionRepository(self._db)
        if resolved_category in STRUCTURED_CATEGORIES:
            parent_type = ParentType.DATASET
            parent_id, asset_created = get_or_create_dataset(
                DatasetRepository(self._db),
                version_repo,
                source_id=source_id,
                filename=filename,
                content_hash=blob_ref.content_hash,
                blob_uri=blob_ref.uri,
            )
        else:
            parent_type = ParentType.DOCUMENT
            parent_id, asset_created = get_or_create_document(
                DocumentRepository(self._db),
                version_repo,
                source_id=source_id,
                filename=filename,
                category=resolved_category,
                content_hash=blob_ref.content_hash,
                blob_uri=blob_ref.uri,
                semantic_type=semantic_type,
            )

        job = IngestionJob(
            job_type=JobType.INGEST,
            source_id=source_id,
            parent_type=parent_type,
            parent_id=parent_id,
            status=JobStatus.QUEUED,
            progress=0,
            stage=f"queued — {resolved_category} upload ({filename})",
            content_hash=blob_ref.content_hash,
        )
        job_id = job_repo.create(job)
        created = job_repo.get(job_id)

        logger.info(
            "submit | job_id=%s | %s=%s | created=%s | filename=%r | category=%s | "
            "size=%d | content_hash=%s",
            job_id, parent_type, parent_id, asset_created, filename,
            resolved_category, len(data), blob_ref.content_hash,
        )
        return SubmissionResult(
            job=created,
            job_id=job_id,
            parent_type=parent_type,
            parent_id=parent_id,
            content_hash=blob_ref.content_hash,
            blob_uri=blob_ref.uri,
            category=resolved_category,
            duplicate_of_content=False,
            asset_created=asset_created,
        )

    def __repr__(self) -> str:
        return "IngestionSubmitter()"
