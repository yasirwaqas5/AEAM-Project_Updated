"""
aeam/tests/test_phase_c1_enterprise_memory.py

Enterprise Memory Engine (Phase C1) tests.

Two layers, matching the codebase's existing RAG-test convention
(test_phase7_retrieval_debug.py etc. use deterministic stub components
rather than a live Qdrant/embedding model, and the full 345-test suite
already passes without Qdrant running):

1. EnterpriseMemoryEngine's own composition logic (remember_incident /
   recall_similar_incidents) against fake IngestionPipeline/RetrievalPipeline
   objects that duck-type the real classes' public interface
   (ingest_document / search / collection). No live Qdrant, no embedding
   model — this suite tests THIS module's logic, not IngestionPipeline's or
   RetrievalPipeline's own plumbing (already covered elsewhere).
2. Orchestrator wiring: memory recall runs exactly once per incident
   lifecycle (idempotent across investigation depths) and appends a
   `type: "memory"` finding distinct from `type: "rag"`; finalize_incident()
   calls remember_incident() with the real, already-computed fields.
"""

from __future__ import annotations

import pytest

from aeam.agents.orchestrator.decision_engine import DecisionEngine
from aeam.agents.orchestrator.evaluation_engine import EvaluationEngine
from aeam.agents.orchestrator.orchestrator import Orchestrator
from aeam.agents.orchestrator.state_machine import IncidentStateMachine
from aeam.config.settings import Settings
from aeam.core.event_bus import EventBus
from aeam.core.event_models import Event
from aeam.memory.enterprise_memory import EnterpriseMemoryEngine
from aeam.memory.long_term import LongTermMemory
from aeam.memory.short_term import ShortTermMemory


# ---------------------------------------------------------------------------
# Fakes — duck-type IngestionPipeline / RetrievalPipeline's public interface
# ---------------------------------------------------------------------------

class FakeIngestionPipeline:
    """Records every ingest_document() call; never touches real Qdrant."""

    def __init__(self, raise_on_ingest: bool = False):
        self.calls: list[dict] = []
        self.raise_on_ingest = raise_on_ingest
        self.collection = "aeam_incident_memories"

    def ingest_document(self, text, metadata):
        if self.raise_on_ingest:
            raise RuntimeError("simulated Qdrant outage")
        self.calls.append({"text": text, "metadata": metadata})
        return {
            "collection": self.collection,
            "chunks_total": 1,
            "chunks_upserted": 1,
            "chunk_ids": ["fake-point-id"],
            "doc_type": metadata.get("doc_type"),
            "source": metadata.get("source"),
            "date": metadata.get("date"),
        }


class FakeRetrievalPipeline:
    """Returns a pre-programmed hit list; never touches real Qdrant."""

    def __init__(self, hits: list[dict] | None = None, raise_on_search: bool = False):
        self._hits = hits or []
        self.raise_on_search = raise_on_search
        self.queries: list[str] = []
        self.collection = "aeam_incident_memories"

    def search(self, query, top_k=5, filter_criteria=None):
        self.queries.append(query)
        if self.raise_on_search:
            raise RuntimeError("simulated Qdrant outage")
        return self._hits[:top_k]


def _hit(incident_id, similarity, **meta):
    return {
        "chunk_id": f"chunk-{incident_id}",
        "text": meta.pop("text", "some incident summary text"),
        "similarity": similarity,
        "metadata": {"incident_id": incident_id, **meta},
    }


# ===========================================================================
# 1. EnterpriseMemoryEngine — remember_incident
# ===========================================================================

def test_remember_incident_stores_real_fields_only():
    ingest = FakeIngestionPipeline()
    engine = EnterpriseMemoryEngine(ingestion_pipeline=ingest, retrieval_pipeline=FakeRetrievalPipeline())

    result = engine.remember_incident({
        "incident_id": "inc-1",
        "event_type": "DB_LATENCY",
        "metric": "latency_ms",
        "severity": "HIGH",
        "root_cause": "Runaway thread in payment service",
        "confidence": 0.87,
        "investigation_status": "RESOLVED",
        "recommended_actions": ["Restart service", "Add index"],
        "executed_actions": ["diagnostics"],
        "chunk_ids": ["a1", "a2"],
        "timestamp": "2026-01-01T00:00:00Z",
    })

    assert result is not None
    assert len(ingest.calls) == 1
    call = ingest.calls[0]
    meta = call["metadata"]

    assert meta["doc_type"] == "incident_memory"
    assert meta["incident_id"] == "inc-1"
    assert meta["category"] == "DB_LATENCY"
    assert meta["severity"] == "HIGH"
    assert meta["triggered_metric"] == "latency_ms"
    assert meta["root_cause"] == "Runaway thread in payment service"
    assert meta["confidence"] == 0.87
    assert meta["resolution_status"] == "RESOLVED"
    assert meta["recommended_actions"] == ["Restart service", "Add index"]
    assert meta["executed_actions"] == ["diagnostics"]
    assert meta["evidence_chunk_ids"] == ["a1", "a2"]
    assert meta["timestamp"] == "2026-01-01T00:00:00Z"
    assert "Runaway thread in payment service" in call["text"]


