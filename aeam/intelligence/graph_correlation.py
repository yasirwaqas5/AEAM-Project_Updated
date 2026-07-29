"""
aeam/intelligence/graph_correlation.py

Graph-aware correlation (Phase F4 — the correlation-engine upgrade).

C4's :class:`~aeam.intelligence.cross_dataset_analyzer.CrossDatasetAnalyzer`
answers one question well: "right now, are the OTHER currently-activated
datasets also anomalous, and do their series correlate with this one?" It
does that by scanning pairwise, from scratch, every incident — which means
it can only ever see what is activated at this moment, only ever one hop
out, and only ever what the current series show.

This engine answers the complementary question: "what does the platform
ALREADY KNOW is connected to this metric?" It traverses the persisted
business graph instead of re-scanning series, so it can surface:

* a metric whose dataset is **not currently activated** — invisible to a
  pairwise scan over the activated set, but recorded in the graph from
  when it was;
* a **two-hop** relationship (this metric correlates with A; A co-occurred
  with B in three past incidents) that no pairwise comparison produces;
* the **policies** that govern the metric and the **past incidents** that
  cited it, reached through the same traversal rather than a second query
  path.

The advisory boundary
---------------------
This engine appends a ``graph`` finding and nothing else. It computes no
decision, adjusts no confidence, evaluates no rule, and is not consulted by
``RuleEngine``, ``StatisticalDetector``, ``KPIAgent``, or ``ForecastAgent``
— the deterministic path does not import this module and cannot reach it
(AGENT-5). It also performs no writes: the graph is built only by an
explicit, privileged build, never as a side effect of an investigation, so
an investigation can never mutate the graph it just read.

Explainability
--------------
Every relationship reported here carries the ordered traversal path, each
contributing edge with its own type/confidence/observation count/evidence
pointers, the depth at which it was found, and the compounded
``path_confidence``. The budget it ran under and whether that budget
truncated the answer are reported alongside. There is no summarised score
without the edges that produced it.
"""

from __future__ import annotations

import logging
from typing import Any

from aeam.intelligence.business_graph import BusinessGraphStore, TraversalBudget
from aeam.registry.models import GraphEdgeType, GraphNodeType, graph_node_key

logger = logging.getLogger(__name__)


class GraphCorrelationEngine:
    """
    Reads the business graph for one incident's metric and produces the
    advisory ``graph`` finding.

    Args:
        store:   The :class:`~aeam.intelligence.business_graph.BusinessGraphStore`
                 to traverse. Read-only from here.
        budget:  Traversal limits. Defaults to the module defaults, always
                 clamped to the store's hard ceilings.

    Raises:
        ValueError: If ``store`` is ``None``.
    """

    def __init__(
        self,
        store: BusinessGraphStore,
        budget: TraversalBudget | None = None,
    ) -> None:
        if store is None:
            raise ValueError("store must not be None.")
        self._store = store
        self._budget = budget or TraversalBudget.clamped()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def analyze(self, metric: str) -> dict[str, Any]:
        """
        Traverse the graph outward from ``metric`` and report what is
        connected to it.

        Args:
            metric: The metric name under investigation (``event.metric``).

        Returns:
            A dict, always with the same shape (never raises)::

                {
                    "available": bool,        # graph had this metric
                    "reason": str | None,     # set iff not available
                    "origin_key": str,
                    "budget": {...},
                    "truncated": bool,
                    "truncation_reason": str | None,
                    "depth_reached": int,
                    "nodes_visited": int,
                    "edges_traversed": int,
                    "correlated_metrics": [...],
                    "governing_policies": [...],
                    "related_datasets": [...],
                    "related_services": [...],
                    "prior_incidents": [...],
                    "related_total": int,
                }

            Every entry in every list carries ``path``, ``edges``,
            ``depth``, and ``path_confidence``. ``available: false`` means
            the graph genuinely holds nothing about this metric — never a
            silent empty result dressed up as "no relationships found".
        """
        origin_key = graph_node_key(GraphNodeType.METRIC, metric or "")
        try:
            traversal = self._store.neighborhood(origin_key, budget=self._budget)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "GraphCorrelationEngine.analyze | metric=%s | traversal failed: %s",
                metric, exc, exc_info=True,
            )
            return self._empty(
                origin_key,
                reason=f"Business graph traversal failed: {exc}",
            )

        if not traversal.get("origin_found"):
            return self._empty(
                origin_key,
                reason=(
                    f"The business graph holds no node for metric {metric!r}. "
                    "Run a graph build after this metric has appeared in a "
                    "dataset profile, a policy, or an incident."
                ),
            )

        related = traversal.get("related") or []
        correlated: list[dict[str, Any]] = []
        policies: list[dict[str, Any]] = []
        datasets: list[dict[str, Any]] = []
        services: list[dict[str, Any]] = []
        incidents: list[dict[str, Any]] = []

        for entry in related:
            node_type = entry.get("node_type")
            record = self._disclosed(entry)
            if node_type == GraphNodeType.METRIC:
                correlated.append(record)
            elif node_type == GraphNodeType.POLICY:
                policies.append(record)
            elif node_type == GraphNodeType.DATASET:
                datasets.append(record)
            elif node_type == GraphNodeType.SERVICE:
                services.append(record)
            elif node_type == GraphNodeType.INCIDENT:
                incidents.append(record)

        return {
            "available": True,
            "reason": None,
            "origin_key": origin_key,
            "origin_label": traversal.get("origin_label"),
            "budget": traversal.get("budget"),
            "truncated": bool(traversal.get("truncated")),
            "truncation_reason": traversal.get("truncation_reason"),
            "depth_reached": int(traversal.get("depth_reached") or 0),
            "nodes_visited": int(traversal.get("nodes_visited") or 0),
            "edges_traversed": int(traversal.get("edges_traversed") or 0),
            "correlated_metrics": correlated,
            "governing_policies": policies,
            "related_datasets": datasets,
            "related_services": services,
            "prior_incidents": incidents,
            "related_total": len(related),
        }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _disclosed(entry: dict[str, Any]) -> dict[str, Any]:
        """One related node, with its complete provenance attached.

        ``relation`` names the edge type of the FINAL hop — the most
        specific true statement about how this node relates to the origin
        — while ``edges`` retains every hop, so "correlates_with, two hops
        away" is never collapsed into a bare "related".
        """
        edges = entry.get("edges") or []
        final_edge = edges[-1] if edges else {}
        return {
            "node_key": entry.get("node_key"),
            "node_type": entry.get("node_type"),
            "label": entry.get("label"),
            "attributes": entry.get("attributes") or {},
            "depth": entry.get("depth"),
            "relation": final_edge.get("edge_type"),
            "path": entry.get("path") or [],
            "edges": edges,
            "edge_confidences": [e.get("confidence") for e in edges],
            "path_confidence": entry.get("path_confidence"),
            # Two hops through the graph is a genuinely weaker claim than
            # one, and saying so explicitly is cheaper than expecting every
            # reader to infer it from the path length.
            "direct": entry.get("depth") == 1,
        }

    @staticmethod
    def _empty(origin_key: str, reason: str | None) -> dict[str, Any]:
        return {
            "available": False,
            "reason": reason,
            "origin_key": origin_key,
            "origin_label": None,
            "budget": None,
            "truncated": False,
            "truncation_reason": None,
            "depth_reached": 0,
            "nodes_visited": 0,
            "edges_traversed": 0,
            "correlated_metrics": [],
            "governing_policies": [],
            "related_datasets": [],
            "related_services": [],
            "prior_incidents": [],
            "related_total": 0,
        }

    def __repr__(self) -> str:
        return f"GraphCorrelationEngine(budget={self._budget.as_dict()})"


