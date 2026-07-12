"""
aeam/tests/test_phase_b1_6_knowledge_api.py

Phase B1.6 — Knowledge Center API.

Exercises aeam.api.knowledge via a real FastAPI TestClient against a real
SQLite DatabaseClient, seeding rows through the SAME existing repositories the
router itself uses (DocumentRepository, DatasetRepository, SchemaRepository,
VersionRepository, SourceRepository) — no mocking of the data layer. No
Qdrant, no BlobStore file I/O, no Redis, no live network required.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aeam.api.knowledge import router
from aeam.integrations.database import DatabaseClient
from aeam.registry.models import (
    AssetStatus, Dataset, Document, ParentType, Schema, Source, SourceKind, Version,
)
from aeam.registry.repositories import (
    DatasetRepository, DocumentRepository, SchemaRepository, SourceRepository, VersionRepository,
)


@pytest.fixture()
def db(tmp_path):
    client = DatabaseClient(database_url=f"sqlite:///{(tmp_path / 'b16.db').as_posix()}")
    yield client
    client.dispose()


@pytest.fixture()
def client(db):
    class _Container:
        pass
    container = _Container()
    container.db = db

    app = FastAPI()
    app.include_router(router)
    app.state.container = container
    return TestClient(app)


def _seed_source(db, name="Manual Upload"):
    return SourceRepository(db).create(Source(name=name, kind=SourceKind.UPLOAD))


def _seed_document(db, source_id, title="runbook.md", status=AssetStatus.INDEXED, chunk_count=3):
    doc_repo = DocumentRepository(db)
    version_repo = VersionRepository(db)
    doc_id = doc_repo.create(Document(
        title=title, source_id=source_id, origin_path=title, doc_type="markdown",
        status=status, chunk_count=chunk_count, content_hash="abc123",
    ))
    version_repo.create(Version(
        parent_type=ParentType.DOCUMENT, parent_id=doc_id, version=1,
        content_hash="abc123", blob_ref="local://abc123", is_active=True,
        chunk_ids=["p1", "p2", "p3"],
    ))
    return doc_id


def _seed_dataset(db, source_id, name="sales.csv", status=AssetStatus.INDEXED):
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
        name=name, source_id=source_id, schema_id=schema_id, status=status,
        row_count=10, metric_columns=["revenue"],
    ))
    version_repo.create(Version(
        parent_type=ParentType.DATASET, parent_id=dataset_id, version=1,
        content_hash="def456", blob_ref="local://def456", is_active=True,
    ))
    return dataset_id, schema_id


# ===========================================================================
# Documents
# ===========================================================================

def test_list_documents_empty(client):
    r = client.get("/api/v1/knowledge/documents")
    assert r.status_code == 200
    assert r.json() == []


def test_list_documents_with_source_name_and_file_type(client, db):
    source_id = _seed_source(db, name="Manual Upload")
    _seed_document(db, source_id, title="runbook.md")

    r = client.get("/api/v1/knowledge/documents")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["source_name"] == "Manual Upload"
    assert body[0]["file_type"] == "markdown"
    assert body[0]["embedding_status"] == "indexed"
    assert body[0]["chunk_count"] == 3


def test_list_documents_status_filter(client, db):
    source_id = _seed_source(db)
    _seed_document(db, source_id, title="a.md", status=AssetStatus.INDEXED)
    _seed_document(db, source_id, title="b.md", status=AssetStatus.ERROR)

    r = client.get("/api/v1/knowledge/documents", params={"status": "error"})
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1 and body[0]["title"] == "b.md"


def test_list_documents_invalid_status_422(client):
    r = client.get("/api/v1/knowledge/documents", params={"status": "bogus"})
    assert r.status_code == 422


def test_list_documents_search(client, db):
    source_id = _seed_source(db)
    _seed_document(db, source_id, title="incident_runbook.md")
    _seed_document(db, source_id, title="quarterly_report.pdf")

    r = client.get("/api/v1/knowledge/documents", params={"q": "incident"})
    body = r.json()
    assert len(body) == 1 and body[0]["title"] == "incident_runbook.md"


def test_get_document_includes_active_version(client, db):
    source_id = _seed_source(db)
    doc_id = _seed_document(db, source_id)

    r = client.get(f"/api/v1/knowledge/documents/{doc_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["doc_id"] == doc_id
    assert body["active_version"]["is_active"] is True
    assert body["active_version"]["chunk_count"] == 3


def test_get_document_404(client):
    r = client.get("/api/v1/knowledge/documents/does-not-exist")
    assert r.status_code == 404


def test_delete_document_cascades_versions(client, db):
    source_id = _seed_source(db)
    doc_id = _seed_document(db, source_id)

    r = client.delete(f"/api/v1/knowledge/documents/{doc_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["deleted"] is True and body["versions_deleted"] == 1

    assert client.get(f"/api/v1/knowledge/documents/{doc_id}").status_code == 404
    # Version rows for this parent are gone too.
    versions = client.get(
        "/api/v1/knowledge/versions",
        params={"parent_type": ParentType.DOCUMENT, "parent_id": doc_id},
    ).json()
    assert versions == []


def test_delete_document_404(client):
    r = client.delete("/api/v1/knowledge/documents/does-not-exist")
    assert r.status_code == 404


# ===========================================================================
# Datasets
# ===========================================================================

def test_list_datasets_with_file_type_inference(client, db):
    source_id = _seed_source(db)
    _seed_dataset(db, source_id, name="sales.csv")

    r = client.get("/api/v1/knowledge/datasets")
    body = r.json()
    assert len(body) == 1
    assert body[0]["file_type"] == "csv"
    assert body[0]["processing_status"] == "indexed"
    assert body[0]["metric_columns"] == ["revenue"]


def test_get_dataset_includes_schema_and_version(client, db):
    source_id = _seed_source(db)
    dataset_id, schema_id = _seed_dataset(db, source_id)

    r = client.get(f"/api/v1/knowledge/datasets/{dataset_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["schema"]["schema_id"] == schema_id
    assert {c["name"] for c in body["schema"]["columns"]} == {"region", "revenue"}
    assert body["active_version"]["is_active"] is True


def test_get_dataset_404(client):
    assert client.get("/api/v1/knowledge/datasets/nope").status_code == 404


def test_delete_dataset_cascades_schema_and_versions(client, db):
    source_id = _seed_source(db)
    dataset_id, schema_id = _seed_dataset(db, source_id)

    r = client.delete(f"/api/v1/knowledge/datasets/{dataset_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["deleted"] is True and body["schema_deleted"] is True and body["versions_deleted"] == 1

    assert client.get(f"/api/v1/knowledge/datasets/{dataset_id}").status_code == 404
    assert client.get(f"/api/v1/knowledge/schemas/{schema_id}").status_code == 404


def test_list_datasets_search_and_status(client, db):
    source_id = _seed_source(db)
    _seed_dataset(db, source_id, name="sales.csv", status=AssetStatus.INDEXED)
    _seed_dataset(db, source_id, name="inventory.xlsx", status=AssetStatus.PENDING)

    r = client.get("/api/v1/knowledge/datasets", params={"q": "inventory"})
    assert [d["name"] for d in r.json()] == ["inventory.xlsx"]

    r2 = client.get("/api/v1/knowledge/datasets", params={"status": "pending"})
    assert [d["name"] for d in r2.json()] == ["inventory.xlsx"]


# ===========================================================================
# Schemas
# ===========================================================================

def test_list_and_get_schema(client, db):
    source_id = _seed_source(db)
    _dataset_id, schema_id = _seed_dataset(db, source_id)

    listed = client.get("/api/v1/knowledge/schemas").json()
    assert any(s["schema_id"] == schema_id for s in listed)

    detail = client.get(f"/api/v1/knowledge/schemas/{schema_id}")
    assert detail.status_code == 200
    assert detail.json()["object_name"] == "sales.csv"


def test_delete_schema_standalone(client, db):
    schema_id = SchemaRepository(db).create(Schema(object_name="standalone"))
    r = client.delete(f"/api/v1/knowledge/schemas/{schema_id}")
    assert r.status_code == 200
    assert client.get(f"/api/v1/knowledge/schemas/{schema_id}").status_code == 404


def test_get_schema_404(client):
    assert client.get("/api/v1/knowledge/schemas/nope").status_code == 404


# ===========================================================================
# Versions
# ===========================================================================

def test_list_versions_for_parent(client, db):
    source_id = _seed_source(db)
    doc_id = _seed_document(db, source_id)

    r = client.get("/api/v1/knowledge/versions", params={"parent_type": "document", "parent_id": doc_id})
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1 and body[0]["parent_id"] == doc_id


def test_list_versions_invalid_parent_type_422(client):
    r = client.get("/api/v1/knowledge/versions", params={"parent_type": "bogus", "parent_id": "x"})
    assert r.status_code == 422


def test_get_and_delete_version(client, db):
    source_id = _seed_source(db)
    doc_id = _seed_document(db, source_id)
    versions = client.get(
        "/api/v1/knowledge/versions", params={"parent_type": "document", "parent_id": doc_id}
    ).json()
    version_id = versions[0]["version_id"]

    detail = client.get(f"/api/v1/knowledge/versions/{version_id}")
    assert detail.status_code == 200 and detail.json()["version_id"] == version_id

    r = client.delete(f"/api/v1/knowledge/versions/{version_id}")
    assert r.status_code == 200
    assert client.get(f"/api/v1/knowledge/versions/{version_id}").status_code == 404


def test_get_version_404(client):
    assert client.get("/api/v1/knowledge/versions/nope").status_code == 404
