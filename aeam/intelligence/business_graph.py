"""
aeam/intelligence/business_graph.py

The Business Graph (Phase F4 — Correlation Intelligence & Business Graph).

Two classes, with a deliberately hard line between them:

- :class:`BusinessGraphStore` — persistence and BOUNDED traversal. It reads
  and writes ``graph_nodes``/``graph_edges`` through the existing repository
  layer and answers "what is connected to X?" within an explicit budget. It
  derives nothing.
- :class:`BusinessGraphBuilder` — derivation. It reads the evidence the
  platform already holds (dataset registry, dataset intelligence, policy
  registry, incident history including the ``cross_dataset`` findings C4
  already persisted) and emits nodes and edges. It answers no questions.

Why the graph exists
--------------------
C4 correlates one incident's metric against the currently-activated
datasets, pairwise, and discards the result at finalize. So correlation
cannot compound: the twentieth incident on ``checkout_latency`` starts as
ignorant as the first, and nobody can ask "what is connected to checkout
latency?" outside an investigation. This module makes the relationships
durable so they accumulate, and queryable so an operator can read them.

What the graph is NOT
---------------------
It is an **advisory evidence source**, exactly like Enterprise Memory, the
Policy Registry, and C4 itself. Nothing in this module evaluates a rule,
computes a decision, changes a confidence, or dispatches an action. The
Orchestrator appends its finding alongside the others and the deterministic
path — ``RuleEngine``/``StatisticalDetector``/``KPIAgent``/``ForecastAgent``
— never reads it (AGENT-5).

Edge grounding (the rule that makes this honest)
------------------------------------------------
**Every edge originates from an existing record.** Four derivation rules,
each implemented exactly once below, each naming the evidence it reads:

===========================  ====================================  ==========
Edge                         Evidence                              Confidence
===========================  ====================================  ==========
``derived_from``             dataset schema / source registry      1.0 (fact)
``governed_by``              ``policies.related_metrics``          1.0 (fact)
``correlates_with``          persisted ``cross_dataset`` findings  mean \\|r\\|
``co_occurred_in_incident``  ``incidents.metric`` + its findings   1.0 (fact)
===========================  ====================================  ==========

There is no similarity heuristic, no name-fuzzing, no transitive inference,
and no LLM anywhere in this file. When the evidence for a relationship is
absent or too thin, **no edge is created** — the graph is simply smaller,
which is the honest outcome.

Determinism
-----------
A build is a pure function of the database's current contents. Node and
edge primary keys are UUID5 hashes of their natural keys, per-edge
strengths are recomputed from the complete evidence set rather than
incremented, and every traversal orders by ``(confidence, observation
count, id)``. Rebuilding from unchanged evidence therefore produces
byte-identical rows, and "the graph changed" always means "the evidence
changed". The graph never mutates itself: builds happen only when a
privileged caller asks for one.

Bounded reads (E6)
------------------
Traversal is breadth-first with FOUR simultaneous budgets — depth, visited
nodes, traversed edges, and edges read per hop — and it reports which one
stopped it. There is no recursive traversal anywhere in this module; the
frontier is an explicit queue, each hop is a single ``LIMIT``-ed query, and
a hub node with fifty thousand edges costs one bounded read, not fifty
thousand rows.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from aeam.registry.models import (
    GraphEdge,
    GraphEdgeType,
    GraphNode,
    GraphNodeType,
    PolicyStatus,
    _now_iso,
    graph_node_key,
)
from aeam.registry.repositories import GraphEdgeRepository, GraphNodeRepository

logger = logging.getLogger(__name__)

#: Default traversal budgets. Deliberately small: the graph is advisory
#: context appended to an investigation, not a report — an operator who
#: wants more raises them explicitly on the query endpoint, within the
#: hard ceilings below.
DEFAULT_MAX_DEPTH: int = 2
DEFAULT_MAX_NODES: int = 100
DEFAULT_MAX_EDGES: int = 300

#: Hard ceilings. A caller (including an API client) cannot exceed these
#: whatever it asks for, so "bounded" is a property of the code rather than
#: a property of how the caller was configured.
MAX_DEPTH_CEILING: int = 5
MAX_NODES_CEILING: int = 1000
MAX_EDGES_CEILING: int = 5000

#: Per-hop read ceiling. Even at ``MAX_EDGES_CEILING`` no single hop reads
#: more than this many rows, which is what bounds the cost of expanding a
#: hub node.
EDGES_PER_HOP_CEILING: int = 500

#: Minimum |Pearson r| C4 must have observed before a ``correlates_with``
#: edge is created at all. Below this the observation is noise, and an edge
#: asserting a relationship that weak would be worse than no edge.
MIN_CORRELATION_FOR_EDGE: float = 0.7


# ---------------------------------------------------------------------------
# Traversal result
# ---------------------------------------------------------------------------

@dataclass
class TraversalBudget:
    """The four simultaneous limits every traversal runs under (E6).

    Constructed through :meth:`clamped` so a caller cannot request more
    than the module's hard ceilings, whatever it passes.
    """

    max_depth: int = DEFAULT_MAX_DEPTH
    max_nodes: int = DEFAULT_MAX_NODES
    max_edges: int = DEFAULT_MAX_EDGES
    min_confidence: float = 0.0

    @classmethod
    def clamped(
        cls,
        max_depth: int | None = None,
        max_nodes: int | None = None,
        max_edges: int | None = None,
        min_confidence: float | None = None,
    ) -> "TraversalBudget":
        """Build a budget, clamping every field into its permitted range."""
        return cls(
            max_depth=max(1, min(int(max_depth or DEFAULT_MAX_DEPTH), MAX_DEPTH_CEILING)),
            max_nodes=max(1, min(int(max_nodes or DEFAULT_MAX_NODES), MAX_NODES_CEILING)),
            max_edges=max(1, min(int(max_edges or DEFAULT_MAX_EDGES), MAX_EDGES_CEILING)),
            min_confidence=max(0.0, min(float(min_confidence or 0.0), 1.0)),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "max_depth": self.max_depth,
            "max_nodes": self.max_nodes,
            "max_edges": self.max_edges,
            "min_confidence": self.min_confidence,
        }


@dataclass
class GraphBuildReport:
    """What one deterministic build actually did.

    Every count is observed, not estimated, so an operator comparing two
    builds can see exactly what changed in the evidence.
    """

    build_id: str = ""
    started_at: str = ""
    completed_at: str = ""
    nodes_written: int = 0
    edges_written: int = 0
    nodes_retired: int = 0
    edges_retired: int = 0
    nodes_by_type: dict[str, int] = field(default_factory=dict)
    edges_by_type: dict[str, int] = field(default_factory=dict)
    #: Evidence sources that could not be read (e.g. no policy table yet).
    #: Recorded rather than silently skipped: a graph built from three of
    #: four sources is a different object from one built from all four.
    skipped_sources: list[dict[str, str]] = field(default_factory=list)
    incidents_scanned: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "build_id": self.build_id,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "nodes_written": self.nodes_written,
            "edges_written": self.edges_written,
            "nodes_retired": self.nodes_retired,
            "edges_retired": self.edges_retired,
            "nodes_by_type": dict(self.nodes_by_type),
            "edges_by_type": dict(self.edges_by_type),
            "skipped_sources": list(self.skipped_sources),
            "incidents_scanned": self.incidents_scanned,
        }


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class BusinessGraphStore:
    """
    Persistence plus bounded traversal over the business graph.

    Args:
        node_repo: Existing :class:`~aeam.registry.repositories.GraphNodeRepository`.
        edge_repo: Existing :class:`~aeam.registry.repositories.GraphEdgeRepository`.

    Raises:
        ValueError: If either repository is ``None``.
    """

    def __init__(self, node_repo: GraphNodeRepository, edge_repo: GraphEdgeRepository) -> None:
        if node_repo is None:
            raise ValueError("node_repo must not be None.")
        if edge_repo is None:
            raise ValueError("edge_repo must not be None.")
        self._nodes = node_repo
        self._edges = edge_repo

    # ------------------------------------------------------------------
    # Writes (only ever called by BusinessGraphBuilder, only ever from an
    # explicit, privileged build — never from an investigation)
    # ------------------------------------------------------------------

    def upsert_node(self, node: GraphNode, build_id: str | None = None) -> str:
        return self._nodes.upsert(node, build_id=build_id)

    def upsert_edge(self, edge: GraphEdge, build_id: str | None = None) -> str:
        return self._edges.upsert(edge, build_id=build_id)

    def sweep_stale(self, cutoff_iso: str) -> tuple[int, int]:
        """Retire everything not re-confirmed since ``cutoff_iso``.

        Edges are swept before nodes so the graph never briefly contains an
        edge pointing at a node that has already gone.

        Returns:
            ``(nodes_retired, edges_retired)``.
        """
        edges_removed = self._edges.delete_stale(cutoff_iso)
        nodes_removed = self._nodes.delete_stale(cutoff_iso)
        return nodes_removed, edges_removed

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get_node(self, node_key: str) -> GraphNode | None:
        return self._nodes.get_by_key(node_key)

    def stats(self) -> dict[str, Any]:
        """Node/edge counts by type — the cheapest honest description of
        the graph's current size, used by the console and by the bounded-
        query tests to prove a budget held on a genuinely large graph."""
        nodes_by_type = self._nodes.count_by_type()
        edges_by_type = self._edges.count_by_type()
        return {
            "nodes": sum(nodes_by_type.values()),
            "edges": sum(edges_by_type.values()),
            "nodes_by_type": nodes_by_type,
            "edges_by_type": edges_by_type,
        }

    def search_nodes(self, term: str, limit: int = 50) -> list[GraphNode]:
        return self._nodes.search(term, limit=min(max(1, int(limit)), MAX_NODES_CEILING))

    def neighborhood(
        self,
        origin_key: str,
        budget: TraversalBudget | None = None,
        edge_types: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        Breadth-first, hard-bounded traversal outward from ``origin_key``.

        The traversal is EXPLICITLY iterative — a frontier list and a queue,
        never recursion — so its depth is a number this method controls
        rather than a property of the call stack. Each hop issues exactly
        one ``LIMIT``-ed query for the whole frontier.

        Every result discloses its full provenance (EXPL): each related node
        carries the ordered ``path`` of node keys walked to reach it, the
        ordered ``edges`` traversed with each edge's own type/confidence/
        observation count/evidence, the ``depth`` at which it was found, and
        a ``path_confidence`` that is the PRODUCT of the traversed edges'
        confidences — so a two-hop relationship through two 0.8 edges reads
        as 0.64, not as 0.8. Nothing here is opaque and nothing is asserted
        without the edges that support it.

        Args:
            origin_key: Natural key to start from, e.g. ``"metric:sales"``.
            budget:     Traversal limits. Defaults to the module defaults,
                        always clamped to the hard ceilings.
            edge_types: Restrict traversal to these edge types. ``None``
                        traverses all four.

        Returns:
            A dict that always has the same shape (never raises)::

                {
                    "origin_key": str,
                    "origin_found": bool,
                    "budget": {...},
                    "truncated": bool,
                    "truncation_reason": str | None,
                    "depth_reached": int,
                    "nodes_visited": int,
                    "edges_traversed": int,
                    "related": [ {node_key, node_type, label, depth,
                                  path, edges, path_confidence}, ... ],
                }

            ``related`` is ordered by ``path_confidence`` descending, then
            depth ascending, then node key — deterministic, so the same
            graph always yields the same answer in the same order.
        """
        budget = budget or TraversalBudget.clamped()
        origin = self._nodes.get_by_key(origin_key)

        result: dict[str, Any] = {
            "origin_key": origin_key,
            "origin_found": origin is not None,
            "origin_type": origin.node_type if origin else None,
            "origin_label": origin.label if origin else None,
            "budget": budget.as_dict(),
            "truncated": False,
            "truncation_reason": None,
            "depth_reached": 0,
            "nodes_visited": 0,
            "edges_traversed": 0,
            "related": [],
        }
        if origin is None:
            return result

        # best[node_key] = (path_confidence, depth, path, edges)
        best: dict[str, tuple[float, int, list[str], list[dict[str, Any]]]] = {
            origin_key: (1.0, 0, [origin_key], [])
        }
        visited: set[str] = {origin_key}
        frontier: list[str] = [origin_key]
        edges_traversed = 0
        truncation_reason: str | None = None
        depth_reached = 0

        for depth in range(1, budget.max_depth + 1):
            if not frontier:
                break
            remaining_edges = budget.max_edges - edges_traversed
            if remaining_edges <= 0:
                truncation_reason = "edge_budget_exhausted"
                break

            hop_limit = min(remaining_edges, EDGES_PER_HOP_CEILING)
            hop_edges = self._edges.list_touching(
                frontier,
                limit=hop_limit,
                min_confidence=budget.min_confidence,
                edge_types=edge_types,
            )
            if not hop_edges:
                break
            if len(hop_edges) >= hop_limit:
                # The hop filled its allowance, so there may be more edges
                # we did not read. Say so rather than presenting a partial
                # neighbourhood as complete.
                truncation_reason = truncation_reason or "edge_budget_exhausted"

            next_frontier: list[str] = []
            frontier_set = set(frontier)
            node_budget_hit = False

            for edge in hop_edges:
                edges_traversed += 1
                # Traversal is UNDIRECTED: 'metric:a correlates_with
                # metric:b' relates them symmetrically, and a metric's
                # dataset is as reachable from the metric as the reverse.
                # Direction is preserved in the disclosed edge record so
                # the operator still sees which way the evidence points.
                if edge.source_key in frontier_set:
                    from_key, to_key = edge.source_key, edge.target_key
                elif edge.target_key in frontier_set:
                    from_key, to_key = edge.target_key, edge.source_key
                else:  # pragma: no cover - the SQL filter makes this unreachable
                    continue
                if to_key in visited:
                    continue

                parent = best.get(from_key)
                if parent is None:  # pragma: no cover - frontier is always in best
                    continue
                parent_conf, _parent_depth, parent_path, parent_edges = parent

                if len(visited) >= budget.max_nodes:
                    node_budget_hit = True
                    break

                edge_record = {
                    "edge_id": edge.edge_id,
                    "edge_type": edge.edge_type,
                    "from": edge.source_key,
                    "to": edge.target_key,
                    "traversed_from": from_key,
                    "traversed_to": to_key,
                    "confidence": round(float(edge.confidence), 4),
                    "observation_count": int(edge.observation_count),
                    "evidence_source": edge.evidence_source,
                    "evidence": edge.evidence,
                }
                path_conf = parent_conf * float(edge.confidence)
                best[to_key] = (
                    path_conf, depth, [*parent_path, to_key], [*parent_edges, edge_record]
                )
                visited.add(to_key)
                next_frontier.append(to_key)

            depth_reached = depth
            if node_budget_hit:
                truncation_reason = "node_budget_exhausted"
                break
            if edges_traversed >= budget.max_edges:
                truncation_reason = truncation_reason or "edge_budget_exhausted"
                break
            frontier = next_frontier

        else:
            # The loop ran to its full depth without breaking. If anything
            # remained on the frontier, deeper relationships exist that the
            # depth budget stopped us from reading.
            if frontier:
                truncation_reason = truncation_reason or "depth_budget_reached"

        # Resolve labels/types for everything reached, in ONE query.
        reached = [k for k in best if k != origin_key]
        node_lookup = {n.node_key: n for n in self._nodes.list_by_keys(reached)}

        related: list[dict[str, Any]] = []
        for node_key in reached:
            path_conf, depth, path, edges = best[node_key]
            node = node_lookup.get(node_key)
            related.append({
                "node_key": node_key,
                "node_type": node.node_type if node else None,
                "label": node.label if node else node_key,
                "attributes": node.attributes if node else {},
                "depth": depth,
                "path": path,
                "edges": edges,
                "path_confidence": round(path_conf, 4),
            })
        related.sort(key=lambda r: (-r["path_confidence"], r["depth"], r["node_key"]))

        result.update({
            "truncated": truncation_reason is not None,
            "truncation_reason": truncation_reason,
            "depth_reached": depth_reached,
            "nodes_visited": len(visited),
            "edges_traversed": edges_traversed,
            "related": related,
        })
        return result

    def __repr__(self) -> str:
        return "BusinessGraphStore()"


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------

