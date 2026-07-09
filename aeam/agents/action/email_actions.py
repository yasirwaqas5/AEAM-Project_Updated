"""
aeam/agents/action/email_actions.py

Gmail integration for the AEAM Action layer.

Sends incident report emails via the Gmail REST API using service account
credentials retrieved from SecretManager. Builds a MIME message, encodes
it as base64url, and POSTs to the Gmail API send endpoint.
Called exclusively through the ActionAgent registry.

Phase 6 constraints:
- No retry logic (handled by ActionAgent).
- No LLM usage.
- No decision or Orchestrator logic.
- requests library only for HTTP.
- HTTP timeout: 10 seconds.
- Raises on non-200 responses.
- Fully typed, logging throughout.
"""

from __future__ import annotations

import base64
import logging
from aeam.monitoring.logging_config import get_logger
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

import requests

from aeam.agents.action.errors import ActionConfigurationError

logger = get_logger(__name__, agent="action")

# Enforced HTTP timeout (Phase 6 spec).
_HTTP_TIMEOUT: int = 10

# Required Google Cloud / Gmail credentials, in the order they are reported
# when missing.
_REQUIRED_CREDENTIALS: tuple[str, ...] = (
    "gmail_private_key",
    "gmail_client_email",
    "gmail_sender_address",
)

# Gmail API send endpoint (user "me" = the authenticated service account).
_GMAIL_SEND_URL: str = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"

# Google OAuth2 token endpoint.
_GOOGLE_TOKEN_URL: str = "https://oauth2.googleapis.com/token"

# Required OAuth2 scope for sending mail.
_GMAIL_SEND_SCOPE: str = "https://www.googleapis.com/auth/gmail.send"


