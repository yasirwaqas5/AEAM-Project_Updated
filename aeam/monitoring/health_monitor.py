"""
aeam/monitoring/health_monitor.py

System health tracking for the AEAM monitoring layer.

Evaluates the availability and configuration of core system components
(database, Redis, vector DB, LLM) based on injected settings. Returns
structured health status dicts consumed by the ``/health`` FastAPI
endpoint in ``aeam/main.py``.

No external API calls are made — health is inferred from configuration
presence. Components with missing or disabled configuration are reported
as ``"disabled"`` rather than ``"unhealthy"``, and do not degrade the
overall system status.
"""

from __future__ import annotations

import logging
from typing import Any

from aeam.config.settings import Settings

logger = logging.getLogger(__name__)

# Status string constants.
_HEALTHY: str = "healthy"
_DISABLED: str = "disabled"
_UNHEALTHY: str = "unhealthy"


class HealthMonitor:
    """
    Evaluates the health of AEAM system components from configuration.

    Each ``check_*`` method returns one of three status strings:
    - ``"healthy"``  — component is configured and expected to be available.
    - ``"disabled"`` — component is not configured or explicitly disabled.
    - ``"unhealthy"`` — component is expected but misconfigured.

    :meth:`overall_status` aggregates all checks. The overall status is
    ``"degraded"`` if any individual check returns ``"unhealthy"``;
    ``"disabled"`` components do not degrade the system.

    Args:
        settings: Injected :class:`~aeam.config.settings.Settings` instance.

    Raises:
        ValueError: If ``settings`` is None.

    Example::

        monitor = HealthMonitor(settings=settings)
        status = monitor.overall_status()
        # {
        #   "status": "healthy",
        #   "checks": {
        #       "database":  "healthy",
        #       "redis":     "healthy",
        #       "vector_db": "healthy",
        #       "llm":       "disabled",
        #   }
        # }
    """

    def __init__(self, settings: Settings) -> None:
        """
        Initialise HealthMonitor with injected settings.

        Args:
            settings: Application settings instance. Must not be None.

        Raises:
            ValueError: If ``settings`` is None.
        """
        if settings is None:
            raise ValueError("settings must not be None.")
        self._settings: Settings = settings

    # ------------------------------------------------------------------
    # Individual component checks
    # ------------------------------------------------------------------

    def check_database(self) -> str:
        """
        Return the database health status based on ``DATABASE_URL``.

        Returns:
            ``"healthy"``  — ``DATABASE_URL`` is present and non-empty.
            ``"unhealthy"`` — ``DATABASE_URL`` is missing or empty.

        Note:
            No actual connection is attempted. Status reflects
            configuration presence only.
        """
        url: str = str(self._settings.DATABASE_URL or "").strip()
        if url:
            logger.debug("check_database | healthy | url configured")
            return _HEALTHY

        logger.warning("check_database | unhealthy | DATABASE_URL is missing")
        return _UNHEALTHY

    def check_redis(self) -> str:
        """
        Return the Redis health status based on ``REDIS_URL``.

        Returns:
            ``"disabled"`` — ``REDIS_URL`` is empty or not set.
            ``"healthy"``  — ``REDIS_URL`` is present and non-empty.

        Note:
            No actual connection is attempted. Status reflects
            configuration presence only.
        """
        url: str = str(self._settings.REDIS_URL or "").strip()
        if not url:
            logger.debug("check_redis | disabled | REDIS_URL not configured")
            return _DISABLED

        logger.debug("check_redis | healthy | url configured")
        return _HEALTHY

    def check_vector_db(self) -> str:
        """
        Return the vector database health status based on ``VECTOR_DB_URL``.

        Returns:
            ``"disabled"`` — ``VECTOR_DB_URL`` is empty or not set.
            ``"healthy"``  — ``VECTOR_DB_URL`` is present and non-empty.

        Note:
            No actual connection is attempted. Status reflects
            configuration presence only.
        """
        url: str = str(self._settings.VECTOR_DB_URL or "").strip()
        if not url:
            logger.debug("check_vector_db | disabled | VECTOR_DB_URL not configured")
            return _DISABLED

        logger.debug("check_vector_db | healthy | url configured")
        return _HEALTHY

    def check_llm(self) -> str:
        """
        Return the LLM health status based on the ``LLM_ENABLED`` flag.

        Returns:
            ``"disabled"`` — ``LLM_ENABLED`` is ``False``.
            ``"healthy"``  — ``LLM_ENABLED`` is ``True``.

        Note:
            No LLM endpoint is contacted. Status reflects the feature
            flag only.
        """
        if not self._settings.LLM_ENABLED:
            logger.debug("check_llm | disabled | LLM_ENABLED=false")
            return _DISABLED

        logger.debug("check_llm | healthy | LLM_ENABLED=true")
        return _HEALTHY

    # ------------------------------------------------------------------
    # Aggregate status
    # ------------------------------------------------------------------

    def overall_status(self) -> dict[str, Any]:
        """
        Aggregate all component checks into a single health status dict.

        Runs all four component checks and derives the overall system
        status:
        - ``"healthy"``  — all checks return ``"healthy"`` or
          ``"disabled"`` (disabled components do not degrade the system).
        - ``"degraded"`` — at least one check returns ``"unhealthy"``.

        Returns:
            Dict::

                {
                    "status": "healthy" | "degraded",
                    "checks": {
                        "database":  "healthy" | "unhealthy",
                        "redis":     "healthy" | "disabled",
                        "vector_db": "healthy" | "disabled",
                        "llm":       "healthy" | "disabled",
                    }
                }

        Example::

            monitor = HealthMonitor(settings=settings)
            result = monitor.overall_status()
            if result["status"] == "degraded":
                alert_manager.trigger(result)
        """
        checks: dict[str, str] = {
            "database":  self.check_database(),
            "redis":     self.check_redis(),
            "vector_db": self.check_vector_db(),
            "llm":       self.check_llm(),
        }

        # Only "unhealthy" degrades the system — "disabled" is acceptable.
        is_degraded: bool = any(
            status == _UNHEALTHY for status in checks.values()
        )
        status: str = "degraded" if is_degraded else "healthy"

        logger.info(
            "HealthMonitor.overall_status | status=%s | checks=%s",
            status, checks,
        )

        return {
            "status": status,
            "checks": checks,
        }

    def __repr__(self) -> str:
        return (
            f"HealthMonitor("
            f"environment={self._settings.ENVIRONMENT!r})"
        )