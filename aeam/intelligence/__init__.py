"""
aeam/intelligence

Dataset Intelligence layer (Phases B1.5.1 + B1.5.2 + B1.5.3).

B1.5.1 — a pure semantic layer over the B1.4 registry metadata (``Dataset`` +
``Schema``): turns structural facts already inferred at ingestion into a
business-facing :class:`~aeam.intelligence.models.DatasetMonitoringProfile` —
measures, dimensions, identifiers, the identified time axis, forecast
candidates, and one :class:`~aeam.intelligence.models.MonitorableMetric` per
measure. Contains no ingestion, no blob/file access, no monitoring, no rule
evaluation, no forecasting execution, and no incident creation — see
:mod:`aeam.intelligence.dataset_intelligence` for the enforced boundary.

B1.5.2 — :class:`~aeam.intelligence.dataset_kpi_source.DatasetKPISource`: the
data-access adapter that implements the existing
:class:`aeam.agents.monitor.monitor_agent.KPIRowSource` protocol over a
registered dataset's active-version blob, reusing the B1.5.1 profile to know
which columns matter. No monitoring, rule evaluation, forecasting, or incident
creation happens here either — see its module docstring for the boundary.

B1.5.3 — :mod:`aeam.intelligence.dataset_activation`: the explicit,
never-automatic policy deciding WHICH registered datasets are actually
monitored. Composed with ``DatasetKPISource`` via
:class:`~aeam.connectors.composite_kpi_source.CompositeKPISource`, which is
what ``MonitorAgent`` actually receives as its ``kpi_source`` — see that
module for the composition mechanism.
"""

from aeam.intelligence.models import DatasetMonitoringProfile, MonitorableMetric
from aeam.intelligence.dataset_intelligence import (
    DatasetIntelligenceError,
    DatasetIntelligenceService,
    build_monitorable_metrics,
    discover_dimensions,
    discover_forecast_candidates,
    discover_identifiers,
    discover_measures,
    discover_timestamp_column,
)
from aeam.intelligence.dataset_kpi_source import DatasetKPISource
from aeam.intelligence.dataset_activation import (
    DatasetActivation,
    StaticDatasetActivation,
    RedisDatasetActivation,
    parse_activated_dataset_ids,
)

__all__ = [
    "MonitorableMetric",
    "DatasetMonitoringProfile",
    "DatasetIntelligenceService",
    "DatasetIntelligenceError",
    "discover_measures",
    "discover_dimensions",
    "discover_identifiers",
    "discover_timestamp_column",
    "discover_forecast_candidates",
    "build_monitorable_metrics",
    "DatasetKPISource",
    "DatasetActivation",
    "StaticDatasetActivation",
    "RedisDatasetActivation",
    "parse_activated_dataset_ids",
]
