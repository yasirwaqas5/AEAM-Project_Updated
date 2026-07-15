"""
aeam/tests/test_phase_c2_policy_extraction.py

Enterprise Policy Intelligence Engine (Phase C2) tests.

Two layers, matching this codebase's established RAG/ingestion test
convention (test_phase_b1_3_ingestion.py's FAKE IngestionPipeline; no live
Qdrant, no live LLM):

1. PolicyExtractor's own extraction + chunk-attribution logic against a FAKE
   LLMService (records prompts, returns canned JSON) — no live LLM call.
2. DocumentIngestJobProcessor wiring: policy extraction runs after a real
   ingestion job completes (via the real IngestionWorker + a FAKE
   IngestionPipeline, exactly like test_phase_b1_3_ingestion.py), and
   extracted policies persist to the real `policies` table with correct
   doc_id/source_document/source_chunk attribution.
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aeam.agents.rag.chunking import TextChunker
from aeam.api.knowledge import router as knowledge_router
from aeam.ingestion.processor import DocumentIngestJobProcessor
from aeam.ingestion.worker import IngestionWorker
from aeam.integrations.database import DatabaseClient
from aeam.intelligence.policy_extraction import PolicyExtractor
from aeam.registry.models import (
    AssetStatus, Document, IngestionJob, JobStatus, JobType, ParentType, Policy,
    Source, SourceKind, Version,
)
from aeam.registry.repositories import (
    DocumentRepository, IngestionJobRepository, PolicyRepository,
    SourceRepository, VersionRepository,
)
from aeam.storage.blob_store import LocalDiskBlobStore


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeLLMService:
    """Stand-in for LLMService.query() — records prompts, no live LLM call."""

    def __init__(self, response: str | Exception = "{}"):
        self.response = response
        self.prompts: list[str] = []

    def query(self, prompt, *, temperature=0.7, max_tokens=1000):
        self.prompts.append(prompt)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class FakeIngestionPipeline:
    """Stand-in for the RAG IngestionPipeline — records calls, no Qdrant."""

    collection = "aeam_documents"

    def __init__(self, chunk_ids=None):
        self.calls: list[dict] = []
        self._chunk_ids = chunk_ids if chunk_ids is not None else ["pt-0", "pt-1", "pt-2"]

    def ingest_document(self, text: str, metadata: dict) -> dict:
        self.calls.append({"text": text, "metadata": metadata})
        return {
            "collection": self.collection,
            "chunks_total": len(self._chunk_ids),
            "chunks_upserted": len(self._chunk_ids),
            "chunk_ids": list(self._chunk_ids),
            "doc_type": metadata.get("doc_type"),
            "source": metadata.get("source"),
            "date": metadata.get("date"),
        }


SALES_POLICY_TEXT = "If sales decrease by more than 30%, notify Marketing and increase advertising budget."

SALES_POLICY_LLM_RESPONSE = json.dumps({
    "policies": [
        {
            "raw_text": SALES_POLICY_TEXT,
            "business_rule": "Notify marketing and increase ad spend on a sales drop over 30%.",
            "condition": "sales_drop > 30%",
            "actions": ["notify_marketing", "increase_ad_budget"],
            "priority": "high",
            "department": "marketing",
            "related_metrics": ["sales"],
        }
    ]
})


# ===========================================================================
# 1. PolicyExtractor
# ===========================================================================

def test_extract_returns_structured_policy_from_example():
    llm = FakeLLMService(response=SALES_POLICY_LLM_RESPONSE)
    extractor = PolicyExtractor(llm_service=llm)

    policies = extractor.extract(text=SALES_POLICY_TEXT, chunk_ids=["chunk-1"], chunk_metadata={"source": "s", "date": "d", "doc_type": "markdown"})

    assert len(policies) == 1
    p = policies[0]
    assert p["condition"] == "sales_drop > 30%"
    assert p["actions"] == ["notify_marketing", "increase_ad_budget"]
    assert p["priority"] == "high"
    assert p["department"] == "marketing"
    assert p["related_metrics"] == ["sales"]
    assert p["raw_text"] == SALES_POLICY_TEXT
    # Single short chunk == the whole text -> attributed confidently.
    assert p["source_chunk"] == "chunk-1"


def test_extract_omits_fields_the_text_never_specified():
    """Only condition/actions/priority/department/related_metrics were in the
    canned response -- threshold/escalation_rule/role/time_constraint/
    approval_required must be genuinely absent, never defaulted."""
    llm = FakeLLMService(response=SALES_POLICY_LLM_RESPONSE)
    extractor = PolicyExtractor(llm_service=llm)

    p = extractor.extract(text=SALES_POLICY_TEXT)[0]

    for absent_field in ("threshold", "escalation_rule", "role", "time_constraint", "approval_required"):
        assert absent_field not in p


def test_extract_empty_document_returns_empty_list():
    llm = FakeLLMService(response="{}")
    assert PolicyExtractor(llm_service=llm).extract(text="") == []
    assert llm.prompts == []  # never even calls the LLM for blank text


def test_extract_honest_when_llm_finds_no_policy():
    llm = FakeLLMService(response='{"policies": []}')
    extractor = PolicyExtractor(llm_service=llm)

    result = extractor.extract(text="This document is a general product overview with no rules.")

    assert result == []
    assert len(llm.prompts) == 1  # it DID look, honestly found nothing


def test_extract_survives_llm_failure():
    llm = FakeLLMService(response=RuntimeError("LLM outage"))
    extractor = PolicyExtractor(llm_service=llm)
    assert extractor.extract(text=SALES_POLICY_TEXT) == []  # never raises


def test_extract_survives_unparseable_llm_response():
    llm = FakeLLMService(response="not json at all, sorry")
    extractor = PolicyExtractor(llm_service=llm)
    assert extractor.extract(text=SALES_POLICY_TEXT) == []


def test_extract_drops_empty_placeholder_entries():
    """A stray object with no genuine rule content must never become a
    fabricated 'policy' with nothing in it."""
    llm = FakeLLMService(response=json.dumps({"policies": [{"priority": "high"}]}))
    extractor = PolicyExtractor(llm_service=llm)
    assert extractor.extract(text="some text") == []


def test_extract_multiple_chunks_attributes_correct_source_chunk():
    """Two distinct sentences in two distinct chunks -- each policy must be
    attributed to the chunk it actually came from, not just chunk[0]."""
    chunk_a = "If sales decrease by more than 30%, notify Marketing and increase advertising budget."
    chunk_b = "If server CPU exceeds 90% for ten minutes, escalate to the on-call engineer immediately."
    full_text = chunk_a + " " + chunk_b

    response = json.dumps({"policies": [
        {"raw_text": chunk_a, "condition": "sales_drop > 30%", "actions": ["notify_marketing"]},
        {"raw_text": chunk_b, "condition": "cpu > 90% for 10m", "escalation_rule": "escalate to on-call engineer"},
    ]})
    llm = FakeLLMService(response=response)
    # Force each sentence into its own chunk via a tiny chunk_size.
    chunker = TextChunker(chunk_size=20, overlap=5, strategy="sentence")
    extractor = PolicyExtractor(llm_service=llm, chunker=chunker)

    chunks = chunker.chunk_text(text=full_text, metadata={})
    chunk_ids = [f"pt-{i}" for i in range(len(chunks))]

    policies = extractor.extract(text=full_text, chunk_ids=chunk_ids, chunk_metadata={})

    by_condition = {p["condition"]: p["source_chunk"] for p in policies}
    assert by_condition["sales_drop > 30%"] != by_condition["cpu > 90% for 10m"]
    assert by_condition["sales_drop > 30%"] in chunk_ids
    assert by_condition["cpu > 90% for 10m"] in chunk_ids


def test_extract_source_chunk_none_without_chunk_ids():
    llm = FakeLLMService(response=SALES_POLICY_LLM_RESPONSE)
    extractor = PolicyExtractor(llm_service=llm)
    p = extractor.extract(text=SALES_POLICY_TEXT, chunk_ids=None)[0]
    assert p["source_chunk"] is None


def test_extractor_rejects_none_llm_service():
    with pytest.raises(ValueError):
        PolicyExtractor(llm_service=None)


# ===========================================================================
# 2. DocumentIngestJobProcessor wiring
# ===========================================================================

@pytest.fixture()
def db(tmp_path):
    client = DatabaseClient(database_url=f"sqlite:///{(tmp_path / 'c2.db').as_posix()}")
    yield client
    client.dispose()


@pytest.fixture()
def blob_store(tmp_path):
    return LocalDiskBlobStore(tmp_path / "blobs")


def _seed_upload(db, blob_store, data: bytes, category: str, filename: str):
    blob_ref = blob_store.put(data)
    source_repo = SourceRepository(db)
    doc_repo = DocumentRepository(db)
    version_repo = VersionRepository(db)
    job_repo = IngestionJobRepository(db)

    source_id = source_repo.create(Source(name="Manual Upload", kind=SourceKind.UPLOAD))
    doc_id = doc_repo.create(Document(
        title=filename, source_id=source_id, origin_path=filename, doc_type=category,
        content_hash=blob_ref.content_hash, status=AssetStatus.PENDING,
    ))
    version_repo.create(Version(
        parent_type=ParentType.DOCUMENT, parent_id=doc_id, version=1,
        content_hash=blob_ref.content_hash, blob_ref=blob_ref.uri, is_active=True,
    ))
    job_id = job_repo.create(IngestionJob(
        job_type=JobType.INGEST, source_id=source_id,
        parent_type=ParentType.DOCUMENT, parent_id=doc_id,
        status=JobStatus.QUEUED, content_hash=blob_ref.content_hash,
    ))
    return job_id, doc_id


def _drain(db, blob_store, pipeline, policy_extractor=None, policy_extraction_enabled=True):
    job_repo = IngestionJobRepository(db)
    processor = DocumentIngestJobProcessor(
        blob_store=blob_store, ingestion_pipeline=pipeline, db=db,
        policy_extractor=policy_extractor, policy_extraction_enabled=policy_extraction_enabled,
    )
    worker = IngestionWorker(job_repo=job_repo, processor=processor, poll_interval=1)
    return worker.run_once()


def test_processor_persists_extracted_policy_with_attribution(db, blob_store):
    pipeline = FakeIngestionPipeline(chunk_ids=["pt-a"])
    llm = FakeLLMService(response=SALES_POLICY_LLM_RESPONSE)
    extractor = PolicyExtractor(llm_service=llm)

    job_id, doc_id = _seed_upload(db, blob_store, SALES_POLICY_TEXT.encode(), "markdown", "policy.md")
    assert _drain(db, blob_store, pipeline, policy_extractor=extractor) is True

    stored = PolicyRepository(db).list_by_document(doc_id)
    assert len(stored) == 1
    policy = stored[0]
    assert policy.doc_id == doc_id
    assert policy.source_document == "policy.md"
    assert policy.source_chunk == "pt-a"
    assert policy.condition == "sales_drop > 30%"
    assert policy.actions == ["notify_marketing", "increase_ad_budget"]
    assert policy.priority == "high"
    assert policy.department == "marketing"

    # Ingestion itself still finalised normally -- policy extraction never
    # interfered with the core B1.3 pipeline.
    doc = DocumentRepository(db).get(doc_id)
    assert doc.status == AssetStatus.INDEXED


def test_processor_stores_nothing_when_no_policy_found(db, blob_store):
    pipeline = FakeIngestionPipeline()
    llm = FakeLLMService(response='{"policies": []}')
    extractor = PolicyExtractor(llm_service=llm)

    job_id, doc_id = _seed_upload(db, blob_store, b"General overview with no rules.", "markdown", "overview.md")
    assert _drain(db, blob_store, pipeline, policy_extractor=extractor) is True

    assert PolicyRepository(db).list_by_document(doc_id) == []
    # Document ingestion still succeeded -- honest "no policy" is not a failure.
    assert DocumentRepository(db).get(doc_id).status == AssetStatus.INDEXED


def test_processor_skips_extraction_when_disabled(db, blob_store):
    pipeline = FakeIngestionPipeline()
    llm = FakeLLMService(response=SALES_POLICY_LLM_RESPONSE)
    extractor = PolicyExtractor(llm_service=llm)

    job_id, doc_id = _seed_upload(db, blob_store, SALES_POLICY_TEXT.encode(), "markdown", "policy.md")
    _drain(db, blob_store, pipeline, policy_extractor=extractor, policy_extraction_enabled=False)

    assert llm.prompts == []  # never even called
    assert PolicyRepository(db).list_by_document(doc_id) == []


def test_processor_without_policy_extractor_behaves_exactly_as_before_c2(db, blob_store):
    """Default (no policy_extractor passed) -- byte-for-byte pre-C2 behaviour."""
    pipeline = FakeIngestionPipeline(chunk_ids=["a", "b", "c", "d"])
    job_id, doc_id = _seed_upload(db, blob_store, b"# Runbook\n\nRestart the service.", "markdown", "run.md")

    assert _drain(db, blob_store, pipeline) is True

    doc = DocumentRepository(db).get(doc_id)
    assert doc.status == AssetStatus.INDEXED
    assert doc.chunk_count == 4
    assert PolicyRepository(db).list_by_document(doc_id) == []


def test_processor_survives_policy_extraction_raising(db, blob_store):
    """A hard exception inside extraction must not fail the ingestion job."""
    class _BrokenExtractor:
        def extract(self, **kwargs):
            raise RuntimeError("boom")

    pipeline = FakeIngestionPipeline()
    job_id, doc_id = _seed_upload(db, blob_store, SALES_POLICY_TEXT.encode(), "markdown", "policy.md")

    assert _drain(db, blob_store, pipeline, policy_extractor=_BrokenExtractor()) is True
    assert DocumentRepository(db).get(doc_id).status == AssetStatus.INDEXED


# ===========================================================================
# 3. GET /api/v1/knowledge/documents/{doc_id}/policies
# ===========================================================================

@pytest.fixture()
def client(db):
    class _Container:
        pass
    container = _Container()
    container.db = db
    app = FastAPI()
    app.include_router(knowledge_router)
    app.state.container = container
    return TestClient(app)


def _seed_document_only(db, title="policy.md"):
    source_id = SourceRepository(db).create(Source(name="Manual Upload", kind=SourceKind.UPLOAD))
    doc_id = DocumentRepository(db).create(Document(
        title=title, source_id=source_id, origin_path=title, doc_type="markdown",
        status=AssetStatus.INDEXED,
    ))
    return doc_id


def test_api_lists_policies_for_document(db, client):
    doc_id = _seed_document_only(db)
    PolicyRepository(db).create(Policy(
        doc_id=doc_id, source_document="policy.md", source_chunk="pt-a",
        raw_text=SALES_POLICY_TEXT, condition="sales_drop > 30%",
        actions=["notify_marketing", "increase_ad_budget"], priority="high", department="marketing",
    ))

    r = client.get(f"/api/v1/knowledge/documents/{doc_id}/policies")
    assert r.status_code == 200
    body = r.json()
    assert body["doc_id"] == doc_id
    assert body["count"] == 1
    assert body["policies"][0]["condition"] == "sales_drop > 30%"
    assert body["policies"][0]["source_chunk"] == "pt-a"
    assert body["policies"][0]["source_document"] == "policy.md"


def test_api_returns_empty_list_honestly_when_no_policies(db, client):
    doc_id = _seed_document_only(db)
    r = client.get(f"/api/v1/knowledge/documents/{doc_id}/policies")
    assert r.status_code == 200
    assert r.json() == {"doc_id": doc_id, "count": 0, "policies": []}


def test_api_404_for_missing_document(client):
    assert client.get("/api/v1/knowledge/documents/nope/policies").status_code == 404


def test_api_serializes_native_datetime_extracted_at(db, client, monkeypatch):
    """
    Regression test: PostgreSQL returns a native datetime.datetime for a
    TIMESTAMP column (unlike SQLite, which returns the ISO string as-is —
    see aeam.api.knowledge._iso's own docstring for this exact caveat).
    The endpoint must serialize via _iso(), never pass a raw datetime
    straight into JSONResponse.
    """
    import datetime as dt
    from aeam.registry.repositories import PolicyRepository as _PR

    doc_id = _seed_document_only(db)
    real_policy = Policy(doc_id=doc_id, source_document="policy.md", raw_text="text", condition="x")
    PolicyRepository(db).create(real_policy)

    def _fake_list_by_document(self, doc_id_):
        p = real_policy
        p.extracted_at = dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=dt.timezone.utc)
        return [p]

    monkeypatch.setattr(_PR, "list_by_document", _fake_list_by_document)

    r = client.get(f"/api/v1/knowledge/documents/{doc_id}/policies")
    assert r.status_code == 200
    assert r.json()["policies"][0]["extracted_at"] == "2026-01-01T12:00:00+00:00"
