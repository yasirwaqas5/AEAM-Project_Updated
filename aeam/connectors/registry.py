"""
aeam/connectors/registry.py

The connector registry (Phase F7).

One table mapping ``SourceKind`` → connector class, one per-connector flag, and
one factory. Everything that needs to know "which connectors exist and are
enabled" asks here, so the answer has a single definition: the sync engine, the
health reporter, the API, the KPI composition, and the console all read the
same registry.

Flag gating (the F7 rollback posture)
-------------------------------------
Every connector is independently gated by ``CONNECTOR_<KIND>_ENABLED``, and all
of them default **off**. A fresh deployment therefore has zero connectors
enabled and behaves exactly as it did before this phase: upload plus Google
Sheets, unchanged (COMPAT-4). With no connectors registered the framework is
inert — :meth:`ConnectorRegistry.build` returns ``None``, the sync engine finds
no connector-kind sources, and the health surface honestly reports an empty
roster rather than an error.

Mock mode
---------
``CONNECTOR_MOCK_MODE=true`` makes every connector use the deterministic in-repo
client from :mod:`aeam.connectors.enterprise.mock`. That exists so an operator
can stand the framework up and watch a real sync run end-to-end — real
listing, real change detection, real ingestion, real retrieval — before any
credential exists. It is honest about itself: health reports
``client_mode: "injected"`` and ``mock_mode: true``, so nobody can mistake a
mock sync for a tenant sync.
"""

from __future__ import annotations

import logging
from typing import Any

from aeam.connectors.base import EnterpriseConnector
from aeam.connectors.enterprise.bigquery import BigQueryConnector
from aeam.connectors.enterprise.confluence import ConfluenceConnector
from aeam.connectors.enterprise.github import GitHubConnector
from aeam.connectors.enterprise.google_workspace import GoogleWorkspaceConnector
from aeam.connectors.enterprise.mock import (
    DIALECTS,
    MockDocumentClient,
    MockMetricsClient,
    default_document_fixture,
    default_metrics_fixture,
)
from aeam.connectors.enterprise.salesforce import SalesforceConnector
from aeam.connectors.enterprise.sap import SAPConnector
from aeam.connectors.enterprise.sharepoint import SharePointConnector
from aeam.connectors.enterprise.snowflake import SnowflakeConnector
from aeam.registry.models import ConnectorCapability, SourceKind

logger = logging.getLogger(__name__)

#: ``SourceKind`` → connector class. The ONE place the eight implementations
#: are named. A ninth connector is one entry here plus one module.
CONNECTOR_CLASSES: dict[str, type[EnterpriseConnector]] = {
    SourceKind.SHAREPOINT: SharePointConnector,
    SourceKind.CONFLUENCE: ConfluenceConnector,
    SourceKind.GITHUB: GitHubConnector,
    SourceKind.GOOGLE_WORKSPACE: GoogleWorkspaceConnector,
    SourceKind.SAP: SAPConnector,
    SourceKind.SALESFORCE: SalesforceConnector,
    SourceKind.SNOWFLAKE: SnowflakeConnector,
    SourceKind.BIGQUERY: BigQueryConnector,
}

#: ``SourceKind`` → the settings flag that enables it. Derived mechanically from
#: the kind so a new connector cannot accidentally ship without a flag.
FLAG_FOR_KIND: dict[str, str] = {
    kind: f"CONNECTOR_{kind.upper()}_ENABLED" for kind in CONNECTOR_CLASSES
}