# ---------------------------------------------------------------------------
# The C4 upgrade: known relationships as a prior for the pairwise scan
# ---------------------------------------------------------------------------

def known_related_metrics(
    store: BusinessGraphStore,
    metric: str,
    budget: TraversalBudget | None = None,
) -> dict[str, dict[str, Any]]:
    """
    The metrics the graph already knows relate to ``metric``, keyed by
    lower-cased metric name.

    This is the seam that makes C4 graph-AWARE rather than replacing it.
    :class:`~aeam.intelligence.cross_dataset_analyzer.CrossDatasetAnalyzer`
    consults this to label a candidate whose relationship is already
    recorded — so a dataset that shares neither the metric name nor a
    dimension column, and which C4 would therefore file under the generic
    ``activated_dataset`` relation and discard when it looks normal, is
    instead recognised as a known relative and reported.

    Only ``correlates_with`` and ``co_occurred_in_incident`` edges are
    followed: those are the two types that assert a BEHAVIOURAL
    relationship between signals. ``derived_from`` and ``governed_by``
    describe structure and governance, which say nothing about whether two
    metrics move together.

    Returns an empty dict on any failure — this runs on the investigation
    path, where a graph problem must degrade C4 to its exact pre-F4
    behaviour rather than break an investigation.
    """
    try:
        traversal = store.neighborhood(
            graph_node_key(GraphNodeType.METRIC, metric or ""),
            budget=budget or TraversalBudget.clamped(),
            edge_types=[
                GraphEdgeType.CORRELATES_WITH,
                GraphEdgeType.CO_OCCURRED_IN_INCIDENT,
            ],
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("known_related_metrics | metric=%s | %s", metric, exc)
        return {}

    known: dict[str, dict[str, Any]] = {}
    for entry in traversal.get("related") or []:
        if entry.get("node_type") != GraphNodeType.METRIC:
            continue
        label = str(entry.get("label") or "").strip().lower()
        if not label:
            continue
        edges = entry.get("edges") or []
        known[label] = {
            "node_key": entry.get("node_key"),
            "depth": entry.get("depth"),
            "path": entry.get("path") or [],
            "relation": (edges[-1].get("edge_type") if edges else None),
            "path_confidence": entry.get("path_confidence"),
            "edges": edges,
        }
    return known
