"""
aeam/tests/test_phase_e12_knowledge_governance.py

Phase E12 — Knowledge, Policy & Memory Governance regression ledger
(MEM-4/MEM-6, RAG-7, MOD-4, COMPAT-6, SEC-7).

Covers the E12 contract:

1. **Policy lifecycle (COMPAT-6).** A retired policy never matches a new
   investigation; ``active``/``pending_review`` still do; every pre-E12 row
   behaves exactly as it did before the lifecycle existed.
2. **Memory curation (MEM-4).** Expunge removes an entry from recall and
   records who/why/when; correction rewrites it (re-embedding, not
   payload-patching) and carries its provenance; both refuse to act
   unattributed or unexplained.
3. **Semantic document typing (MOD-4/RAG-7).** A document declared as a
   runbook earns the authoritative-source bonus WITH the reason attached; an
   undeclared document keeps its pre-E12 behaviour exactly.
4. **Retrieval evaluation harness.** The golden set gates a deliberately
   degraded retrieval change, is honest about corpus gaps, and refuses to
   pass on an empty measurement.
5. **Curation is privileged (SEC-7)** and flag-gated (rollback posture).
6. **Migration/schema parity** for both new column groups.

All tests run in-process against SQLite and in-memory fakes — no live
Qdrant, no embedding model, no network (TEST-3).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect

from aeam.agents.rag.advanced_retrieval import BusinessRelevanceScorer
from aeam.api.knowledge import router as knowledge_router
from aeam.integrations.database import DatabaseClient
from aeam.intelligence.policy_registry import PolicyRegistry
from aeam.memory.enterprise_memory import EnterpriseMemoryEngine
from aeam.middleware.security_middleware import _ENDPOINT_RBAC_MAP
from aeam.registry.models import Document, Policy, PolicyStatus, SemanticDocType
from aeam.registry.repositories import DocumentRepository, PolicyRepository
from aeam.security.rbac import RBAC
from aeam.tests.retrieval_eval import (
    EvaluationReport,
    evaluate_retrieval,
    load_golden_set,
)


# ---------------------------------------------------------------------------
# Fixtures & fakes
# ---------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path):
    client = DatabaseClient(database_url=f"sqlite:///{tmp_path / 'e12.db'}")
    yield client
    client.dispose()


@pytest.fixture()
def policy_repo(db):
    return PolicyRepository(db)


class _FakeEmbedding:
    """Deterministic embeddings — no model download, no network."""

    def encode_text(self, text: str) -> list[float]:
        return [float(len(text) % 7), 1.0, 0.5]

    def encode_batch(self, texts):
        return [self.encode_text(t) for t in texts]


class _FakeRuleEngine:
    loaded_domains: list[str] = ["sales"]


def _make_policy(**kw) -> Policy:
    defaults = dict(
        doc_id="doc-1",
        source_document="handbook.md",
        raw_text="Escalate latency incidents above 2s to the on-call engineer.",
        business_rule="Escalate high latency",
        related_metrics=["db_latency"],
    )
    defaults.update(kw)
    return Policy(**defaults)


class _FakeQdrant:
    """In-memory stand-in exposing only the three calls curation uses."""

    def __init__(self):
        self.points: dict[str, dict] = {}
        self.deleted_filters: list[str] = []

    def upsert(self, collection_name, points):
        for p in points:
            self.points[str(p.id)] = dict(p.payload)

    def delete(self, collection_name, points_selector):
        incident_id = (
            points_selector.filter.must[0].match.value
        )
        self.deleted_filters.append(incident_id)
        self.points = {
            pid: payload for pid, payload in self.points.items()
            if payload.get("incident_id") != incident_id
        }
        return type("Result", (), {"operation_id": 1})()

    def scroll(self, collection_name, scroll_filter, limit=1, with_payload=True):
        incident_id = scroll_filter.must[0].match.value
        for payload in self.points.values():
            if payload.get("incident_id") == incident_id:
                return [type("Point", (), {"payload": dict(payload)})()], None
        return [], None


class _FakeIngestPipeline:
    def __init__(self, qdrant, collection="aeam_incident_memories"):
        self._qdrant = qdrant
        self._collection = collection

    @property
    def collection(self):
        return self._collection

    def ingest_document(self, text, metadata):
        from qdrant_client.http import models as qmodels

        point_id = f"{metadata['incident_id']}::0"
        self._qdrant.upsert(
            self._collection,
            [qmodels.PointStruct(id=point_id, vector=[0.1, 0.2, 0.3], payload={
                "text": text, **metadata,
            })],
        )
        return {"collection": self._collection, "chunks_upserted": 1, "chunk_ids": [point_id]}


class _FakeRetrievalPipeline:
    def __init__(self, qdrant, collection="aeam_incident_memories"):
        self._qdrant = qdrant
        self.collection = collection

    def search(self, query, top_k=3):
        return [
            {"similarity": 0.9, "metadata": dict(payload)}
            for payload in self._qdrant.points.values()
        ][:top_k]


@pytest.fixture()
def memory_engine():
    qdrant = _FakeQdrant()
    return EnterpriseMemoryEngine(
        ingestion_pipeline=_FakeIngestPipeline(qdrant),
        retrieval_pipeline=_FakeRetrievalPipeline(qdrant),
    ), qdrant


class _StubContainer:
    def __init__(self, db, settings=None, memory=None):
        self.db = db
        self.settings = settings
        self.enterprise_memory = memory


class _Settings:
    def __init__(self, curation_enabled=True):
        self.KNOWLEDGE_CURATION_ENABLED = curation_enabled


def _knowledge_app(db, settings=None, memory=None, principal="admin-1") -> FastAPI:
    app = FastAPI()
    app.state.container = _StubContainer(db, settings=settings, memory=memory)

    class _Audit:
        def __init__(self):
            self.entries = []

        def log(self, entry):
            self.entries.append(entry)

    app.state.audit_logger = _Audit()

    @app.middleware("http")
    async def _principal(request, call_next):
        if principal:
            request.state.user_id = principal
            request.state.roles = ["admin"]
        return await call_next(request)

    app.include_router(knowledge_router)
    return app


# ===========================================================================
# 1. Policy lifecycle (COMPAT-6)
# ===========================================================================

def test_retired_policy_never_matches_a_new_investigation(policy_repo):
    """The phase's headline acceptance criterion."""
    active_id = policy_repo.create(_make_policy(business_rule="Active rule"))
    retired_id = policy_repo.create(_make_policy(business_rule="Retired rule"))
    policy_repo.set_status(
        retired_id, PolicyStatus.RETIRED, changed_by="admin-1", reason="superseded",
    )

    registry = PolicyRegistry(
        policy_repository=policy_repo,
        rule_engine=_FakeRuleEngine(),
        embedding_service=_FakeEmbedding(),
    )
    matches = registry.match_for_incident(metric="db_latency", query="latency spike")

    matched_ids = {m["policy_id"] for m in matches}
    assert active_id in matched_ids
    assert retired_id not in matched_ids, "a RETIRED policy matched a new investigation"