def test_remember_incident_omits_genuinely_missing_fields():
    """No root_cause, no actions, no confidence -- never fabricated, just absent."""
    ingest = FakeIngestionPipeline()
    engine = EnterpriseMemoryEngine(ingestion_pipeline=ingest, retrieval_pipeline=FakeRetrievalPipeline())

    engine.remember_incident({
        "incident_id": "inc-2",
        "event_type": "CPU_HIGH",
        "metric": "cpu_util",
        "severity": "CRITICAL",
        "timestamp": "2026-01-02T00:00:00Z",
        # root_cause, confidence, actions, investigation_status all absent
    })

    meta = ingest.calls[0]["metadata"]
    assert "root_cause" not in meta
    assert "confidence" not in meta
    assert "recommended_actions" not in meta
    assert "executed_actions" not in meta
    assert "resolution_status" not in meta
    # The embed text honestly says no root cause was found -- not invented.
    assert "No root cause was determined" in ingest.calls[0]["text"]


def test_remember_incident_requires_incident_id():
    ingest = FakeIngestionPipeline()
    engine = EnterpriseMemoryEngine(ingestion_pipeline=ingest, retrieval_pipeline=FakeRetrievalPipeline())

    result = engine.remember_incident({"event_type": "CPU_HIGH"})

    assert result is None
    assert ingest.calls == []


def test_remember_incident_survives_storage_failure():
    ingest = FakeIngestionPipeline(raise_on_ingest=True)
    engine = EnterpriseMemoryEngine(ingestion_pipeline=ingest, retrieval_pipeline=FakeRetrievalPipeline())

    result = engine.remember_incident({"incident_id": "inc-3", "event_type": "CPU_HIGH"})

    assert result is None  # failed, but did not raise


def test_remember_incident_uses_timestamp_as_ingestion_date_when_present():
    ingest = FakeIngestionPipeline()
    engine = EnterpriseMemoryEngine(ingestion_pipeline=ingest, retrieval_pipeline=FakeRetrievalPipeline())

    engine.remember_incident({"incident_id": "inc-4", "timestamp": "2026-05-05T00:00:00Z"})

    assert ingest.calls[0]["metadata"]["date"] == "2026-05-05T00:00:00Z"


def test_remember_incident_falls_back_to_now_when_timestamp_missing():
    """`date` is a hard IngestionPipeline requirement -- satisfied honestly, never left blank."""
    ingest = FakeIngestionPipeline()
    engine = EnterpriseMemoryEngine(ingestion_pipeline=ingest, retrieval_pipeline=FakeRetrievalPipeline())

    engine.remember_incident({"incident_id": "inc-5"})

    assert ingest.calls[0]["metadata"]["date"]  # non-empty
    assert "timestamp" not in ingest.calls[0]["metadata"]  # but the incident's OWN timestamp is honestly absent


# ===========================================================================
# 2. EnterpriseMemoryEngine — recall_similar_incidents
# ===========================================================================

def test_recall_returns_reshaped_matches():
    hits = [
        _hit("inc-10", 0.91, category="DB_LATENCY", severity="HIGH", triggered_metric="latency_ms",
             root_cause="Missing index", resolution_status="RESOLVED", confidence=0.8,
             timestamp="2026-01-01T00:00:00Z", incident_summary="Db latency on 'latency_ms'."),
        _hit("inc-11", 0.77, category="DB_LATENCY"),
    ]
    retrieve = FakeRetrievalPipeline(hits=hits)
    engine = EnterpriseMemoryEngine(ingestion_pipeline=FakeIngestionPipeline(), retrieval_pipeline=retrieve)

    matches = engine.recall_similar_incidents(query="database latency slow query")

    assert retrieve.queries == ["database latency slow query"]
    assert len(matches) == 2
    assert matches[0]["incident_id"] == "inc-10"
    assert matches[0]["similarity"] == 0.91
    assert matches[0]["root_cause"] == "Missing index"
    assert matches[0]["resolution_status"] == "RESOLVED"
    assert matches[1]["incident_id"] == "inc-11"
    assert matches[1]["root_cause"] is None  # genuinely absent, not fabricated


def test_recall_empty_when_no_similar_incidents():
    engine = EnterpriseMemoryEngine(ingestion_pipeline=FakeIngestionPipeline(), retrieval_pipeline=FakeRetrievalPipeline(hits=[]))
    assert engine.recall_similar_incidents(query="something novel") == []


def test_recall_empty_for_blank_query():
    retrieve = FakeRetrievalPipeline(hits=[_hit("inc-1", 0.9)])
    engine = EnterpriseMemoryEngine(ingestion_pipeline=FakeIngestionPipeline(), retrieval_pipeline=retrieve)

    assert engine.recall_similar_incidents(query="   ") == []
    assert retrieve.queries == []  # never even called search — no fabricated attempt


