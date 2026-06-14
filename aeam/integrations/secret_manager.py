"""
aeam/integrations/secret_manager.py

Secret management for the AEAM system.

Resolves secrets from environment variables with an optional fallback to
settings attributes. Designed for production use and demo safety:

- Never logs secret values.
- Never raises exceptions to callers.
- Missing secrets return a safe default (None) rather than crashing.
- Compatible with AEAM startup without any external service dependency.

Resolution order for ``get_secret(key)``:
1. ``os.environ`` — environment variable matching ``key`` (exact case, then
   upper-cased).
2. ``settings`` attribute — ``getattr(settings, key, None)`` (exact case,
   then upper-cased).
3. ``default`` — caller-supplied fallback (defaults to ``None``).
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger("aeam.integrations.secret_manager")


class SecretManager:
    """
    Environment-backed secret resolver for the AEAM system.

    Resolves secret values from environment variables with a fallback to
    settings attributes. Callers receive ``None`` (or a supplied default)
    when a secret is not found — no exception is ever raised.

    Secret values are never written to logs. Only key names (and resolution
    outcomes) are logged at DEBUG or WARNING level.

    The constructor accepts either:
    - ``settings`` (the primary fallback source), or
    - ``project_id`` (for future Google Secret Manager integration), or both.

    Args:
        settings:   Application settings object. Used as a secondary lookup
                    source when a key is not found in the environment.
                    May be ``None`` — environment variables are always
                    checked regardless.
        project_id: Optional GCP project ID (reserved for future use).
                    Currently only stored, not actively used.

    Example::

        manager = SecretManager(settings=settings)

        token = manager.get_secret("SLACK_BOT_TOKEN")
        if token is None:
            logger.warning("Slack token not configured.")

        if manager.has_secret("JIRA_API_TOKEN"):
            jira = JiraActions(secret_manager=manager)
    """

    def __init__(
        self,
        settings: Any = None,
        project_id: str | None = None,
    ) -> None:
        """
        Initialise the SecretManager.

        Args:
            settings:   Settings object used as a fallback secret source.
                        May be ``None`` — environment variables are always
                        checked regardless.
            project_id: Optional GCP project ID (reserved).
        """
        self._settings: Any = settings
        self._project_id: str | None = project_id
        logger.info(
            "SecretManager initialised | settings_fallback=%s, project_id=%s",
            settings is not None,
            project_id is not None,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_secret(self, key: str, default: Any = None) -> Any:
        """
        Resolve a secret value by ``key``.

        Resolution order:
        1. ``os.environ[key]`` — exact case.
        2. ``os.environ[key.upper()]`` — upper-cased key.
        3. ``getattr(settings, key)`` — exact case settings attribute.
        4. ``getattr(settings, key.upper())`` — upper-cased settings attribute.
        5. ``default`` — caller-supplied fallback (``None`` by default).

        Args:
            key:     Secret name / environment variable key. Case-insensitive
                     (both original case and upper-case are tried).
            default: Value returned when the secret is not found anywhere.
                     Defaults to ``None``.

        Returns:
            The resolved secret value, or ``default`` if not found.
            The return type mirrors whatever is stored (typically ``str``
            for environment variables, but may be any type from settings).

        Note:
            The resolved value is never logged. Only the key name and
            resolution outcome (found/not-found) are written to the log.

        Example::

            token = manager.get_secret("SLACK_BOT_TOKEN", default="")
            api_key = manager.get_secret("JIRA_API_TOKEN")
        """
        if not key or not key.strip():
            logger.warning("get_secret | empty key supplied — returning default.")
            return default

        # 1. Environment — exact case.
        value = os.environ.get(key)
        if value is not None:
            logger.debug("get_secret | key=%r | resolved from env (exact case)", key)
            return value

        # 2. Environment — upper-cased.
        key_upper = key.upper()
        if key_upper != key:
            value = os.environ.get(key_upper)
            if value is not None:
                logger.debug("get_secret | key=%r | resolved from env (upper case)", key)
                return value

        # 3. Settings — exact case.
        if self._settings is not None:
            value = self._get_from_settings(key)
            if value is not None:
                logger.debug("get_secret | key=%r | resolved from settings (exact case)", key)
                return value

            # 4. Settings — upper-cased.
            if key_upper != key:
                value = self._get_from_settings(key_upper)
                if value is not None:
                    logger.debug(
                        "get_secret | key=%r | resolved from settings (upper case)", key
                    )
                    return value

        # 5. Default.
        logger.warning("get_secret | key=%r | not found — returning default.", key)
        return default

    def get(self, key: str, default: Any = None) -> Any:
        """
        Alias for :meth:`get_secret` to maintain compatibility with code
        that expects ``secret_manager.get(...)`` (e.g., legacy webhook_actions).

        Args:
            key:     Secret name to retrieve.
            default: Value returned if the secret is not found.

        Returns:
            The resolved secret value, or ``default``.
        """
        return self.get_secret(key, default)

    def has_secret(self, key: str) -> bool:
        """
        Return ``True`` if a non-empty value exists for ``key``.

        Applies the same resolution order as :meth:`get_secret`. A value is
        considered present if it is not ``None`` and not an empty string after
        stripping whitespace.

        Args:
            key: Secret name to check.

        Returns:
            ``True``  — a non-empty value was found.
            ``False`` — the key is absent, empty, or whitespace-only.

        Example::

            if manager.has_secret("GOOGLE_SHEETS_SA_CREDENTIALS"):
                connector = SheetsConnector(settings=settings, secret_manager=manager)
        """
        value = self.get_secret(key, default=None)
        present = value is not None and str(value).strip() != ""
        logger.debug("has_secret | key=%r | present=%s", key, present)
        return present

    def health_check(self) -> dict[str, str]:
        """
        Return the health status of the SecretManager.

        The manager is always considered ``"healthy"`` as long as it is
        initialised (it requires no external service). A ``"degraded"``
        status is returned only if an unexpected internal error prevents
        the check from completing.

        Returns:
            Dict with ``"service"`` and ``"status"`` keys::

                {"service": "secret_manager", "status": "healthy"}

            or on unexpected error::

                {"service": "secret_manager", "status": "degraded"}

        Example::

            status = manager.health_check()
            if status["status"] != "healthy":
                logger.error("Secret manager degraded: %s", status)
        """
        try:
            # Basic self-check: can we read from os.environ?
            _ = len(os.environ)
            logger.debug("health_check | status=healthy")
            return {"service": "secret_manager", "status": "healthy"}
        except Exception as exc:  # noqa: BLE001
            logger.error("health_check | unexpected error: %s", exc)
            return {"service": "secret_manager", "status": "degraded"}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_from_settings(self, key: str) -> Any:
        """
        Safely retrieve ``key`` from the settings object.

        Uses ``getattr`` with a sentinel default so that attributes set to
        ``None`` are treated as absent (consistent with environment variable
        behaviour where unset == None).

        Args:
            key: Attribute name to look up on ``self._settings``.

        Returns:
            The attribute value if present and not ``None``; ``None`` otherwise.
        """
        _SENTINEL = object()
        try:
            value = getattr(self._settings, key, _SENTINEL)
            if value is _SENTINEL or value is None:
                return None
            # Treat empty strings as absent.
            if isinstance(value, str) and not value.strip():
                return None
            return value
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "_get_from_settings | key=%r | error reading settings: %s", key, exc
            )
            return None

    def __repr__(self) -> str:
        return (
            f"SecretManager("
            f"settings={type(self._settings).__name__ if self._settings is not None else None}, "
            f"project_id={self._project_id!r})"
        )