def test_pending_review_policy_still_matches(policy_repo):
    """A review backlog must not silently degrade investigation quality."""
    pid = policy_repo.create(_make_policy())
    policy_repo.set_status(
        pid, PolicyStatus.PENDING_REVIEW, changed_by="admin-1", reason="needs check",
    )
    registry = PolicyRegistry(
        policy_repository=policy_repo,
        rule_engine=_FakeRuleEngine(),
        embedding_service=_FakeEmbedding(),
    )
    matches = registry.match_for_incident(metric="db_latency", query="latency")
    assert {m["policy_id"] for m in matches} == {pid}
    assert matches[0]["status"] == PolicyStatus.PENDING_REVIEW


def test_policies_default_to_active_so_pre_phase_rows_are_unchanged(policy_repo):
    """COMPAT-6: adopting the lifecycle changes nothing until an operator acts."""
    pid = policy_repo.create(_make_policy())
    stored = policy_repo.get(pid)
    assert stored.status == PolicyStatus.ACTIVE

    registry = PolicyRegistry(
        policy_repository=policy_repo,
        rule_engine=_FakeRuleEngine(),
        embedding_service=_FakeEmbedding(),
    )
    assert len(registry.match_for_incident(metric="db_latency", query="latency")) == 1


def test_policy_with_null_status_reads_back_as_active(db, policy_repo):
    """A row written by a pre-E12 code path has status NULL. It must never be
    ambiguous — PolicyRegistry cannot be left guessing whether it is in force."""
    pid = policy_repo.create(_make_policy())
    db.execute("UPDATE policies SET status = NULL WHERE policy_id = :p", {"p": pid})

    assert policy_repo.get(pid).status == PolicyStatus.ACTIVE
    assert pid in {p.policy_id for p in policy_repo.list_matchable()}


def test_status_transition_records_who_when_and_why(policy_repo):
    pid = policy_repo.create(_make_policy())
    policy_repo.set_status(
        pid, PolicyStatus.RETIRED, changed_by="alice", reason="policy withdrawn by legal",
    )
    stored = policy_repo.get(pid)
    assert stored.status == PolicyStatus.RETIRED
    assert stored.status_changed_by == "alice"
    assert stored.status_reason == "policy withdrawn by legal"
    assert stored.status_changed_at is not None