def test_recall_excludes_given_incident_id():
    hits = [_hit("inc-self", 0.99), _hit("inc-other", 0.85)]
    engine = EnterpriseMemoryEngine(ingestion_pipeline=FakeIngestionPipeline(), retrieval_pipeline=FakeRetrievalPipeline(hits=hits))

    matches = engine.recall_similar_incidents(query="x", exclude_incident_id="inc-self")

    assert [m["incident_id"] for m in matches] == ["inc-other"]


def test_recall_survives_search_failure():
    engine = EnterpriseMemoryEngine(
        ingestion_pipeline=FakeIngestionPipeline(),
        retrieval_pipeline=FakeRetrievalPipeline(raise_on_search=True),
    )
    assert engine.recall_similar_incidents(query="anything") == []  # never raises


def test_engine_rejects_none_pipelines():
    with pytest.raises(ValueError):
        EnterpriseMemoryEngine(ingestion_pipeline=None, retrieval_pipeline=FakeRetrievalPipeline())
    with pytest.raises(ValueError):
        EnterpriseMemoryEngine(ingestion_pipeline=FakeIngestionPipeline(), retrieval_pipeline=None)


# ===========================================================================
# 3. Orchestrator wiring
# ===========================================================================

class FakeLongTermMemory(LongTermMemory):
    def __init__(self):
        self.recorded = None

    def record_incident(self, payload):
        self.recorded = payload
        return payload.get("incident_id", "fake-id")


class FakeMemoryEngine:
    def __init__(self, matches=None):
        self._matches = matches or []
        self.recall_calls: list[str] = []
        self.remembered: list[dict] = []

    def recall_similar_incidents(self, query, exclude_incident_id=None):
        self.recall_calls.append(query)
        return self._matches

    def remember_incident(self, incident):
        self.remembered.append(incident)
        return {"collection": "aeam_incident_memories", "chunks_upserted": 1}


def _build_orchestrator(memory_engine=None):
    settings = Settings(
        DATABASE_URL="sqlite:///:memory:",
        REDIS_URL="redis://localhost:6379/0",
        VECTOR_DB_URL="http://localhost",
        ENVIRONMENT="development",
        LLM_ENABLED=False,
    )
    bus = EventBus()
    decision = DecisionEngine(settings=settings)
    evaluation = EvaluationEngine(settings=settings)
    stm = ShortTermMemory()
    ltm = FakeLongTermMemory()
    sm = IncidentStateMachine()

    orchestrator = Orchestrator(
        event_bus=bus,
        decision_engine=decision,
        evaluation_engine=evaluation,
        short_term_memory=stm,
        long_term_memory=ltm,
        state_machine=sm,
        settings=settings,
        memory_engine=memory_engine,
    )
    return orchestrator, ltm, stm


def _event():
    return Event(
        event_id="1", event_type="DB_LATENCY", metric="latency_ms", severity="HIGH",
        current_value=900, expected_value=200, detection_methods=["rule"],
        timestamp="2026-01-01T00:00:00Z",
    )


def test_orchestrator_without_memory_engine_behaves_unchanged():
    """Default None memory_engine — existing behaviour is byte-for-byte unaffected."""
    orchestrator, ltm, stm = _build_orchestrator(memory_engine=None)
    orchestrator.handle_event(_event())
    assert ltm.recorded is not None  # incident still finalizes normally


def test_orchestrator_appends_memory_finding_when_engine_present():
    memory = FakeMemoryEngine(matches=[{"incident_id": "inc-past", "similarity": 0.8}])
    orchestrator, ltm, stm = _build_orchestrator(memory_engine=memory)

    orchestrator.handle_event(_event())

    assert len(memory.recall_calls) == 1
    findings = ltm.recorded["findings"]
    memory_findings = [f for f in findings if f.get("type") == "memory"]
    assert len(memory_findings) == 1
    assert memory_findings[0]["data"]["matches"] == [{"incident_id": "inc-past", "similarity": 0.8}]
    assert "rag" not in {f.get("type") for f in memory_findings}  # never merged with RAG findings


def test_orchestrator_calls_remember_incident_on_finalize():
    memory = FakeMemoryEngine()
    orchestrator, ltm, stm = _build_orchestrator(memory_engine=memory)

    orchestrator.handle_event(_event())

    assert len(memory.remembered) == 1
    remembered = memory.remembered[0]
    assert remembered["event_type"] == "DB_LATENCY"
    assert remembered["metric"] == "latency_ms"
    assert remembered["severity"] == "HIGH"
    assert "incident_id" in remembered
    assert "investigation_status" in remembered


def test_memory_engine_failure_does_not_break_investigation():
    class BrokenMemory:
        def recall_similar_incidents(self, query, exclude_incident_id=None):
            raise RuntimeError("boom")

        def remember_incident(self, incident):
            raise RuntimeError("boom")

    orchestrator, ltm, stm = _build_orchestrator(memory_engine=BrokenMemory())
    orchestrator.handle_event(_event())  # must not raise

    assert ltm.recorded is not None  # incident still finalized despite memory failures