class ConnectorRegistry:
    """
    Builds connectors for ``sources`` rows, subject to per-connector flags.

    Args:
        settings:       Application settings — read for the per-connector flags
                        and ``CONNECTOR_MOCK_MODE``.
        secret_manager: The shared
                        :class:`~aeam.integrations.secret_manager.SecretManager`.
                        Passed to every connector; it is the only credential
                        path any of them has (SEC-5).
        classes:        Override the kind→class table. Exists for tests, so a
                        deliberately-failing connector can be registered
                        without touching the real table.
    """

    def __init__(
        self,
        settings: Any,
        secret_manager: Any = None,
        classes: dict[str, type[EnterpriseConnector]] | None = None,
    ) -> None:
        self._settings = settings
        self._secret_manager = secret_manager
        self._classes = dict(classes if classes is not None else CONNECTOR_CLASSES)

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def is_connector_kind(self, kind: str | None) -> bool:
        """Whether a ``sources.kind`` has a connector implementation.

        ``upload`` and ``gsheet`` are deliberately false: they are not
        connectors, and treating them as unimplemented connectors would report
        two permanent failures on every health read.
        """
        return bool(kind) and kind in self._classes

    def is_enabled(self, kind: str) -> bool:
        """Whether this connector kind is enabled by configuration.

        ``CONNECTORS_ENABLED`` is a master switch: off, every connector is off
        regardless of its own flag, which is the one-line way to restore the
        pre-F7 posture.
        """
        if not bool(getattr(self._settings, "CONNECTORS_ENABLED", False)):
            return False
        flag = FLAG_FOR_KIND.get(kind)
        return bool(flag) and bool(getattr(self._settings, flag, False))

    @property
    def mock_mode(self) -> bool:
        return bool(getattr(self._settings, "CONNECTOR_MOCK_MODE", False))

    def known_kinds(self) -> list[str]:
        return sorted(self._classes)

    def enabled_kinds(self) -> list[str]:
        return sorted(kind for kind in self._classes if self.is_enabled(kind))

    def capability_of(self, kind: str) -> str | None:
        cls = self._classes.get(kind)
        return getattr(cls, "capability", None) if cls else None

    def catalog(self) -> list[dict[str, Any]]:
        """Every known connector kind with its capability and flag state.

        Lets the console show all eight connectors — including the ones nobody
        has configured — so an operator can see what is available rather than
        having to read the source to find out.
        """
        return [
            {
                "kind": kind,
                "display_name": getattr(cls, "display_name", kind),
                "capability": getattr(cls, "capability", None),
                "required_config": sorted(getattr(cls, "required_config", ())),
                "flag": FLAG_FOR_KIND.get(kind),
                "enabled": self.is_enabled(kind),
            }
            for kind, cls in sorted(self._classes.items())
        ]

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def build(self, source: Any) -> EnterpriseConnector | None:
        """
        Build the connector for one ``sources`` row, or ``None``.

        ``None`` — never an exception — when the kind has no implementation, is
        disabled, or the class cannot be constructed. The caller (the sync
        engine, the health reporter) turns that into an honest "not enabled"
        rather than a failure, because a disabled connector is a configuration
        state and not an error.
        """
        kind = getattr(source, "kind", None)
        cls = self._classes.get(kind or "")
        if cls is None:
            return None
        if not self.is_enabled(kind):
            logger.debug("connector registry | kind=%s is disabled — not built", kind)
            return None
        try:
            return cls(
                source_id=source.source_id,
                config=dict(getattr(source, "config", None) or {}),
                secret_manager=self._secret_manager,
                secret_ref=getattr(source, "secret_ref", None),
                client=self._mock_client_for(cls, source) if self.mock_mode else None,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "connector registry | build failed | kind=%s | source_id=%s | %s",
                kind, getattr(source, "source_id", "?"), exc,
            )
            return None

    def _mock_client_for(self, cls: type[EnterpriseConnector], source: Any) -> Any:
        """The deterministic in-repo client for this connector's capability.

        Chosen by declared capability, not by class, so a ninth connector gets a
        working mock with no change here.
        """
        if getattr(cls, "capability", None) == ConnectorCapability.METRICS:
            selector = str(
                (getattr(source, "config", None) or {}).get(
                    getattr(cls, "selector_config_key", "selector")
                )
                or "mock-selector"
            )
            return default_metrics_fixture(selector)
        # The mock speaks the VENDOR's field naming, not a generic one, so the
        # connector's own field map does the same translation work it would do
        # against the real API.
        return default_document_fixture(dialect=DIALECTS.get(getattr(cls, "kind", "")))

    # ------------------------------------------------------------------
    # KPI composition (metrics connectors)
    # ------------------------------------------------------------------

    def build_metric_sources(self, sources: list[Any]) -> list[EnterpriseConnector]:
        """
        Every enabled METRICS connector, ready to join ``CompositeKPISource``.

        The composition root adds each of these as a member with its own
        selector. ``MonitorAgent`` receives one ``CompositeKPISource`` and never
        learns any of them exist — no agent change, no second KPI path.

        A connector that fails to authenticate is still returned, deliberately:
        its ``fetch_rows`` yields an empty list (never raises), which is the
        same no-op ``MonitorAgent`` already handles, and the failure is visible
        in connector health. Excluding it would make a broken connector
        indistinguishable from an absent one.
        """
        built: list[EnterpriseConnector] = []
        for source in sources:
            if self.capability_of(getattr(source, "kind", "")) != ConnectorCapability.METRICS:
                continue
            connector = self.build(source)
            if connector is None:
                continue
            try:
                connector.authenticate()
            except Exception as exc:  # noqa: BLE001
                # Authentication failure must not prevent the member from
                # joining: it will report zero rows and an honest health error,
                # which is strictly better than KPI collection never seeing it.
                logger.warning(
                    "connector registry | %s authentication failed at composition: %s",
                    connector.kind, connector.sanitize(exc),
                )
            built.append(connector)
        return built

    def __repr__(self) -> str:
        return (
            f"ConnectorRegistry(known={len(self._classes)}, "
            f"enabled={len(self.enabled_kinds())}, mock_mode={self.mock_mode})"
        )


__all__ = [
    "CONNECTOR_CLASSES",
    "FLAG_FOR_KIND",
    "ConnectorRegistry",
    "MockDocumentClient",
    "MockMetricsClient",
]