def test_invalid_status_is_rejected_at_the_repository(policy_repo):
    pid = policy_repo.create(_make_policy())
    with pytest.raises(ValueError, match="invalid policy status"):
        policy_repo.set_status(pid, "deleted", changed_by="a", reason="b")


def test_retired_policy_row_is_retained_not_deleted(policy_repo):
    """History must stay explainable: an incident that cited a policy before it
    was retired still needs that policy to exist."""
    pid = policy_repo.create(_make_policy())
    policy_repo.set_status(pid, PolicyStatus.RETIRED, changed_by="a", reason="b")
    assert policy_repo.get(pid) is not None


def test_lifecycle_matrix_across_every_status(policy_repo):
    ids = {}
    for status in sorted(PolicyStatus.ALL):
        pid = policy_repo.create(_make_policy(business_rule=f"rule-{status}"))
        policy_repo.set_status(pid, status, changed_by="a", reason="matrix test")
        ids[status] = pid

    matchable = {p.policy_id for p in policy_repo.list_matchable()}
    for status, pid in ids.items():
        if status in PolicyStatus.MATCHABLE:
            assert pid in matchable, f"{status} should be matchable"
        else:
            assert pid not in matchable, f"{status} must NOT be matchable"


def test_count_by_status_aggregates_in_sql(policy_repo):
    for status in (PolicyStatus.ACTIVE, PolicyStatus.RETIRED, PolicyStatus.RETIRED):
        pid = policy_repo.create(_make_policy())
        policy_repo.set_status(pid, status, changed_by="a", reason="r")
    counts = policy_repo.count_by_status()
    assert counts[PolicyStatus.RETIRED] == 2
    assert counts[PolicyStatus.ACTIVE] == 1


# ===========================================================================
# 2. Memory curation (MEM-4)
# ===========================================================================

def test_expunge_removes_the_entry_from_recall(memory_engine):
    engine, _qdrant = memory_engine
    engine.remember_incident({"incident_id": "inc-1", "root_cause": "disk full"})
    assert engine.recall_similar_incidents("disk") != []

    record = engine.expunge_incident_memory(
        "inc-1", reason="root cause was wrong", actor="alice",
    )
    assert record["expunged"] is True
    assert engine.recall_similar_incidents("disk") == []


def test_expunge_records_who_why_and_when(memory_engine):
    engine, _ = memory_engine
    engine.remember_incident({"incident_id": "inc-2", "root_cause": "bad"})
    record = engine.expunge_incident_memory(
        "inc-2", reason="incorrect analysis", actor="bob",
    )
    assert record["actor"] == "bob"
    assert record["reason"] == "incorrect analysis"
    assert record["expunged_at"] is not None
    assert record["incident_id"] == "inc-2"


def test_expunge_never_reports_a_deletion_count_it_did_not_receive(memory_engine):
    """Qdrant's delete-by-filter does not report a row count. Reporting one
    would be a fabricated fact inside an audit record."""
    engine, _ = memory_engine
    engine.remember_incident({"incident_id": "inc-3", "root_cause": "x"})
    record = engine.expunge_incident_memory("inc-3", reason="r", actor="a")
    assert record["points_deleted"] is None


@pytest.mark.parametrize("missing", ["incident_id", "reason", "actor"])
def test_curation_refuses_to_act_unattributed_or_unexplained(memory_engine, missing):
    """MEM-4 makes who and why mandatory, not optional."""
    engine, _ = memory_engine
    args = {"incident_id": "inc-4", "reason": "because", "actor": "alice"}
    args[missing] = "   "
    with pytest.raises(ValueError, match=missing):
        engine.expunge_incident_memory(
            args["incident_id"], reason=args["reason"], actor=args["actor"],
        )


def test_correction_rewrites_the_memory_and_changes_what_recall_returns(memory_engine):
    engine, _ = memory_engine
    engine.remember_incident({
        "incident_id": "inc-5", "root_cause": "wrong cause", "metric": "db_latency",
    })

    record = engine.correct_incident_memory(
        "inc-5", {"root_cause": "index missing"}, reason="reanalysed", actor="carol",
    )
    assert record["corrected"] is True
    assert record["fields_corrected"] == ["root_cause"]
    assert record["previous"] == {"root_cause": "wrong cause"}

    recalled = engine.recall_similar_incidents("latency")
    assert len(recalled) == 1
    assert recalled[0]["root_cause"] == "index missing"


