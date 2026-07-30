"""
aeam/connectors/enterprise/metrics.py

Shared scaffolding for the four metric-yielding connectors (Phase F7).

SAP, Salesforce, Snowflake, and BigQuery are queried the same way from AEAM's
perspective: name a report/query/table, get rows back. They differ in the
vendor SDK, the credential shape, and how a "selector" maps onto their query
language — and nothing else. So the query loop lives here once, and each
connector supplies its differences.

The contract these connectors satisfy is not a new one. ``fetch_rows(selector)``
is the **existing** ``KPIRowSource`` protocol that ``SheetsConnector`` and
``DatasetKPISource`` already implement, so a metrics connector is added to
``CompositeKPISource`` as an ordinary member and ``MonitorAgent`` never learns
it exists. No second detector, no second KPI path, no agent change (ENG-6).

``fetch_rows`` never raises
---------------------------
It runs on ``MonitorAgent``'s cycle, alongside every other KPI source. An
exception there would take down KPI collection for *all* sources, which is
precisely the failure isolation this phase promises not to break. So every
failure inside :meth:`QueryMetricsConnector.fetch_rows` is caught, sanitised,
counted, and turned into an empty list — the same "no rows" case
``MonitorAgent._run_cycle`` already handles as a no-op. The failure is not
hidden: it is recorded on the connector and surfaces in connector health.

Vendor SDKs are not dependencies
--------------------------------
``snowflake-connector-python``, ``google-cloud-bigquery``,
``simple-salesforce``, and ``pyrfc`` are not in ``requirements.txt``, and this
phase does not add them — gating CI never calls those services, so shipping
four heavyweight SDKs to satisfy tests that mock them would be waste. A
connector whose SDK is absent reports ``authenticated: false`` with that as
the stated reason. It does not silently degrade to zero rows and it does not
claim health it cannot verify (SEC-8). Injecting a client — a mock, or a
real SDK client an operator constructs — makes it fully functional.
"""

from __future__ import annotations

import logging
from typing import Any

from aeam.connectors.base import ConnectorArtifactRef, EnterpriseConnector
from aeam.registry.models import ConnectorCapability

logger = logging.getLogger(__name__)

#: Bound on rows one selector may return per cycle. MonitorAgent re-reads every
#: source on every cycle, so an unbounded warehouse query would dominate the
#: monitoring loop's cost. Truncation is reported on the connector's health
#: rather than silently applied.
DEFAULT_ROW_LIMIT: int = 5000


