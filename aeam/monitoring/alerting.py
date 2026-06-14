"""
aeam/monitoring/alerting.py

Threshold-based alert generation for the AEAM monitoring layer.

Evaluates system health metrics against configured thresholds and returns
structured alert dicts. Never sends alerts externally — callers (e.g. the
Orchestrator or a monitoring loop) are responsible for forwarding alerts
to ActionAgent if delivery is required.

No external API calls. No LLM usage. Pure deterministic logic.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# ============================================================
# Threshold constants
# ============================================================

_INCIDENT_COUNT_THRESHOLD: int = 10
_FAILURE_RATE_THRESHOLD: float = 0.3
_LATENCY_THRESHOLD_SECONDS: float = 5.0


class AlertManager:
    """
    Evaluates system health metrics against thresholds and returns
    structured alert dicts.

    All methods return a dict when the threshold is breached, or ``None``
    when the metric is within acceptable bounds. No external calls are
    made — the returned dict is the only output.

    Alert format::

        {
            "type":      str,   # alert category identifier
            "severity":  str,   # always "HIGH" for threshold breaches
            "message":   str,   # human-readable description
            "timestamp": str,   # UTC ISO 8601
        }

    Example::

        manager = AlertManager()

        alert = manager.check_incident_threshold(active_incidents=12)
        if alert:
            action_agent.execute("slack", {"message": alert["message"], ...})
    """

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check_incident_threshold(
        self,
        active_incidents: int,
    ) -> dict[str, Any] | None:
        """
        Alert if the number of active incidents exceeds the threshold.

        Threshold: ``> 10`` active incidents.

        Args:
            active_incidents: Current count of concurrently active
                              incidents being investigated.

        Returns:
            Alert dict if ``active_incidents > 10``, otherwise ``None``.

        Example::

            alert = manager.check_incident_threshold(active_incidents=15)
            # {
            #   "type":      "high_incident_volume",
            #   "severity":  "HIGH",
            #   "message":   "Active incidents (15) exceed threshold (10).",
            #   "timestamp": "2024-01-15T14:32:00.000000+00:00",
            # }
        """
        if active_incidents > _INCIDENT_COUNT_THRESHOLD:
            alert = self._build_alert(
                alert_type="high_incident_volume",
                message=(
                    f"Active incidents ({active_incidents}) exceed "
                    f"threshold ({_INCIDENT_COUNT_THRESHOLD})."
                ),
            )
            logger.warning(
                "AlertManager | HIGH_INCIDENT_VOLUME | count=%d | threshold=%d",
                active_incidents, _INCIDENT_COUNT_THRESHOLD,
            )
            return alert

        logger.debug(
            "AlertManager.check_incident_threshold | OK | count=%d",
            active_incidents,
        )
        return None

    def check_failure_rate(
        self,
        failures: int,
        total: int,
    ) -> dict[str, Any] | None:
        """
        Alert if the action failure rate exceeds the threshold.

        Threshold: failure rate ``> 0.3`` (30%).

        Args:
            failures: Number of failed actions in the measurement window.
            total:    Total number of actions attempted in the same window.

        Returns:
            Alert dict if ``failures / total > 0.3``, otherwise ``None``.
            Returns ``None`` (no alert) when ``total`` is zero to avoid
            division by zero.

        Example::

            alert = manager.check_failure_rate(failures=4, total=10)
            # {
            #   "type":      "high_failure_rate",
            #   "severity":  "HIGH",
            #   "message":   "Action failure rate (40.0%) exceeds threshold (30%).",
            #   "timestamp": "...",
            # }
        """
        if total <= 0:
            logger.debug(
                "AlertManager.check_failure_rate | total=0 | skipping."
            )
            return None

        rate: float = failures / total

        if rate > _FAILURE_RATE_THRESHOLD:
            alert = self._build_alert(
                alert_type="high_failure_rate",
                message=(
                    f"Action failure rate ({rate * 100:.1f}%) exceeds "
                    f"threshold ({int(_FAILURE_RATE_THRESHOLD * 100)}%)."
                ),
            )
            logger.warning(
                "AlertManager | HIGH_FAILURE_RATE | failures=%d | total=%d | "
                "rate=%.2f | threshold=%.2f",
                failures, total, rate, _FAILURE_RATE_THRESHOLD,
            )
            return alert

        logger.debug(
            "AlertManager.check_failure_rate | OK | rate=%.2f", rate
        )
        return None

    def check_latency(
        self,
        latency_seconds: float,
    ) -> dict[str, Any] | None:
        """
        Alert if investigation or agent latency exceeds the threshold.

        Threshold: ``> 5.0`` seconds.

        Args:
            latency_seconds: Measured latency in seconds (e.g. the value
                             returned by :func:`~aeam.monitoring.metrics.end_timer`).

        Returns:
            Alert dict if ``latency_seconds > 5.0``, otherwise ``None``.

        Example::

            alert = manager.check_latency(latency_seconds=7.3)
            # {
            #   "type":      "high_latency",
            #   "severity":  "HIGH",
            #   "message":   "Latency (7.30s) exceeds threshold (5.0s).",
            #   "timestamp": "...",
            # }
        """
        if latency_seconds > _LATENCY_THRESHOLD_SECONDS:
            alert = self._build_alert(
                alert_type="high_latency",
                message=(
                    f"Latency ({latency_seconds:.2f}s) exceeds "
                    f"threshold ({_LATENCY_THRESHOLD_SECONDS}s)."
                ),
            )
            logger.warning(
                "AlertManager | HIGH_LATENCY | latency=%.2fs | threshold=%.1fs",
                latency_seconds, _LATENCY_THRESHOLD_SECONDS,
            )
            return alert

        logger.debug(
            "AlertManager.check_latency | OK | latency=%.2fs", latency_seconds
        )
        return None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_alert(
        alert_type: str,
        message: str,
        severity: str = "HIGH",
    ) -> dict[str, Any]:
        """
        Construct a standardised alert dict.

        Args:
            alert_type: Short category string for the alert
                        (e.g. ``"high_latency"``).
            message:    Human-readable description of the breach.
            severity:   Alert severity level. Defaults to ``"HIGH"``.

        Returns:
            Alert dict with ``type``, ``severity``, ``message``,
            and ``timestamp`` fields.
        """
        return {
            "type":      alert_type,
            "severity":  severity,
            "message":   message,
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        }

    def __repr__(self) -> str:
        return (
            f"AlertManager("
            f"incident_threshold={_INCIDENT_COUNT_THRESHOLD}, "
            f"failure_rate_threshold={_FAILURE_RATE_THRESHOLD}, "
            f"latency_threshold={_LATENCY_THRESHOLD_SECONDS}s)"
        )