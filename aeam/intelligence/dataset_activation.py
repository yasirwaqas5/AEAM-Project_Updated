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

import logging
from typing import TYPE_CHECKING, Iterable, Protocol, runtime_checkable

if TYPE_CHECKING:
    from aeam.integrations.redis_client import RedisClient

logger = logging.getLogger(__name__)


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


#: Redis key holding the set of currently-activated dataset ids.
_ACTIVATION_SET_KEY = "aeam:activated_datasets"


class RedisDatasetActivation:
    """
    Mutable dataset-activation policy backed by a Redis set.

    ``StaticDatasetActivation`` is frozen at construction — it has no way to
    support an operator toggling a dataset on/off at runtime (e.g. from the
    Enterprise Data Center UI). This is exactly the extension point
    :class:`StaticDatasetActivation`'s own docstring reserved for a later
    phase: a second implementation of the same :class:`DatasetActivation`
    protocol, requiring zero change to
    :class:`~aeam.connectors.composite_kpi_source.CompositeKPISource`,
    :class:`~aeam.agents.kpi.composite_rule_engine.CompositeRuleEngine`, or
    any agent — they only ever call ``list_activated_dataset_ids()``.

    Redis (not a new database table) is the storage: it is already a core,
    already-wired AEAM dependency, and this is exactly the kind of
    operational (non-Registry) state it already holds for deduplication.
    Adding a new relational table for this would be a Registry/schema
    change; this is not.

    Degrades to an empty list on any Redis failure — never raises — matching
    the :class:`DatasetActivation` protocol's contract, since this is
    consulted on the same hot monitoring-cycle path as
    :class:`StaticDatasetActivation`.

    Args:
        redis_client: An existing :class:`~aeam.integrations.redis_client.RedisClient`.
        seed:         Dataset ids to activate once, only if the underlying Redis
                      key does not already exist (preserves
                      ``Settings.ACTIVATED_DATASET_IDS`` as the initial state on
                      first boot, without overriding operator changes made
                      since — Redis ``SADD`` is naturally idempotent, but a
                      fresh empty key each restart would otherwise silently
                      re-add a dataset an operator had deliberately deactivated).
        key:          Redis key for the activation set. Overridable for tests.

    Raises:
        ValueError: If ``redis_client`` is ``None``.
    """

    def __init__(
        self,
        redis_client: "RedisClient",
        seed: Iterable[str] | None = None,
        key: str = _ACTIVATION_SET_KEY,
    ) -> None:
        if redis_client is None:
            raise ValueError("redis_client must not be None.")
        self._redis = redis_client
        self._key = key
        if seed and not self._redis.exists(self._key):
            for dataset_id in seed:
                if dataset_id and dataset_id.strip():
                    self._redis.sadd(self._key, dataset_id.strip())

    def list_activated_dataset_ids(self) -> list[str]:
        try:
            return sorted(self._redis.smembers(self._key))
        except Exception as exc:  # noqa: BLE001 - never break the monitoring cycle
            logger.error("RedisDatasetActivation | list failed, degrading to empty: %s", exc)
            return []

    def activate(self, dataset_id: str) -> None:
        """Mark ``dataset_id`` as activated — takes effect on the next monitoring cycle."""
        self._redis.sadd(self._key, dataset_id)

    def deactivate(self, dataset_id: str) -> None:
        """Mark ``dataset_id`` as deactivated — takes effect on the next monitoring cycle."""
        self._redis.srem(self._key, dataset_id)

    def is_activated(self, dataset_id: str) -> bool:
        try:
            return dataset_id in self._redis.smembers(self._key)
        except Exception as exc:  # noqa: BLE001
            logger.error("RedisDatasetActivation | is_activated failed, degrading to False: %s", exc)
            return False

    def __repr__(self) -> str:
        return f"RedisDatasetActivation(key={self._key!r})"


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
