"""
aeam/agents/action/diagnostics_actions.py

Local diagnostics-capture integration for the AEAM Action layer.

Captures a structured snapshot of the incident's known fields at the moment
of execution. This is an intentionally LOCAL, side-effect-free action: it
makes no external API calls, contacts no monitoring platform, and mutates no
external system. It exists so "capture diagnostics" (and, for business
incidents, "capture analytics snapshot") is a real, auditable, reversible
runbook step rather than a claim of an action that never actually happened —
the snapshot it returns is genuinely captured and is persisted verbatim via
ActionAgent's existing action_logs audit trail.

Called exclusively through the ActionAgent registry, like every other
integration in this package.

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


class DiagnosticsActions:
    """
    Local, no-I/O diagnostics/analytics snapshot capture.

    Takes whatever incident context the caller supplies and returns it back
    as a timestamped, structured snapshot dict. The "capture" is genuine: the
    returned snapshot is exactly what gets persisted to ``action_logs`` by
    ActionAgent, so it is real, inspectable evidence — not a placeholder.

    Args:
        secret_manager: Accepted for interface consistency with other
                        integrations (:class:`~aeam.agents.action.action_agent.ActionAgent`
                        constructs every handler the same way) but unused —
                        this handler needs no credentials.

    Example::

        diag = DiagnosticsActions(secret_manager=None)
        result = diag.execute({
            "kind": "diagnostics",
            "incident_id": "INC-42",
            "metric": "latency_ms",
            "current_value": 1500.0,
            "expected_value": 300.0,
        })
        # {"kind": "diagnostics", "captured_at": "...", "snapshot": {...}}
    """

    def __init__(self, secret_manager: Any = None) -> None:
        """
        Initialise DiagnosticsActions.

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
        Capture a structured diagnostic (or analytics) snapshot.

        Args:
            params: Dict of arbitrary incident-context fields to capture.
                    An optional ``"kind"`` key (defaults to ``"diagnostics"``)
                    labels the snapshot — pass ``"analytics_snapshot"`` for
                    business-metric incidents so the audit trail and UI can
                    distinguish the two without needing a second handler.

        Returns:
            Dict::

                {
                    "kind":        str,   # "diagnostics" | "analytics_snapshot" | caller-supplied
                    "captured_at": str,   # UTC ISO-8601 timestamp
                    "snapshot":    dict,  # all params except "kind", verbatim
                }

        Raises:
            ValueError: If ``params`` is not a dict.
        """
        if not isinstance(params, dict):
            raise ValueError(
                f"params must be a dict, got {type(params).__name__!r}."
            )

        kind = str(params.get("kind", "diagnostics"))
        snapshot = {k: v for k, v in params.items() if k != "kind"}

        result = {
            "kind": kind,
            "captured_at": datetime.now(tz=timezone.utc).isoformat(),
            "snapshot": snapshot,
        }

        logger.info(
            "DiagnosticsActions.execute | kind=%s | fields=%d",
            kind, len(snapshot),
        )

        return result

    def __repr__(self) -> str:
        return "DiagnosticsActions()"