class QueryMetricsConnector(EnterpriseConnector):
    """
    Base for every metric-yielding connector.

    Subclasses declare ``kind``, ``display_name``, ``required_config``, the
    name of the SDK they need, and how to build a real client. They implement
    no querying, no row normalisation, and no failure handling — all of that is
    here, once.
    """

    capability = ConnectorCapability.METRICS
    #: Import name of the vendor SDK a real client needs. Reported verbatim in
    #: the auth error so an operator knows exactly what to install.
    sdk_module: str = ""
    #: Secret key checked when ``sources.secret_ref`` is unset.
    default_secret_key: str = ""
    #: ``sources.config`` key naming the default selector (report id, table,
    #: saved query) for this connector.
    selector_config_key: str = "selector"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        #: Observed row-fetch outcomes, so a connector that is quietly failing
        #: on MonitorAgent's cycle is visible in health rather than looking
        #: like a metric with no data.
        self._fetch_failures: int = 0
        self._fetch_successes: int = 0
        self._last_fetch_error: str | None = None
        self._last_row_count: int | None = None
        self._truncated: bool = False

    # ------------------------------------------------------------------
    # Credentials
    # ------------------------------------------------------------------

    def authenticate(self) -> bool:
        configured, reason = self.validate_config()
        if not configured:
            self._authenticated = False
            self._auth_error = reason
            return False

        if self._client is None:
            secret = self._resolve_secret() or self._resolve_secret(self.default_secret_key)
            if not secret:
                self._authenticated = False
                self._auth_error = (
                    f"No credential available. Set "
                    f"{self._secret_ref or self.default_secret_key!r} so SecretManager "
                    "can resolve it, or inject a client."
                )
                return False
            try:
                self._client = self._build_client(secret)
            except Exception as exc:  # noqa: BLE001
                self._authenticated = False
                self._auth_error = self.sanitize(str(exc))
                return False

        try:
            self._client.ping()
        except Exception as exc:  # noqa: BLE001
            self._authenticated = False
            self._auth_error = self.sanitize(f"upstream rejected the connection: {exc}")
            return False

        self._authenticated = True
        self._auth_error = None
        return True

    def _build_client(self, secret: str) -> Any:
        """Construct the real vendor client.

        The default states the honest situation: the SDK this connector needs
        is not a platform dependency, so without an injected client there is
        nothing to connect with. Reported as an auth error with the module
        name, never as a silent zero-row success.
        """
        raise RuntimeError(
            f"{type(self).__name__} needs the {self.sdk_module!r} SDK, which is not a "
            "platform dependency. Install it and inject a client, or run this "
            "connector in mock mode. No rows are reported and the connector is "
            "not marked authenticated."
        )

    # ------------------------------------------------------------------
    # Capability: METRICS — the existing KPIRowSource protocol
    # ------------------------------------------------------------------

    def default_selector(self) -> str:
        """The selector to use when the caller supplies none.

        ``CompositeKPISource`` passes MonitorAgent's incoming selector through
        for a pass-through member, and that selector is a *sheet name* by
        origin — meaningless to a warehouse. So a metrics connector falls back
        to its configured report/table, which is the only selector it can
        honestly answer.
        """
        return str(self._config.get(self.selector_config_key) or "").strip()

    def fetch_rows(self, selector: str) -> list[dict[str, Any]]:
        """
        Rows for ``selector`` — never raises (see the module docstring).

        Returns an empty list on any failure, after recording the reason on
        this connector so health can report it. An empty list is exactly what
        ``MonitorAgent`` already treats as "nothing to do" for a source with no
        data, so a failing metrics connector degrades to invisible rather than
        to disruptive.
        """
        target = (selector or "").strip() or self.default_selector()
        if not target:
            self._last_fetch_error = (
                f"No selector supplied and none configured under "
                f"{self.selector_config_key!r}."
            )
            return []
        if self._client is None or not self._authenticated:
            self._last_fetch_error = (
                self._auth_error or "Connector is not authenticated; no rows fetched."
            )
            return []

        try:
            rows = self._client.query(target) or []
        except Exception as exc:  # noqa: BLE001
            self._fetch_failures += 1
            self._last_fetch_error = self.sanitize(exc)
            logger.warning(
                "connector %s | fetch_rows failed for selector=%r | %s",
                self.kind, target, self._last_fetch_error,
            )
            return []

        normalised = [self._normalise_row(row) for row in rows if isinstance(row, dict)]
        self._truncated = len(normalised) > DEFAULT_ROW_LIMIT
        if self._truncated:
            normalised = normalised[:DEFAULT_ROW_LIMIT]
        self._fetch_successes += 1
        self._last_fetch_error = None
        self._last_row_count = len(normalised)
        return normalised

    def _normalise_row(self, row: dict[str, Any]) -> dict[str, Any]:
        """One upstream row → one KPI row.

        Overridable, because vendors return column names in their own
        conventions (Snowflake upper-cases, SAP prefixes). The default is
        pass-through: a connector whose upstream already uses the metric names
        the platform monitors needs no translation, and inventing one would
        rename metrics nobody asked to rename.
        """
        return dict(row)

    # ------------------------------------------------------------------
    # Capability: DOCUMENTS — deliberately absent
    # ------------------------------------------------------------------

    def list_artifacts(self, since: str | None = None) -> list[ConnectorArtifactRef]:
        """No documents. Empty rather than an error — see the base contract."""
        return []

    # ------------------------------------------------------------------
    # Observability
    # ------------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        return {
            **super().health(),
            "selector": self.default_selector() or None,
            "sdk_module": self.sdk_module or None,
            "row_fetch_successes": self._fetch_successes,
            "row_fetch_failures": self._fetch_failures,
            "last_row_count": self._last_row_count,
            "last_fetch_error": self._last_fetch_error,
            "rows_truncated": self._truncated,
            "row_limit": DEFAULT_ROW_LIMIT,
        }

    def close(self) -> None:
        client_close = getattr(self._client, "close", None)
        if callable(client_close):
            try:
                client_close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("connector %s | client close failed: %s", self.kind, exc)
        super().close()
