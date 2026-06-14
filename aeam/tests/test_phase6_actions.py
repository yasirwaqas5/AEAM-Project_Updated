"""
Phase 6 tests — Action Layer validation.

These tests verify:
- action registry
- idempotency protection
- retry logic
- database logging
- correct return structures

External APIs are mocked — no real network calls occur.
"""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock

from aeam.agents.action.action_agent import ActionAgent
from aeam.core.idempotency import IdempotencyManager


# -------------------------------------------------------------------
# Dummy infrastructure
# -------------------------------------------------------------------

class DummyRedis:
    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def setex(self, key, ttl, value):
        self.store[key] = value

    def flushdb(self):
        self.store.clear()


class DummyDB:
    def __init__(self):
        self.rows = []

    def insert(self, table: str, data: dict):
        self.rows.append((table, data))


class DummySecretManager:
    def get(self, name: str):
        # WebhookActions expects JSON for webhook_registry
        if name == "webhook_registry":
            return "{}"
        return "dummy-secret"


# -------------------------------------------------------------------
# Dummy Action Handler
# -------------------------------------------------------------------

class DummyHandler:
    def __init__(self):
        self.calls = 0

    def execute(self, params: dict):
        self.calls += 1
        return {"ok": True, "params": params}


class FailingHandler:
    def __init__(self):
        self.calls = 0

    def execute(self, params: dict):
        self.calls += 1
        raise RuntimeError("failure")


# -------------------------------------------------------------------
# Test Helpers
# -------------------------------------------------------------------

def build_agent():
    redis = DummyRedis()
    redis.flushdb()  # ensure fresh state for each test
    db = DummyDB()

    redis_client = MagicMock()
    redis_client.get.side_effect = redis.get
    redis_client.setex.side_effect = redis.setex
    redis_client.flushdb = MagicMock(side_effect=redis.flushdb)

    db_client = MagicMock()
    db_client.insert.side_effect = db.insert

    secret_manager = DummySecretManager()

    idempotency = IdempotencyManager(redis_client)

    # Create a mock settings object with Jira configuration
    settings = MagicMock()
    settings.JIRA_URL = "https://example.atlassian.net"
    settings.JIRA_API_TOKEN = "dummy-token"
    settings.JIRA_USER_EMAIL = "test@example.com"
    settings.JIRA_PROJECT_KEY = "TEST"
    settings.JIRA_ISSUE_TYPE = "10001"

    agent = ActionAgent(
        secret_manager=secret_manager,
        redis_client=redis_client,
        database_client=db_client,
        idempotency_manager=idempotency,
        settings=settings,          # <-- pass settings so Jira is registered
    )

    return agent, db


# -------------------------------------------------------------------
# Tests
# -------------------------------------------------------------------

def test_action_registry_exists():
    agent, _ = build_agent()

    assert "jira" in agent.registered_actions
    assert "slack" in agent.registered_actions
    assert "email" in agent.registered_actions
    assert "webhook" in agent.registered_actions
    assert "sheets" in agent.registered_actions


def test_successful_execution():
    agent, db = build_agent()

    handler = DummyHandler()
    agent._registry["jira"] = handler

    result = agent.execute(
        action_type="jira",
        parameters={"summary": "test"},
        incident_id="INC-1",
    )

    assert result["status"] == "SUCCESS"
    assert handler.calls == 1
    assert len(db.rows) == 1


def test_idempotency_blocks_duplicates():
    agent, _ = build_agent()

    handler = DummyHandler()
    agent._registry["jira"] = handler

    first = agent.execute("jira", {"a": 1}, "INC-1")
    second = agent.execute("jira", {"a": 1}, "INC-1")

    assert first["status"] == "SUCCESS"
    assert second["status"] == "ALREADY_EXECUTED"
    assert handler.calls == 1


def test_retry_logic():
    agent, _ = build_agent()

    handler = FailingHandler()
    agent._registry["jira"] = handler

    result = agent.execute("jira", {"a": 1}, "INC-2")

    assert result["status"] == "FAILED"
    assert handler.calls == 2


def test_invalid_action_type():
    agent, _ = build_agent()

    with pytest.raises(ValueError):
        agent.execute("invalid", {}, "INC-1")