"""
aeam/agents/action/slack_actions.py

Slack integration for the AEAM Action layer.

Sends formatted incident alerts to Slack channels via the Slack Web API
``chat.postMessage`` endpoint. Messages use Block Kit with severity-coded
colour sidebars. Called exclusively through the ActionAgent registry.

Phase 6 constraints:
- No retry logic (handled by ActionAgent).
- No LLM usage.
- No decision or Orchestrator logic.
- requests library only.
- HTTP timeout: 10 seconds.
- Raises on non-200 or Slack API error responses.
- Fully typed, logging throughout.
"""

from __future__ import annotations

import logging
from aeam.monitoring.logging_config import get_logger
from typing import Any

import requests

logger = get_logger(__name__, agent="action")

# Enforced HTTP timeout (Phase 6 spec).
_HTTP_TIMEOUT: int = 10

# Slack Web API endpoint.
_SLACK_POST_MESSAGE_URL: str = "https://slack.com/api/chat.postMessage"

# Severity → hex colour for the Block Kit attachment sidebar.
_SEVERITY_COLOURS: dict[str, str] = {
    "CRITICAL": "#B22222",   # firebrick red
    "HIGH":     "#FF8C00",   # dark orange
    "MEDIUM":   "#FFD700",   # gold
    "LOW":      "#36A64F",   # green
}

# Fallback colour when severity is unrecognised.
_DEFAULT_COLOUR: str = "#808080"  # grey


