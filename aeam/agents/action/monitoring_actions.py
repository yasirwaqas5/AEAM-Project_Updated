"""
aeam/agents/action/monitoring_actions.py

Local "increase monitoring" flag integration for the AEAM Action layer.

Records a structured, auditable monitoring directive for the affected metric
— e.g. "watch this metric more closely for the next N minutes." This is
intentionally LOCAL and side-effect-free: AEAM has no integration with a real
external monitoring platform (Datadog, Prometheus alertmanager, etc.), so
this handler does NOT claim to have configured one. It honestly records the
directive as data, persisted via ActionAgent's action_logs audit trail, so
an operator (or a future real integration) can act on it — it never asserts
a side effect that did not occur.

Called exclusively through the ActionAgent registry.

Constraints (same as the rest of the Action layer):
- No retry logic (handled by ActionAgent).
- No LLM usage.
- No decision or Orchestrator logic.
- No external I/O of any kind — this handler is pure/local.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from aeam.monitoring.logging_config import get_logger

logger = get_logger(__name__, agent="action")

# Default monitoring window when the caller does not specify one.
_DEFAULT_WINDOW_MINUTES: int = 60


class MonitoringActions:
    """
    Local, no-I/O "flag for increased monitoring" directive recorder.

    Args:
        secret_manager: Accepted for interface consistency with other
                        integrations but unused — this handler needs no
                        credentials.

    Example::

        mon = MonitoringActions(secret_manager=None)
        result = mon.execute({
            "metric": "latency_ms",
            "window_minutes": 120,
            "reason": "Root cause identified; watching for recurrence.",
        })
        # {"metric": "latency_ms", "window_minutes": 120, "flagged_at": "...", ...}
    """

    def __init__(self, secret_manager: Any = None) -> None:
        """
        Initialise MonitoringActions.

        Args:
            secret_manager: Unused; accepted for constructor-signature
                            consistency with the other Action layer handlers.
        """
        self._secrets = secret_manager

    # ------------------------------------------------------------------
    # ActionAgent registry interface
    # ------------------------------------------------------------------

    def execute(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        Record an increased-monitoring directive for a metric.

        Args:
            params: Dict containing:

                - ``"metric"``         *(optional)* — the metric to flag.
                  Defaults to ``"unknown"``.
                - ``"window_minutes"`` *(optional)* — how long to flag it for.
                  Defaults to 60.
                - ``"reason"``         *(optional)* — why monitoring is
                  being increased.

        Returns:
            Dict::

                {
                    "metric":         str,
                    "window_minutes": int,
                    "reason":         str | None,
                    "flagged_at":     str,  # UTC ISO-8601 timestamp
                }

        Raises:
            ValueError: If ``params`` is not a dict.
        """
        if not isinstance(params, dict):
            raise ValueError(
                f"params must be a dict, got {type(params).__name__!r}."
            )

        metric = str(params.get("metric", "unknown"))
        window_minutes = int(params.get("window_minutes", _DEFAULT_WINDOW_MINUTES))
        reason = params.get("reason")

        result = {
            "metric": metric,
            "window_minutes": window_minutes,
            "reason": reason,
            "flagged_at": datetime.now(tz=timezone.utc).isoformat(),
        }

        logger.info(
            "MonitoringActions.execute | metric=%s | window_minutes=%d",
            metric, window_minutes,
        )

        return result

    def __repr__(self) -> str:
        return "MonitoringActions()"
