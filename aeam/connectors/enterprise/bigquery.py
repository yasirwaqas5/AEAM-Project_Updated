"""
aeam/connectors/enterprise/bigquery.py

BigQuery metric connector (Phase F7).

Runs a configured standard-SQL query against a BigQuery dataset and yields its
rows as KPI series through the existing ``CompositeKPISource``.

BigQuery returns column names as written in the query and dates as
``datetime.date`` objects rather than strings. :meth:`_normalise_row`
lower-cases the names and renders date/datetime values as ISO strings, because
the existing KPI extractor aligns series by a string date key — handing it a
``date`` object would make every row's key unmatchable.
"""

from __future__ import annotations

import datetime as _datetime
from typing import Any

from aeam.connectors.enterprise.metrics import QueryMetricsConnector
from aeam.registry.models import SourceKind


class BigQueryConnector(QueryMetricsConnector):
    """BigQuery metric connector."""

    kind = SourceKind.BIGQUERY
    display_name = "BigQuery"
    #: ``project_id``/``dataset`` address the data; ``query`` is the statement
    #: whose rows become the metric series.
    required_config = ("project_id", "dataset", "query")
    default_secret_key = "BIGQUERY_SERVICE_ACCOUNT_JSON"
    sdk_module = "google.cloud.bigquery"
    selector_config_key = "query"

    def _normalise_row(self, row: dict[str, Any]) -> dict[str, Any]:
        aliases = self._config.get("column_aliases") or {}
        normalised: dict[str, Any] = {}
        for key, value in row.items():
            cleaned = str(key).strip().lower()
            normalised[str(aliases.get(key) or aliases.get(cleaned) or cleaned)] = (
                value.isoformat()
                if isinstance(value, (_datetime.date, _datetime.datetime))
                else value
            )
        return normalised
