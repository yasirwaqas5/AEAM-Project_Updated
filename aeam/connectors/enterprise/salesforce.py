"""
aeam/connectors/enterprise/salesforce.py

Salesforce metric connector (Phase F7).

Runs a configured SOQL query (or named report) and yields its rows as KPI
series through the existing ``CompositeKPISource``.

Salesforce returns every record with an ``attributes`` envelope carrying its
type and URL. :meth:`_normalise_row` drops that envelope: it is API bookkeeping,
not data, and leaving it in would put a dict where the KPI extractor expects a
scalar.
"""

from __future__ import annotations

from typing import Any

from aeam.connectors.enterprise.metrics import QueryMetricsConnector
from aeam.registry.models import SourceKind


class SalesforceConnector(QueryMetricsConnector):
    """Salesforce SOQL/report metric connector."""

    kind = SourceKind.SALESFORCE
    display_name = "Salesforce"
    #: ``instance_url`` addresses the org; ``soql`` is the query whose rows
    #: become the metric series.
    required_config = ("instance_url", "soql")
    default_secret_key = "SALESFORCE_ACCESS_TOKEN"
    sdk_module = "simple_salesforce"
    selector_config_key = "soql"

    def _normalise_row(self, row: dict[str, Any]) -> dict[str, Any]:
        """Strip Salesforce's API envelope and lower-case the field names.

        Nested objects (a related record returned inline) are dropped rather
        than flattened: flattening would invent column names that exist in no
        Salesforce report, and the KPI extractor only ever reads scalars.
        """
        aliases = self._config.get("column_aliases") or {}
        normalised: dict[str, Any] = {}
        for key, value in row.items():
            if key == "attributes":
                continue
            if isinstance(value, (dict, list)):
                continue
            cleaned = str(key).strip().lower()
            normalised[str(aliases.get(key) or aliases.get(cleaned) or cleaned)] = value
        return normalised
