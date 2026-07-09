"""
aeam/agents/action/errors.py

Typed, non-retryable error signals for the AEAM Action layer.

These exceptions let integration handlers distinguish *permanent* failures
(malformed payloads, missing configuration) from *transient* ones (network
blips, 5xx responses). ``ActionAgent`` treats them as non-retryable: it records
a single structured failure instead of burning retry attempts on an error that
cannot succeed on retry.

This module intentionally contains no framework and no external dependencies —
just two small exception types plus a shared base.
"""

from __future__ import annotations

from typing import Any


class NonRetryableActionError(Exception):
    """
    Base class for action failures that must NOT be retried.

    Carries a human-readable ``reason`` and optional structured ``details``
    so the failure can be surfaced verbatim in logs, API responses, and the
    dashboard without further parsing.

    Args:
        reason:  Short, human-readable failure summary.
        details: Optional structured detail (e.g. a list of validation errors
                 or a dict of missing configuration keys).
    """

    def __init__(self, reason: str, details: Any = None) -> None:
        super().__init__(reason)
        self.reason: str = reason
        self.details: Any = details

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable structured representation of the error."""
        payload: dict[str, Any] = {"error": self.reason, "reason": self.reason}
        if self.details is not None:
            payload["details"] = self.details
        return payload


class ActionValidationError(NonRetryableActionError):
    """
    Raised when an action payload fails pre-send validation.

    Used by SlackActions when a Block Kit payload violates Slack's structural
    rules (the ``invalid_blocks`` family of failures) so the ActionAgent can
    return a structured reason instead of firing an invalid request at Slack.
    """


class ActionConfigurationError(NonRetryableActionError):
    """
    Raised when required configuration/credentials are missing.

    Used by EmailActions when Google Cloud credentials are not configured.
    Retrying cannot resolve a missing-configuration error, so the ActionAgent
    records a single structured failure.
    """
