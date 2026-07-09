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

from aeam.agents.action.errors import ActionValidationError

logger = get_logger(__name__, agent="action")

# Enforced HTTP timeout (Phase 6 spec).
_HTTP_TIMEOUT: int = 10

# Slack Web API endpoint.
_SLACK_POST_MESSAGE_URL: str = "https://slack.com/api/chat.postMessage"

# Slack Block Kit hard limits (exceeding these yields an `invalid_blocks` error).
_HEADER_TEXT_MAX: int = 150      # header plain_text limit
_SECTION_TEXT_MAX: int = 3000    # section text limit
_MAX_BLOCKS: int = 50            # per-message / per-attachment block limit

# Slack payload-level error codes that are permanent (retrying cannot help).
_PERMANENT_SLACK_ERRORS: frozenset[str] = frozenset({
    "invalid_blocks",
    "invalid_blocks_format",
    "invalid_arguments",
    "messages_tab_disabled",
})

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

    Retrieves the bot token and default channel either from the injected
    ``settings`` object (preferred) or from a ``secret_manager``.
    Builds a Block Kit message with a severity-coloured attachment, and POSTs
    to the Slack ``chat.postMessage`` API. Raises on HTTP errors or on a
    Slack API ``"ok": false`` response.

    This class:
    - Contains no retry logic (ActionAgent handles retries).
    - Makes no LLM calls.
    - Contains no decision or Orchestrator logic.

    Args:
        settings:       Optional application settings object (provides
                        ``SLACK_BOT_TOKEN`` and ``SLACK_CHANNEL``).
        secret_manager: Optional secrets provider with a ``get(key)`` method.
                        Used as fallback if ``settings`` is not provided.

    Raises:
        ValueError: If neither ``settings`` nor ``secret_manager`` is provided,
                    or if the token cannot be obtained.

    Example::

        slack = SlackActions(settings=settings)
        result = slack.execute({
            "channel":  "#ops-alerts",
            "message":  "CPU spike detected on web-01.",
            "severity": "HIGH",
        })
        # {"ts": "1712345678.000100", "channel": "C01234ABCD"}
    """

    def __init__(self, settings: Any = None, secret_manager: Any = None) -> None:
        """
        Initialise SlackActions with either a settings object or a secret manager.

        Args:
            settings:       Application settings (preferred).
            secret_manager: Secrets provider (fallback).

        Raises:
            ValueError: If neither is provided or token cannot be retrieved.
        """
        if settings is not None:
            self.token = getattr(settings, "SLACK_BOT_TOKEN", "")
            self.channel = getattr(settings, "SLACK_CHANNEL", "#aeam-alerts")
            if not self.token:
                raise ValueError("settings.SLACK_BOT_TOKEN must be non-empty.")
        elif secret_manager is not None:
            self.token = secret_manager.get("slack_bot_token")
            self.channel = "#aeam-alerts"
            if not self.token:
                raise ValueError("secret_manager returned empty slack_bot_token.")
        else:
            raise ValueError("Either settings or secret_manager must be provided.")

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
        1. Use the stored token from initialisation.
        2. Validate required parameters (``channel``, ``message``).
           If ``channel`` is not provided, fall back to ``self.channel``.
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

                - ``"channel"``    *(optional)* — Slack channel name or ID.
                  Defaults to ``self.channel``.
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
            ValueError:               If ``message`` is missing or blank.
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
        # Step 1: token is already stored in self.token.
        bot_token: str = self.token

        # Step 2: validate required parameters (channel uses default).
        channel: str = params.get("channel", "").strip()
        if not channel:
            channel = self.channel
            if not channel:
                raise ValueError("No channel provided and no default channel available.")

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

        # Step 3b: validate the payload BEFORE sending. A structural problem is
        # reported as a non-retryable ActionValidationError rather than being
        # discovered as an opaque `invalid_blocks` response after the network
        # round-trip (and never silently dropped).
        validation_errors = self._validate_payload(payload)
        if validation_errors:
            logger.error(
                "SlackActions.send_alert | payload validation FAILED | channel=%s | "
                "errors=%s",
                channel, validation_errors,
            )
            raise ActionValidationError(
                reason="invalid_blocks",
                details=validation_errors,
            )

        logger.info(
            "SlackActions.send_alert | POST chat.postMessage | channel=%s | "
            "severity=%s | title=%r | validation=passed",
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
            # Permanent payload errors (e.g. invalid_blocks) must not be retried;
            # surface them as a structured, non-retryable failure. Transient
            # errors keep the original RuntimeError so ActionAgent can retry.
            if error_code in _PERMANENT_SLACK_ERRORS:
                raise ActionValidationError(
                    reason=error_code,
                    details={"channel": channel, "slack_error": error_code},
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

        # Enforce Slack Block Kit length limits so a long title/message can
        # never trigger `invalid_blocks`. Header is truncated to 150 chars and
        # the section body to 3000 chars (with an ellipsis marker when cut).
        safe_title = title if len(title) <= _HEADER_TEXT_MAX else title[:_HEADER_TEXT_MAX - 1] + "…"
        safe_message = message if len(message) <= _SECTION_TEXT_MAX else message[:_SECTION_TEXT_MAX - 1] + "…"

        blocks: list[dict[str, Any]] = [
            {
                "type": "header",
                "text": {
                    "type":  "plain_text",
                    "text":  safe_title,
                    "emoji": True,
                },
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": safe_message,
                },
            },
            {"type": "divider"},
            {
                "type":     "context",
                "elements": context_elements,
            },
        ]

        # Plain-text fallback used for notifications / accessibility, and as the
        # attachment fallback. Kept short and free of block markup.
        fallback_text = f"[{severity}] {safe_title}: {safe_message}"

        # Render the blocks INSIDE a coloured attachment so the severity colour
        # sidebar actually wraps the message content. Previously the blocks were
        # placed at the top level next to a content-less colour-only attachment,
        # so the colour bar rendered detached from the message.
        return {
            "channel": channel,
            "text":    fallback_text,
            "attachments": [
                {
                    "color":    colour,
                    "fallback": fallback_text,
                    "blocks":   blocks,
                }
            ],
        }

    # ------------------------------------------------------------------
    # Payload validation
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_payload(payload: dict[str, Any]) -> list[str]:
        """
        Validate a ``chat.postMessage`` payload against Slack's structural rules.

        Checks the constraints that Slack rejects with ``invalid_blocks`` /
        ``invalid_arguments``:

        - ``channel`` is present and non-empty.
        - The payload carries renderable content (``text``, ``blocks``, or
          ``attachments``).
        - Every Block Kit block (top-level and inside attachments) is
          structurally valid: known type, ``header`` ``plain_text`` ≤ 150 chars
          and non-empty, ``section`` text ≤ 3000 chars and non-empty,
          ``context`` has a non-empty ``elements`` list.
        - No block group exceeds the 50-block limit.

        Args:
            payload: The fully built ``chat.postMessage`` payload.

        Returns:
            A list of human-readable error strings. Empty list means the
            payload is valid.
        """
        errors: list[str] = []

        channel = payload.get("channel")
        if not isinstance(channel, str) or not channel.strip():
            errors.append("channel must be a non-empty string.")

        has_content = bool(
            payload.get("text") or payload.get("blocks") or payload.get("attachments")
        )
        if not has_content:
            errors.append("payload must include text, blocks, or attachments.")

        # Validate top-level blocks (if any) and blocks inside each attachment.
        errors.extend(SlackActions._validate_blocks(payload.get("blocks"), "blocks"))
        for i, attachment in enumerate(payload.get("attachments", []) or []):
            if not isinstance(attachment, dict):
                errors.append(f"attachments[{i}] must be an object.")
                continue
            errors.extend(
                SlackActions._validate_blocks(
                    attachment.get("blocks"), f"attachments[{i}].blocks"
                )
            )

        return errors

    @staticmethod
    def _validate_blocks(blocks: Any, path: str) -> list[str]:
        """Validate a Block Kit ``blocks`` array. Returns a list of errors."""
        errors: list[str] = []
        if blocks is None:
            return errors
        if not isinstance(blocks, list):
            return [f"{path} must be a list."]
        if len(blocks) > _MAX_BLOCKS:
            errors.append(f"{path} exceeds the {_MAX_BLOCKS}-block limit ({len(blocks)}).")

        for i, block in enumerate(blocks):
            here = f"{path}[{i}]"
            if not isinstance(block, dict):
                errors.append(f"{here} must be an object.")
                continue
            btype = block.get("type")
            if not btype:
                errors.append(f"{here} is missing 'type'.")
                continue

            if btype == "header":
                text_obj = block.get("text", {})
                text = text_obj.get("text", "") if isinstance(text_obj, dict) else ""
                if text_obj.get("type") != "plain_text":
                    errors.append(f"{here} header text must be plain_text.")
                if not text.strip():
                    errors.append(f"{here} header text must be non-empty.")
                elif len(text) > _HEADER_TEXT_MAX:
                    errors.append(
                        f"{here} header text exceeds {_HEADER_TEXT_MAX} chars ({len(text)})."
                    )
            elif btype == "section":
                text_obj = block.get("text", {})
                text = text_obj.get("text", "") if isinstance(text_obj, dict) else ""
                if not text.strip():
                    errors.append(f"{here} section text must be non-empty.")
                elif len(text) > _SECTION_TEXT_MAX:
                    errors.append(
                        f"{here} section text exceeds {_SECTION_TEXT_MAX} chars ({len(text)})."
                    )
            elif btype == "context":
                elements = block.get("elements")
                if not isinstance(elements, list) or not elements:
                    errors.append(f"{here} context must have a non-empty elements list.")
            elif btype == "divider":
                pass  # no fields to validate
            # Unknown block types are left to Slack; we only guard the ones we emit.

        return errors

    def __repr__(self) -> str:
        return "SlackActions()"