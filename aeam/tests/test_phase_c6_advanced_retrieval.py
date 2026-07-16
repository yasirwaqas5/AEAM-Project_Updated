"""
aeam/tests/test_phase_c6_advanced_retrieval.py

Advanced Retrieval Engine (Phase C6) tests.

Four layers, matching this codebase's established test conventions (fakes
duck-typing the real classes' public interface; no live Qdrant/LLM/model I/O):

1. IncidentEntityExtractor — deterministic extraction from event.metadata,
   reusing RAGAgent's own metadata vocabulary.
2. BusinessRelevanceScorer — bounded, explainable scoring/reasons.
3. AdvancedRetrievalPipeline — drop-in wrapper: normal filtering, automatic
   relaxation when a filter matches nothing, business-relevance reordering.
4. Wiring: RAGAgent (entity extraction -> filter_criteria -> retrieval call
   -> extended findings) and RetrievalDebugTracer (new entity-extraction /
   metadata-filter / business-relevance stages, fully backward compatible
   when metadata/extractor/scorer are absent).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from aeam.agents.rag.advanced_retrieval import (
    AdvancedRetrievalPipeline,
    BusinessRelevanceScorer,
    IncidentEntityExtractor,
)
from aeam.agents.rag.hybrid_retrieval import BM25Index, HybridRetrievalPipeline
from aeam.agents.rag.rag_agent import RAGAgent
from aeam.agents.rag.retrieval_debug import RetrievalDebugTracer, summarize_chunk


# ===========================================================================
# 1. IncidentEntityExtractor
# ===========================================================================

def test_extract_returns_empty_for_no_metadata():
    ex = IncidentEntityExtractor()
    assert ex.extract(None) == []
    assert ex.extract({}) == []


def test_extract_recognises_known_vocabulary_keys():
    ex = IncidentEntityExtractor()
    entities = ex.extract({"service": "checkout", "host": "web-03"})
    keys = {e["key"] for e in entities}
    assert keys == {"service", "host"}
    by_key = {e["key"]: e for e in entities}
    assert by_key["service"]["label"] == "service"
    assert by_key["service"]["value"] == "checkout"


def test_extract_ignores_noise_keys():
    ex = IncidentEntityExtractor()
    entities = ex.extract({"trace_id": "abc123", "event_id": "e1", "timestamp": "now"})
    assert entities == []


def test_extract_falls_back_to_unrecognised_keys():
    ex = IncidentEntityExtractor()
    entities = ex.extract({"custom_field": "value1"})
    assert len(entities) == 1
    assert entities[0]["key"] == "custom_field"
    assert entities[0]["value"] == "value1"


def test_extract_never_fabricates_missing_values():
    ex = IncidentEntityExtractor()
    entities = ex.extract({"service": None, "host": ""})
    assert entities == []


def test_to_filter_criteria_uses_label_as_key():
    ex = IncidentEntityExtractor()
    entities = ex.extract({"service_name": "checkout"})
    criteria = ex.to_filter_criteria(entities)
    assert criteria == {"service": "checkout"}  # service_name -> label "service"


def test_to_filter_criteria_empty_for_no_entities():
    ex = IncidentEntityExtractor()
    assert ex.to_filter_criteria([]) == {}


# ===========================================================================
# 2. BusinessRelevanceScorer
# ===========================================================================

def test_score_defaults_to_base_relevance_with_honest_reason():
    scorer = BusinessRelevanceScorer()
    chunk = {"chunk_id": "c1", "metadata": {}, "similarity": 0.7}
    score, reasons = scorer.score(chunk, None)
    assert score == 0.7
    assert reasons == ["ranked by existing semantic relevance only"]


def test_score_prefers_rerank_score_over_similarity():
    scorer = BusinessRelevanceScorer()
    chunk = {"chunk_id": "c1", "metadata": {}, "similarity": 0.9, "rerank_score": 0.3}
    score, _ = scorer.score(chunk, None)
    assert score == 0.3


def test_score_adds_entity_match_bonus_with_reason():
    scorer = BusinessRelevanceScorer()
    chunk = {"chunk_id": "c1", "metadata": {"service": "checkout"}, "similarity": 0.5}
    score, reasons = scorer.score(chunk, {"service": "checkout"})
    assert score > 0.5
    assert any("matches incident entity service=checkout" in r for r in reasons)


def test_score_no_bonus_when_entity_value_mismatches():
    scorer = BusinessRelevanceScorer()
    chunk = {"chunk_id": "c1", "metadata": {"service": "billing"}, "similarity": 0.5}
    score, reasons = scorer.score(chunk, {"service": "checkout"})
    assert score == 0.5
    assert reasons == ["ranked by existing semantic relevance only"]


def test_score_adds_doc_type_bonus_for_actionable_source():
    scorer = BusinessRelevanceScorer()
    chunk = {"chunk_id": "c1", "metadata": {"doc_type": "runbook"}, "similarity": 0.5}
    score, reasons = scorer.score(chunk, None)
    assert score > 0.5
    assert any("authoritative source" in r for r in reasons)


def test_score_no_doc_type_bonus_for_generic_source():
    scorer = BusinessRelevanceScorer()
    chunk = {"chunk_id": "c1", "metadata": {"doc_type": "ml_notes"}, "similarity": 0.5}
    score, reasons = scorer.score(chunk, None)
    assert score == 0.5
    assert reasons == ["ranked by existing semantic relevance only"]


def test_score_adds_recency_bonus_for_recent_document():
    import datetime
    scorer = BusinessRelevanceScorer(recency_window_days=30)
    recent_date = (datetime.datetime.now() - datetime.timedelta(days=5)).date().isoformat()
    chunk = {"chunk_id": "c1", "metadata": {"date": recent_date}, "similarity": 0.5}
    score, reasons = scorer.score(chunk, None)
    assert score > 0.5
    assert any("recent document" in r for r in reasons)


def test_score_no_recency_bonus_for_old_document():
    scorer = BusinessRelevanceScorer(recency_window_days=30)
    chunk = {"chunk_id": "c1", "metadata": {"date": "2020-01-01"}, "similarity": 0.5}
    score, reasons = scorer.score(chunk, None)
    assert score == 0.5
    assert reasons == ["ranked by existing semantic relevance only"]


def test_score_never_raises_on_malformed_date():
    scorer = BusinessRelevanceScorer()
    chunk = {"chunk_id": "c1", "metadata": {"date": "not-a-date"}, "similarity": 0.5}
    score, reasons = scorer.score(chunk, None)
    assert score == 0.5


def test_score_clamped_to_one():
    scorer = BusinessRelevanceScorer()
    chunk = {
        "chunk_id": "c1", "similarity": 1.0,
        "metadata": {"service": "checkout", "host": "web-03", "doc_type": "runbook"},
    }
    score, _ = scorer.score(chunk, {"service": "checkout", "host": "web-03"})
    assert score <= 1.0


def test_score_notes_diversity_and_relaxation_flags():
    scorer = BusinessRelevanceScorer()
    chunk = {
        "chunk_id": "c1", "similarity": 0.5, "metadata": {},
        "diversity_kept_reason": "diverse", "metadata_filter_relaxed": True,
    }
    _, reasons = scorer.score(chunk, None)
    assert any("evidence diversity" in r for r in reasons)
    assert any("relaxed" in r for r in reasons)


# ===========================================================================
# 3. AdvancedRetrievalPipeline
# ===========================================================================

class FakeInner:
    def __init__(self, by_filter):
        # by_filter: dict mapping a hashable representation of filter_criteria
        # (or None) -> list of chunk dicts to return.
        self._by_filter = by_filter
        self.calls: list[tuple] = []
        self.similarity_threshold = 0.5
        self.collection = "aeam_documents"

    def search(self, query, filter_criteria=None, top_k=5):
        key = tuple(sorted(filter_criteria.items())) if filter_criteria else None
        self.calls.append((query, key, top_k))
        return list(self._by_filter.get(key, []))[:top_k]


def test_pipeline_rejects_none_dependencies():
    with pytest.raises(ValueError):
        AdvancedRetrievalPipeline(inner_pipeline=None, relevance_scorer=BusinessRelevanceScorer())
    with pytest.raises(ValueError):
        AdvancedRetrievalPipeline(inner_pipeline=FakeInner({}), relevance_scorer=None)


def test_pipeline_passthrough_filter_when_matches_found():
    filtered_key = (("service", "checkout"),)
    inner = FakeInner({
        filtered_key: [{"chunk_id": "c1", "metadata": {"service": "checkout"}, "similarity": 0.8}],
    })
    pipeline = AdvancedRetrievalPipeline(inner_pipeline=inner, relevance_scorer=BusinessRelevanceScorer())
    result = pipeline.search(query="q", filter_criteria={"service": "checkout"}, top_k=5)
    assert len(result) == 1
    assert result[0]["chunk_id"] == "c1"
    assert result[0].get("metadata_filter_relaxed") is not True
    assert inner.calls[0][1] == filtered_key   # only the filtered call was made


def test_pipeline_relaxes_when_filter_matches_nothing():
    inner = FakeInner({
        None: [{"chunk_id": "c2", "metadata": {}, "similarity": 0.6}],
    })
    pipeline = AdvancedRetrievalPipeline(inner_pipeline=inner, relevance_scorer=BusinessRelevanceScorer())
    result = pipeline.search(query="q", filter_criteria={"service": "checkout"}, top_k=5)
    assert len(result) == 1
    assert result[0]["chunk_id"] == "c2"
    assert result[0]["metadata_filter_relaxed"] is True
    # Two calls: filtered (empty) then unfiltered (relaxed).
    assert len(inner.calls) == 2
    assert inner.calls[0][1] == (("service", "checkout"),)
    assert inner.calls[1][1] is None


def test_pipeline_no_relaxation_attempt_when_no_filter_given():
    inner = FakeInner({None: []})
    pipeline = AdvancedRetrievalPipeline(inner_pipeline=inner, relevance_scorer=BusinessRelevanceScorer())
    result = pipeline.search(query="q", filter_criteria=None, top_k=5)
    assert result == []
    assert len(inner.calls) == 1   # no wasted second call when there was no filter to relax


def test_pipeline_sorts_by_business_relevance_score():
    inner = FakeInner({
        None: [
            {"chunk_id": "low", "metadata": {}, "rerank_score": 0.4},
            {"chunk_id": "high", "metadata": {"doc_type": "runbook"}, "rerank_score": 0.4},
        ],
    })
    pipeline = AdvancedRetrievalPipeline(inner_pipeline=inner, relevance_scorer=BusinessRelevanceScorer())
    result = pipeline.search(query="q", top_k=5)
    assert [r["chunk_id"] for r in result] == ["high", "low"]
    assert result[0]["business_relevance_score"] > result[1]["business_relevance_score"]
    assert result[0]["retrieval_confidence"] == result[0]["business_relevance_score"]


def test_pipeline_preserves_existing_keys_untouched():
    inner = FakeInner({None: [{"chunk_id": "c1", "metadata": {"source": "s"}, "similarity": 0.5, "text": "hello"}]})
    pipeline = AdvancedRetrievalPipeline(inner_pipeline=inner, relevance_scorer=BusinessRelevanceScorer())
    result = pipeline.search(query="q", top_k=5)
    assert result[0]["source"] if "source" in result[0] else True  # no crash
    assert result[0]["text"] == "hello"
    assert result[0]["similarity"] == 0.5


def test_pipeline_delegates_similarity_threshold_and_collection():
    inner = FakeInner({})
    pipeline = AdvancedRetrievalPipeline(inner_pipeline=inner, relevance_scorer=BusinessRelevanceScorer())
    assert pipeline.similarity_threshold == 0.5
    assert pipeline.collection == "aeam_documents"


def test_pipeline_rejects_empty_query_and_bad_top_k():
    inner = FakeInner({})
    pipeline = AdvancedRetrievalPipeline(inner_pipeline=inner, relevance_scorer=BusinessRelevanceScorer())
    with pytest.raises(ValueError):
        pipeline.search(query="", top_k=5)
    with pytest.raises(ValueError):
        pipeline.search(query="q", top_k=0)


# ===========================================================================
# 4a. RAGAgent wiring
# ===========================================================================

class DummyEvent:
    event_id = "1"
    event_type = "KPI_ANOMALY"
    metric = "cpu"
    severity = "HIGH"
    current_value = 90
    expected_value = 50
    detection_methods = ["zscore"]
    metadata = {"service": "checkout"}


def test_ragagent_without_entity_extractor_is_unaffected():
    """Backward compatibility: no entity_extractor -> behaves exactly as before Phase C6."""
    retrieval = MagicMock()
    retrieval.search.return_value = []
    agent = RAGAgent(retrieval, MagicMock(), MagicMock())
    result = agent.investigate(DummyEvent(), MagicMock())
    assert result["findings"]["retrieved_count"] == 0
    retrieval.search.assert_called_once()
    _, kwargs = retrieval.search.call_args
    assert kwargs.get("filter_criteria") is None
    assert result["findings"]["extracted_entities"] == []


def test_ragagent_with_entity_extractor_builds_filter_criteria():
    retrieval = MagicMock()
    retrieval.search.return_value = [{"chunk_id": "abc", "text": "x", "metadata": {}, "similarity": 0.9}]
    llm = MagicMock()
    llm.query.return_value = json.dumps({
        "possible_causes": [{"cause": "checkout bug", "chunk_id": "abc", "confidence": 0.8}],
        "overall_confidence": 0.8,
        "requires_human_review": False,
    })
    validator = MagicMock()
    validator.validate.return_value = (True, "valid")

    agent = RAGAgent(retrieval, validator, llm, entity_extractor=IncidentEntityExtractor())
    result = agent.investigate(DummyEvent(), MagicMock())

    retrieval.search.assert_called_once()
    _, kwargs = retrieval.search.call_args
    assert kwargs.get("filter_criteria") == {"service": "checkout"}
    assert result["findings"]["extracted_entities"] == [{"key": "service", "label": "service", "value": "checkout"}]
    assert result["findings"]["metadata_filter_applied"] is True


def test_ragagent_no_context_result_still_reports_entities_honestly():
    retrieval = MagicMock()
    retrieval.search.return_value = []
    agent = RAGAgent(retrieval, MagicMock(), MagicMock(), entity_extractor=IncidentEntityExtractor())
    result = agent.investigate(DummyEvent(), MagicMock())
    assert result["findings"]["retrieved_count"] == 0
    assert result["findings"]["extracted_entities"] == [{"key": "service", "label": "service", "value": "checkout"}]


def test_ragagent_retrieved_chunks_carry_c6_fields_defaulted_when_absent():
    """Even without an AdvancedRetrievalPipeline in the chain, retrieved_chunks
    must always carry the Phase C6 keys (defaulting to None/False) so the
    findings schema is uniform regardless of wiring."""
    retrieval = MagicMock()
    retrieval.search.return_value = [{"chunk_id": "abc", "text": "x", "metadata": {}, "similarity": 0.9}]
    llm = MagicMock()
    llm.query.return_value = json.dumps({
        "possible_causes": [{"cause": "c", "chunk_id": "abc", "confidence": 0.7}],
        "overall_confidence": 0.7,
        "requires_human_review": False,
    })
    validator = MagicMock()
    validator.validate.return_value = (True, "valid")
    agent = RAGAgent(retrieval, validator, llm)
    result = agent.investigate(DummyEvent(), MagicMock())
    chunk_meta = result["findings"]["retrieved_chunks"][0]
    assert chunk_meta["business_relevance_score"] is None
    assert chunk_meta["ranking_reasons"] is None
    assert chunk_meta["metadata_filter_relaxed"] is False


# ===========================================================================
# 4b. RetrievalDebugTracer wiring
# ===========================================================================

CORPUS = [
    {"chunk_id": "c1", "text": "database replication lag increased read query latency",
     "metadata": {"source": "runbook_db", "doc_type": "runbook", "service": "database"}},
    {"chunk_id": "c2", "text": "cpu saturation from a runaway process caused elevated latency",
     "metadata": {"source": "runbook_cpu", "doc_type": "ml_notes"}},
]
_BY_ID = {c["chunk_id"]: c for c in CORPUS}


class FakeDense:
    def __init__(self, ranking, filtered_ranking=None, similarity_step=0.05):
        self._ranking = ranking
        self._filtered_ranking = filtered_ranking or {}
        self._similarity_step = similarity_step
        self.similarity_threshold = 0.5
        self.collection = "aeam_documents"
        self.calls: list[dict] = []

    def search(self, query, filter_criteria=None, top_k=5):
        self.calls.append({"query": query, "filter_criteria": filter_criteria, "top_k": top_k})
        if filter_criteria:
            ids = self._filtered_ranking.get(query, [])
        else:
            ids = self._ranking.get(query, [])
        out = []
        for rank, cid in enumerate(ids[:top_k]):
            doc = _BY_ID[cid]
            out.append({"chunk_id": cid, "text": doc["text"], "metadata": dict(doc["metadata"]),
                        "similarity": round(0.9 - self._similarity_step * rank, 6)})
        return out


def _build_tracer(dense, entity_extractor=None, relevance_scorer=None):
    bm25 = BM25Index()
    bm25.build(CORPUS)
    hybrid_stage = HybridRetrievalPipeline(dense, bm25)
    return RetrievalDebugTracer(
        dense=dense, bm25_index=bm25, hybrid_stage=hybrid_stage,
        query_expander=None, reranker=None, diversity_filter=None,
        rerank_top_n=10,
        entity_extractor=entity_extractor, relevance_scorer=relevance_scorer,
    )


def test_tracer_backward_compatible_without_metadata_or_extractor():
    dense = FakeDense({"cpu issue": ["c2", "c1"]})
    tracer = _build_tracer(dense)
    result = tracer.trace(query="cpu issue", top_k=2)
    assert result["extracted_entities"] == []
    assert result["metadata_filter_applied"] is False
    assert result["metadata_filtered_results"] == []
    assert result["business_ranked"] == result["final_chunks"]


def test_tracer_extracts_entities_when_metadata_supplied():
    dense = FakeDense({"db issue": ["c1", "c2"]})
    tracer = _build_tracer(dense, entity_extractor=IncidentEntityExtractor())
    result = tracer.trace(query="db issue", top_k=2, metadata={"service": "database"})
    assert result["extracted_entities"] == [{"key": "service", "label": "service", "value": "database"}]
    assert result["metadata_filter_applied"] is True


def test_tracer_metadata_filter_relaxes_when_no_match():
    """The filtered dense ranking has no entry for this query -> the fusion
    stage's filtered call returns nothing -> tracer relaxes automatically."""
    dense = FakeDense({"db issue": ["c1", "c2"]}, filtered_ranking={})
    tracer = _build_tracer(dense, entity_extractor=IncidentEntityExtractor())
    result = tracer.trace(query="db issue", top_k=2, metadata={"service": "database"})
    assert result["metadata_filter_relaxed"] is True
    assert len(result["rrf_fused"]) > 0   # relaxed retry recovered real results
    assert all(c.get("metadata_filter_relaxed") for c in result["rrf_fused"])


