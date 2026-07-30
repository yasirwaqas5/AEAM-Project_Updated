"""
aeam/connectors/enterprise/snowflake.py

Snowflake metric connector (Phase F7).

Runs a configured query against a Snowflake warehouse and yields its rows as
KPI series through the existing ``CompositeKPISource``.

Snowflake returns unquoted identifiers upper-cased (``REVENUE``, not
``revenue``). :meth:`_normalise_row` lower-cases them, because the platform's
monitored metric names are lower-case and ``REVENUE`` would never match a rule
written for ``revenue``. Purely mechanical, and documented rather than silent.
"""

from __future__ import annotations

from typing import Any

from aeam.connectors.enterprise.metrics import QueryMetricsConnector
from aeam.registry.models import SourceKind


class SnowflakeConnector(QueryMetricsConnector):
    """Snowflake warehouse metric connector."""

    kind = SourceKind.SNOWFLAKE
    display_name = "Snowflake"
    #: ``account``/``warehouse``/``database`` address the compute and data;
    #: ``query`` is the statement whose rows become the metric series.
    required_config = ("account", "warehouse", "database", "query")
    default_secret_key = "SNOWFLAKE_PASSWORD"
    sdk_module = "snowflake.connector"
    selector_config_key = "query"

    def _normalise_row(self, row: dict[str, Any]) -> dict[str, Any]:
        aliases = self._config.get("column_aliases") or {}
        normalised: dict[str, Any] = {}
        for key, value in row.items():
            cleaned = str(key).strip().lower()
            normalised[str(aliases.get(key) or aliases.get(cleaned) or cleaned)] = value
        return normalised