def test_correction_preserves_fields_it_was_not_asked_to_change(memory_engine):
    """Correcting one field must never silently drop the others."""
    engine, _ = memory_engine
    engine.remember_incident({
        "incident_id": "inc-6", "root_cause": "old", "metric": "sales",
        "severity": "HIGH", "event_type": "SALES_DROP",
    })
    engine.correct_incident_memory(
        "inc-6", {"root_cause": "new"}, reason="fix", actor="dave",
    )
    recalled = engine.recall_similar_incidents("sales")[0]
    assert recalled["root_cause"] == "new"
    assert recalled["severity"] == "HIGH"
    assert recalled["category"] == "SALES_DROP"
    assert recalled["triggered_metric"] == "sales"


def test_corrected_memory_carries_its_correction_provenance(memory_engine):
    """A corrected memory that looks identical to an original one would hide
    exactly the fact an auditor needs."""
    engine, qdrant = memory_engine
    engine.remember_incident({"incident_id": "inc-7", "root_cause": "old"})
    engine.correct_incident_memory(
        "inc-7", {"root_cause": "new"}, reason="reanalysed after postmortem", actor="erin",
    )
    payload = next(iter(qdrant.points.values()))
    assert payload["corrected"] is True
    assert payload["corrected_by"] == "erin"
    assert payload["correction_reason"] == "reanalysed after postmortem"
    assert payload["fields_corrected"] == ["root_cause"]


def test_correcting_a_nonexistent_memory_raises_rather_than_fabricating_one(memory_engine):
    engine, _ = memory_engine
    with pytest.raises(LookupError, match="nothing to correct"):
        engine.correct_incident_memory(
            "never-existed", {"root_cause": "x"}, reason="r", actor="a",
        )


def test_correction_rejects_unrecognised_fields(memory_engine):
    engine, _ = memory_engine
    engine.remember_incident({"incident_id": "inc-8", "root_cause": "x"})
    with pytest.raises(ValueError, match="correctable memory field"):
        engine.correct_incident_memory(
            "inc-8", {"not_a_field": "y"}, reason="r", actor="a",
        )


def test_empty_corrections_are_a_caller_error_not_a_silent_noop(memory_engine):
    engine, _ = memory_engine
    with pytest.raises(ValueError, match="must not be empty"):
        engine.correct_incident_memory("inc-9", {}, reason="r", actor="a")


# ===========================================================================
# 3. Semantic document typing (MOD-4 / RAG-7)
# ===========================================================================

def test_declared_runbook_earns_the_authoritative_bonus_with_a_reason():
    """The phase's RAG-7 acceptance criterion."""
    scorer = BusinessRelevanceScorer()
    chunk = {
        "similarity": 0.5,
        "metadata": {"doc_type": "runbook", "semantic_type": "runbook"},
    }
    score, reasons = scorer.score(chunk, filter_criteria=None)

    assert score > 0.5, "declared runbook did not earn the authoritative bonus"
    authoritative = [r for r in reasons if "authoritative source" in r]
    assert authoritative, "bonus applied with no reason attached (RAG-7 violation)"
    assert "declared at upload" in authoritative[0]


def test_format_stored_as_doc_type_earns_no_authoritative_bonus():
    """The pre-E12 defect, pinned: a markdown FORMAT is not a semantic type."""
    scorer = BusinessRelevanceScorer()
    score, reasons = scorer.score(
        {"similarity": 0.5, "metadata": {"doc_type": "markdown"}}, filter_criteria=None,
    )
    assert score == pytest.approx(0.5)
    assert not [r for r in reasons if "authoritative source" in r]


def test_undeclared_document_keeps_pre_phase_behaviour_with_honest_provenance():
    """COMPAT-1: a document whose type came from the stored value, not a
    declaration, still earns the bonus — but says which it was."""
    scorer = BusinessRelevanceScorer()
    _score, reasons = scorer.score(
        {"similarity": 0.5, "metadata": {"doc_type": "runbook"}}, filter_criteria=None,
    )
    authoritative = [r for r in reasons if "authoritative source" in r]
    assert authoritative
    assert "derived from the document's stored type" in authoritative[0]


def test_processor_prefers_semantic_type_over_format_for_retrieval_metadata(db):
    """The MOD-4 fix, at the exact point the defect lived."""
    doc_repo = DocumentRepository(db)
    doc_id = doc_repo.create(Document(
        title="db-runbook.md", doc_type="markdown", semantic_type="runbook",
    ))
    doc = doc_repo.get(doc_id)

    # Mirrors DocumentIngestJobProcessor._process's metadata construction.
    retrieval_doc_type = getattr(doc, "semantic_type", None) or doc.doc_type or "document"
    assert retrieval_doc_type == "runbook"
    assert doc.doc_type == "markdown", "the format must still be preserved separately"


