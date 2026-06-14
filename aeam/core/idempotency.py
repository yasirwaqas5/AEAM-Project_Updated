"""
aeam/core/idempotency.py

Idempotency management for the AEAM Action layer.

Prevents duplicate execution of actions by storing a hashed key in Redis
with a 24-hour TTL. Before executing any action, the ActionAgent checks
whether the key already exists; if it does, the action is skipped.

Phase 6 constraints:
- No business logic.
- No LLM usage.
- No orchestration logic.
- No external HTTP calls.
- Fully typed.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from aeam.integrations.redis_client import RedisClient

logger = logging.getLogger(__name__)

# TTL for idempotency keys — 24 hours in seconds.
_TTL_SECONDS: int = 24 * 60 * 60

# Namespace prefix to avoid collisions with other AEAM Redis keys.
_KEY_PREFIX: str = "aeam:idempotency"


class IdempotencyManager:
    """
    Prevents duplicate execution of actions by keying on a stable hash.

    Each action is uniquely identified by its ``incident_id``, ``action_type``,
    and ``params``. These are hashed with SHA-256 to produce a Redis key. If
    the key already exists in Redis, the action has already been executed
    within the 24-hour window and should not be re-executed.

    Keys are stored with a 24-hour TTL via Redis ``SETEX``. Stored values
    carry the serialised action result for optional inspection.

    Args:
        redis_client: Connected :class:`~aeam.integrations.redis_client.RedisClient`.

    Raises:
        ValueError: If ``redis_client`` is None.

    Example::

        manager = IdempotencyManager(redis_client=redis_client)
        key = manager.generate_key("INC-42", "create_jira_ticket", {"priority": "high"})

        if manager.check(key):
            logger.info("Action already executed — skipping.")
        else:
            result = execute_action()
            manager.store(key, result)
    """

    def __init__(self, redis_client: RedisClient) -> None:
        """
        Initialise IdempotencyManager with an injected Redis client.

        Args:
            redis_client: Active RedisClient instance. Must not be None.

        Raises:
            ValueError: If ``redis_client`` is None.
        """
        if redis_client is None:
            raise ValueError("redis_client must not be None.")
        self._redis: RedisClient = redis_client

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def generate_key(
        self,
        incident_id: str,
        action_type: str,
        params: dict[str, Any],
    ) -> str:
        """
        Generate a deterministic idempotency key for an action.

        The key is a SHA-256 digest of the canonical JSON serialisation of
        ``incident_id``, ``action_type``, and ``params``, prefixed with the
        AEAM namespace. Parameters are serialised with sorted keys to ensure
        that dict ordering never produces different keys for logically
        identical inputs.

        Args:
            incident_id:  Unique identifier for the incident triggering the
                          action (e.g. ``"INC-42"``).
            action_type:  The action to be executed (e.g.
                          ``"create_jira_ticket"``).
            params:       Action-specific parameters dict. Must be
                          JSON-serialisable. Nested dicts are supported;
                          keys are sorted recursively.

        Returns:
            Redis key string of the form
            ``"aeam:idempotency:<sha256hex>"``.

        Raises:
            ValueError:    If ``incident_id`` or ``action_type`` is empty.
            TypeError:     If ``params`` contains non-JSON-serialisable values.

        Example::

            key = manager.generate_key(
                incident_id="INC-42",
                action_type="create_jira_ticket",
                params={"priority": "high", "project": "OPS"},
            )
            # "aeam:idempotency:a3f7..."
        """
        if not incident_id or not incident_id.strip():
            raise ValueError("incident_id must be a non-empty string.")
        if not action_type or not action_type.strip():
            raise ValueError("action_type must be a non-empty string.")

        payload: dict[str, Any] = {
            "incident_id": incident_id.strip(),
            "action_type": action_type.strip(),
            "params":      params,
        }

        # Canonical serialisation: sorted keys, no whitespace.
        try:
            canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        except TypeError as exc:
            raise TypeError(
                f"params contains non-JSON-serialisable values: {exc}"
            ) from exc

        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        key = f"{_KEY_PREFIX}:{digest}"

        logger.debug(
            "generate_key | incident_id=%r | action_type=%r | key=%s",
            incident_id, action_type, key,
        )

        return key

    def check(self, key: str) -> bool:
        """
        Return ``True`` if the idempotency key already exists in Redis.

        A ``True`` result means the action was already executed within the
        24-hour TTL window and must not be re-executed.

        Args:
            key: Idempotency key produced by :meth:`generate_key`.

        Returns:
            ``True`` if the key exists; ``False`` otherwise.

        Note:
            Redis connectivity errors are logged and treated as ``False``
            (i.e. allow the action to proceed) to avoid blocking execution
            on a transient cache miss. Callers that require strict
            idempotency under Redis failure should handle this explicitly.
        """
        if not key or not key.strip():
            logger.warning("check | received empty key; returning False.")
            return False

        try:
            value = self._redis.get(key)
            exists = value is not None
            if exists:
                logger.info(
                    "check | key EXISTS — action already executed | key=%s", key
                )
            else:
                logger.debug("check | key not found | key=%s", key)
            return exists
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "check | Redis error for key=%s: %s | defaulting to False.",
                key, exc,
            )
            return False

    def store(self, key: str, result: dict[str, Any]) -> None:
        """
        Persist the action result under ``key`` with a 24-hour TTL.

        Uses Redis ``SETEX`` so the key expires automatically. The result
        dict is serialised to JSON before storage.

        Args:
            key:    Idempotency key produced by :meth:`generate_key`.
            result: The action result to store. Must be JSON-serialisable.

        Raises:
            TypeError: If ``result`` contains non-JSON-serialisable values.

        Note:
            Redis connectivity errors are logged as errors but do not raise,
            to avoid blocking the action completion path. The action has
            already executed at the point ``store`` is called.
        """
        if not key or not key.strip():
            logger.warning("store | received empty key; skipping store.")
            return

        try:
            serialised = json.dumps(result, sort_keys=True, default=str)
        except TypeError as exc:
            raise TypeError(
                f"result contains non-JSON-serialisable values: {exc}"
            ) from exc

        try:
            # Use positional arguments to match RedisClient.setex signature
            self._redis.setex(key, _TTL_SECONDS, serialised)
            logger.info(
                "store | stored idempotency record | key=%s | ttl=%ds",
                key, _TTL_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "store | Redis error for key=%s: %s | idempotency record NOT saved.",
                key, exc,
            )

    def __repr__(self) -> str:
        return f"IdempotencyManager(ttl={_TTL_SECONDS}s)"