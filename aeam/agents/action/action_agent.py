"""
aeam/agents/action/action_agent.py

Central executor for all external actions in AEAM Phase 6.

The ActionAgent is the only component in the system permitted to call external
APIs. It enforces idempotency, retries, logging, and result persistence for
every action it executes. It contains no LLM logic, no decision logic, and no
Orchestrator logic — it only executes whatever action it is told to execute.

Phase 6 constraints (all enforced):
- Only ActionAgent may call external APIs.
- No LLM usage.
- No decision or Orchestrator logic.
- All integrations called through the registry.
- HTTP timeout: 10 seconds (enforced in integration classes).
- Retry: max 2 attempts on failure.
- Idempotency TTL: 24 hours.
- Results logged to ``action_logs`` table.
- Fully typed, logging throughout.
"""

from __future__ import annotations

import logging
import random
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from aeam.core.idempotency import IdempotencyManager
from aeam.integrations.database import DatabaseClient
from aeam.integrations.redis_client import RedisClient

# Correct imports based on your folder structure
from aeam.agents.action.jira_actions import JiraActions
from aeam.agents.action.slack_actions import SlackActions
from aeam.agents.action.email_actions import EmailActions
from aeam.agents.action.webhook_actions import WebhookActions
from aeam.agents.action.sheets_actions import GoogleSheetsActions

logger = logging.getLogger(__name__)

# Retry configuration.
_MAX_ATTEMPTS: int = 2
_BASE_RETRY_DELAY_SECONDS: float = 1.0
_MAX_RETRY_DELAY_SECONDS: float = 8.0
_RETRY_JITTER: float = 0.1  # ±10% jitter

# Circuit breaker configuration.
_CB_FAILURE_THRESHOLD: int = 3          # Open circuit after this many consecutive failures.
_CB_TIMEOUT_SECONDS: float = 60.0       # Time before trying half-open.
_CB_HALF_OPEN_SUCCESS_RESET: bool = True  # One success in half-open closes circuit.

# Result status literals.
STATUS_SUCCESS: str = "SUCCESS"
STATUS_FAILED: str = "FAILED"
STATUS_ALREADY_EXECUTED: str = "ALREADY_EXECUTED"
STATUS_CIRCUIT_OPEN: str = "CIRCUIT_OPEN"


class CircuitBreaker:
    """
    Simple circuit breaker per action type.

    States:
        CLOSED   – normal operation, calls allowed.
        OPEN     – calls are blocked; after timeout transitions to HALF_OPEN.
        HALF_OPEN – one trial call allowed; if success → CLOSED, if failure → OPEN.

    Args:
        failure_threshold: Number of consecutive failures to open the circuit.
        timeout_seconds:   Time in seconds before moving from OPEN to HALF_OPEN.
    """

    def __init__(self, failure_threshold: int, timeout_seconds: float) -> None:
        self._failure_threshold = failure_threshold
        self._timeout = timeout_seconds
        self._failure_count = 0
        self._state = "CLOSED"  # CLOSED, OPEN, HALF_OPEN
        self._open_until: float | None = None

    def allow_request(self) -> bool:
        """Return True if the request should be allowed."""
        if self._state == "CLOSED":
            return True
        if self._state == "OPEN":
            if self._open_until is not None and time.monotonic() > self._open_until:
                self._state = "HALF_OPEN"
                logger.debug("Circuit breaker moved from OPEN to HALF_OPEN")
                return True
            return False
        # HALF_OPEN – allow exactly one request.
        return True

    def record_success(self) -> None:
        """Call after a successful action."""
        if self._state == "HALF_OPEN":
            self._state = "CLOSED"
            self._failure_count = 0
            logger.debug("Circuit breaker closed (success in half-open).")
        else:
            self._failure_count = 0

    def record_failure(self) -> None:
        """Call after a failed action."""
        self._failure_count += 1
        if self._state == "HALF_OPEN":
            self._state = "OPEN"
            self._open_until = time.monotonic() + self._timeout
            logger.warning("Circuit breaker opened (failure in half-open).")
        elif self._failure_count >= self._failure_threshold and self._state == "CLOSED":
            self._state = "OPEN"
            self._open_until = time.monotonic() + self._timeout
            logger.warning(
                "Circuit breaker opened after %d failures",
                self._failure_count,
            )

    def __repr__(self) -> str:
        return (
            f"CircuitBreaker(state={self._state}, "
            f"failures={self._failure_count})"
        )


