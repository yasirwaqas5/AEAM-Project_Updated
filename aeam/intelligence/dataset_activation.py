"""
aeam/intelligence/dataset_activation.py

Dataset activation policy (Phase B1.5.3).

Deciding WHICH registered datasets are monitored is a deliberate, explicit
decision — never automatic. Uploading/registering a dataset (B1.2–B1.4) only
makes it *eligible*; it becomes a live KPI feed only once its ``dataset_id``
appears in the activation list consulted by
:class:`~aeam.connectors.composite_kpi_source.CompositeKPISource`.

This module defines the policy as a narrow ``Protocol`` (mirroring
``KPIRowSource``/``MetricsSink``/``HistoricalDataSource`` in
:mod:`aeam.agents.monitor.monitor_agent` /
:mod:`aeam.agents.forecast.forecast_agent`) so the *mechanism* backing the
activation list can change later without touching
:class:`~aeam.connectors.composite_kpi_source.CompositeKPISource` or any
agent: swap the injected implementation, nothing else moves.

:class:`StaticDatasetActivation` is the only implementation this phase ships
— an explicit, operator-supplied allowlist (sourced from configuration; see
``Settings.ACTIVATED_DATASET_IDS``). No new database table or API endpoint is
introduced in this phase (both are out of scope per B1.5.3's constraints).

Designed for future UI integration: ``CompositeKPISource.add_multi()`` calls
:meth:`DatasetActivation.list_activated_dataset_ids` fresh on every
monitoring cycle — never cached. A later phase can therefore introduce a
DB-backed or admin-API-driven ``DatasetActivation`` implementation (e.g. an
activation flag surfaced through a future dataset management UI) and toggling
a dataset's activation takes effect on the very next cycle, with zero change
to this protocol's consumers.
"""

from __future__ import annotations

from typing import Iterable, Protocol, runtime_checkable


@runtime_checkable
class DatasetActivation(Protocol):
    """
    Structural protocol for the dataset-activation policy.

    Implementations must degrade to an empty list on any failure — never
    raise — mirroring :class:`~aeam.agents.monitor.monitor_agent.KPIRowSource`'s
    contract, since this is consulted on the same hot monitoring-cycle path.
    """

    def list_activated_dataset_ids(self) -> list[str]:
        """Return the ids of datasets currently approved for monitoring."""
        ...


class StaticDatasetActivation:
    """
    Fixed, explicitly-supplied allowlist of activated dataset ids.

    "Static" — the list is frozen at construction (typically parsed once from
    ``Settings.ACTIVATED_DATASET_IDS`` at bootstrap). It answers the same list
    on every call within this process's lifetime. A future dynamic
    (DB-backed / admin-UI-driven) implementation satisfies the same
    :class:`DatasetActivation` protocol without requiring this class to
    change, or any caller of it to change.

    Args:
        dataset_ids: Iterable of dataset id strings. Blank/whitespace-only
                    entries are dropped; order and duplicates from the input
                    are otherwise preserved as given.
    """

    def __init__(self, dataset_ids: Iterable[str] | None = None) -> None:
        self._dataset_ids: list[str] = [
            d.strip() for d in (dataset_ids or []) if d and d.strip()
        ]

    def list_activated_dataset_ids(self) -> list[str]:
        return list(self._dataset_ids)

    def __repr__(self) -> str:
        return f"StaticDatasetActivation(count={len(self._dataset_ids)})"


def parse_activated_dataset_ids(raw: str | None) -> list[str]:
    """
    Parse a comma-separated dataset id list (e.g. ``Settings.ACTIVATED_DATASET_IDS``)
    into a clean list, dropping blanks and de-duplicating while preserving order.

    Args:
        raw: Comma-separated string, or ``None``/empty for no activated datasets.

    Returns:
        Ordered list of unique, non-blank dataset id strings.
    """
    if not raw or not raw.strip():
        return []
    seen: set[str] = set()
    result: list[str] = []
    for part in raw.split(","):
        dataset_id = part.strip()
        if dataset_id and dataset_id not in seen:
            seen.add(dataset_id)
            result.append(dataset_id)
    return result