class SlackActions:
    """
    Slack alert integration for the AEAM Action layer.

    Retrieves the bot token from the injected ``secret_manager``, builds a
    Block Kit message with a severity-coloured attachment, and POSTs to the
    Slack ``chat.postMessage`` API. Raises on HTTP errors or on a Slack API
    ``"ok": false`` response.

    This class:
    - Contains no retry logic (ActionAgent handles retries).
    - Makes no LLM calls.
    - Contains no decision or Orchestrator logic.

    Secrets expected from ``secret_manager``:
    - ``"slack_bot_token"`` — Slack Bot OAuth token (``xoxb-...``).

    Args:
        secret_manager: Secrets provider with a ``get(key: str) -> str`` interface.

    Raises:
        ValueError: If ``secret_manager`` is None.

    Example::

        slack = SlackActions(secret_manager=secret_manager)
        result = slack.execute({
            "channel":  "#ops-alerts",
            "message":  "CPU spike detected on web-01.",
            "severity": "HIGH",
        })
        # {"ts": "1712345678.000100", "channel": "C01234ABCD"}
    """

    def __init__(self, secret_manager: Any) -> None:
        """
        Initialise SlackActions with an injected secrets provider.

        Args:
            secret_manager: Secrets provider. Must not be None.

        Raises:
            ValueError: If ``secret_manager`` is None.
        """
        if secret_manager is None:
            raise ValueError("secret_manager must not be None.")
        self._secrets: Any = secret_manager

    # ------------------------------------------------------------------
    # ActionAgent registry interface
    # ------------------------------------------------------------------

    def execute(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        ActionAgent registry entry point — delegates to :meth:`send_alert`.

        Args:
            params: Action parameters dict. See :meth:`send_alert`.

        Returns:
            Result dict from :meth:`send_alert`.
        """
        return self.send_alert(params)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def send_alert(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        Send a formatted incident alert to a Slack channel.

        Steps:
        1. Retrieve ``slack_bot_token`` from ``SecretManager``.
        2. Validate required parameters (``channel``, ``message``).
        3. Build a Block Kit message payload with a severity-coloured
           attachment sidebar.
        4. POST to ``https://slack.com/api/chat.postMessage``.
        5. Raise on HTTP error or Slack API ``"ok": false`` response.
        6. Return the message timestamp and channel ID.

        Severity colour map:
        - ``CRITICAL`` → firebrick red (``#B22222``)
        - ``HIGH``     → dark orange   (``#FF8C00``)
        - ``MEDIUM``   → gold          (``#FFD700``)
        - ``LOW``      → green         (``#36A64F``)
        - unknown      → grey          (``#808080``)

        Args:
            params: Dict containing:

                - ``"channel"``    *(required)* — Slack channel name or ID
                  (e.g. ``"#ops-alerts"`` or ``"C01234ABCD"``).
                - ``"message"``    *(required)* — main alert body text.
                - ``"severity"``   *(optional)* — one of ``"CRITICAL"``,
                  ``"HIGH"``, ``"MEDIUM"``, ``"LOW"``. Defaults to ``"MEDIUM"``.
                - ``"title"``      *(optional)* — bold header above the message.
                  Defaults to ``"AEAM Incident Alert"``.
                - ``"incident_id"`` *(optional)* — appended to the footer if
                  provided.

        Returns:
            Dict::

                {
                    "ts":      str,  # Slack message timestamp (unique ID)
                    "channel": str,  # Slack channel ID where message was posted
                }

        Raises:
            ValueError:               If ``channel`` or ``message`` is missing
                                      or blank.
            requests.HTTPError:       If the HTTP request fails (non-200).
            requests.Timeout:         If the request exceeds 10 seconds.
            requests.ConnectionError: If the Slack API is unreachable.
            RuntimeError:             If Slack returns ``"ok": false`` with an
                                      error code.

        Example::

            result = slack.send_alert({
                "channel":     "#ops-alerts",
                "message":     "CPU spike on web-01 — 97% utilisation at 14:32 UTC.",
                "severity":    "CRITICAL",
                "title":       "CPU Anomaly Detected",
                "incident_id": "INC-42",
            })
        """
        # Step 1: retrieve token.
        bot_token: str = self._secrets.get("slack_bot_token")

        # Step 2: validate required parameters.
        channel: str = params.get("channel", "").strip()
        if not channel:
            raise ValueError("params['channel'] must be a non-empty string.")

        message: str = params.get("message", "").strip()
        if not message:
            raise ValueError("params['message'] must be a non-empty string.")

        severity: str = params.get("severity", "MEDIUM").upper()
        title: str = params.get("title", "AEAM Incident Alert")
        incident_id: str | None = params.get("incident_id")

        # Step 3: build Block Kit payload.
        colour: str = _SEVERITY_COLOURS.get(severity, _DEFAULT_COLOUR)
        payload = self._build_payload(
            channel=channel,
            title=title,
            message=message,
            severity=severity,
            colour=colour,
            incident_id=incident_id,
        )

        logger.info(
            "SlackActions.send_alert | POST chat.postMessage | channel=%s | "
            "severity=%s | title=%r",
            channel, severity, title,
        )

        # Step 4: POST to Slack.
        response = requests.post(
            url=_SLACK_POST_MESSAGE_URL,
            json=payload,
            headers={
                "Authorization":  f"Bearer {bot_token}",
                "Content-Type":   "application/json; charset=utf-8",
            },
            timeout=_HTTP_TIMEOUT,
        )

        # Step 5: raise on HTTP error.
        if response.status_code != 200:
            logger.error(
                "SlackActions.send_alert | HTTP FAILED | status=%d | body=%s",
                response.status_code, response.text[:500],
            )
            response.raise_for_status()

        body: dict[str, Any] = response.json()

        # Slack always returns 200 — check the payload-level "ok" flag.
        if not body.get("ok"):
            error_code: str = body.get("error", "unknown_error")
            logger.error(
                "SlackActions.send_alert | Slack API error | channel=%s | "
                "error=%s",
                channel, error_code,
            )
            raise RuntimeError(
                f"Slack API returned ok=false: {error_code!r}. "
                f"Channel: {channel!r}."
            )

        # Step 6: return message timestamp and channel.
        ts: str = body.get("ts", "")
        response_channel: str = body.get("channel", channel)

        logger.info(
            "SlackActions.send_alert | SUCCESS | channel=%s | ts=%s",
            response_channel, ts,
        )

        return {
            "ts":      ts,
            "channel": response_channel,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_payload(
        channel: str,
        title: str,
        message: str,
        severity: str,
        colour: str,
        incident_id: str | None,
    ) -> dict[str, Any]:
        """
        Build a Slack ``chat.postMessage`` Block Kit payload.

        Produces a message with:
        - A ``header`` block containing the alert title.
        - A ``section`` block containing the message text.
        - A ``context`` block showing the severity badge and optional incident ID.
        - An ``attachments`` entry to apply the severity colour sidebar.

        Args:
            channel:     Target Slack channel name or ID.
            title:       Bold header text.
            message:     Main alert body.
            severity:    Severity label (e.g. ``"CRITICAL"``).
            colour:      Hex colour string for the attachment sidebar.
            incident_id: Optional incident identifier for the footer.

        Returns:
            Fully formed ``chat.postMessage`` request payload dict.
        """
        severity_emoji: dict[str, str] = {
            "CRITICAL": "🔴",
            "HIGH":     "🟠",
            "MEDIUM":   "🟡",
            "LOW":      "🟢",
        }
        emoji = severity_emoji.get(severity, "⚪")

        # Context footer elements.
        context_elements: list[dict[str, str]] = [
            {
                "type": "mrkdwn",
                "text": f"*Severity:* {emoji} {severity}",
            }
        ]
        if incident_id:
            context_elements.append({
                "type": "mrkdwn",
                "text": f"*Incident:* {incident_id}",
            })

        blocks: list[dict[str, Any]] = [
            {
                "type": "header",
                "text": {
                    "type":  "plain_text",
                    "text":  title,
                    "emoji": True,
                },
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": message,
                },
            },
            {"type": "divider"},
            {
                "type":     "context",
                "elements": context_elements,
            },
        ]

        return {
            "channel":     channel,
            "blocks":      blocks,
            "attachments": [
                {
                    "color":    colour,
                    "fallback": f"[{severity}] {title}: {message}",
                }
            ],
        }

    def __repr__(self) -> str:
        return "SlackActions()"