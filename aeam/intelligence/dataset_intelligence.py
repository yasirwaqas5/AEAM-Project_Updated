"""
aeam/intelligence/dataset_intelligence.py

Dataset Intelligence Service (Phase B1.5.1).

Pure semantic layer over the B1.4 registry metadata (``Dataset`` + ``Schema``):
turns structural facts already inferred at ingestion (column ``type``/``role``/
``is_metric``) into a business-facing :class:`~aeam.intelligence.models.DatasetMonitoringProfile`
— which columns are measures, which is the time axis, which are dimensions,
which measures are forecast candidates, and one
:class:`~aeam.intelligence.models.MonitorableMetric` per measure.

Deliberately NOT part of this module (by design, enforced by omission):
- No ingestion, no blob/file access (``BlobStore`` is never imported here).
- No monitoring, rule evaluation, or anomaly detection.
- No forecasting execution.
- No incident/event creation.
- No new database tables, no API endpoints.

It only reads two existing repositories (:class:`~aeam.registry.repositories.DatasetRepository`,
:class:`~aeam.registry.repositories.SchemaRepository`) and computes a value
object. Because it operates purely on the ``Schema.columns`` shape B1.4
defined (``[{name, type, nullable, is_metric, role}]``) — not on any
dataset-source-specific detail — any future connector that populates a
``Schema`` row in that same shape (PostgreSQL, Snowflake, Google Sheets, REST,
...) is profiled by this service unchanged, with no redesign.
"""

from __future__ import annotations

import logging
import re
from typing import Iterable

from aeam.ingestion.schema_inference import (
    ROLE_DIMENSION,
    ROLE_IDENTIFIER,
    ROLE_TIMESTAMP,
)
from aeam.intelligence.models import DatasetMonitoringProfile, MonitorableMetric
from aeam.registry.models import Dataset, Schema
from aeam.registry.repositories import DatasetRepository, SchemaRepository

logger = logging.getLogger(__name__)

# Column-name tokens (whole-token match only, never substring) that indicate a
# time axis when no column was already typed/role'd as a timestamp. Guards
# against false positives like "validated", "candidate", "update" — none of
# which tokenise to "date"/"time"/etc. "at" covers the common enterprise
# ``created_at``/``updated_at`` suffix convention.
_TIMESTAMP_NAME_TOKENS: frozenset[str] = frozenset({
    "date", "time", "timestamp", "datetime", "ts", "dt", "at",
})
_TOKEN_SPLIT_RE = re.compile(r"[^a-z0-9]+")


class DatasetIntelligenceError(Exception):
    """
    Raised when a dataset cannot be profiled.

    Args:
        reason: Machine-stable short code (e.g. ``"dataset_not_found"``),
                mirroring :class:`~aeam.ingestion.schema_inference.SchemaInferenceError`.
        detail: Human-readable explanation.
    """

    def __init__(self, reason: str, detail: str) -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(detail)


# ---------------------------------------------------------------------------
# Pure discovery functions — operate only on a Schema's ``columns`` list, so
# they are independently unit-testable with no repository/DB involved and work
# unchanged for any future connector emitting the same column shape.
# ---------------------------------------------------------------------------

def discover_measures(columns: list[dict]) -> list[str]:
    """Return the names of columns flagged as metric measures (``is_metric``)."""
    return [str(c["name"]) for c in columns if c.get("is_metric")]


def discover_dimensions(columns: list[dict]) -> list[str]:
    """Return the names of categorical/descriptive columns (``role == dimension``)."""
    return [str(c["name"]) for c in columns if c.get("role") == ROLE_DIMENSION]


def discover_identifiers(columns: list[dict]) -> list[str]:
    """Return the names of identifier/key columns (``role == identifier``)."""
    return [str(c["name"]) for c in columns if c.get("role") == ROLE_IDENTIFIER]


