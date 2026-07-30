"""
aeam/connectors/enterprise/sap.py

SAP metric connector (Phase F7).

Reads a configured SAP query/CDS view and yields its rows as KPI series through
the existing ``CompositeKPISource``. ``MonitorAgent`` consumes them exactly as
it consumes Google Sheets rows today — no agent change, no second detector.

SAP field names are conventionally upper-case with an underscore prefix
(``/BIC/ZREVENUE``). :meth:`_normalise_row` strips the namespace prefix and
lower-cases, because the platform's monitored metric names are lower-case and a
column called ``/BIC/ZREVENUE`` would never match a rule for ``revenue``. This
is a documented, mechanical mapping — not a rename of anything the operator
chose.
"""

from __future__ import annotations

from typing import Any

from aeam.connectors.enterprise.metrics import QueryMetricsConnector
from aeam.registry.models import SourceKind


class SAPConnector(QueryMetricsConnector):
    """SAP query/CDS-view metric connector."""

    kind = SourceKind.SAP
    display_name = "SAP"
    #: ``host`` and ``client`` address the system; ``query`` names the CDS view
    #: or BW query whose rows become the metric series.
    required_config = ("host", "client", "query")
    default_secret_key = "SAP_PASSWORD"
    sdk_module = "pyrfc"
    selector_config_key = "query"

    def _normalise_row(self, row: dict[str, Any]) -> dict[str, Any]:
        """Map SAP's namespaced upper-case columns onto plain metric names.

        ``/BIC/ZREVENUE`` → ``zrevenue`` → the operator's configured alias when
        one exists. An unmapped column keeps its cleaned name rather than being
        dropped: a column the platform does not monitor is harmless (the
        existing ``_extract_series`` ignores it), whereas dropping one would
        hide data the operator can see in SAP.
        """
        aliases = self._config.get("column_aliases") or {}
        normalised: dict[str, Any] = {}
        for key, value in row.items():
            cleaned = str(key).strip()
            if cleaned.startswith("/") and cleaned.count("/") >= 2:
                cleaned = cleaned.rsplit("/", 1)[-1]
            cleaned = cleaned.lower()
            normalised[str(aliases.get(key) or aliases.get(cleaned) or cleaned)] = value
        return normalised