class EmailActions:
    """
    Gmail email integration for the AEAM Action layer.

    Authenticates with the Gmail API using a service account OAuth2 token
    obtained from Google's token endpoint (credentials supplied by the
    injected ``secret_manager``), builds a MIME email, encodes it as
    base64url, and POSTs to the Gmail API ``messages.send`` endpoint.

    This class:
    - Contains no retry logic (ActionAgent handles retries).
    - Makes no LLM calls.
    - Contains no decision or Orchestrator logic.

    Secrets expected from ``secret_manager``:
    - ``"gmail_client_email"``  — service account email address.
    - ``"gmail_private_key"``   — RSA private key (PEM string) for JWT signing.
    - ``"gmail_sender_address"`` — ``From`` address for outgoing mail.

    Args:
        secret_manager: Secrets provider with a ``get(key: str) -> str`` interface.

    Raises:
        ValueError: If ``secret_manager`` is None.

    Example::

        email = EmailActions(secret_manager=secret_manager)
        result = email.execute({
            "to":      ["ops@example.com"],
            "subject": "AEAM Incident Report: INC-42",
            "body":    "CPU spike detected on web-01 at 14:32 UTC.",
        })
        # {"message_id": "18e4f2a3b1c00001"}
    """

    def __init__(self, secret_manager: Any) -> None:
        """
        Initialise EmailActions with an injected secrets provider.

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
        ActionAgent registry entry point — delegates to :meth:`send_email`.

        Args:
            params: Action parameters dict. See :meth:`send_email`.

        Returns:
            Result dict from :meth:`send_email`.
        """
        return self.send_email(params)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def send_email(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        Send an incident report email via the Gmail API.

        Steps:
        1. Retrieve ``gmail_client_email``, ``gmail_private_key``, and
           ``gmail_sender_address`` from ``SecretManager``.
        2. Obtain a short-lived OAuth2 bearer token via a JWT assertion POST
           to ``https://oauth2.googleapis.com/token``.
        3. Validate required parameters (``to``, ``subject``, ``body``).
        4. Build a ``multipart/alternative`` MIME email with plain-text and
           HTML parts.
        5. Encode the serialised MIME message as base64url (URL-safe, no
           padding).
        6. POST the encoded message to the Gmail API ``messages.send``
           endpoint.
        7. Raise :class:`requests.HTTPError` on non-200 responses.
        8. Return the Gmail message ID.

        Args:
            params: Dict containing:

                - ``"to"``       *(required)* — list of recipient email
                  addresses.
                - ``"subject"``  *(required)* — email subject line.
                - ``"body"``     *(required)* — plain-text email body.
                  An HTML version is auto-generated by wrapping the body
                  in ``<pre>`` tags for readability.
                - ``"cc"``       *(optional)* — list of CC addresses.
                - ``"reply_to"`` *(optional)* — Reply-To address.

        Returns:
            Dict::

                {"message_id": str}  # Gmail message ID

        Raises:
            ValueError:               If ``to`` is empty, ``subject`` or
                                      ``body`` is blank, or an address in
                                      ``to`` is not a string.
            requests.HTTPError:       If the Gmail API returns a non-200
                                      status.
            requests.Timeout:         If any request exceeds 10 seconds.
            requests.ConnectionError: If the Gmail API is unreachable.
            RuntimeError:             If the OAuth2 token exchange fails.

        Example::

            result = email.send_email({
                "to":      ["sre@example.com", "oncall@example.com"],
                "subject": "[AEAM] CRITICAL: CPU spike INC-42",
                "body":    "Automated report: CPU reached 97% on web-01.",
                "cc":      ["management@example.com"],
            })
        """
        # Step 0: verify credentials are configured BEFORE doing any work.
        # Missing credentials are a configuration error, not a transient
        # failure — surface a structured, non-retryable error so the caller
        # never silently fails and never wastes retry attempts on it.
        resolved: dict[str, Any] = {
            key: self._secrets.get(key) for key in _REQUIRED_CREDENTIALS
        }
        missing: list[str] = [
            key for key in _REQUIRED_CREDENTIALS
            if not (isinstance(resolved[key], str) and resolved[key].strip())
        ]
        if missing:
            logger.error(
                "EmailActions.send_email | missing Google Cloud credentials | missing=%s",
                missing,
            )
            raise ActionConfigurationError(
                reason="Missing Google Cloud credentials",
                details={"missing": missing},
            )

        # Step 1: retrieve secrets (validated present above).
        client_email: str = resolved["gmail_client_email"]
        private_key: str = resolved["gmail_private_key"]
        sender: str = resolved["gmail_sender_address"]

        # Step 2: obtain OAuth2 token.
        access_token = self._get_access_token(
            client_email=client_email,
            private_key=private_key,
        )

        # Step 3: validate parameters.
        to_addresses: list[str] = params.get("to", [])
        if not to_addresses:
            raise ValueError("params['to'] must be a non-empty list of addresses.")
        if not all(isinstance(addr, str) and addr.strip() for addr in to_addresses):
            raise ValueError("All addresses in params['to'] must be non-empty strings.")

        subject: str = params.get("subject", "").strip()
        if not subject:
            raise ValueError("params['subject'] must be a non-empty string.")

        body: str = params.get("body", "").strip()
        if not body:
            raise ValueError("params['body'] must be a non-empty string.")

        cc_addresses: list[str] = params.get("cc", [])
        reply_to: str | None = params.get("reply_to")

        # Step 4: build MIME message.
        mime_msg = self._build_mime(
            sender=sender,
            to_addresses=to_addresses,
            cc_addresses=cc_addresses,
            subject=subject,
            body_text=body,
            reply_to=reply_to,
        )

        # Step 5: encode as base64url.
        raw_bytes: bytes = mime_msg.as_bytes()
        encoded: str = base64.urlsafe_b64encode(raw_bytes).decode("utf-8").rstrip("=")

        logger.info(
            "EmailActions.send_email | sending | to=%s | subject=%r",
            to_addresses, subject,
        )

        # Step 6: POST to Gmail API.
        response = requests.post(
            url=_GMAIL_SEND_URL,
            json={"raw": encoded},
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type":  "application/json",
            },
            timeout=_HTTP_TIMEOUT,
        )

        # Step 7: raise on non-200.
        if response.status_code != 200:
            logger.error(
                "EmailActions.send_email | FAILED | status=%d | body=%s",
                response.status_code, response.text[:500],
            )
            response.raise_for_status()

        # Step 8: return message ID.
        message_id: str = response.json().get("id", "")

        logger.info(
            "EmailActions.send_email | SUCCESS | message_id=%s | to=%s",
            message_id, to_addresses,
        )

        return {"message_id": message_id}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_access_token(
        self,
        client_email: str,
        private_key: str,
    ) -> str:
        """
        Exchange a JWT service account assertion for a Google OAuth2 bearer token.

        Constructs and signs a JWT using the ``PyJWT`` library, then POSTs it
        to ``https://oauth2.googleapis.com/token`` to obtain a short-lived
        access token scoped to ``gmail.send``.

        Args:
            client_email: Service account email address (``iss`` claim).
            private_key:  PEM-encoded RSA private key for RS256 signing.

        Returns:
            Short-lived OAuth2 access token string.

        Raises:
            RuntimeError:             If the token response is missing
                                      ``access_token``.
            requests.HTTPError:       If the token exchange request fails.
            requests.Timeout:         If the token exchange exceeds 10 seconds.
            ImportError:              If ``PyJWT`` is not installed.
        """
        import time
        import jwt  # pip install PyJWT[cryptography]

        now = int(time.time())
        claims = {
            "iss":   client_email,
            "sub":   client_email,
            "scope": _GMAIL_SEND_SCOPE,
            "aud":   _GOOGLE_TOKEN_URL,
            "iat":   now,
            "exp":   now + 3600,
        }

        assertion = jwt.encode(claims, private_key, algorithm="RS256")

        logger.debug("EmailActions._get_access_token | requesting token for %s", client_email)

        token_response = requests.post(
            url=_GOOGLE_TOKEN_URL,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion":  assertion,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=_HTTP_TIMEOUT,
        )

        if token_response.status_code != 200:
            logger.error(
                "_get_access_token | token exchange failed | status=%d | body=%s",
                token_response.status_code, token_response.text[:500],
            )
            token_response.raise_for_status()

        token_data: dict[str, Any] = token_response.json()
        access_token: str | None = token_data.get("access_token")

        if not access_token:
            raise RuntimeError(
                f"OAuth2 token exchange succeeded but response contained no "
                f"'access_token'. Response keys: {list(token_data.keys())}."
            )

        logger.debug("EmailActions._get_access_token | token obtained successfully.")
        return access_token

    @staticmethod
    def _build_mime(
        sender: str,
        to_addresses: list[str],
        cc_addresses: list[str],
        subject: str,
        body_text: str,
        reply_to: str | None,
    ) -> MIMEMultipart:
        """
        Construct a ``multipart/alternative`` MIME email message.

        Includes both a plain-text part and an HTML part. The HTML part wraps
        the plain-text body in a ``<pre>`` block for monospace rendering in
        email clients, preserving any structured formatting (e.g. incident
        report tables).

        Args:
            sender:        ``From`` address.
            to_addresses:  List of ``To`` addresses.
            cc_addresses:  List of ``Cc`` addresses (may be empty).
            subject:       Email subject line.
            body_text:     Plain-text message body.
            reply_to:      Optional ``Reply-To`` address.

        Returns:
            Fully assembled :class:`email.mime.multipart.MIMEMultipart` object.
        """
        msg = MIMEMultipart("alternative")
        msg["From"] = sender
        msg["To"] = ", ".join(to_addresses)
        msg["Subject"] = subject

        if cc_addresses:
            msg["Cc"] = ", ".join(cc_addresses)

        if reply_to:
            msg["Reply-To"] = reply_to

        # Plain-text part.
        msg.attach(MIMEText(body_text, "plain", "utf-8"))

        # HTML part — wrap body in <pre> for readability.
        html_body = (
            "<!DOCTYPE html><html><body>"
            f"<pre style='font-family:monospace;font-size:14px;'>"
            f"{body_text}"
            f"</pre>"
            "</body></html>"
        )
        msg.attach(MIMEText(html_body, "html", "utf-8"))

        return msg

    def __repr__(self) -> str:
        return "EmailActions()"