def _looks_like_timestamp_name(name: str) -> bool:
    """
    Whole-token (never substring) match against known time-axis name tokens.

    Splits ``name`` on any non-alphanumeric boundary (handles ``created_at``,
    ``event date``, ``event-date``) and requires an exact token match, so
    ``validated``, ``candidate``, and ``update`` — which merely *contain* the
    substring "date" — are never mistaken for a timestamp column.
    """
    tokens = _TOKEN_SPLIT_RE.split(name.strip().lower())
    return any(tok in _TIMESTAMP_NAME_TOKENS for tok in tokens if tok)


def discover_timestamp_column(columns: list[dict]) -> str | None:
    """
    Identify the dataset's time axis, if any.

    Two-tier discovery:
    1. Authoritative: the first column whose B1.4-inferred ``role`` is
       ``timestamp`` (a real ``datetime``-dtype column detected from actual
       data at ingestion time).
    2. Heuristic fallback: among ``dimension``-role (string-typed) columns,
       the first whose name whole-token-matches a time-axis pattern (e.g.
       ``created_at``, ``event_date``). This layer has no data access — only
       column names — so it cannot verify the values actually parse as dates;
       that verification is deferred to a data-access layer (B1.5.2).

    Returns:
        The column name, or ``None`` if no timestamp could be identified.
    """
    for c in columns:
        if c.get("role") == ROLE_TIMESTAMP:
            return str(c["name"])

    for c in columns:
        if c.get("role") == ROLE_DIMENSION and _looks_like_timestamp_name(str(c["name"])):
            return str(c["name"])

    return None


def discover_forecast_candidates(measures: list[str], timestamp_column: str | None) -> list[str]:
    """
    Return the subset of ``measures`` eligible for forecasting.

    A measure is forecastable iff the dataset has an identified time axis — a
    forecast needs both a value series and a time index. With no timestamp,
    returns an empty list (never raises): the dataset is still monitorable
    statistically, just not forecastable.
    """
    return list(measures) if timestamp_column else []