def test_processor_falls_back_to_format_when_nothing_was_declared(db):
    doc_repo = DocumentRepository(db)
    doc_id = doc_repo.create(Document(title="notes.md", doc_type="markdown"))
    doc = doc_repo.get(doc_id)
    retrieval_doc_type = getattr(doc, "semantic_type", None) or doc.doc_type or "document"
    assert retrieval_doc_type == "markdown"


def test_semantic_doc_type_vocabulary_covers_the_actionable_allowlist():
    """A declarable type that retrieval does not recognise would be a
    declaration that silently does nothing."""
    from aeam.agents.rag.advanced_retrieval import DEFAULT_ACTIONABLE_DOC_TYPES

    declarable_actionable = SemanticDocType.ALL & DEFAULT_ACTIONABLE_DOC_TYPES
    assert {"runbook", "sre_runbook", "incident_report", "post_mortem"} <= declarable_actionable


# ===========================================================================
# 4. Retrieval evaluation harness
# ===========================================================================

def _corpus() -> dict[str, str]:
    """A tiny deterministic corpus mirroring the golden set's expectations."""
    return {
        "startup_runbook::db_latency": (
            "database latency spike investigation steps: check connection pool "
            "usage, examine lock tables, verify replica sync failure remediation"
        ),
        "startup_runbook::sales_drop": (
            "what to do when sales drop below expected value: verify the feed, "
            "compare against the forecast baseline"
        ),
        "startup_runbook::escalation": (
            "when should an incident be escalated to a human reviewer: severity "
            "critical or unresolved after two investigation passes"
        ),
        "reference::glossary": "a glossary of database and sales terminology",
    }


def _keyword_retriever(corpus: dict[str, str]):
    """A deterministic lexical retriever — no embeddings, no Qdrant, no network."""
    def _retrieve(query: str, k: int) -> list[dict]:
        terms = {t for t in query.lower().split() if len(t) > 3}
        scored = []
        for chunk_id, text in corpus.items():
            overlap = sum(1 for t in terms if t in text.lower())
            if overlap:
                scored.append((overlap, chunk_id))
        scored.sort(key=lambda pair: (-pair[0], pair[1]))
        return [{"chunk_id": cid} for _score, cid in scored[:k]]
    return _retrieve


def test_golden_set_fixture_is_well_formed():
    data = load_golden_set()
    assert data["cases"], "golden set has no cases"
    for case in data["cases"]:
        assert case["id"] and case["query"] and case["expected_chunk_ids"]
        assert case.get("rationale"), f"case {case['id']} has no stated rationale"
    thresholds = data["thresholds"]
    assert 0.0 < thresholds["min_recall_at_k"] <= 1.0
    assert 0.0 < thresholds["min_mrr"] <= 1.0


def test_healthy_retriever_passes_the_golden_set():
    corpus = _corpus()
    report = evaluate_retrieval(
        _keyword_retriever(corpus), corpus_chunk_ids=corpus.keys(),
    )
    assert report.passed, report.failure_summary()
    assert report.mean_recall_at_k >= report.min_recall_at_k
    assert report.mrr >= report.min_mrr


def test_golden_set_gates_a_deliberately_degraded_retrieval_change():
    """The phase's headline acceptance criterion for the harness: a degraded
    retriever must turn the build red."""
    corpus = _corpus()

    def _degraded(query: str, k: int) -> list[dict]:
        # Returns the wrong document for everything — the exact shape of a
        # silent corpus/chunking regression.
        return [{"chunk_id": "reference::glossary"}]

    report = evaluate_retrieval(_degraded, corpus_chunk_ids=corpus.keys())
    assert report.passed is False
    assert "Retrieval quality regression" in report.failure_summary()
    assert report.mean_recall_at_k == 0.0
    assert report.mrr == 0.0


def test_harness_catches_a_ranking_only_regression():
    """Recall can stay perfect while ranking collapses. MRR must catch that."""
    corpus = _corpus()

    def _buried(query: str, k: int) -> list[dict]:
        healthy = _keyword_retriever(corpus)(query, k)
        # Push the right answer to the bottom of the page.
        return [{"chunk_id": "reference::glossary"}] * (k - 1) + healthy[:1]

    report = evaluate_retrieval(_buried, corpus_chunk_ids=corpus.keys())
    assert report.mean_recall_at_k > 0.0, "recall should still find the evidence"
    assert report.mrr < report.min_mrr, "MRR should expose the ranking collapse"
    assert report.passed is False


