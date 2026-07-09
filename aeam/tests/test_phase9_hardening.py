"""
Phase 9 tests — Investigation loop hardening.

Validates:
- Root-cause quality gate (cause_quality.py)
- Canonical investigation status derivation (investigation_status.py)
- Safe-action runbooks (runbooks.py)
- Structured Slack/Jira formatters (notifications.py) never dump raw JSON
- RAGAgent adaptive query rewriting: distinct queries per attempt, and
  exhaustion after 3 attempts with 0 chunks (no repeated identical search)
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from aeam.agents.rag.cause_quality import best_meaningful_cause, is_meaningful_root_cause
from aeam.agents.orchestrator.investigation_status import (
    COMPLETE, ESCALATED, FAILED, INVESTIGATING, RESOLVED,
    derive_investigation_status,
)
from aeam.agents.orchestrator.runbooks import get_runbook, resolve_action_step
from aeam.agents.orchestrator.notifications import format_jira_description, format_slack_message
from aeam.agents.rag.rag_agent import RAGAgent
from aeam.agents.rag.response_validator import RAGResponseValidator
from aeam.core.event_models import Event
from aeam.memory.short_term import ShortTermMemory


# -------------------------------------------------------------------
# cause_quality
# -------------------------------------------------------------------

def test_vague_single_word_causes_rejected():
    for bad in ["queries", "issues", "errors", "gateway", "loss", "Loss.", "  latency  "]:
        assert is_meaningful_root_cause(bad) is False


def test_descriptive_multi_word_causes_accepted():
    for good in [
        "Inefficient queries",
        "Replication lag on read replica",
        "Missing indexes",
        "Redis memory eviction",
    ]:
        assert is_meaningful_root_cause(good) is True


def test_empty_and_none_rejected():
    assert is_meaningful_root_cause(None) is False
    assert is_meaningful_root_cause("") is False
    assert is_meaningful_root_cause("   ") is False


def test_best_meaningful_cause_skips_vague_leader():
    causes = [
        {"cause": "queries", "confidence": 0.9},
        {"cause": "insufficient IOPS", "confidence": 0.85},
        {"cause": "replication lag on primary", "confidence": 0.8},
    ]
    chosen = best_meaningful_cause(causes)
    assert chosen["cause"] == "insufficient IOPS"


def test_best_meaningful_cause_all_vague_returns_none():
    causes = [{"cause": "queries", "confidence": 0.9}, {"cause": "loss", "confidence": 0.5}]
    assert best_meaningful_cause(causes) is None


# -------------------------------------------------------------------
# investigation_status
# -------------------------------------------------------------------

def test_status_investigating_when_not_finalized():
    assert derive_investigation_status(
        root_cause=None, requires_human=False, is_finalized=False,
    ) == INVESTIGATING


def test_status_resolved_when_root_cause_present():
    assert derive_investigation_status(
        root_cause="Inefficient queries", requires_human=False,
    ) == RESOLVED


def test_status_escalated_wins_over_root_cause():
    assert derive_investigation_status(
        root_cause="Inefficient queries", requires_human=True,
    ) == ESCALATED


def test_status_failed_when_error_and_no_root_cause():
    assert derive_investigation_status(
        root_cause=None, requires_human=False, had_error=True,
    ) == FAILED


def test_status_complete_fallback():
    assert derive_investigation_status(
        root_cause=None, requires_human=False, had_error=False,
    ) == COMPLETE


# -------------------------------------------------------------------
# runbooks
# -------------------------------------------------------------------

def test_db_latency_runbook_matches_spec():
    rb = get_runbook("DB_LATENCY")
    assert "Optimize indexes" in rb["recommended_actions"]
    assert set(rb["action_plan"]) >= {"jira", "slack", "diagnostics", "monitoring"}


def test_sales_drop_runbook_uses_marketing_notify():
    rb = get_runbook("SALES_DROP")
    assert "marketing_slack" in rb["action_plan"]
    assert "jira" in rb["action_plan"]


def test_unknown_event_type_falls_back_to_default():
    rb = get_runbook("SOME_TOTALLY_UNKNOWN_TYPE")
    assert rb["action_plan"]  # non-empty, safe default
    assert all(step in {"jira", "slack", "diagnostics", "monitoring", "marketing_slack"}
               for step in rb["action_plan"])


def test_resolve_action_step_alias():
    registry_type, extra = resolve_action_step("marketing_slack")
    assert registry_type == "slack"
    assert "channel" in extra


def test_resolve_action_step_direct():
    assert resolve_action_step("jira") == ("jira", {})


# -------------------------------------------------------------------
# notifications — must never embed raw JSON
# -------------------------------------------------------------------

def test_slack_message_has_no_raw_json():
    msg = format_slack_message({
        "incident_id": "INC-1", "metric": "latency_ms", "severity": "HIGH",
        "investigation_status": "RESOLVED",
        "root_cause": "Inefficient queries and replication lag",
        "confidence": 0.83, "evidence_count": 5,
        "recommended_actions": ["Optimize indexes"],
        "executed_actions": ["jira", "slack"],
        "requires_human": False,
    })
    assert "{" not in msg and "}" not in msg
    assert "Incident ID: INC-1" in msg
    assert "Root Cause: Inefficient queries and replication lag" in msg
    assert "Confidence: 83%" in msg
    assert "Evidence: 5 chunks" in msg
    assert "Human Review: Not Required" in msg


def test_jira_description_structured_no_findings_dump():
    desc = format_jira_description({
        "incident_id": "INC-1", "metric": "latency_ms", "severity": "HIGH",
        "current_value": 1500.0, "expected_value": 300.0,
        "retrieval_completed": True, "evidence_count": 4,
        "validation_status": "PASSED",
        "root_cause": "Inefficient queries",
        "top_confidence": 0.83, "chunk_ids": ["abc123", "def456"],
        "recommended_actions": ["Optimize indexes"],
        "executed_actions": ["diagnostics", "monitoring"],
        "llm_reasoning": "Some reasoning text.",
    })
    assert "h3. Investigation Summary" in desc
    assert "Retrieved 4 chunks" in desc
    assert "Validation passed" in desc
    assert "{expand}" in desc  # collapsible, not inlined at top level
    # Must not contain a literal possible_causes JSON dump.
    assert '"possible_causes"' not in desc


# -------------------------------------------------------------------
# RAGAgent adaptive query rewriting / exhaustion
# -------------------------------------------------------------------

def _make_event(event_type="DB_LATENCY", metric="latency_ms", metadata=None):
    return Event(
        event_id="e1", event_type=event_type, metric=metric,
        severity="HIGH", current_value=100.0, expected_value=50.0,
        detection_methods=["zscore"], metadata=metadata or {},
        timestamp=datetime.now(timezone.utc),
    )


def _make_stm():
    stm = ShortTermMemory()
    stm.initialize(task_type="test", incident_id="INC-TEST")
    stm.set("investigation_depth", 1)
    stm.set("findings", [])
    return stm


def test_query_variants_are_distinct_across_attempts():
    # Realistic event with metadata, so attempt 1 (original, includes
    # metadata) and attempt 2 (rewritten, metadata dropped) actually differ.
    # With empty metadata they would legitimately be identical — that's not
    # a bug, there's simply nothing for attempt 2 to drop.
    event = _make_event(metadata={"service": "payment-service", "host": "db-01"})
    q1, s1 = RAGAgent._formulate_query_variant(event, 1)
    q2, s2 = RAGAgent._formulate_query_variant(event, 2)
    q3, s3 = RAGAgent._formulate_query_variant(event, 3)
    assert (s1, s2, s3) == ("original", "rewritten", "broadened")
    assert q1 != q2 != q3
    assert len(q3.split()) <= len(q2.split()) <= len(q1.split())


def test_rag_agent_no_chunks_first_pass_reports_attempt_1():
    retrieval = MagicMock()
    retrieval.search.return_value = []
    retrieval.similarity_threshold = 0.5

    agent = RAGAgent(
        retrieval_pipeline=retrieval,
        validator=RAGResponseValidator(),
        llm_service=MagicMock(),
    )
    stm = _make_stm()
    result = agent.investigate(event=_make_event(), memory=stm)

    findings = result["findings"]
    assert findings["retrieved_count"] == 0
    assert findings["query_attempt"] == 1
    assert findings["query_strategy"] == "original"
    assert findings["validation_passed"] is False


def test_rag_agent_exhausts_after_three_zero_attempts():
    retrieval = MagicMock()
    retrieval.search.return_value = []
    retrieval.similarity_threshold = 0.5

    agent = RAGAgent(
        retrieval_pipeline=retrieval,
        validator=RAGResponseValidator(),
        llm_service=MagicMock(),
    )
    stm = _make_stm()
    event = _make_event()

    seen_strategies = []
    for _ in range(3):
        result = agent.investigate(event=event, memory=stm)
        findings = result["findings"]
        seen_strategies.append(findings["query_strategy"])
        # Simulate the Orchestrator recording this pass into STM findings.
        stm.append("findings", {"type": "rag", "data": findings})

    assert seen_strategies == ["original", "rewritten", "broadened"]
    assert retrieval.search.call_count == 3

    # 4th call must NOT trigger a new search — exhausted.
    result4 = agent.investigate(event=event, memory=stm)
    assert result4["findings"]["query_strategy"] == "exhausted"
    assert retrieval.search.call_count == 3  # unchanged — no new search fired
    assert result4["findings"]["requires_human_review"] is True


def test_rag_agent_success_includes_retrieved_chunks_metadata():
    retrieval = MagicMock()
    retrieval.search.return_value = [
        {"chunk_id": "c1", "text": "Inefficient queries and replication lag.",
         "similarity": 0.71, "metadata": {"source": "runbook.md"}},
    ]
    retrieval.similarity_threshold = 0.5

    llm = MagicMock()
    llm.query.return_value = json.dumps({
        "possible_causes": [
            {"cause": "Inefficient queries and replication lag", "chunk_id": "c1", "confidence": 0.8},
        ],
        "overall_confidence": 0.8,
        "requires_human_review": False,
    })

    agent = RAGAgent(retrieval_pipeline=retrieval, validator=RAGResponseValidator(), llm_service=llm)
    stm = _make_stm()
    result = agent.investigate(event=_make_event(), memory=stm)

    findings = result["findings"]
    assert findings["validation_passed"] is True
    chunks_meta = findings["retrieved_chunks"]
    assert chunks_meta[0]["chunk_id"] == "c1"
    assert chunks_meta[0]["similarity"] == 0.71
    assert chunks_meta[0]["source"] == "runbook.md"
    assert chunks_meta[0]["cited"] is True
