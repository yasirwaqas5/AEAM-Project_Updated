"""
aeam/connectors/composite_kpi_source.py

CompositeKPISource — generic KPI source composition (Phase B1.5.3).

Implements the existing ``KPIRowSource`` protocol
(:class:`aeam.agents.monitor.monitor_agent.KPIRowSource`) by fanning a single
``fetch_rows(selector)`` call out to any number of member sources and
concatenating their rows. ``MonitorAgent`` sees exactly one object satisfying
the protocol — it never learns how many underlying sources exist, or what
kind they are (Google Sheets, a registered dataset, a future PostgreSQL/
Snowflake/REST/SAP connector, ...). No agent changes.

Why concatenation is safe for MonitorAgent's consumer, unmodified
---------------------------------------------------------------
``MonitorAgent._extract_series`` (unchanged) scans the returned row list and,
for a given metric name, keeps only the rows carrying a matching column
header — rows from a source that doesn't have that column are silently
skipped, exactly as they already are for a single source. Concatenating
blocks from different sources therefore composes safely: for any metric name
that appears in only ONE member's rows, that metric's series is exactly that
member's own chronologically-ordered contribution — untouched by the other
members' presence in the list.

Known, disclosed limitation: if the SAME metric name were emitted by two
different members with overlapping calendar ranges, concatenation does not
interleave-merge by time — the combined series would be non-monotonic across
that boundary. Practically this needs deliberate metric-name collision
between two live sources (e.g. a dataset column literally named "sales") and
is the same identity concern already flagged for B1.8 (Business Metric
Registry) to resolve with governed metric identity. Not a regression: today
there is only ever one source, so the situation cannot occur yet.

Two composition modes cover every source shape without any source-specific
branching inside this class:
- **pass-through** — the member receives the caller's incoming ``selector``
  verbatim, once. Used for Google Sheets: preserves its exact current
  behaviour and call pattern, zero regression.
- **multi** — the member is queried once per selector from a dynamically
  evaluated list (re-evaluated on every ``fetch_rows`` call, not cached here),
  ignoring the caller's incoming selector. Used for datasets: one
  ``DatasetKPISource`` instance is queried once per *activated* dataset id.
  A future admin-driven activation list is picked up automatically, with no
  change to this class, because the list is re-read every cycle.

Any future connector (PostgreSQL, MySQL, Oracle, Snowflake, REST, SAP, a CSV
folder watcher, SharePoint, ...) plugs in via one of these same two modes —
never a new composition mechanism.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from aeam.agents.monitor.monitor_agent import KPIRowSource

logger = logging.getLogger(__name__)

#: A member's selector-derivation function: given the caller's incoming
#: selector, returns the list of selectors this member should be queried
#: with (0, 1, or many times).
_SelectorFn = Callable[[str], list[str]]


class CompositeKPISource:
    """
    Fans a single ``KPIRowSource.fetch_rows`` call out to multiple member
    sources and concatenates their rows.

    Holds members privately; never exposes a member's identity, type, or
    selector convention to a caller — the composite itself is the only thing
    ``MonitorAgent`` (or any future consumer) ever sees.

    Failure isolation: each member is queried inside its own ``try/except``.
    A single misbehaving member (including one that violates the
    ``KPIRowSource`` "never raise" contract) is logged and skipped — it can
    never take down the whole monitoring cycle, and never prevents the other
    members' rows from being returned.

    Example::

        composite = (
            CompositeKPISource()
            .add_passthrough(sheets_connector)
            .add_multi(dataset_kpi_source, activation.list_activated_dataset_ids)
        )
        monitor_agent = MonitorAgent(..., kpi_source=composite)
    """

    def __init__(self) -> None:
        self._members: list[tuple[KPIRowSource, _SelectorFn]] = []

    # ------------------------------------------------------------------
    # Composition (fluent builder — used once at bootstrap)
    # ------------------------------------------------------------------

    def add_passthrough(self, source: KPIRowSource) -> "CompositeKPISource":
        """
        Add a member that receives the caller's incoming selector verbatim.

        Use for a single default/primary feed (e.g. Google Sheets) whose
        existing selector-derivation and call pattern must be preserved
        exactly as-is.
        """
        self._members.append((source, lambda selector: [selector]))
        return self

    def add_multi(self, source: KPIRowSource, selectors: Callable[[], list[str]]) -> "CompositeKPISource":
        """
        Add a member queried once per selector from a dynamically evaluated list.

        ``selectors`` is called fresh on every :meth:`fetch_rows` — never
        cached here — so a future selector provider backed by a live store
        (e.g. an admin-managed activation list) is picked up automatically
        with no change to this class. The caller's incoming selector is
        ignored for this member.

        Use for any source keyed by a stable identity list rather than a
        single spreadsheet-style range (e.g. activated dataset ids).
        """
        self._members.append((source, lambda _selector: list(selectors())))
        return self

    @property
    def member_count(self) -> int:
        """Number of member sources currently composed."""
        return len(self._members)

    # ------------------------------------------------------------------
    # KPIRowSource protocol
    # ------------------------------------------------------------------

    def fetch_rows(self, selector: str) -> list[dict[str, Any]]:
        """
        Query every member and return the concatenation of their rows.

        Never raises — a failing member is logged and skipped, matching the
        ``KPIRowSource`` contract exactly. An empty composite (no members
        added) returns ``[]``.
        """
        rows: list[dict[str, Any]] = []
        for source, selector_fn in self._members:
            try:
                for member_selector in selector_fn(selector):
                    rows.extend(source.fetch_rows(member_selector))
            except Exception as exc:  # noqa: BLE001 - one bad member must not break the cycle
                logger.error(
                    "CompositeKPISource | member %r failed for selector=%r: %s",
                    source, selector, exc, exc_info=True,
                )
                continue
        return rows

    def __repr__(self) -> str:
        return f"CompositeKPISource(members={self.member_count})"
