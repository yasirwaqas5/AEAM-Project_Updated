"""
aeam/intelligence/dataset_kpi_source.py

DatasetKPISource — the data-access adapter for registered datasets (Phase B1.5.2).

Implements the existing ``KPIRowSource`` protocol
(:class:`aeam.agents.monitor.monitor_agent.KPIRowSource`) so a registered
dataset becomes a first-class KPI feed WITHOUT any change to ``MonitorAgent``,
``RuleEngine``, ``ForecastAgent``, or the Orchestrator — those already accept
any object satisfying ``fetch_rows(selector) -> list[dict]``.

Responsibilities — strictly data access. No semantic profiling (that is
:mod:`aeam.intelligence.dataset_intelligence`'s job), no monitoring, no rule
evaluation, no forecasting, no incident creation:

- Resolve a dataset's ACTIVE version (``VersionRepository``) and read its
  bytes from the existing ``BlobStore``.
- Reuse :class:`~aeam.intelligence.dataset_intelligence.DatasetIntelligenceService`
  (B1.5.1) to know WHICH columns are measures and WHICH column is the time
  axis — never re-derives that.
- Parse/verify the identified timestamp column's actual values — the one
  thing B1.5.1 explicitly could not do without blob access — and sort rows
  chronologically.
- Project rows down to ``{timestamp_column, *measures}``, exactly what
  ``KPIRowSource``'s contract (and ``MonitorAgent._extract_series``) needs.
- Cache the fully-processed row list by the immutable
  ``(dataset_id, active_version.content_hash)`` identity. Correct without a
  TTL: a Version's bytes never change in place, and a re-ingest always
  produces a new ``content_hash`` (and a new ``schema_id``), so a cache hit
  under a given key is valid forever; a bounded LRU only caps memory.
- Never raise: any failure (missing dataset/schema/version/blob, an
  unparseable file, or any unexpected error) degrades to an empty list and is
  logged — matching the ``KPIRowSource`` contract exactly (see its docstring:
  "Implementations must degrade to an empty list on any failure ... never
  raise.").

Generic by construction: everything past "resolve this dataset's identity" is
driven by :class:`~aeam.intelligence.models.DatasetMonitoringProfile`, not by
dataset-specific branching. A future ``PostgresKPISource`` /
``SnowflakeKPISource`` / ``RestKPISource`` follows the identical shape:
resolve a stable identity, fetch rows via its own I/O primitive, reuse
``DatasetIntelligenceService`` (or an analogous profiler) for which columns
matter, project/sort/window/cache, never raise.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from typing import Any

from aeam.ingestion.schema_inference import SchemaInferenceError, read_primary_table
from aeam.ingestion.validation import SUPPORTED_EXTENSIONS
from aeam.intelligence.dataset_intelligence import (
    DatasetIntelligenceError,
    DatasetIntelligenceService,
)
from aeam.registry.models import ParentType
from aeam.registry.repositories import DatasetRepository, VersionRepository
from aeam.storage.blob_store import BlobNotFoundError, BlobStore

logger = logging.getLogger(__name__)

_DEFAULT_MAX_ROWS = 500
_DEFAULT_MAX_CACHE_ENTRIES = 32


def _pandas():
    import pandas as pd  # noqa: PLC0415 (lazy by design — mirrors schema_inference._pandas())
    return pd


def _category_from_name(name: str | None) -> str | None:
    """Resolve a format category from a filename extension (mirrors DatasetIngestJobProcessor)."""
    if not name or "." not in name:
        return None
    ext = name.rsplit(".", 1)[-1].strip().lower()
    return SUPPORTED_EXTENSIONS.get(ext)


class DatasetKPISource:
    """
    ``KPIRowSource`` adapter over registered datasets.

    Implements ``fetch_rows(selector) -> list[dict]`` exactly per
    :class:`aeam.agents.monitor.monitor_agent.KPIRowSource` — ``MonitorAgent``
    (or any future consumer of the protocol) never needs to know this reads a
    dataset's blob rather than a spreadsheet tab.

    ``selector`` is a ``dataset_id`` — the registry's stable primary key, not
    a display name (which can collide or be renamed). This mirrors how a
    future ``PostgresKPISource`` would key off a stable object id rather than
    a human-editable label.

    Args:
        blob_store:        Existing :class:`~aeam.storage.blob_store.BlobStore`
                           (read-only; only ``.get()`` is used).
        dataset_repo:      Existing :class:`~aeam.registry.repositories.DatasetRepository`.
        version_repo:      Existing :class:`~aeam.registry.repositories.VersionRepository`.
        intelligence:      :class:`~aeam.intelligence.dataset_intelligence.DatasetIntelligenceService`
                           (B1.5.1) — the sole source of which columns are
                           measures / the time axis. Never recomputed here.
        max_rows:          Row window returned per fetch (most-recent ``N``
                           after chronological sort). Bounds cost regardless
                           of dataset size. Defaults to 500.
        max_cache_entries: Bounded LRU cap on distinct ``(dataset_id,
                           content_hash)`` cache entries held in memory.
                           Defaults to 32.

    Raises:
        ValueError: If any dependency is ``None``, or ``max_rows``/
                    ``max_cache_entries`` is not a positive integer.
    """

    def __init__(
        self,
        blob_store: BlobStore,
        dataset_repo: DatasetRepository,
        version_repo: VersionRepository,
        intelligence: DatasetIntelligenceService,
        max_rows: int = _DEFAULT_MAX_ROWS,
        max_cache_entries: int = _DEFAULT_MAX_CACHE_ENTRIES,
    ) -> None:
        if blob_store is None:
            raise ValueError("blob_store must not be None.")
        if dataset_repo is None:
            raise ValueError("dataset_repo must not be None.")
        if version_repo is None:
            raise ValueError("version_repo must not be None.")
        if intelligence is None:
            raise ValueError("intelligence must not be None.")
        if max_rows <= 0:
            raise ValueError(f"max_rows must be > 0. Got: {max_rows}.")
        if max_cache_entries <= 0:
            raise ValueError(f"max_cache_entries must be > 0. Got: {max_cache_entries}.")

        self._blob_store = blob_store
        self._dataset_repo = dataset_repo
        self._version_repo = version_repo
        self._intelligence = intelligence
        self._max_rows = max_rows
        self._max_cache_entries = max_cache_entries
        # (dataset_id, content_hash) -> processed rows. Correctness relies on
        # content-addressing: an entry is valid forever for that exact key,
        # since Version bytes are immutable and a re-ingest always produces a
        # new content_hash. No TTL needed — only a size cap for memory.
        self._cache: OrderedDict[tuple[str, str], list[dict[str, Any]]] = OrderedDict()

    # ------------------------------------------------------------------
    # KPIRowSource protocol
    # ------------------------------------------------------------------

    def fetch_rows(self, selector: str) -> list[dict[str, Any]]:
        """
        Return chronological, projected rows for the dataset identified by
        ``selector`` (a ``dataset_id``).

        Never raises — degrades to ``[]`` on any failure (missing dataset,
        dataset not yet profilable, missing active version, missing blob,
        unparseable file, no monitorable measures, or any unexpected error),
        matching the ``KPIRowSource`` contract exactly.
        """
        dataset_id = (selector or "").strip()
        if not dataset_id:
            return []

        try:
            return self._fetch_rows_unsafe(dataset_id)
        except Exception as exc:  # noqa: BLE001 - protocol mandates never raise
            logger.error(
                "DatasetKPISource.fetch_rows | dataset_id=%s | unexpected failure: %s",
                dataset_id, exc, exc_info=True,
            )
            return []

    @property
    def cache_size(self) -> int:
        """Current number of cached (dataset_id, content_hash) entries."""
        return len(self._cache)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _fetch_rows_unsafe(self, dataset_id: str) -> list[dict[str, Any]]:
        dataset = self._dataset_repo.get(dataset_id)
        if dataset is None:
            logger.debug("DatasetKPISource | dataset_id=%s not found.", dataset_id)
            return []

        version = self._version_repo.get_active(ParentType.DATASET, dataset_id)
        if version is None or not version.content_hash:
            logger.debug("DatasetKPISource | dataset_id=%s has no active version.", dataset_id)
            return []

        cache_key = (dataset_id, version.content_hash)
        cached = self._cache.get(cache_key)
        if cached is not None:
            self._cache.move_to_end(cache_key)
            return cached

        try:
            profile = self._intelligence.build_profile(dataset_id)
        except DatasetIntelligenceError as exc:
            logger.debug(
                "DatasetKPISource | dataset_id=%s not yet profilable (%s): %s",
                dataset_id, exc.reason, exc.detail,
            )
            return []

        if not profile.measures:
            logger.debug("DatasetKPISource | dataset_id=%s has no monitorable measures.", dataset_id)
            return []

        try:
            data = self._blob_store.get(version.content_hash)
        except BlobNotFoundError:
            logger.error(
                "DatasetKPISource | dataset_id=%s | blob missing for content_hash=%s",
                dataset_id, version.content_hash,
            )
            return []

        category = _category_from_name(dataset.name)
        try:
            df, _detail = read_primary_table(
                data, category or "", object_name=dataset.name or dataset_id
            )
        except SchemaInferenceError as exc:
            logger.error(
                "DatasetKPISource | dataset_id=%s | could not read table (%s): %s",
                dataset_id, exc.reason, exc.detail,
            )
            return []

        rows = self._project_sort_window(df, profile.measures, profile.timestamp_column)

        self._cache[cache_key] = rows
        self._cache.move_to_end(cache_key)
        if len(self._cache) > self._max_cache_entries:
            self._cache.popitem(last=False)  # evict least-recently-used

        return rows

    def _project_sort_window(
        self,
        df: Any,
        measures: list[str],
        timestamp_column: str | None,
    ) -> list[dict[str, Any]]:
        """
        Project ``df`` down to ``{timestamp_column, *measures}``, sort
        chronologically if a timestamp column is available, then window to
        ``self._max_rows`` most-recent rows.
        """
        pd = _pandas()

        keep = [
            c for c in ([timestamp_column] if timestamp_column else []) + measures
            if c in df.columns
        ]
        if not keep:
            return []
        projected = df[keep].copy()

        if timestamp_column and timestamp_column in projected.columns:
            # B1.5.1 may have only NAME-heuristically identified this column
            # (no data access at that layer) — verify/parse it here, the one
            # thing that phase explicitly deferred to blob-reading B1.5.2.
            # Sorting via a standalone parsed Series (never written into the
            # projected frame) avoids any risk of colliding with a real
            # dataset column and preserves the row's original textual value.
            parsed = pd.to_datetime(projected[timestamp_column], errors="coerce")
            order = parsed.sort_values(na_position="last", kind="stable").index
            projected = projected.loc[order]

        if len(projected) > self._max_rows:
            projected = projected.tail(self._max_rows)

        records = projected.to_dict(orient="records")
        return [
            {k: (None if pd.isna(v) else v) for k, v in row.items()}
            for row in records
        ]

    def __repr__(self) -> str:
        return f"DatasetKPISource(max_rows={self._max_rows}, cached={self.cache_size})"