class ActionAgent:
    """
    Central executor for all external actions in the AEAM Action layer.

    The ActionAgent is the **sole** component permitted to call external APIs.
    It delegates to integration-specific handlers stored in an internal
    registry, enforces idempotency, retries failed actions up to
    ``_MAX_ATTEMPTS`` times, persists results via Redis, and writes an audit
    record to the ``action_logs`` database table.

    The agent contains:
    - No LLM logic.
    - No decision or routing logic.
    - No Orchestrator references.

    All action handlers are instantiated at construction time and stored in
    the registry. Handlers are called exclusively through
    ``self._registry[action_type]``.

    Args:
        secret_manager:      Provider of credentials/secrets needed by
                             integration handlers (injected into each handler).
        redis_client:        Connected Redis client for idempotency storage.
        database_client:     Connected database client for audit logging.
        idempotency_manager: :class:`~aeam.core.idempotency.IdempotencyManager`
                             for duplicate-execution prevention.
        settings:            Optional application settings object (used by
                             SlackActions and JiraActions to get configuration).

    Raises:
        ValueError: If any required dependency is None.

    Example::

        agent = ActionAgent(
            secret_manager=secret_manager,
            redis_client=redis_client,
            database_client=db_client,
            idempotency_manager=IdempotencyManager(redis_client),
            settings=settings,
        )
        result = agent.execute(
            action_type="jira",
            parameters={"summary": "CPU spike", "project": "OPS", "priority": "High"},
            incident_id="INC-42",
        )
        # {"status": "SUCCESS", "action_id": "...", "result": {...}}
    """

    def __init__(
        self,
        secret_manager: Any,
        redis_client: RedisClient,
        database_client: DatabaseClient,
        idempotency_manager: IdempotencyManager,
        settings: Any = None,          # <-- added settings parameter
    ) -> None:
        """
        Initialise the ActionAgent and build the action registry.

        Each integration handler receives ``secret_manager`` at construction
        so that credentials are never hardcoded or passed per-call.

        Args:
            secret_manager:      Secrets provider injected into each handler.
            redis_client:        Active RedisClient instance.
            database_client:     Active DatabaseClient instance.
            idempotency_manager: Active IdempotencyManager instance.
            settings:            Optional application settings object.

        Raises:
            ValueError: If ``redis_client``, ``database_client``, or
                        ``idempotency_manager`` is None.
        """
        if redis_client is None:
            raise ValueError("redis_client must not be None.")
        if database_client is None:
            raise ValueError("database_client must not be None.")
        if idempotency_manager is None:
            raise ValueError("idempotency_manager must not be None.")

        self._secret_manager: Any = secret_manager
        self._redis: RedisClient = redis_client
        self._db: DatabaseClient = database_client
        self._idempotency: IdempotencyManager = idempotency_manager
        self._settings: Any = settings

        # Build action registry — all integrations called through this dict.
        # SlackActions: use settings if provided (new style), else fallback to secret_manager.
        slack_handler: SlackActions
        if settings is not None:
            slack_handler = SlackActions(secret_manager=secret_manager)
        else:
            slack_handler = SlackActions(secret_manager=secret_manager)
        # Start with base registry (excluding Jira for now)
        self._registry: dict[str, Any] = {
            "slack":   slack_handler,
            "email":   EmailActions(secret_manager=secret_manager),
            "webhook": WebhookActions(secret_manager=secret_manager),
            "sheets":  GoogleSheetsActions(secret_manager=secret_manager),
        }

        # Conditionally add Jira if settings provide JIRA_URL
        if settings and hasattr(settings, 'JIRA_URL') and settings.JIRA_URL:
            self._registry["jira"] = JiraActions(settings=settings)
            # 🔧 FIX: removed the manual circuit breaker assignment here
            # The circuit breakers are built below from the final registry.
            logger.info("Jira action registered.")
        else:
            # Jira not configured; omit from registry
            logger.debug("Jira not configured – skipping registration.")

        # Circuit breakers per action type — build from the final registry.
        self._circuit_breakers: dict[str, CircuitBreaker] = {
            action_type: CircuitBreaker(
                failure_threshold=_CB_FAILURE_THRESHOLD,
                timeout_seconds=_CB_TIMEOUT_SECONDS,
            )
            for action_type in self._registry
        }

        logger.info(
            "ActionAgent initialised | registry=%s",
            list(self._registry.keys()),
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def execute(
        self,
        action_type: str,
        parameters: dict[str, Any],
        incident_id: str,
    ) -> dict[str, Any]:
        """
        Execute an external action through the registry.

        Execution steps:
        1. Validate that ``action_type`` exists in the registry.
        2. Check circuit breaker; if open, return failure immediately.
        3. Generate an idempotency key from ``incident_id``, ``action_type``,
           and ``parameters``.
        4. Check Redis for a duplicate execution record.
        5. Log the action attempt.
        6. Execute the action handler (via ``self._registry[action_type]``) with
           exponential backoff + jitter retries.
        7. On success, record success in circuit breaker.
        8. On failure, record failure and retry; after all attempts exhausted,
           final failure is recorded.
        9. Store result in Redis with a 24-hour TTL via
           :class:`~aeam.core.idempotency.IdempotencyManager`.
        10. Log the action result to the ``action_logs`` database table.

        Args:
            action_type:  Registry key for the desired action handler.
                          One of: ``"jira"``, ``"slack"``, ``"email"``,
                          ``"webhook"``, ``"sheets"``.
            parameters:   Action-specific parameter dict passed to the handler.
            incident_id:  Incident identifier for idempotency scoping and
                          audit logging.

        Returns:
            Result dict with the following structure::

                {
                    "status":    "SUCCESS" | "FAILED" | "ALREADY_EXECUTED" | "CIRCUIT_OPEN",
                    "action_id": str,   # UUID for this execution record
                    "result":    dict,  # handler response or error detail
                }

        Raises:
            ValueError: If ``action_type`` is not registered.

        Note:
            This method never raises on handler failure — failures are captured
            in the return dict with ``"status": "FAILED"`` and the exception
            message in ``"result"``. The caller (Orchestrator) decides how to
            handle failures.
        """
        action_id: str = str(uuid.uuid4())

        # Step 1: validate action_type.
        if action_type not in self._registry:
            raise ValueError(
                f"Unknown action_type {action_type!r}. "
                f"Registered types: {sorted(self._registry.keys())}."
            )

        # Step 2: check circuit breaker.
        cb = self._circuit_breakers[action_type]
        if not cb.allow_request():
            logger.warning(
                "execute | CIRCUIT_OPEN | action_type=%s | incident_id=%s | action_id=%s",
                action_type, incident_id, action_id,
            )
            return {
                "status":    STATUS_CIRCUIT_OPEN,
                "action_id": action_id,
                "result":    {"detail": "Circuit breaker open; action temporarily blocked."},
            }

        # Step 3: generate idempotency key.
        idempotency_key = self._idempotency.generate_key(
            incident_id=incident_id,
            action_type=action_type,
            params=parameters,
        )

        # Step 4: check for duplicate execution.
        if self._idempotency.check(idempotency_key):
            logger.info(
                "execute | ALREADY_EXECUTED | action_type=%s | incident_id=%s | "
                "action_id=%s",
                action_type, incident_id, action_id,
            )
            return {
                "status":    STATUS_ALREADY_EXECUTED,
                "action_id": action_id,
                "result":    {"detail": "Action already executed within 24-hour window."},
            }

        # Step 5: log action attempt.
        logger.info(
            "execute | ATTEMPT | action_type=%s | incident_id=%s | action_id=%s",
            action_type, incident_id, action_id,
        )

        # Steps 6–8: execute with exponential backoff + jitter, and circuit breaker updates.
        handler_result: dict[str, Any]
        final_status: str
        last_exc: Exception | None = None
        success: bool = False

        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                handler = self._registry[action_type]
                handler_result = handler.execute(parameters)
                final_status = STATUS_SUCCESS
                success = True

                logger.info(
                    "execute | SUCCESS | action_type=%s | incident_id=%s | "
                    "attempt=%d/%d | action_id=%s",
                    action_type, incident_id, attempt, _MAX_ATTEMPTS, action_id,
                )
                break

            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                logger.warning(
                    "execute | FAILURE | action_type=%s | incident_id=%s | "
                    "attempt=%d/%d | error=%s | action_id=%s",
                    action_type, incident_id, attempt, _MAX_ATTEMPTS, exc, action_id,
                )
                if attempt < _MAX_ATTEMPTS:
                    # Exponential backoff with jitter.
                    delay = min(
                        _MAX_RETRY_DELAY_SECONDS,
                        _BASE_RETRY_DELAY_SECONDS * (2 ** (attempt - 1))
                    )
                    jitter = random.uniform(-delay * _RETRY_JITTER, delay * _RETRY_JITTER)
                    sleep_time = max(0, delay + jitter)
                    logger.debug("execute | retrying in %.2fs", sleep_time)
                    time.sleep(sleep_time)
        else:
            # All attempts exhausted.
            final_status = STATUS_FAILED
            handler_result = {
                "error":   str(last_exc),
                "detail":  f"Action failed after {_MAX_ATTEMPTS} attempts.",
            }
            logger.error(
                "execute | FAILED (all attempts) | action_type=%s | "
                "incident_id=%s | action_id=%s | error=%s",
                action_type, incident_id, action_id, last_exc,
            )

        # Update circuit breaker.
        if success:
            cb.record_success()
        else:
            cb.record_failure()

        # Step 9: store result in Redis (idempotency record).
        self._idempotency.store(
            key=idempotency_key,
            result={
                "action_id":   action_id,
                "status":      final_status,
                "action_type": action_type,
                "incident_id": incident_id,
                **handler_result,
            },
        )

        # Step 10: persist audit record to action_logs table.
        self._log_to_database(
            action_id=action_id,
            action_type=action_type,
            incident_id=incident_id,
            parameters=parameters,
            status=final_status,
            result=handler_result,
        )

        return {
            "status":    final_status,
            "action_id": action_id,
            "result":    handler_result,
        }

    @property
    def registered_actions(self) -> list[str]:
        """Return the sorted list of registered action type keys."""
        return sorted(self._registry.keys())

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _log_to_database(
        self,
        action_id: str,
        action_type: str,
        incident_id: str,
        parameters: dict[str, Any],
        status: str,
        result: dict[str, Any],
    ) -> None:
        """
        Write an audit record to the ``action_logs`` database table.

        Failures are logged as errors but never raised — the action has
        already completed and audit write failures must not retroactively
        fail a successful action.

        Args:
            action_id:   UUID for this execution.
            action_type: Registry key of the executed handler.
            incident_id: Incident that triggered the action.
            parameters:  Parameters passed to the handler.
            status:      Final execution status string.
            result:      Handler result or error detail dict.
        """
        import json

        record: dict[str, Any] = {
            "action_id":   action_id,
            "action_type": action_type,
            "incident_id": incident_id,
            "parameters":  json.dumps(parameters, default=str),
            "status":      status,
            "result":      json.dumps(result, default=str),
            "executed_at": datetime.now(tz=timezone.utc).isoformat(),
        }

        try:
            self._db.insert(table="action_logs", data=record)
            logger.debug(
                "_log_to_database | written | action_id=%s | status=%s",
                action_id, status,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "_log_to_database | failed to write audit record | "
                "action_id=%s | error=%s",
                action_id, exc,
            )

    def __repr__(self) -> str:
        return (
            f"ActionAgent("
            f"registry={list(self._registry.keys())})"
        )