def test_harness_skips_rather_than_blames_when_the_corpus_lacks_the_evidence():
    """A missing document is a corpus problem, not a retrieval score."""
    report = evaluate_retrieval(
        _keyword_retriever(_corpus()), corpus_chunk_ids=["reference::glossary"],
    )
    assert len(report.skipped) == len(load_golden_set()["cases"])
    assert report.scored == []
    assert report.passed is False
    assert "corpus problem, not a retrieval score" in report.failure_summary()


def test_harness_never_passes_on_an_empty_measurement():
    """'We measured nothing' must never look like a green build."""
    report = EvaluationReport(k=5, min_recall_at_k=0.6, min_mrr=0.5)
    assert report.passed is False


def test_a_crashing_retriever_is_a_failure_not_a_skip():
    def _boom(query: str, k: int):
        raise RuntimeError("retriever exploded")

    corpus = _corpus()
    report = evaluate_retrieval(_boom, corpus_chunk_ids=corpus.keys())
    assert report.passed is False
    assert report.skipped == [], "a crash must be scored, never skipped"


# ===========================================================================
# 5. Curation is privileged (SEC-7) and flag-gated
# ===========================================================================

def test_curation_namespace_is_rbac_mapped_to_admin_config():
    mapping = [(p, r, a) for p, r, a in _ENDPOINT_RBAC_MAP]
    entry = next((e for e in mapping if e[0] == "/api/v1/knowledge/curate"), None)
    assert entry is not None, "curation namespace is not RBAC-mapped"
    _prefix, resource, action = entry
    assert (resource, action) == ("admin", "config")

    rbac = RBAC()
    assert rbac.check_permission(["admin"], resource, action) is True
    for role in ("analyst", "operator", "auditor", "readonly"):
        assert rbac.check_permission([role], resource, action) is False, role


def test_curation_prefix_resolves_before_the_broader_knowledge_entry():
    """Longest-prefix-first ordering: a curate path must never fall through to
    the read-only documents:search mapping."""
    from aeam.middleware.security_middleware import SecurityMiddleware

    resource, action = SecurityMiddleware._resolve_rbac(
        "/api/v1/knowledge/curate/policies/p1/status"
    )
    assert (resource, action) == ("admin", "config")


