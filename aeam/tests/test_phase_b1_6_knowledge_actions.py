"""
aeam/tests/test_phase_b1_6_knowledge_actions.py

Enterprise Knowledge Center — write/action capabilities (re-index, preview,
purge-delete) added on top of the read-only B1.6 API.

Exercises aeam.api.knowledge via a real FastAPI TestClient, real SQLite
DatabaseClient, and a real LocalDiskBlobStore (tmp_path — so preview reads
real bytes and purge really deletes files on disk). Qdrant is a lightweight
in-memory fake (records .delete() calls) — no live Qdrant required, and no
existing code is mocked: only the external Qdrant service boundary is
stubbed, exactly as the ingestion pipeline itself calls it.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aeam.api.knowledge import router
from aeam.integrations.database import DatabaseClient
from aeam.registry.models import (
    AssetStatus, Dataset, Document, JobStatus, JobType, ParentType, Schema, Source, SourceKind, Version,
)
from aeam.registry.repositories import (
    DatasetRepository, DocumentRepository, IngestionJobRepository, SchemaRepository,
    SourceRepository, VersionRepository,
)
from aeam.storage.blob_store import LocalDiskBlobStore


class _FakeQdrantClient:
    """Records every delete() call; no live Qdrant needed."""

    def __init__(self):
        self.deleted_calls: list[tuple[str, list[str]]] = []
        self.raise_on_delete = False

    def delete(self, collection_name: str, points_selector, **kwargs):
        if self.raise_on_delete:
            raise RuntimeError("simulated Qdrant outage")
        self.deleted_calls.append((collection_name, list(points_selector)))


class _FakeIngestionPipeline:
    collection = "aeam_documents"


@pytest.fixture()
def db(tmp_path):
    client = DatabaseClient(database_url=f"sqlite:///{(tmp_path / 'b16actions.db').as_posix()}")
    yield client
    client.dispose()


@pytest.fixture()
def blob_store(tmp_path):
    return LocalDiskBlobStore(tmp_path / "blobs")


@pytest.fixture()
def qdrant():
    return _FakeQdrantClient()


@pytest.fixture()
def client(db, blob_store, qdrant):
    class _Container:
        pass
    container = _Container()
    container.db = db
    container.blob_store = blob_store
    container.qdrant_client = qdrant
    container.ingestion_pipeline = _FakeIngestionPipeline()

    app = FastAPI()
    app.include_router(router)
    app.state.container = container
    return TestClient(app), container


def _seed_source(db, name="Manual Upload"):
    return SourceRepository(db).create(Source(name=name, kind=SourceKind.UPLOAD))


def _seed_document(db, blob_store, source_id, title="runbook.md", body=b"# Runbook\n\nRestart the service.",
                   status=AssetStatus.INDEXED, chunk_ids=None):
    ref = blob_store.put(body)
    doc_repo = DocumentRepository(db)
    version_repo = VersionRepository(db)
    doc_id = doc_repo.create(Document(
        title=title, source_id=source_id, origin_path=title, doc_type="markdown",
        status=status, chunk_count=len(chunk_ids or []), content_hash=ref.content_hash,
    ))
    version_repo.create(Version(
        parent_type=ParentType.DOCUMENT, parent_id=doc_id, version=1,
        content_hash=ref.content_hash, blob_ref=ref.uri, is_active=True,
        chunk_ids=chunk_ids if chunk_ids is not None else ["p1", "p2"],
    ))
    return doc_id, ref.content_hash


def _seed_dataset(db, blob_store, source_id, name="sales.csv",
                  body=b"region,revenue\nEMEA,100\nAPAC,200\n", status=AssetStatus.INDEXED):
    ref = blob_store.put(body)
    dataset_repo = DatasetRepository(db)
    schema_repo = SchemaRepository(db)
    version_repo = VersionRepository(db)
    schema_id = schema_repo.create(Schema(
        object_name=name,
        columns=[
            {"name": "region", "type": "string", "nullable": False, "is_metric": False, "role": "dimension"},
            {"name": "revenue", "type": "float", "nullable": False, "is_metric": True, "role": "metric"},
        ],
    ))
    dataset_id = dataset_repo.create(Dataset(
        name=name, source_id=source_id, schema_id=schema_id, status=status, row_count=2,
        metric_columns=["revenue"],
    ))
    version_repo.create(Version(
        parent_type=ParentType.DATASET, parent_id=dataset_id, version=1,
        content_hash=ref.content_hash, blob_ref=ref.uri, is_active=True,
    ))
    return dataset_id, ref.content_hash


# ===========================================================================
# Preview — documents
# ===========================================================================

def test_preview_document_returns_extracted_text(client, db):
    tc, container = client
    source_id = _seed_source(db)
    doc_id, _ = _seed_document(db, container.blob_store, source_id, body=b"# Runbook\n\nRestart the payment service.")

    r = tc.get(f"/api/v1/knowledge/documents/{doc_id}/preview")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is True
    assert "payment service" in body["text"]
    assert body["truncated"] is False


def test_preview_document_truncates_long_text(client, db):
    tc, container = client
    source_id = _seed_source(db)
    long_body = ("# Doc\n\n" + "word " * 2000).encode()
    doc_id, _ = _seed_document(db, container.blob_store, source_id, body=long_body)

    r = tc.get(f"/api/v1/knowledge/documents/{doc_id}/preview")
    body = r.json()
    assert body["available"] is True
    assert body["truncated"] is True
    assert len(body["text"]) == 4000


def test_preview_document_404_for_missing(client):
    tc, _ = client
    assert tc.get("/api/v1/knowledge/documents/nope/preview").status_code == 404


def test_preview_document_unavailable_for_deferred_category(client, db):
    tc, container = client
    source_id = _seed_source(db)
    ref = container.blob_store.put(b"\x89PNGfakebytes")
    doc_id = DocumentRepository(db).create(Document(
        title="pic.png", source_id=source_id, origin_path="pic.png", doc_type="image",
        status=AssetStatus.INDEXED, content_hash=ref.content_hash,
    ))
    VersionRepository(db).create(Version(
        parent_type=ParentType.DOCUMENT, parent_id=doc_id, version=1,
        content_hash=ref.content_hash, blob_ref=ref.uri, is_active=True,
    ))

    r = tc.get(f"/api/v1/knowledge/documents/{doc_id}/preview")
    assert r.status_code == 200  # expected outcome, not a server error
    body = r.json()
    assert body["available"] is False
    assert body["reason"] == "deferred_category"


# ===========================================================================
# Preview — datasets
# ===========================================================================

def test_preview_dataset_returns_rows(client, db):
    tc, container = client
    source_id = _seed_source(db)
    dataset_id, _ = _seed_dataset(db, container.blob_store, source_id,
                                  body=b"region,revenue\nEMEA,100\nAPAC,200\n")

    r = tc.get(f"/api/v1/knowledge/datasets/{dataset_id}/preview")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is True
    assert set(body["columns"]) == {"region", "revenue"}
    assert body["total_rows"] == 2
    assert body["rows"][0]["region"] == "EMEA"


def test_preview_dataset_respects_row_limit(client, db):
    tc, container = client
    source_id = _seed_source(db)
    rows = "region,revenue\n" + "".join(f"R{i},{i}\n" for i in range(50))
    dataset_id, _ = _seed_dataset(db, container.blob_store, source_id, body=rows.encode())

    r = tc.get(f"/api/v1/knowledge/datasets/{dataset_id}/preview")
    body = r.json()
    assert body["total_rows"] == 50
    assert body["previewed_rows"] == 20
    assert len(body["rows"]) == 20


def test_preview_dataset_404_for_missing(client):
    tc, _ = client
    assert tc.get("/api/v1/knowledge/datasets/nope/preview").status_code == 404


# ===========================================================================
# Re-index
# ===========================================================================

def test_reindex_document_resets_status_and_queues_job(client, db):
    tc, container = client
    source_id = _seed_source(db)
    doc_id, content_hash = _seed_document(db, container.blob_store, source_id)

    r = tc.post(f"/api/v1/knowledge/documents/{doc_id}/reindex")
    assert r.status_code == 202
    body = r.json()
    assert body["doc_id"] == doc_id
    assert body["status"] == JobStatus.QUEUED

    doc = DocumentRepository(db).get(doc_id)
    assert doc.status == AssetStatus.PENDING

    job = IngestionJobRepository(db).get(body["job_id"])
    assert job.job_type == JobType.REINDEX
    assert job.parent_type == ParentType.DOCUMENT
    assert job.parent_id == doc_id
    assert job.content_hash == content_hash
    assert job.status == JobStatus.QUEUED


def test_reindex_dataset_resets_status_and_queues_job(client, db):
    tc, container = client
    source_id = _seed_source(db)
    dataset_id, content_hash = _seed_dataset(db, container.blob_store, source_id)

    r = tc.post(f"/api/v1/knowledge/datasets/{dataset_id}/reindex")
    assert r.status_code == 202
    body = r.json()
    assert body["dataset_id"] == dataset_id

    ds = DatasetRepository(db).get(dataset_id)
    assert ds.status == AssetStatus.PENDING

    job = IngestionJobRepository(db).get(body["job_id"])
    assert job.job_type == JobType.REINDEX
    assert job.parent_type == ParentType.DATASET
    assert job.content_hash == content_hash


def test_reindex_document_404_for_missing(client):
    tc, _ = client
    assert tc.post("/api/v1/knowledge/documents/nope/reindex").status_code == 404


def test_reindex_reuses_unmodified_worker_end_to_end(client, db):
    """
    Proves re-index is not a parallel code path: the SAME RoutingJobProcessor
    (unmodified) that handles a fresh upload correctly reprocesses a
    REINDEX-typed job, since it only branches on parent_type.
    """
    from aeam.ingestion.processor import DocumentIngestJobProcessor
    from aeam.ingestion.routing import RoutingJobProcessor
    from aeam.ingestion.worker import IngestionWorker

    tc, container = client
    source_id = _seed_source(db)
    doc_id, _ = _seed_document(db, container.blob_store, source_id, body=b"# Doc\n\nSome content here.")

    r = tc.post(f"/api/v1/knowledge/documents/{doc_id}/reindex")
    job_id = r.json()["job_id"]
    assert DocumentRepository(db).get(doc_id).status == AssetStatus.PENDING

    class _FakePipeline:
        collection = "aeam_documents"
        def ingest_document(self, text, metadata):
            return {"collection": self.collection, "chunks_total": 1, "chunks_upserted": 1,
                    "chunk_ids": ["reindexed-chunk"], "doc_type": metadata.get("doc_type"),
                    "source": metadata.get("source"), "date": metadata.get("date")}

    processor = RoutingJobProcessor(
        document_processor=DocumentIngestJobProcessor(
            blob_store=container.blob_store, ingestion_pipeline=_FakePipeline(), db=db,
        ),
        dataset_processor=lambda job, job_repo: None,
    )
    worker = IngestionWorker(job_repo=IngestionJobRepository(db), processor=processor, poll_interval=1)
    assert worker.run_once() is True

    job = IngestionJobRepository(db).get(job_id)
    assert job.status == JobStatus.DONE
    assert DocumentRepository(db).get(doc_id).status == AssetStatus.INDEXED


# ===========================================================================
# Purge-delete — documents
# ===========================================================================

def test_delete_document_default_does_not_purge(client, db):
    """Default (purge omitted) stays byte-identical to B1.6 behaviour."""
    tc, container = client
    source_id = _seed_source(db)
    doc_id, content_hash = _seed_document(db, container.blob_store, source_id)

    r = tc.delete(f"/api/v1/knowledge/documents/{doc_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["vectors_purged"] is False
    assert body["blob_purged"] is False
    assert container.blob_store.exists(content_hash) is True  # blob untouched
    assert container.qdrant_client.deleted_calls == []


def test_delete_document_purge_removes_vectors_and_blob(client, db):
    tc, container = client
    source_id = _seed_source(db)
    doc_id, content_hash = _seed_document(db, container.blob_store, source_id, chunk_ids=["c1", "c2", "c3"])

    r = tc.delete(f"/api/v1/knowledge/documents/{doc_id}", params={"purge": "true"})
    assert r.status_code == 200
    body = r.json()
    assert body["vectors_purged"] is True
    assert body["blob_purged"] is True

    assert container.qdrant_client.deleted_calls == [("aeam_documents", ["c1", "c2", "c3"])]
    assert container.blob_store.exists(content_hash) is False
    assert DocumentRepository(db).get(doc_id) is None


def test_delete_document_purge_keeps_blob_when_shared_with_dataset(client, db):
    """The core safety property: never delete a blob another live asset needs."""
    tc, container = client
    source_id = _seed_source(db)
    shared_bytes = b"shared,content\n1,2\n"
    doc_id, content_hash = _seed_document(db, container.blob_store, source_id, body=shared_bytes)
    # A dataset happens to reference the SAME bytes (BlobStore is content-addressed).
    _seed_dataset(db, container.blob_store, source_id, body=shared_bytes)

    r = tc.delete(f"/api/v1/knowledge/documents/{doc_id}", params={"purge": "true"})
    body = r.json()
    assert body["blob_purged"] is False  # kept — dataset still references it
    assert container.blob_store.exists(content_hash) is True


def test_delete_document_purge_keeps_blob_when_shared_with_another_document(client, db):
    tc, container = client
    source_id = _seed_source(db)
    shared_bytes = b"# Same content\n"
    doc_id_1, content_hash = _seed_document(db, container.blob_store, source_id, title="a.md", body=shared_bytes)
    # Force a second, distinct document row referencing the identical content_hash
    # (simulates two separately-registered assets sharing dedup'd bytes).
    doc_repo = DocumentRepository(db)
    doc_id_2 = doc_repo.create(Document(
        title="b.md", source_id=source_id, origin_path="b.md", doc_type="markdown",
        status=AssetStatus.INDEXED, content_hash=content_hash,
    ))

    r = tc.delete(f"/api/v1/knowledge/documents/{doc_id_1}", params={"purge": "true"})
    assert r.json()["blob_purged"] is False
    assert container.blob_store.exists(content_hash) is True
    assert DocumentRepository(db).get(doc_id_2) is not None  # untouched


def test_delete_document_purge_survives_qdrant_failure(client, db):
    """A Qdrant outage during purge must not block the registry delete."""
    tc, container = client
    container.qdrant_client.raise_on_delete = True
    source_id = _seed_source(db)
    doc_id, content_hash = _seed_document(db, container.blob_store, source_id, chunk_ids=["c1"])

    r = tc.delete(f"/api/v1/knowledge/documents/{doc_id}", params={"purge": "true"})
    assert r.status_code == 200
    body = r.json()
    assert body["vectors_purged"] is False  # failed, reported honestly
    assert body["deleted"] is True  # registry delete still succeeded
    assert DocumentRepository(db).get(doc_id) is None


def test_delete_document_404_for_missing(client):
    tc, _ = client
    assert tc.delete("/api/v1/knowledge/documents/nope").status_code == 404


# ===========================================================================
# Purge-delete — datasets
# ===========================================================================

def test_delete_dataset_purge_removes_blob(client, db):
    tc, container = client
    source_id = _seed_source(db)
    dataset_id, content_hash = _seed_dataset(db, container.blob_store, source_id)

    r = tc.delete(f"/api/v1/knowledge/datasets/{dataset_id}", params={"purge": "true"})
    assert r.status_code == 200
    assert r.json()["blob_purged"] is True
    assert container.blob_store.exists(content_hash) is False


def test_delete_dataset_purge_keeps_blob_when_shared_with_document(client, db):
    tc, container = client
    source_id = _seed_source(db)
    shared_bytes = b"region,revenue\nEMEA,1\n"
    dataset_id, content_hash = _seed_dataset(db, container.blob_store, source_id, body=shared_bytes)
    _seed_document(db, container.blob_store, source_id, body=shared_bytes)

    r = tc.delete(f"/api/v1/knowledge/datasets/{dataset_id}", params={"purge": "true"})
    assert r.json()["blob_purged"] is False
    assert container.blob_store.exists(content_hash) is True


def test_delete_dataset_default_does_not_purge(client, db):
    tc, container = client
    source_id = _seed_source(db)
    dataset_id, content_hash = _seed_dataset(db, container.blob_store, source_id)

    r = tc.delete(f"/api/v1/knowledge/datasets/{dataset_id}")
    assert r.json()["blob_purged"] is False
    assert container.blob_store.exists(content_hash) is True