class BusinessGraphBuilder:
    """
    Derives the graph deterministically from evidence the platform already
    holds. Called ONLY from the privileged build endpoint — never from an
    investigation, never on a timer, never by an agent deciding on its own
    that the graph should change.

    Args:
        store:               The :class:`BusinessGraphStore` to write through.
        database_client:     Existing ``DatabaseClient`` — used ONLY to read
                             the ``incidents`` table (SELECT; MEM-2: no
                             incident row is ever written or altered here).
        dataset_repo:        Existing ``DatasetRepository``.
        source_repo:         Existing ``SourceRepository``.
        policy_repo:         Existing ``PolicyRepository``.
        intelligence:        Existing ``DatasetIntelligenceService`` — the
                             SAME profiler MonitorAgent/C4 already use. This
                             module never re-derives which columns are
                             measures.
        incident_limit:      Maximum incidents read per build (E6 bound).
        min_correlation:     Minimum |r| for a ``correlates_with`` edge.

    Raises:
        ValueError: If ``store`` is ``None``.
    """

    def __init__(
        self,
        store: BusinessGraphStore,
        database_client: Any | None = None,
        dataset_repo: Any | None = None,
        source_repo: Any | None = None,
        policy_repo: Any | None = None,
        intelligence: Any | None = None,
        incident_limit: int = 5000,
        min_correlation: float = MIN_CORRELATION_FOR_EDGE,
    ) -> None:
        if store is None:
            raise ValueError("store must not be None.")
        self._store = store
        self._db = database_client
        self._datasets = dataset_repo
        self._sources = source_repo
        self._policies = policy_repo
        self._intelligence = intelligence
        self._incident_limit = max(1, int(incident_limit))
        self._min_correlation = float(min_correlation)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def build(self, retire_stale: bool = True) -> GraphBuildReport:
        """
        Rebuild the graph from current evidence and return what changed.

        Deterministic: the same database contents always produce the same
        nodes and edges, with the same primary keys and the same
        confidences. Idempotent: running it twice in a row writes the same
        rows the second time and retires nothing.

        Args:
            retire_stale: When true (the default), nodes and edges not
                          re-confirmed by this build are removed — the only
                          way an edge whose grounding evidence disappeared
                          leaves the graph. Set false to layer a partial
                          build without retiring anything.

        Returns:
            A :class:`GraphBuildReport`. Never raises for an unreadable
            evidence source: that source is recorded in ``skipped_sources``
            and the build continues, because a graph built from three of
            four sources is still useful as long as it says so.
        """
        build_id = str(uuid.uuid4())
        started_at = _now_iso()
        report = GraphBuildReport(build_id=build_id, started_at=started_at)

        nodes: dict[str, GraphNode] = {}
        # edge natural key -> GraphEdge
        edges: dict[tuple[str, str, str], GraphEdge] = {}

        self._derive_dataset_structure(nodes, edges, report)
        self._derive_policy_governance(nodes, edges, report)
        self._derive_incident_evidence(nodes, edges, report)

        for node in nodes.values():
            node.last_seen_at = started_at
            self._store.upsert_node(node, build_id=build_id)
        for edge in edges.values():
            edge.last_seen_at = started_at
            self._store.upsert_edge(edge, build_id=build_id)

        report.nodes_written = len(nodes)
        report.edges_written = len(edges)

        if retire_stale:
            retired_nodes, retired_edges = self._store.sweep_stale(started_at)
            report.nodes_retired = retired_nodes
            report.edges_retired = retired_edges

        stats = self._store.stats()
        report.nodes_by_type = stats["nodes_by_type"]
        report.edges_by_type = stats["edges_by_type"]
        report.completed_at = _now_iso()

        logger.info(
            "business graph build | build_id=%s | nodes=%d | edges=%d | "
            "retired_nodes=%d | retired_edges=%d | skipped=%d",
            build_id, report.nodes_written, report.edges_written,
            report.nodes_retired, report.edges_retired, len(report.skipped_sources),
        )
        return report

    # ------------------------------------------------------------------
    # Derivation rule 1 — structure (datasets, their measures, their source)
    # ------------------------------------------------------------------

    def _derive_dataset_structure(
        self,
        nodes: dict[str, GraphNode],
        edges: dict[tuple[str, str, str], GraphEdge],
        report: GraphBuildReport,
    ) -> None:
        """``metric -[derived_from]-> dataset -[derived_from]-> service``.

        Grounded entirely in the registry: a measure column exists in the
        dataset's profiled schema, and the dataset records the source it was
        ingested from. Confidence is 1.0 because neither claim is inferred —
        both are rows someone can go read.

        A dataset that cannot be profiled (still ingesting, no schema yet)
        contributes its own node and its source edge but no metric edges.
        That is the honest representation: the platform genuinely does not
        yet know what that dataset measures.
        """
        if self._datasets is None:
            report.skipped_sources.append({
                "source": "dataset_registry",
                "reason": "No dataset repository wired.",
            })
            return

        try:
            datasets = self._datasets.list_all()
        except Exception as exc:  # noqa: BLE001
            report.skipped_sources.append({
                "source": "dataset_registry", "reason": f"Dataset read failed: {exc}"
            })
            return

        sources_by_id: dict[str, Any] = {}
        if self._sources is not None:
            try:
                sources_by_id = {s.source_id: s for s in self._sources.list_all()}
            except Exception as exc:  # noqa: BLE001
                report.skipped_sources.append({
                    "source": "source_registry", "reason": f"Source read failed: {exc}"
                })

        for dataset in datasets:
            dataset_key = graph_node_key(GraphNodeType.DATASET, dataset.dataset_id)
            self._put_node(nodes, GraphNode(
                node_key=dataset_key,
                node_type=GraphNodeType.DATASET,
                label=dataset.name or dataset.dataset_id,
                attributes={
                    "dataset_id": dataset.dataset_id,
                    "status": dataset.status,
                    "row_count": dataset.row_count,
                },
                evidence_source="dataset_registry",
            ))

            source = sources_by_id.get(dataset.source_id) if dataset.source_id else None
            if source is not None:
                service_key = graph_node_key(GraphNodeType.SERVICE, source.source_id)
                self._put_node(nodes, GraphNode(
                    node_key=service_key,
                    node_type=GraphNodeType.SERVICE,
                    label=source.name or source.source_id,
                    attributes={"source_id": source.source_id, "kind": source.kind},
                    evidence_source="source_registry",
                ))
                self._put_edge(edges, GraphEdge(
                    source_key=dataset_key,
                    target_key=service_key,
                    edge_type=GraphEdgeType.DERIVED_FROM,
                    confidence=1.0,
                    observation_count=1,
                    evidence={
                        "fact": "dataset was ingested from this source",
                        "dataset_id": dataset.dataset_id,
                        "source_id": source.source_id,
                    },
                    evidence_source="source_registry",
                ))

            measures = self._measures_for(dataset)
            for measure in measures:
                metric_key = graph_node_key(GraphNodeType.METRIC, measure)
                self._put_node(nodes, GraphNode(
                    node_key=metric_key,
                    node_type=GraphNodeType.METRIC,
                    label=measure,
                    attributes={},
                    evidence_source="dataset_intelligence",
                ))
                self._put_edge(edges, GraphEdge(
                    source_key=metric_key,
                    target_key=dataset_key,
                    edge_type=GraphEdgeType.DERIVED_FROM,
                    confidence=1.0,
                    observation_count=1,
                    evidence={
                        "fact": "metric is a profiled measure column of this dataset",
                        "dataset_id": dataset.dataset_id,
                        "measure": measure,
                    },
                    evidence_source="dataset_intelligence",
                ))

    def _measures_for(self, dataset: Any) -> list[str]:
        """The dataset's measure columns, via the EXISTING profiler.

        Falls back to the registry's own ``metric_columns`` when the
        profiler is unavailable or the dataset has no schema yet — both are
        recorded facts, so neither is a guess. Returns an empty list when
        the platform genuinely does not know.
        """
        if self._intelligence is not None:
            try:
                return list(self._intelligence.build_profile(dataset.dataset_id).measures)
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "business graph | dataset_id=%s not profilable: %s",
                    dataset.dataset_id, exc,
                )
        return list(getattr(dataset, "metric_columns", []) or [])

    # ------------------------------------------------------------------
    # Derivation rule 2 — policy governance
    # ------------------------------------------------------------------

    def _derive_policy_governance(
        self,
        nodes: dict[str, GraphNode],
        edges: dict[tuple[str, str, str], GraphEdge],
        report: GraphBuildReport,
    ) -> None:
        """``metric -[governed_by]-> policy``.

        Grounded in ``policies.related_metrics`` — the metric names the
        extraction step recorded on the policy itself. A policy that names
        no metric produces no edge; the graph does not guess which metric a
        policy "probably" concerns from its prose.

        Retired policies are excluded via the same ``list_matchable()``
        filter the Policy Registry already uses (E12/COMPAT-6), so a policy
        withdrawn from force also stops governing anything in the graph.
        """
        if self._policies is None:
            report.skipped_sources.append({
                "source": "policy_registry", "reason": "No policy repository wired."
            })
            return

        try:
            load = getattr(self._policies, "list_matchable", None)
            policies = load() if load is not None else self._policies.list_all()
        except Exception as exc:  # noqa: BLE001
            report.skipped_sources.append({
                "source": "policy_registry", "reason": f"Policy read failed: {exc}"
            })
            return

        for policy in policies:
            related = [m for m in (policy.related_metrics or []) if str(m).strip()]
            if not related:
                continue
            policy_key = graph_node_key(GraphNodeType.POLICY, policy.policy_id)
            self._put_node(nodes, GraphNode(
                node_key=policy_key,
                node_type=GraphNodeType.POLICY,
                label=policy.business_rule or policy.policy_id,
                attributes={
                    "policy_id": policy.policy_id,
                    "source_document": policy.source_document,
                    "status": getattr(policy, "status", None) or PolicyStatus.ACTIVE,
                    "department": policy.department,
                },
                evidence_source="policy_registry",
            ))
            for metric in related:
                metric_key = graph_node_key(GraphNodeType.METRIC, str(metric))
                self._put_node(nodes, GraphNode(
                    node_key=metric_key,
                    node_type=GraphNodeType.METRIC,
                    label=str(metric),
                    attributes={},
                    evidence_source="policy_registry",
                ))
                self._put_edge(edges, GraphEdge(
                    source_key=metric_key,
                    target_key=policy_key,
                    edge_type=GraphEdgeType.GOVERNED_BY,
                    confidence=1.0,
                    observation_count=1,
                    evidence={
                        "fact": "policy names this metric in related_metrics",
                        "policy_id": policy.policy_id,
                        "source_document": policy.source_document,
                    },
                    evidence_source="policy_registry",
                ))

    # ------------------------------------------------------------------
    # Derivation rule 3 — incident history and C4's own correlation record
    # ------------------------------------------------------------------

    def _derive_incident_evidence(
        self,
        nodes: dict[str, GraphNode],
        edges: dict[tuple[str, str, str], GraphEdge],
        report: GraphBuildReport,
    ) -> None:
        """``incident -[co_occurred_in_incident]-> metric`` and
        ``metric -[correlates_with]-> metric``.

        Both rules read the SAME persisted evidence: each incident's own
        metric, and the ``cross_dataset`` finding C4 already wrote into that
        incident's findings JSON. Nothing is recomputed and no series is
        re-read — this is a strict re-reading of measurements the platform
        made at investigation time (MEM-2: SELECT only).

        This is the rule that makes correlation COMPOUND. C4 measures a
        correlation once per incident and forgets it; here, every incident
        that observed the same pair contributes an observation, the edge's
        confidence becomes the mean |r| across all of them, and
        ``observation_count`` records how many. A pair seen once at 0.71 and
        a pair seen twenty times at 0.9 are visibly different claims.

        A correlation below :data:`MIN_CORRELATION_FOR_EDGE` produces NO
        edge — C4's own reporting threshold, reused rather than relaxed.
        """
        if self._db is None:
            report.skipped_sources.append({
                "source": "incident_history", "reason": "No database client wired."
            })
            return

        try:
            rows = self._db.fetch_all(
                "SELECT incident_id, metric, severity, timestamp, findings "
                "FROM incidents ORDER BY timestamp DESC LIMIT :limit",
                {"limit": self._incident_limit},
            )
        except Exception as exc:  # noqa: BLE001
            report.skipped_sources.append({
                "source": "incident_history", "reason": f"Incident read failed: {exc}"
            })
            return

        report.incidents_scanned = len(rows)
        # pair -> list of observed |r| values, plus the incidents that saw them
        correlation_observations: dict[tuple[str, str], list[float]] = {}
        correlation_incidents: dict[tuple[str, str], list[str]] = {}

        for row in rows:
            incident_id = str(row.get("incident_id") or "").strip()
            origin_metric = str(row.get("metric") or "").strip()
            if not incident_id or not origin_metric:
                continue

            incident_key = graph_node_key(GraphNodeType.INCIDENT, incident_id)
            origin_key = graph_node_key(GraphNodeType.METRIC, origin_metric)
            self._put_node(nodes, GraphNode(
                node_key=incident_key,
                node_type=GraphNodeType.INCIDENT,
                label=incident_id,
                attributes={
                    "incident_id": incident_id,
                    "metric": origin_metric,
                    "severity": row.get("severity"),
                    "timestamp": str(row.get("timestamp") or ""),
                },
                evidence_source="incident_history",
            ))
            self._put_node(nodes, GraphNode(
                node_key=origin_key,
                node_type=GraphNodeType.METRIC,
                label=origin_metric,
                attributes={},
                evidence_source="incident_history",
            ))
            self._put_edge(edges, GraphEdge(
                source_key=incident_key,
                target_key=origin_key,
                edge_type=GraphEdgeType.CO_OCCURRED_IN_INCIDENT,
                confidence=1.0,
                observation_count=1,
                evidence={
                    "fact": "incident was raised on this metric",
                    "incident_id": incident_id,
                    "role": "origin_metric",
                },
                evidence_source="incident_history",
            ))

            cross = self._cross_dataset_finding(row.get("findings"))
            if not cross:
                continue

            # Every metric this investigation cited as cross-dataset
            # evidence co-occurred with the incident. Recorded from the
            # supporting/contradicting entries C4 itself produced.
            for entry in list(cross.get("supporting") or []) + list(cross.get("contradicting") or []):
                cited = str((entry or {}).get("metric") or "").strip()
                if not cited:
                    continue
                cited_key = graph_node_key(GraphNodeType.METRIC, cited)
                self._put_node(nodes, GraphNode(
                    node_key=cited_key,
                    node_type=GraphNodeType.METRIC,
                    label=cited,
                    attributes={},
                    evidence_source="incident_history",
                ))
                self._put_edge(edges, GraphEdge(
                    source_key=incident_key,
                    target_key=cited_key,
                    edge_type=GraphEdgeType.CO_OCCURRED_IN_INCIDENT,
                    confidence=1.0,
                    observation_count=1,
                    evidence={
                        "fact": "investigation cited this metric as cross-dataset evidence",
                        "incident_id": incident_id,
                        "role": "cross_dataset_evidence",
                        "dataset_id": (entry or {}).get("dataset_id"),
                    },
                    evidence_source="incident_history",
                ))

            for entry in list(cross.get("strong_correlations") or []):
                other = str((entry or {}).get("metric") or "").strip()
                corr = (entry or {}).get("correlation")
                if not other or corr is None:
                    continue
                try:
                    strength = abs(float(corr))
                except (TypeError, ValueError):
                    continue
                if strength < self._min_correlation:
                    continue
                # Undirected pair, canonicalised so 'a↔b' and 'b↔a' are the
                # same edge rather than two half-strength duplicates.
                other_key = graph_node_key(GraphNodeType.METRIC, other)
                if other_key == origin_key:
                    continue
                pair = tuple(sorted((origin_key, other_key)))  # type: ignore[assignment]
                correlation_observations.setdefault(pair, []).append(strength)  # type: ignore[arg-type]
                correlation_incidents.setdefault(pair, []).append(incident_id)  # type: ignore[arg-type]
                self._put_node(nodes, GraphNode(
                    node_key=other_key,
                    node_type=GraphNodeType.METRIC,
                    label=other,
                    attributes={},
                    evidence_source="incident_history",
                ))

        for (left, right), strengths in correlation_observations.items():
            incidents = correlation_incidents[(left, right)]
            mean_strength = sum(strengths) / len(strengths)
            self._put_edge(edges, GraphEdge(
                source_key=left,
                target_key=right,
                edge_type=GraphEdgeType.CORRELATES_WITH,
                confidence=round(min(1.0, mean_strength), 4),
                observation_count=len(strengths),
                evidence={
                    "fact": "C4 measured a strong correlation between these metrics",
                    "mean_abs_correlation": round(mean_strength, 4),
                    "min_abs_correlation": round(min(strengths), 4),
                    "max_abs_correlation": round(max(strengths), 4),
                    # Bounded: an edge observed in a thousand incidents must
                    # not carry a thousand ids into every finding that cites
                    # it. observation_count above is the full number.
                    "incident_ids": sorted(incidents)[:20],
                    "incident_count": len(incidents),
                },
                evidence_source="cross_dataset_findings",
            ))

    @staticmethod
    def _cross_dataset_finding(findings: Any) -> dict[str, Any] | None:
        """The persisted ``cross_dataset`` finding from one incident's
        findings column, or ``None``.

        Tolerates the column being a JSON string (SQLite / the TEXT column
        the incidents table actually declares) or already-decoded JSON
        (PostgreSQL JSONB), and returns ``None`` for anything unparseable
        rather than raising — one malformed historical row must never fail
        an entire build.
        """
        data = findings
        if isinstance(data, str):
            text = data.strip()
            if not text:
                return None
            try:
                data = json.loads(text)
            except (ValueError, TypeError):
                return None
        if not isinstance(data, list):
            return None
        latest: dict[str, Any] | None = None
        for entry in data:
            if isinstance(entry, dict) and entry.get("type") == "cross_dataset":
                payload = entry.get("data")
                if isinstance(payload, dict):
                    latest = payload
        return latest

    # ------------------------------------------------------------------
    # Accumulation helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _put_node(nodes: dict[str, GraphNode], node: GraphNode) -> None:
        """Register a node, keeping the FIRST evidence source that named it.

        Two sources can legitimately name the same metric (a dataset schema
        and a policy). Keeping the first is deterministic because the
        derivation rules always run in the same order; merging them would
        make the field mean "several things", which is less useful than one
        true pointer plus the edges, which carry the rest.
        """
        existing = nodes.get(node.node_key)
        if existing is None:
            nodes[node.node_key] = node
            return
        # Prefer the richer attribute set when one source knows more.
        if not existing.attributes and node.attributes:
            existing.attributes = node.attributes

    @staticmethod
    def _put_edge(
        edges: dict[tuple[str, str, str], GraphEdge], edge: GraphEdge
    ) -> None:
        """Register an edge, or merge an identical one by counting it.

        Merging is additive on ``observation_count`` and takes the maximum
        confidence, which only ever applies to the structural rules where
        confidence is a constant 1.0. Correlation strength is NOT merged
        here — it is computed once from the complete observation set, which
        is what keeps rebuilds convergent instead of drifting.
        """
        key = (edge.source_key, edge.edge_type, edge.target_key)
        existing = edges.get(key)
        if existing is None:
            edges[key] = edge
            return
        existing.observation_count += edge.observation_count
        existing.confidence = max(existing.confidence, edge.confidence)

    def __repr__(self) -> str:
        return f"BusinessGraphBuilder(incident_limit={self._incident_limit})"