def test_policy_status_endpoint_transitions_and_audits(db):
    repo = PolicyRepository(db)
    pid = repo.create(_make_policy())
    app = _knowledge_app(db, settings=_Settings())
    client = TestClient(app)

    resp = client.post(
        f"/api/v1/knowledge/curate/policies/{pid}/status",
        json={"status": "retired", "reason": "superseded by the 2026 handbook"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["previous_status"] == "active"
    assert body["new_status"] == "retired"
    assert body["matches_new_investigations"] is False
    assert repo.get(pid).status == PolicyStatus.RETIRED

    audited = app.state.audit_logger.entries
    assert any(e["action"] == "policy_status_changed" for e in audited)
    entry = next(e for e in audited if e["action"] == "policy_status_changed")
    assert entry["user_id"] == "admin-1"
    assert entry["reason"] == "superseded by the 2026 handbook"


def test_status_transition_requires_a_reason(db):
    repo = PolicyRepository(db)
    pid = repo.create(_make_policy())
    client = TestClient(_knowledge_app(db, settings=_Settings()))
    resp = client.post(
        f"/api/v1/knowledge/curate/policies/{pid}/status",
        json={"status": "retired", "reason": ""},
    )
    assert resp.status_code == 422


def test_invalid_status_rejected_by_the_endpoint(db):
    repo = PolicyRepository(db)
    pid = repo.create(_make_policy())
    client = TestClient(_knowledge_app(db, settings=_Settings()))
    resp = client.post(
        f"/api/v1/knowledge/curate/policies/{pid}/status",
        json={"status": "obliterated", "reason": "why not"},
    )
    assert resp.status_code == 422


def test_curation_disabled_returns_503_while_reads_keep_working(db):
    repo = PolicyRepository(db)
    pid = repo.create(_make_policy())
    client = TestClient(_knowledge_app(db, settings=_Settings(curation_enabled=False)))

    write = client.post(
        f"/api/v1/knowledge/curate/policies/{pid}/status",
        json={"status": "retired", "reason": "r"},
    )
    assert write.status_code == 503
    assert "KNOWLEDGE_CURATION_ENABLED" in write.json()["detail"]

    read = client.get("/api/v1/knowledge/policies")
    assert read.status_code == 200, "reads must be unaffected by the curation flag"


def test_policies_list_exposes_lifecycle_and_counts(db):
    repo = PolicyRepository(db)
    active = repo.create(_make_policy(business_rule="still in force"))
    retired = repo.create(_make_policy(business_rule="withdrawn"))
    repo.set_status(retired, PolicyStatus.RETIRED, changed_by="a", reason="r")

    client = TestClient(_knowledge_app(db, settings=_Settings()))
    body = client.get("/api/v1/knowledge/policies").json()

    by_id = {p["policy_id"]: p for p in body["policies"]}
    assert by_id[active]["matchable"] is True
    assert by_id[retired]["matchable"] is False
    assert body["status_counts"]["retired"] == 1
    assert set(body["lifecycle"]["statuses"]) == PolicyStatus.ALL


def test_policies_list_filters_by_status(db):
    repo = PolicyRepository(db)
    repo.create(_make_policy())
    retired = repo.create(_make_policy())
    repo.set_status(retired, PolicyStatus.RETIRED, changed_by="a", reason="r")

    client = TestClient(_knowledge_app(db, settings=_Settings()))
    body = client.get("/api/v1/knowledge/policies", params={"status": "retired"}).json()
    assert [p["policy_id"] for p in body["policies"]] == [retired]


def test_memory_curation_endpoints_audit_who_and_why(db, memory_engine):
    engine, _ = memory_engine
    engine.remember_incident({"incident_id": "inc-api", "root_cause": "wrong"})

    app = _knowledge_app(db, settings=_Settings(), memory=engine)
    client = TestClient(app)

    resp = client.post(
        "/api/v1/knowledge/curate/memory/expunge",
        json={"incident_id": "inc-api", "reason": "analysis was incorrect"},
    )
    assert resp.status_code == 200
    assert resp.json()["expunged"] is True

    entry = next(e for e in app.state.audit_logger.entries if e["action"] == "memory_expunged")
    assert entry["user_id"] == "admin-1"
    assert entry["reason"] == "analysis was incorrect"
    assert entry["incident_id"] == "inc-api"


def test_memory_correction_endpoint_returns_and_audits_changed_fields(db, memory_engine):
    engine, _ = memory_engine
    engine.remember_incident({"incident_id": "inc-fix", "root_cause": "old", "metric": "m"})

    app = _knowledge_app(db, settings=_Settings(), memory=engine)
    client = TestClient(app)
    resp = client.post(
        "/api/v1/knowledge/curate/memory/correct",
        json={
            "incident_id": "inc-fix",
            "corrections": {"root_cause": "corrected cause"},
            "reason": "reanalysed",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["fields_corrected"] == ["root_cause"]

    entry = next(e for e in app.state.audit_logger.entries if e["action"] == "memory_corrected")
    assert entry["fields_corrected"] == ["root_cause"]


def test_correcting_an_unknown_incident_returns_404(db, memory_engine):
    engine, _ = memory_engine
    client = TestClient(_knowledge_app(db, settings=_Settings(), memory=engine))
    resp = client.post(
        "/api/v1/knowledge/curate/memory/correct",
        json={"incident_id": "nope", "corrections": {"root_cause": "x"}, "reason": "r"},
    )
    assert resp.status_code == 404


def test_semantic_type_declaration_endpoint(db):
    doc_repo = DocumentRepository(db)
    doc_id = doc_repo.create(Document(title="ops.md", doc_type="markdown"))

    client = TestClient(_knowledge_app(db, settings=_Settings()))
    resp = client.post(
        f"/api/v1/knowledge/curate/documents/{doc_id}/semantic-type",
        params={"semantic_type": "runbook"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["semantic_type"] == "runbook"
    assert body["format"] == "markdown"
    assert body["authoritative_source"] is True
    # Honest about when it takes effect rather than implying immediacy.
    assert "next re-index" in body["takes_effect"]
    assert doc_repo.get(doc_id).semantic_type == "runbook"


def test_document_read_api_exposes_the_declared_semantic_type(db):
    """Regression: the console renders the semantic-type panel from the
    document READ payload. If the serializer omits the field, every document
    reports 'not declared' regardless of what was stored — the classification
    would be invisible even though it is working. Caught in E2E; pinned here."""
    doc_repo = DocumentRepository(db)
    declared_id = doc_repo.create(Document(
        title="runbook.md", doc_type="markdown", semantic_type="runbook",
    ))
    undeclared_id = doc_repo.create(Document(title="notes.md", doc_type="markdown"))

    client = TestClient(_knowledge_app(db, settings=_Settings()))

    declared = client.get(f"/api/v1/knowledge/documents/{declared_id}").json()
    assert declared["semantic_type"] == "runbook"
    # The format must remain visible and separate — neither stands in for the other.
    assert declared["doc_type"] == "markdown"
    assert declared["file_type"] == "markdown"

    undeclared = client.get(f"/api/v1/knowledge/documents/{undeclared_id}").json()
    # Honest None, never silently backfilled from the format.
    assert undeclared["semantic_type"] is None

    listing = client.get("/api/v1/knowledge/documents").json()
    rows = listing["documents"] if isinstance(listing, dict) else listing
    by_id = {d["doc_id"]: d for d in rows}
    assert by_id[declared_id]["semantic_type"] == "runbook"
    assert by_id[undeclared_id]["semantic_type"] is None


def test_unrecognised_semantic_type_is_rejected(db):
    doc_repo = DocumentRepository(db)
    doc_id = doc_repo.create(Document(title="ops.md", doc_type="markdown"))
    client = TestClient(_knowledge_app(db, settings=_Settings()))
    resp = client.post(
        f"/api/v1/knowledge/curate/documents/{doc_id}/semantic-type",
        params={"semantic_type": "runbok"},
    )
    assert resp.status_code == 422


# ===========================================================================
# 6. Schema parity for both new column groups
# ===========================================================================

def test_knowledge_governance_guide_documents_every_curation_surface():
    """DOC-2: the phase ships a governance guide covering the policy
    lifecycle, the memory correction procedure, and the retrieval evaluation
    methodology. A curation endpoint with no documented procedure is a
    capability nobody can safely use."""
    guide = Path(__file__).resolve().parents[2] / "docs" / "KNOWLEDGE_GOVERNANCE.md"
    assert guide.is_file(), "docs/KNOWLEDGE_GOVERNANCE.md missing"
    text = guide.read_text(encoding="utf-8")

    for required in (
        "Policy lifecycle",
        "Memory curation",
        "Semantic document typing",
        "Retrieval evaluation methodology",
        "/api/v1/knowledge/curate/policies",
        "/api/v1/knowledge/curate/memory/expunge",
        "/api/v1/knowledge/curate/memory/correct",
        "KNOWLEDGE_CURATION_ENABLED",
        "policy_status_changed",
        "memory_expunged",
        "memory_corrected",
    ):
        assert required in text, f"KNOWLEDGE_GOVERNANCE.md does not document {required!r}"

    # Every lifecycle status must be documented, not just the ones in use.
    for status in PolicyStatus.ALL:
        assert status in text, f"lifecycle status {status!r} is undocumented"


def test_startup_ddl_creates_both_e12_column_groups(db):
    inspector = inspect(db._engine)
    policy_cols = {c["name"] for c in inspector.get_columns("policies")}
    doc_cols = {c["name"] for c in inspector.get_columns("documents")}

    for col in ("status", "status_changed_at", "status_changed_by", "status_reason"):
        assert col in policy_cols, f"startup DDL missing policies.{col}"
    assert "semantic_type" in doc_cols, "startup DDL missing documents.semantic_type"


def _alembic_config(db_url: str):
    """Same shim the E5 migration suite uses — env.py reads the URL from the
    -x command-line arg, not from sqlalchemy.url."""
    from alembic.config import Config

    root = Path(__file__).resolve().parents[2]
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    cfg.cmd_opts = type("O", (), {"x": [f"db_url={db_url}"]})()
    return cfg


def test_migration_path_creates_both_e12_column_groups(tmp_path):
    from alembic import command

    url = f"sqlite:///{tmp_path / 'migrated.db'}"
    command.upgrade(_alembic_config(url), "head")

    engine = create_engine(url)
    try:
        policy_cols = {c["name"] for c in inspect(engine).get_columns("policies")}
        doc_cols = {c["name"] for c in inspect(engine).get_columns("documents")}
    finally:
        engine.dispose()

    for col in ("status", "status_changed_at", "status_changed_by", "status_reason"):
        assert col in policy_cols, f"migration missing policies.{col}"
    assert "semantic_type" in doc_cols, "migration missing documents.semantic_type"


def test_migration_backfills_existing_policies_to_active(tmp_path):
    """COMPAT-6: an existing corpus must not end up with NULL-status policies
    that PolicyRegistry would have to guess about."""
    from alembic import command
    from sqlalchemy import text

    url = f"sqlite:///{tmp_path / 'backfill.db'}"
    cfg = _alembic_config(url)

    command.upgrade(cfg, "0004_human_review")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO policies (policy_id, doc_id, raw_text) "
            "VALUES ('legacy-1', 'd1', 'pre-E12 policy')"
        ))
    engine.dispose()

    command.upgrade(cfg, "head")

    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT status FROM policies WHERE policy_id = 'legacy-1'")
            ).mappings().first()
    finally:
        engine.dispose()

    assert row["status"] == "active", "pre-E12 policy was not backfilled to active"
