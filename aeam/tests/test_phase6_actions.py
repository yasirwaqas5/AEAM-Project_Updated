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
from aeam.agents.action.errors import (
    ActionConfigurationError,
    ActionValidationError,
)
from aeam.agents.action.slack_actions import SlackActions
from aeam.agents.action.email_actions import EmailActions
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


# -------------------------------------------------------------------
# Execution metadata (status / duration / retry count / reason / validation)
# -------------------------------------------------------------------

def test_result_contains_execution_metadata_on_success():
    agent, db = build_agent()
    agent._registry["jira"] = DummyHandler()

    result = agent.execute("jira", {"summary": "x"}, "INC-META-1")

    assert result["status"] == "SUCCESS"
    assert result["retry_count"] == 0
    assert result["validation_result"] == "PASSED"
    assert result["failure_reason"] is None
    assert isinstance(result["execution_duration_ms"], int)

    # Metadata is also embedded in the persisted action_logs result JSON.
    _, record = db.rows[-1]
    import json
    logged = json.loads(record["result"])
    assert logged["retry_count"] == 0
    assert logged["validation_result"] == "PASSED"
    assert "execution_duration_ms" in logged


def test_transient_failure_retries_and_reports_metadata():
    agent, _ = build_agent()
    agent._registry["jira"] = FailingHandler()

    result = agent.execute("jira", {"a": 1}, "INC-META-2")

    assert result["status"] == "FAILED"
    assert result["retry_count"] == 1          # 2 attempts -> 1 retry
    assert result["validation_result"] == "N/A"
    assert result["failure_reason"] is not None


# -------------------------------------------------------------------
# Non-retryable errors are NOT retried
# -------------------------------------------------------------------

class ConfigErrorHandler:
    def __init__(self):
        self.calls = 0

    def execute(self, params):
        self.calls += 1
        raise ActionConfigurationError(
            reason="Missing Google Cloud credentials",
            details={"missing": ["gmail_private_key"]},
        )


class ValidationErrorHandler:
    def __init__(self):
        self.calls = 0

    def execute(self, params):
        self.calls += 1
        raise ActionValidationError(reason="invalid_blocks", details=["bad block"])


def test_configuration_error_is_not_retried():
    agent, _ = build_agent()
    handler = ConfigErrorHandler()
    agent._registry["email"] = handler

    result = agent.execute("email", {"to": ["x@y.z"]}, "INC-CFG-1")

    assert result["status"] == "FAILED"
    assert handler.calls == 1                    # NOT retried
    assert result["retry_count"] == 0
    assert result["validation_result"] == "SKIPPED"
    assert result["failure_reason"] == "Missing Google Cloud credentials"
    assert result["result"]["details"]["missing"] == ["gmail_private_key"]


def test_validation_error_is_not_retried():
    agent, _ = build_agent()
    handler = ValidationErrorHandler()
    agent._registry["slack"] = handler

    result = agent.execute("slack", {"message": "x"}, "INC-VAL-1")

    assert result["status"] == "FAILED"
    assert handler.calls == 1                    # NOT retried
    assert result["retry_count"] == 0
    assert result["validation_result"] == "FAILED"
    assert result["failure_reason"] == "invalid_blocks"


# -------------------------------------------------------------------
# EmailActions credential diagnostics
# -------------------------------------------------------------------

class MissingCredsSecretManager:
    """Returns None for every secret (credentials not configured)."""
    def get(self, key, default=None):
        return default


def test_email_missing_credentials_returns_structured_error():
    email = EmailActions(secret_manager=MissingCredsSecretManager())

    with pytest.raises(ActionConfigurationError) as excinfo:
        email.send_email({"to": ["ops@example.com"], "subject": "s", "body": "b"})

    err = excinfo.value
    assert err.reason == "Missing Google Cloud credentials"
    # Reported in the documented order, all three missing.
    assert err.details["missing"] == [
        "gmail_private_key",
        "gmail_client_email",
        "gmail_sender_address",
    ]


# -------------------------------------------------------------------
# Slack payload build + validation
# -------------------------------------------------------------------

def _slack() -> SlackActions:
    return SlackActions(secret_manager=DummySecretManager())


def test_slack_payload_renders_color_wrapped_blocks():
    payload = SlackActions._build_payload(
        channel="#ops",
        title="CPU Anomaly",
        message="CPU at 97% on web-01.",
        severity="CRITICAL",
        colour="#B22222",
        incident_id="INC-42",
    )
    # Colour sidebar must wrap the content: blocks live inside the attachment,
    # not at the top level detached from the colour.
    assert "blocks" not in payload
    assert payload["attachments"][0]["color"] == "#B22222"
    assert payload["attachments"][0]["blocks"][0]["type"] == "header"
    assert payload["text"]  # top-level fallback for notifications

    # A well-formed payload passes validation.
    assert SlackActions._validate_payload(payload) == []


def test_slack_payload_truncates_oversized_fields():
    payload = SlackActions._build_payload(
        channel="#ops",
        title="T" * 500,
        message="M" * 5000,
        severity="HIGH",
        colour="#FF8C00",
        incident_id=None,
    )
    # Even with absurd input the built payload is valid (fields truncated).
    assert SlackActions._validate_payload(payload) == []


def test_slack_validation_detects_invalid_blocks():
    bad_payload = {
        "channel": "#ops",
        "attachments": [
            {
                "color": "#B22222",
                "blocks": [
                    {"type": "header", "text": {"type": "plain_text", "text": "X" * 200}},
                    {"type": "section", "text": {"type": "mrkdwn", "text": ""}},
                ],
            }
        ],
    }
    errors = SlackActions._validate_payload(bad_payload)
    assert any("header text exceeds" in e for e in errors)
    assert any("section text must be non-empty" in e for e in errors)


def test_slack_send_alert_raises_validation_error_on_blank_message():
    slack = _slack()
    # Blank message is rejected before any network call.
    with pytest.raises(ValueError):
        slack.send_alert({"channel": "#ops", "message": "   "})