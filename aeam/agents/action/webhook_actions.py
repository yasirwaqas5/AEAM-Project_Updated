"""
aeam/agents/action/webhook_actions.py

Webhook integration for the AEAM Action layer.

Triggers external services via configurable HTTP webhooks. Webhook
configurations are loaded from the injected SecretManager at construction
time. Supports GET, POST, PUT, and PATCH methods with optional custom
headers and authentication.

Called exclusively through the ActionAgent registry.

Phase 6 constraints:
- No retry logic (handled by ActionAgent).
- No LLM usage.
- No decision or Orchestrator logic.
- requests library only.
- HTTP timeout: 10 seconds.
- Raises on non-200 responses.
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

# Supported HTTP methods.
_SUPPORTED_METHODS: frozenset[str] = frozenset({"GET", "POST", "PUT", "PATCH"})


class WebhookActions:
    """
    Webhook trigger integration for the AEAM Action layer.

    Loads a webhook registry from the injected ``secret_manager`` at
    construction time. Each registered webhook defines a URL, HTTP method,
    and optional headers and authentication. Callers trigger a webhook by
    name and supply a payload dict.

    This class:
    - Contains no retry logic (ActionAgent handles retries).
    - Makes no LLM calls.
    - Contains no decision or Orchestrator logic.

    Webhook registry format expected from ``secret_manager.get("webhook_registry")``:

    .. code-block:: json

        {
            "pagerduty_alert": {
                "url":     "https://events.pagerduty.com/v2/enqueue",
                "method":  "POST",
                "headers": {"Content-Type": "application/json"},
                "auth":    {"type": "token", "token_secret_key": "pagerduty_token"}
            },
            "ops_callback": {
                "url":    "https://internal.example.com/aeam/callback",
                "method": "POST"
            }
        }

    Supported ``auth`` types:
    - ``"token"``  — adds ``Authorization: Bearer <token>`` header.
                     ``token_secret_key`` names the SecretManager key holding
                     the token value.
    - ``"basic"``  — HTTP Basic Auth. ``username_secret_key`` and
                     ``password_secret_key`` name the SecretManager keys.
    - ``None`` / absent — no authentication.

    Args:
        secret_manager: Secrets provider with ``get(key: str) -> Any`` interface.
                        Must return the webhook registry as a dict when called
                        with key ``"webhook_registry"``.

    Raises:
        ValueError: If ``secret_manager`` is None or the webhook registry
                    cannot be loaded.

    Example::

        webhook = WebhookActions(secret_manager=secret_manager)
        result = webhook.execute({
            "webhook_name": "pagerduty_alert",
            "payload": {"summary": "CPU spike", "severity": "critical"},
        })
        # {"status_code": 202, "response_body": {...}}
    """

    def __init__(self, secret_manager: Any) -> None:
        """
        Initialise WebhookActions and load the webhook registry.

        Args:
            secret_manager: Secrets provider. Must not be None.

        Raises:
            ValueError: If ``secret_manager`` is None or webhook registry
                        is missing or not a dict.
        """
        if secret_manager is None:
            raise ValueError("secret_manager must not be None.")

        self._secrets: Any = secret_manager
        self._registry: dict[str, dict[str, Any]] = self._load_registry()

        logger.info(
            "WebhookActions initialised | registered webhooks=%s",
            list(self._registry.keys()),
        )

    # ------------------------------------------------------------------
    # ActionAgent registry interface
    # ------------------------------------------------------------------

    def execute(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        ActionAgent registry entry point.

        Extracts ``webhook_name`` and ``payload`` from ``params`` and
        delegates to :meth:`trigger`.

        Args:
            params: Dict containing:

                - ``"webhook_name"`` *(required)* — name of the registered webhook.
                - ``"payload"``      *(required)* — request body dict.

        Returns:
            Result dict from :meth:`trigger`.

        Raises:
            ValueError: If ``webhook_name`` is missing from ``params``.
        """
        webhook_name: str = params.get("webhook_name", "").strip()
        if not webhook_name:
            raise ValueError("params['webhook_name'] must be a non-empty string.")

        payload: dict[str, Any] = params.get("payload", {})
        return self.trigger(webhook_name=webhook_name, payload=payload)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def trigger(
        self,
        webhook_name: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Trigger a registered webhook by name.

        Steps:
        1. Look up ``webhook_name`` in the loaded registry.
        2. Resolve the URL, HTTP method, headers, and authentication from
           the webhook config.
        3. Send the HTTP request with the supplied ``payload`` and a
           10-second timeout.
        4. Raise :class:`requests.HTTPError` if the response status is not 200.
        5. Return the status code and parsed response body.

        Args:
            webhook_name: Name of the webhook as registered in the config
                          (e.g. ``"pagerduty_alert"``).
            payload:      Dict to send as the JSON request body (for POST/PUT/
                          PATCH) or as query parameters (for GET).

        Returns:
            Dict::

                {
                    "status_code":    int,        # HTTP response status
                    "response_body":  dict | str, # parsed JSON or raw text
                    "webhook_name":   str,
                }

        Raises:
            ValueError:               If ``webhook_name`` is not registered
                                      or is empty.
            requests.HTTPError:       If the response status is not 200.
            requests.Timeout:         If the request exceeds 10 seconds.
            requests.ConnectionError: If the target host is unreachable.

        Example::

            result = webhook.trigger(
                webhook_name="ops_callback",
                payload={"incident_id": "INC-42", "status": "CRITICAL"},
            )
            # {"status_code": 200, "response_body": {"accepted": True}, "webhook_name": "ops_callback"}
        """
        if not webhook_name or not webhook_name.strip():
            raise ValueError("webhook_name must be a non-empty string.")

        # Step 1: look up config.
        if webhook_name not in self._registry:
            raise ValueError(
                f"Webhook {webhook_name!r} is not registered. "
                f"Known webhooks: {sorted(self._registry.keys())}."
            )

        config: dict[str, Any] = self._registry[webhook_name]

        # Step 2: resolve request parameters.
        url: str = config["url"]
        method: str = config.get("method", "POST").upper()

        if method not in _SUPPORTED_METHODS:
            raise ValueError(
                f"Webhook {webhook_name!r} specifies unsupported method "
                f"{method!r}. Supported: {sorted(_SUPPORTED_METHODS)}."
            )

        headers: dict[str, str] = dict(config.get("headers", {}))
        auth_config: dict[str, Any] | None = config.get("auth")
        auth_kwargs = self._resolve_auth(auth_config=auth_config, headers=headers)

        logger.info(
            "WebhookActions.trigger | %s %s | webhook=%s",
            method, url, webhook_name,
        )

        # Step 3: send request.
        if method == "GET":
            response = requests.get(
                url=url,
                params=payload,
                headers=headers,
                timeout=_HTTP_TIMEOUT,
                **auth_kwargs,
            )
        else:
            response = requests.request(
                method=method,
                url=url,
                json=payload,
                headers=headers,
                timeout=_HTTP_TIMEOUT,
                **auth_kwargs,
            )

        # Step 4: raise on non-200.
        if response.status_code != 200:
            logger.error(
                "WebhookActions.trigger | FAILED | webhook=%s | status=%d | body=%s",
                webhook_name, response.status_code, response.text[:500],
            )
            response.raise_for_status()

        # Step 5: parse response.
        try:
            response_body: dict[str, Any] | str = response.json()
        except ValueError:
            response_body = response.text

        logger.info(
            "WebhookActions.trigger | SUCCESS | webhook=%s | status=%d",
            webhook_name, response.status_code,
        )

        return {
            "status_code":   response.status_code,
            "response_body": response_body,
            "webhook_name":  webhook_name,
        }

    @property
    def registered_webhooks(self) -> list[str]:
        """Return the sorted list of registered webhook names."""
        return sorted(self._registry.keys())

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_registry(self) -> dict[str, dict[str, Any]]:
        """
        Load and validate the webhook registry from SecretManager.

        Retrieves the value stored under the key ``"webhook_registry"``.
        Accepts both a pre-parsed dict and a JSON string (auto-parsed).

        Returns:
            Validated webhook registry dict. May be empty if no webhooks
            are configured.

        Raises:
            ValueError: If the registry value is not a dict (after optional
                        JSON parsing).
        """
        import json as _json

        raw = self._secrets.get("webhook_registry")

        if raw is None:
            logger.warning(
                "_load_registry | 'webhook_registry' not found in SecretManager. "
                "WebhookActions will have no registered endpoints."
            )
            return {}

        if isinstance(raw, str):
            try:
                raw = _json.loads(raw)
            except _json.JSONDecodeError as exc:
                raise ValueError(
                    f"'webhook_registry' in SecretManager is a string but not "
                    f"valid JSON: {exc}"
                ) from exc

        if not isinstance(raw, dict):
            raise ValueError(
                f"'webhook_registry' must be a dict, got {type(raw).__name__!r}."
            )

        logger.debug("_load_registry | loaded %d webhook(s).", len(raw))
        return raw

    def _resolve_auth(
        self,
        auth_config: dict[str, Any] | None,
        headers: dict[str, str],
    ) -> dict[str, Any]:
        """
        Resolve authentication from the webhook config into ``requests`` kwargs.

        Mutates ``headers`` in-place to inject a Bearer token when
        ``auth.type == "token"``. Returns a ``{"auth": (user, pass)}`` dict
        for Basic Auth. Returns an empty dict when no auth is configured.

        Supported ``auth`` types:
        - ``"token"`` — Bearer token. ``token_secret_key`` names the secret.
        - ``"basic"`` — HTTP Basic. ``username_secret_key`` and
          ``password_secret_key`` name the secrets.

        Args:
            auth_config: The ``"auth"`` sub-dict from the webhook config, or
                         ``None`` if no auth is configured.
            headers:     Mutable headers dict. May be extended with
                         ``Authorization`` for token auth.

        Returns:
            Dict of extra keyword arguments for the ``requests`` call
            (e.g. ``{"auth": ("user", "pass")}`` for Basic Auth, or ``{}``).

        Raises:
            ValueError: If ``auth_config`` specifies an unsupported type.
        """
        if not auth_config:
            return {}

        auth_type: str = auth_config.get("type", "").lower()

        if auth_type == "token":
            token_key: str = auth_config.get("token_secret_key", "")
            token: str = self._secrets.get(token_key)
            headers["Authorization"] = f"Bearer {token}"
            return {}

        if auth_type == "basic":
            username_key: str = auth_config.get("username_secret_key", "")
            password_key: str = auth_config.get("password_secret_key", "")
            username: str = self._secrets.get(username_key)
            password: str = self._secrets.get(password_key)
            return {"auth": (username, password)}

        raise ValueError(
            f"Unsupported auth type {auth_type!r} in webhook config. "
            f"Supported: 'token', 'basic'."
        )

    def __repr__(self) -> str:
        return (
            f"WebhookActions("
            f"registered={list(self._registry.keys())})"
        )