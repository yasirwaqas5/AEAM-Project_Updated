"""
aeam/connectors/health.py

Connector health (Phase F7, SEC-8).

Reports each connector's state from three sources that already exist: the
connector's own self-description, the persisted ``connector_sync_runs`` ledger,
and the ``connector_artifacts`` provenance table. It creates no monitoring
subsystem, no collector of its own, and no polling loop — the report is
computed on read, exactly like the F6 Supervisor's mesh report, and the
Prometheus gauges it publishes are registered in the existing
``aeam/monitoring/metrics.py``.

The honesty rule
----------------
**Unknown is never reported as healthy.** Every field below is either a fact
this module observed or an explicit absence with a reason:

* a connector that has never synced reports ``last_successful_sync: null`` and
  ``sync_status: "never_synced"`` — not "ok";
* a connector that has never authenticated reports ``authenticated: false`` —
  not "assumed";
* staleness is computed only when there IS a last successful sync AND a
  configured staleness threshold; otherwise ``stale`` is ``null`` with a
  reason, because "we cannot tell whether this is stale" and "this is fresh"
  are different answers.

The twelve required fields
--------------------------
``enabled``, ``configured``, ``authenticated``, ``last_successful_sync``,
``last_failed_sync``, ``sync_status``, ``stale``, ``processed_count``,
``skipped_count``, ``changed_count``, ``sync_duration_seconds``, and
``error_reason`` — every one present on every connector, every time, so a
console never has to branch on which fields exist.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from aeam.monitoring.metrics import (
    connector_last_sync_timestamp_seconds,
    connector_sync_artifacts_total,
    connector_up,
)
from aeam.registry.models import SyncRunStatus
from aeam.registry.repositories import (
    ConnectorArtifactRepository,
    ConnectorSyncRunRepository,
    SourceRepository,
)

logger = logging.getLogger(__name__)

#: Default age past which a connector's last successful sync is called stale.
#: 24h rather than minutes because connector sync is operator-triggered in this
#: phase: a connector nobody has synced today is worth flagging, one nobody has
#: synced in the last hour is not.
DEFAULT_STALE_AFTER_SECONDS: int = 86_400


def _age_seconds(iso_timestamp: str | None) -> float | None:
    """Seconds since ``iso_timestamp``, or ``None`` when it cannot be read.

    ``None`` on an unparseable value rather than a substituted age: a bad
    timestamp must surface as "cannot compute" rather than as a freshness claim
    built on a value nobody can verify.
    """
    if not iso_timestamp:
        return None
    text = str(iso_timestamp).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(tz=timezone.utc) - parsed).total_seconds())


class ConnectorHealthReporter:
    """
    Builds the connector health report.

    Args:
        db:                  The shared ``DatabaseClient``.
        registry:            The :class:`~aeam.connectors.registry.ConnectorRegistry`,
                             so "enabled" has one definition.
        stale_after_seconds: Age past which a last successful sync is stale.

    Raises:
        ValueError: If ``db`` or ``registry`` is ``None``.
    """

    def __init__(
        self,
        db: Any,
        registry: Any,
        stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS,
    ) -> None:
        if db is None:
            raise ValueError("db must not be None.")
        if registry is None:
            raise ValueError("registry must not be None.")
        self._db = db
        self._registry = registry
        self._stale_after = max(1, int(stale_after_seconds))
        self._sources = SourceRepository(db)
        self._runs = ConnectorSyncRunRepository(db)
        self._artifacts = ConnectorArtifactRepository(db)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def report(self, publish_metrics: bool = True) -> dict[str, Any]:
        """
        Health for every registered connector source, plus the catalog.

        Returns a dict of the same shape every time, including on a deployment
        with no connectors at all — where ``connectors`` is empty and
        ``framework_enabled`` says why, rather than the caller having to infer
        it from an absence.
        """
        try:
            sources = [
                source for source in self._sources.list_all()
                if self._registry.is_connector_kind(source.kind)
            ]
        except Exception as exc:  # noqa: BLE001
            logger.error("connector health | source listing failed: %s", exc)
            return {
                "framework_enabled": self._framework_enabled(),
                "mock_mode": self._registry.mock_mode,
                "catalog": self._registry.catalog(),
                "connectors": [],
                "summary": {"total": 0, "enabled": 0, "healthy": 0, "unhealthy": 0, "unknown": 0},
                "reason": f"Connector sources could not be read: {exc}",
            }

        connectors = [self._connector_health(source) for source in sources]
        if publish_metrics:
            self._publish(connectors)

        return {
            "framework_enabled": self._framework_enabled(),
            "mock_mode": self._registry.mock_mode,
            "stale_after_seconds": self._stale_after,
            "catalog": self._registry.catalog(),
            "connectors": connectors,
            "summary": self._summarise(connectors),
            "reason": None,
        }

    def health_for(self, source_id: str) -> dict[str, Any] | None:
        """One connector's health, or ``None`` when the source is unknown."""
        source = self._sources.get(source_id)
        if source is None or not self._registry.is_connector_kind(source.kind):
            return None
        return self._connector_health(source)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _framework_enabled(self) -> bool:
        return bool(self._registry.enabled_kinds())

    def _connector_health(self, source: Any) -> dict[str, Any]:
        """All twelve required fields for one connector."""
        enabled = self._registry.is_enabled(source.kind)
        connector = self._registry.build(source) if enabled else None

        # Configuration is checkable without a built connector — and must be,
        # because a disabled connector's configuration is still worth showing so
        # an operator can finish setting it up before enabling it.
        if connector is not None:
            configured, config_reason = connector.validate_config()
            describe = connector.describe()
        else:
            cls = self._registry_class(source.kind)
            required = sorted(getattr(cls, "required_config", ()) or ())
            config = dict(getattr(source, "config", None) or {})
            missing = [key for key in required if not str(config.get(key) or "").strip()]
            configured = not missing
            config_reason = (
                None if configured
                else f"Missing required configuration: {', '.join(missing)}."
            )
            describe = {
                "source_id": source.source_id,
                "kind": source.kind,
                "capability": self._registry.capability_of(source.kind),
                "display_name": getattr(cls, "display_name", source.kind) if cls else source.kind,
                "configured": configured,
                "configuration_reason": config_reason,
                "config_keys": sorted(config),
                "required_config": required,
                "secret_ref": getattr(source, "secret_ref", None),
                "client_mode": "not_built",
            }

        # `authenticated` is only ever True after a real attempt succeeded.
        # A disabled or unbuilt connector is False with a reason — never
        # optimistically true, which is the whole point of SEC-8.
        authenticated = False
        auth_error: str | None = None
        if connector is not None:
            try:
                authenticated = connector.authenticate()
                auth_error = connector.health().get("auth_error")
            except Exception as exc:  # noqa: BLE001
                auth_error = connector.sanitize(exc)
            finally:
                try:
                    connector.close()
                except Exception:  # noqa: BLE001
                    pass
        elif not enabled:
            auth_error = (
                f"Connector is disabled ({self._flag_for(source.kind)}); authentication "
                "was not attempted."
            )
        else:
            auth_error = "Connector could not be constructed; authentication was not attempted."

        latest = self._runs.latest(source.source_id)
        last_success = self._runs.latest_by_status(
            source.source_id, [SyncRunStatus.SUCCEEDED, SyncRunStatus.PARTIAL]
        )
        last_failure = self._runs.latest_by_status(source.source_id, [SyncRunStatus.FAILED])

        sync_status = (
            latest.status if latest is not None else "never_synced"
        )
        success_age = _age_seconds(last_success.finished_at if last_success else None)
        if last_success is None:
            stale: bool | None = None
            stale_reason = (
                "This connector has never completed a sync, so staleness is not "
                "computable. That is not the same as fresh."
            )
        elif success_age is None:
            stale = None
            stale_reason = (
                "The last successful sync's timestamp could not be read, so staleness "
                "is not computable."
            )
        else:
            stale = success_age > self._stale_after
            stale_reason = None

        return {
            **describe,
            "name": source.name,
            "enabled": enabled,
            "flag": self._flag_for(source.kind),
            "configured": configured,
            "configuration_reason": config_reason,
            "authenticated": authenticated,
            "error_reason": auth_error or (latest.error if latest else None),
            "sync_status": sync_status,
            "sync_in_progress": bool(latest and latest.status == SyncRunStatus.RUNNING),
            "last_successful_sync": last_success.finished_at if last_success else None,
            "last_successful_sync_age_seconds": (
                round(success_age, 2) if success_age is not None else None
            ),
            "last_failed_sync": last_failure.finished_at if last_failure else None,
            "last_failed_sync_error": last_failure.error if last_failure else None,
            "stale": stale,
            "stale_reason": stale_reason,
            "sync_duration_seconds": latest.duration_seconds if latest else None,
            "listed_count": latest.listed_count if latest else 0,
            "changed_count": latest.changed_count if latest else 0,
            "processed_count": latest.processed_count if latest else 0,
            "skipped_count": latest.skipped_count if latest else 0,
            "failed_count": latest.failed_count if latest else 0,
            "known_artifacts": self._artifact_count(source.source_id),
            "cursor": source.last_synced_at,
            "source_status": source.status,
        }

    def _registry_class(self, kind: str) -> Any:
        from aeam.connectors.registry import CONNECTOR_CLASSES

        return CONNECTOR_CLASSES.get(kind)

    @staticmethod
    def _flag_for(kind: str) -> str | None:
        from aeam.connectors.registry import FLAG_FOR_KIND

        return FLAG_FOR_KIND.get(kind)

    def _artifact_count(self, source_id: str) -> int:
        try:
            return self._artifacts.count_by_source(source_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("connector health | artifact count failed: %s", exc)
            return 0

    @staticmethod
    def _summarise(connectors: list[dict[str, Any]]) -> dict[str, int]:
        """Counts by state.

        ``unknown`` is a first-class bucket, not folded into either of the
        others: a connector that is enabled and configured but has never synced
        is genuinely neither healthy nor unhealthy, and counting it as healthy
        is exactly the misrepresentation this phase forbids.
        """
        summary = {"total": len(connectors), "enabled": 0, "healthy": 0, "unhealthy": 0, "unknown": 0}
        for entry in connectors:
            if entry["enabled"]:
                summary["enabled"] += 1
            if not entry["enabled"] or not entry["configured"]:
                summary["unknown"] += 1
            elif entry["sync_status"] == SyncRunStatus.FAILED or not entry["authenticated"]:
                summary["unhealthy"] += 1
            elif entry["last_successful_sync"] is None:
                summary["unknown"] += 1
            elif entry["stale"]:
                summary["unhealthy"] += 1
            else:
                summary["healthy"] += 1
        return summary

    def _publish(self, connectors: list[dict[str, Any]]) -> None:
        """Publish connector state onto the EXISTING Prometheus collectors.

        Extending the existing metrics module rather than standing up a second
        monitoring path. A publish failure is logged and swallowed: a metrics
        problem must never make a health read fail, because the health read is
        what an operator uses to diagnose the metrics problem.
        """
        try:
            for entry in connectors:
                labels = {"connector": entry["kind"], "source_id": entry["source_id"]}
                connector_up.labels(**labels).set(
                    1 if (entry["enabled"] and entry["authenticated"]) else 0
                )
                age = entry["last_successful_sync_age_seconds"]
                if age is not None:
                    connector_last_sync_timestamp_seconds.labels(**labels).set(
                        max(0.0, datetime.now(tz=timezone.utc).timestamp() - float(age))
                    )
                for outcome in ("processed", "skipped", "failed"):
                    connector_sync_artifacts_total.labels(
                        connector=entry["kind"], outcome=outcome
                    ).inc(0)
        except Exception as exc:  # noqa: BLE001
            logger.warning("connector health | metric publication failed: %s", exc)

    def __repr__(self) -> str:
        return f"ConnectorHealthReporter(stale_after_seconds={self._stale_after})"