def build_monitorable_metrics(
    dataset_id: str,
    measures: list[str],
    columns: list[dict],
    timestamp_column: str | None,
    dimensions: list[str],
) -> list[MonitorableMetric]:
    """
    Build one :class:`MonitorableMetric` per measure.

    ``metric_id`` is deterministic (``"{dataset_id}:{column}"``) so it stays
    stable across profile regenerations of the same dataset — required for any
    later phase (e.g. B1.8) that wants to reference a metric persistently.
    """
    type_by_name = {str(c["name"]): str(c.get("type", "")) for c in columns}
    forecastable = timestamp_column is not None
    return [
        MonitorableMetric(
            metric_id=f"{dataset_id}:{name}",
            dataset_id=dataset_id,
            column=name,
            data_type=type_by_name.get(name, ""),
            timestamp_column=timestamp_column,
            dimensions=list(dimensions),
            forecastable=forecastable,
        )
        for name in measures
    ]


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class DatasetIntelligenceService:
    """
    Computes a :class:`DatasetMonitoringProfile` from registered dataset metadata.

    Two entry points:
    - :meth:`profile_from_schema` — pure, no I/O. Takes already-fetched
      :class:`~aeam.registry.models.Dataset` / :class:`~aeam.registry.models.Schema`
      objects and returns a profile. Independently unit-testable without a
      database.
    - :meth:`build_profile` — the repository-backed convenience wrapper used
      by real callers: fetches the ``Dataset`` and its ``Schema`` by id, then
      delegates to :meth:`profile_from_schema`.

    Args:
        dataset_repo: Existing :class:`~aeam.registry.repositories.DatasetRepository`.
        schema_repo:  Existing :class:`~aeam.registry.repositories.SchemaRepository`.

    Raises:
        ValueError: If either repository is ``None``.
    """

    def __init__(self, dataset_repo: DatasetRepository, schema_repo: SchemaRepository) -> None:
        if dataset_repo is None:
            raise ValueError("dataset_repo must not be None.")
        if schema_repo is None:
            raise ValueError("schema_repo must not be None.")
        self._dataset_repo = dataset_repo
        self._schema_repo = schema_repo

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def build_profile(self, dataset_id: str) -> DatasetMonitoringProfile:
        """
        Fetch ``dataset_id`` and its schema via the repositories, then profile it.

        Raises:
            DatasetIntelligenceError: If the dataset does not exist, has not
                                      yet been assigned a schema (e.g. still
                                      ``pending``/``processing``), or its
                                      schema row is missing (registry
                                      inconsistency).
        """
        dataset = self._dataset_repo.get(dataset_id)
        if dataset is None:
            raise DatasetIntelligenceError(
                "dataset_not_found", f"No dataset with id {dataset_id!r}."
            )
        if not dataset.schema_id:
            raise DatasetIntelligenceError(
                "dataset_missing_schema",
                f"Dataset {dataset_id!r} has no schema yet (status={dataset.status!r}); "
                f"it has not completed ingestion processing.",
            )
        schema = self._schema_repo.get(dataset.schema_id)
        if schema is None:
            raise DatasetIntelligenceError(
                "schema_not_found",
                f"Dataset {dataset_id!r} references schema {dataset.schema_id!r}, "
                f"which does not exist.",
            )
        return self.profile_from_schema(dataset, schema)

    def profile_from_schema(self, dataset: Dataset, schema: Schema) -> DatasetMonitoringProfile:
        """
        Compute a :class:`DatasetMonitoringProfile` from already-fetched objects.

        Pure — no I/O, no blob access, no database access. Safe to call
        repeatedly with the same inputs (idempotent; only ``generated_at``
        differs between calls).
        """
        columns = schema.columns or []

        measures = discover_measures(columns)
        dimensions = discover_dimensions(columns)
        identifiers = discover_identifiers(columns)
        timestamp_column = discover_timestamp_column(columns)
        forecastable_metrics = discover_forecast_candidates(measures, timestamp_column)
        monitorable_metrics = build_monitorable_metrics(
            dataset_id=dataset.dataset_id,
            measures=measures,
            columns=columns,
            timestamp_column=timestamp_column,
            dimensions=dimensions,
        )

        return DatasetMonitoringProfile(
            dataset_id=dataset.dataset_id,
            dataset_name=dataset.name,
            schema_id=schema.schema_id,
            row_count=dataset.row_count,
            measures=measures,
            dimensions=dimensions,
            identifiers=identifiers,
            timestamp_column=timestamp_column,
            forecastable_metrics=forecastable_metrics,
            monitorable_metrics=monitorable_metrics,
        )

    def list_monitorable_metric_names(self, dataset_ids: Iterable[str]) -> list[str]:
        """
        Return the union of monitorable metric (measure) column names across
        the given datasets — the domain-discovery seam Phase B1.7 composes
        into ``MonitorAgent``'s monitored domain set (via
        :class:`~aeam.agents.kpi.composite_rule_engine.CompositeRuleEngine`).

        Reuses :meth:`build_profile` unchanged — no new discovery logic. A
        dataset that is not yet processed or otherwise fails to profile
        (:class:`DatasetIntelligenceError`) is logged and skipped, never
        raised, so one bad dataset id never breaks domain discovery for the
        rest — this is called on the hot monitoring-cycle path.

        Args:
            dataset_ids: Ids of datasets to include (e.g. the currently
                        activated set).

        Returns:
            Sorted, de-duplicated list of metric column names. Empty list if
            ``dataset_ids`` is empty or every dataset fails to profile.
        """
        names: set[str] = set()
        for dataset_id in dataset_ids:
            try:
                profile = self.build_profile(dataset_id)
            except DatasetIntelligenceError as exc:
                logger.warning(
                    "list_monitorable_metric_names | dataset_id=%s skipped | reason=%s | detail=%s",
                    dataset_id, exc.reason, exc.detail,
                )
                continue
            names.update(profile.measures)
        return sorted(names)

    def __repr__(self) -> str:
        return "DatasetIntelligenceService()"
