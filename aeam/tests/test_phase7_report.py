"""
Phase 7 tests — Report Agent validation.

These tests verify:
- report generation (fallback + LLM)
- template loading
- safe handling of missing fields
- alert formatting
- no crashes / consistent output

No external APIs are used.
"""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock

from aeam.agents.report.report_agent import ReportAgent
from aeam.config.settings import Settings


# -------------------------------------------------------------------
# Dummy Memory
# -------------------------------------------------------------------

class DummyMemory:
    def __init__(self, data: dict):
        self._data = data

    def get(self, key: str, default=None):
        return self._data.get(key, default)


# -------------------------------------------------------------------
# Dummy LLM
# -------------------------------------------------------------------

class DummyLLM:
    def query(self, prompt: str, **kwargs):
        return (
            "EXECUTIVE_SUMMARY:\n"
            "System detected a critical issue.\n\n"
            "DETAILED_REPORT:\n"
            "Detailed analysis of the issue."
        )


class FailingLLM:
    def query(self, *args, **kwargs):
        raise RuntimeError("LLM failure")


# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------

def build_agent(llm=None):
    settings = Settings(
        DATABASE_URL="sqlite:///test.db",
        REDIS_URL="redis://localhost:6379/0",
        VECTOR_DB_URL="http://localhost:6333",
        ENVIRONMENT="development",  # ✅ Changed from "test" to a valid environment
    )

    return ReportAgent(settings=settings, llm=llm)


# -------------------------------------------------------------------
# Tests
# -------------------------------------------------------------------

def test_generate_report_fallback():
    agent = build_agent(llm=None)

    memory = DummyMemory({
        "event_type": "sales_drop",
        "severity": "HIGH",
        "metric": "sales",
        "findings": "Sales dropped by 30%",
        "root_cause": "Payment failure",
        "evidence": "Logs show gateway errors",
        "actions_taken": "Restarted service",
        "confidence": 0.9,
    })

    result = agent.generate_report(memory)

    assert "executive_summary" in result
    assert "detailed_report" in result
    assert result["confidence"] > 0


def test_generate_report_with_llm():
    agent = build_agent(llm=DummyLLM())

    memory = DummyMemory({
        "event_type": "traffic_spike",
        "severity": "CRITICAL",
    })

    result = agent.generate_report(memory)

    assert "System detected" in result["executive_summary"]
    assert "Detailed analysis" in result["detailed_report"]


def test_llm_failure_fallback():
    agent = build_agent(llm=FailingLLM())

    memory = DummyMemory({
        "event_type": "error_rate",
        "severity": "HIGH",
    })

    result = agent.generate_report(memory)

    # fallback must still work
    assert "executive_summary" in result
    assert result["confidence"] >= 0


def test_missing_fields_handled():
    agent = build_agent()

    memory = DummyMemory({})  # empty memory

    result = agent.generate_report(memory)

    assert result["executive_summary"] != ""
    assert result["detailed_report"] != ""


def test_generate_alert_format():
    agent = build_agent()

    memory = DummyMemory({
        "event_type": "latency",
        "severity": "medium",
        "findings": "Latency increased",
        "actions_taken": "Scaled service",
    })

    alert = agent.generate_alert(memory)

    assert "🚨 AEAM ALERT" in alert["message"]
    assert "latency" in alert["message"].lower()
    assert alert["severity"] == "MEDIUM"


def test_alert_missing_fields():
    agent = build_agent()

    memory = DummyMemory({})

    alert = agent.generate_alert(memory)

    assert alert["message"] != ""
    assert alert["severity"] == "UNKNOWN"


def test_output_structure_consistency():
    agent = build_agent()

    memory = DummyMemory({
        "event_type": "cpu_spike",
    })

    report = agent.generate_report(memory)
    alert = agent.generate_alert(memory)

    assert isinstance(report, dict)
    assert isinstance(alert, dict)

    assert set(report.keys()) == {"executive_summary", "detailed_report", "confidence"}
    assert set(alert.keys()) == {"message", "severity", "event_type"}