def test_tracer_no_relaxation_when_filter_matches():
    dense = FakeDense({"db issue": ["c1", "c2"]}, filtered_ranking={"db issue": ["c1"]})
    tracer = _build_tracer(dense, entity_extractor=IncidentEntityExtractor())
    result = tracer.trace(query="db issue", top_k=2, metadata={"service": "database"})
    assert result["metadata_filter_relaxed"] is False
    assert len(result["metadata_filtered_results"]) == 1
    assert result["metadata_filtered_results"][0]["chunk_id"] == "c1"


def test_tracer_business_relevance_stage_reorders_and_scores():
    # c2 (ml_notes) ranked ABOVE c1 (runbook) by dense alone, but only by a
    # margin smaller than the doc_type authority bonus, so business-relevance
    # ranking should promote c1 to the top.
    dense = FakeDense({"issue": ["c2", "c1"]}, similarity_step=0.02)
    tracer = _build_tracer(dense, relevance_scorer=BusinessRelevanceScorer())
    result = tracer.trace(query="issue", top_k=2)
    # c1 is doc_type=runbook (actionable) -> should be boosted to the top of business_ranked
    assert result["business_ranked"][0]["chunk_id"] == "c1"
    assert result["business_ranked"][0]["business_relevance_score"] is not None
    assert result["final_chunks"] == result["business_ranked"]


