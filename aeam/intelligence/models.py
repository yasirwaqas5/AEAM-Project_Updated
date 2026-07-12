"""
aeam/intelligence/models.py

Data models for the Dataset Intelligence layer (Phase B1.5.1).

Plain, immutable-in-spirit ``@dataclass`` value objects — not registry rows.
They are computed on demand from B1.1/B1.4 registry metadata (``Dataset`` +
``Schema``) and carry no persistence behaviour of their own: no ``to_row()``/
``from_row()``, no table, no repository. ``to_dict()`` is provided purely for
callers (dashboards, later phases) that want a JSON-safe snapshot.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


def _now_iso() -> str:
    """UTC now as an ISO-8601 string."""
    return datetime.now(tz=timezone.utc).isoformat()


@dataclass
class MonitorableMetric:
    """
    A single dataset column identified as a candidate for autonomous monitoring.

    Purely descriptive — carries no monitoring, rule, or forecasting logic of
    its own. ``rules_ref``/``unit`` are deliberate empty seams for a later
    Business Metric Registry (B1.8) to populate without changing this shape.

    Attributes:
        metric_id:        Deterministic, stable identifier — ``"{dataset_id}:{column}"``.
                          Stable across profile regenerations of the same dataset.
        dataset_id:       Owning dataset's registry id.
        column:           Source column name in the dataset.
        data_type:        Inferred column type (from B1.4's
                          ``aeam.ingestion.schema_inference`` type vocabulary,
                          e.g. ``"integer"``, ``"float"``).
        timestamp_column: Name of the dataset's identified time axis, or
                          ``None`` if the dataset has no usable timestamp.
        dimensions:       Names of columns usable to group/slice this metric.
        forecastable:     ``True`` iff a timestamp column was identified for
                          the owning dataset (a time axis is the only
                          requirement to be a forecast candidate at this layer).
        unit:             Business unit label (e.g. ``"USD"``, ``"count"``).
                          ``None`` until a governance layer supplies it.
        rules_ref:        Pointer to a governed rule/threshold definition.
                          ``None`` until a rule provider (B1.8) is wired.
    """

    metric_id: str
    dataset_id: str
    column: str
    data_type: str
    timestamp_column: str | None = None
    dimensions: list[str] = field(default_factory=list)
    forecastable: bool = False
    unit: str | None = None
    rules_ref: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DatasetMonitoringProfile:
    """
    The complete business/monitoring profile inferred for one dataset.

    A pure computed snapshot over the dataset's registry metadata (``Dataset``
    + ``Schema``) at the moment it was generated — never mutated in place;
    re-generate to reflect a re-profiled or reindexed dataset.

    Attributes:
        dataset_id:           The profiled dataset's registry id.
        dataset_name:         The dataset's display name (from ``Dataset.name``).
        schema_id:            The schema row this profile was derived from.
        row_count:            Row count reported by the registry at profile time.
        measures:              Numeric metric-candidate column names.
        dimensions:            Categorical / descriptive column names.
        identifiers:           Identifier (id/key) column names — excluded from
                               both measures and dimensions.
        timestamp_column:      The dataset's identified time axis, or ``None``.
        forecastable_metrics:  Subset of ``measures`` eligible for forecasting
                               (non-empty only when ``timestamp_column`` is set).
        monitorable_metrics:   One :class:`MonitorableMetric` per measure.
        generated_at:          ISO-8601 timestamp this profile was computed.
        detail:                Free-form extensibility bag (mirrors the
                               registry models' ``extra`` escape hatch) — future
                               phases may attach facts here without a shape change.
    """

    dataset_id: str
    dataset_name: str
    schema_id: str | None
    row_count: int
    measures: list[str] = field(default_factory=list)
    dimensions: list[str] = field(default_factory=list)
    identifiers: list[str] = field(default_factory=list)
    timestamp_column: str | None = None
    forecastable_metrics: list[str] = field(default_factory=list)
    monitorable_metrics: list[MonitorableMetric] = field(default_factory=list)
    generated_at: str = field(default_factory=_now_iso)
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            **{k: v for k, v in asdict(self).items() if k != "monitorable_metrics"},
            "monitorable_metrics": [m.to_dict() for m in self.monitorable_metrics],
        }