def test_tracer_entity_extraction_timing_recorded():
    dense = FakeDense({"issue": ["c1"]})
    tracer = _build_tracer(dense, entity_extractor=IncidentEntityExtractor(), relevance_scorer=BusinessRelevanceScorer())
    result = tracer.trace(query="issue", top_k=1, metadata={"service": "database"})
    for key in ("entity_extraction_ms", "metadata_filter_ms", "business_relevance_ms"):
        assert key in result["timings_ms"]
        assert result["timings_ms"][key] >= 0.0


def test_tracer_metadata_ignored_without_extractor_wired():
    """metadata is supplied but no entity_extractor is configured -> no-op,
    identical to the pre-Phase-C6 behaviour."""
    dense = FakeDense({"issue": ["c1"]})
    tracer = _build_tracer(dense)  # no entity_extractor
    result = tracer.trace(query="issue", top_k=1, metadata={"service": "database"})
    assert result["extracted_entities"] == []
    assert result["metadata_filter_applied"] is False


def test_summarize_chunk_never_fabricates_c6_fields():
    item = {"chunk_id": "x", "text": "t", "metadata": {}}
    summary = summarize_chunk(item)
    assert summary["business_relevance_score"] is None
    assert summary["ranking_reasons"] is None
    assert summary["retrieval_confidence"] is None
    assert summary["metadata_filter_relaxed"] is None
