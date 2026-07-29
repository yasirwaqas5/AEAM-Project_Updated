"""
aeam/api/graph.py

Business Graph API (Phase F4).

The operator-facing surface over the graph: what it currently contains,
what is connected to a given signal, and the deterministic rebuild that
refreshes it from evidence.

Endpoints (all under ``/api/v1/graph``):

- ``GET  /stats``        — node/edge counts by type. Read-only.
- ``GET  /nodes``        — bounded node search. Read-only.
- ``GET  /neighborhood`` — bounded traversal from one node. Read-only.
- ``POST /build``        — deterministic rebuild from current evidence.

Read and write paths deliberately share no prefix, and the three read
paths are mapped individually in ``_ENDPOINT_RBAC_MAP`` ahead of the
broader ``/api/v1/graph`` entry — so anything added under this router
later is guarded by ``admin:config`` by default rather than by someone
remembering to map it (SEC-1). The same shape the F2 learning router
already uses, for the same reason.

Rules enforced (mirrors every other API module in this package):

- All state access via ``request.app.state.container``; no DB connections
  created here.
- **No graph logic lives here.** Traversal bounds are
  :class:`~aeam.intelligence.business_graph.BusinessGraphStore`'s and
  derivation is ``BusinessGraphBuilder``'s, so an HTTP caller and the
  Orchestrator can never disagree about what the graph says or how far a
  query may reach.
- **Every read is bounded**, including this one: query parameters are
  clamped by ``TraversalBudget.clamped()``, which cannot be talked past.
  A caller asking for depth 50 gets the ceiling, and the response states
  the budget it actually ran under.

**Why the build is privileged.** A rebuild changes what every subsequent
investigation's graph finding says. It is deterministic and evidence-
grounded, so it cannot invent a relationship — but it is still a change to
platform-wide advisory state, which puts it in the same tier as a
configuration write (SEC-7). It is also the ONLY way the graph ever
changes: no agent, timer, or investigation mutates it.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from aeam.intelligence.business_graph import (
    BusinessGraphBuilder,
    BusinessGraphStore,
    TraversalBudget,
)
from aeam.registry.models import GraphEdgeType, GraphNodeType, graph_node_key
from aeam.registry.repositories import (
    DatasetRepository,
    GraphEdgeRepository,
    GraphNodeRepository,
    PolicyRepository,
    SchemaRepository,
    SourceRepository,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/graph", tags=["Business Graph"])


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class GraphBuildRequest(BaseModel):
    """Body for a deterministic graph rebuild."""

    retire_stale: bool = Field(
        default=True,
        description=(
            "Remove nodes and edges this build did not re-confirm. True (the "
            "default) is what lets a relationship whose grounding evidence "
            "disappeared actually leave the graph. False layers a partial "
            "build without retiring anything."
        ),
    )
    actor_id: str | None = Field(
        default=None,
        description=(
            "Acting principal. Ignored when the security middleware established "
            "a verified identity — a body can never override a verified JWT."
        ),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _principal(request: Request, supplied: str | None) -> tuple[str, list[str], str]:
    """Resolve the acting principal and say honestly where it came from.

    Identical precedence to the E9 review router and the F2 learning
    router, deliberately: three governance surfaces that attribute
    differently would produce an audit trail an operator has to read three
    ways.
    """
    user_id = getattr(request.state, "user_id", None)
    if isinstance(user_id, str) and user_id.strip() and user_id != "anonymous":
        roles = getattr(request.state, "roles", None)
        return user_id.strip(), list(roles or []), "jwt"

    cleaned = (supplied or "").strip()
    if cleaned:
        return cleaned, [], "request"

    return "unattributed", [], "unattributed"


def _store(request: Request) -> BusinessGraphStore:
    """The graph store over the container's database client.

    Raises:
        HTTPException: 503 when no database client is wired — the honest
                       answer for a surface whose entire job is reading
                       persisted relationships.
    """
    container = getattr(request.app.state, "container", None)
    db = getattr(container, "db", None)
    if db is None:
        raise HTTPException(
            status_code=503,
            detail="No database client is configured; the business graph is unavailable.",
        )
    existing = getattr(container, "business_graph_store", None)
    if existing is not None:
        return existing
    return BusinessGraphStore(GraphNodeRepository(db), GraphEdgeRepository(db))


def _budget(request: Request, **overrides: Any) -> TraversalBudget:
    """A clamped budget: caller overrides first, then configured defaults.

    ``TraversalBudget.clamped()`` applies the module's hard ceilings last,
    so neither a request parameter nor a misconfigured setting can produce
    an unbounded traversal.
    """
    settings = getattr(getattr(request.app.state, "container", None), "settings", None)
    return TraversalBudget.clamped(
        max_depth=overrides.get("max_depth") or getattr(settings, "GRAPH_MAX_DEPTH", None),
        max_nodes=overrides.get("max_nodes") or getattr(settings, "GRAPH_MAX_NODES", None),
        max_edges=overrides.get("max_edges") or getattr(settings, "GRAPH_MAX_EDGES", None),
        min_confidence=(
            overrides.get("min_confidence")
            if overrides.get("min_confidence") is not None
            else getattr(settings, "GRAPH_MIN_EDGE_CONFIDENCE", None)
        ),
    )


def _audit(request: Request, principal: str, action: str, detail: dict[str, Any]) -> None:
    """Record a graph-changing action against the acting principal.

    Failure is logged and swallowed — an audit-sink problem must never
    invalidate a build that already happened (SEC-6).
    """
    container = getattr(request.app.state, "container", None)
    audit_logger = getattr(container, "audit_logger", None)
    if audit_logger is None:
        return
    try:
        audit_logger.log({
            "user_id": principal,
            "action": action,
            "endpoint": request.url.path,
            "status_code": 200,
            **detail,
        })
    except Exception as exc:  # noqa: BLE001
        logger.warning("graph | audit write failed | action=%s | %s", action, exc)


def _node_dict(node: Any) -> dict[str, Any]:
    return {
        "node_key": node.node_key,
        "node_type": node.node_type,
        "label": node.label,
        "attributes": node.attributes,
        "evidence_source": node.evidence_source,
        "first_seen_at": node.first_seen_at,
        "last_seen_at": node.last_seen_at,
    }


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


@router.get("/stats", summary="Business graph size, by node and edge type")
async def get_stats(request: Request) -> dict:
    """
    Node and edge counts, plus the vocabularies and traversal ceilings in
    force.

    Returns the ``enabled`` flag alongside the counts so a console can tell
    "the graph is off" from "the graph is on but empty" — two very
    different states that a bare count of zero would conflate.
    """
    store = _store(request)
    settings = getattr(getattr(request.app.state, "container", None), "settings", None)
    try:
        stats = store.stats()
    except Exception as exc:  # noqa: BLE001
        logger.error("graph | stats read failed | %s", exc)
        raise HTTPException(status_code=500, detail=f"Graph stats read failed: {exc}") from exc

    return {
        "enabled": bool(getattr(settings, "BUSINESS_GRAPH_ENABLED", False)),
        **stats,
        "node_types": sorted(GraphNodeType.ALL),
        "edge_types": sorted(GraphEdgeType.ALL),
        "budget_defaults": _budget(request).as_dict(),
    }


@router.get("/nodes", summary="Search business graph nodes (bounded)")
async def search_nodes(
    request: Request,
    q: str = Query(default="", description="Substring matched against node key and label."),
    node_type: str | None = Query(
        default=None, description="Restrict to one node type (metric/dataset/service/policy/incident)."
    ),
    limit: int = Query(default=50, ge=1, le=200, description="Maximum nodes returned."),
) -> dict:
    """
    Bounded node search — the "what can I ask about?" surface.

    An empty ``q`` lists nodes rather than erroring, but the limit applies
    either way: there is no parameter combination that returns the whole
    graph.
    """
    store = _store(request)
    if node_type is not None and node_type not in GraphNodeType.ALL:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown node_type {node_type!r}. Known types: {sorted(GraphNodeType.ALL)}.",
        )
    try:
        nodes = store.search_nodes(q, limit=limit)
    except Exception as exc:  # noqa: BLE001
        logger.error("graph | node search failed | %s", exc)
        raise HTTPException(status_code=500, detail=f"Graph node search failed: {exc}") from exc

    if node_type is not None:
        nodes = [n for n in nodes if n.node_type == node_type]
    return {"query": q, "node_type": node_type, "count": len(nodes),
            "nodes": [_node_dict(n) for n in nodes]}


@router.get("/neighborhood", summary="What is connected to this signal (bounded traversal)")
async def get_neighborhood(
    request: Request,
    node_key: str | None = Query(
        default=None, description="Natural key, e.g. 'metric:sales'. Supply this or `metric`."
    ),
    metric: str | None = Query(
        default=None, description="Metric name — shorthand for node_key='metric:<name>'."
    ),
    max_depth: int | None = Query(default=None, ge=1, description="Traversal hops (clamped)."),
    max_nodes: int | None = Query(default=None, ge=1, description="Node budget (clamped)."),
    max_edges: int | None = Query(default=None, ge=1, description="Edge budget (clamped)."),
    min_confidence: float | None = Query(
        default=None, ge=0.0, le=1.0, description="Skip edges below this confidence."
    ),
    edge_type: list[str] | None = Query(
        default=None, description="Restrict traversal to these edge types (repeatable)."
    ),
) -> dict:
    """
    The question C4 could never answer: *what is connected to this?*

    Every result carries its traversal path, the edges walked with their
    individual confidences, the depth it was found at, and the compounded
    path confidence. The response also states the budget the traversal ran
    under and whether that budget truncated the answer — a partial
    neighbourhood is never presented as a complete one.
    """
    store = _store(request)
    if not node_key and not metric:
        raise HTTPException(
            status_code=400, detail="Supply either `node_key` or `metric`."
        )
    key = node_key or graph_node_key(GraphNodeType.METRIC, metric or "")

    if edge_type:
        unknown = [t for t in edge_type if t not in GraphEdgeType.ALL]
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown edge_type(s) {unknown}. Known types: {sorted(GraphEdgeType.ALL)}.",
            )

    budget = _budget(
        request,
        max_depth=max_depth, max_nodes=max_nodes,
        max_edges=max_edges, min_confidence=min_confidence,
    )
    try:
        return store.neighborhood(key, budget=budget, edge_types=list(edge_type) if edge_type else None)
    except Exception as exc:  # noqa: BLE001
        logger.error("graph | neighborhood traversal failed | node_key=%s | %s", key, exc)
        raise HTTPException(status_code=500, detail=f"Graph traversal failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Write — the only path by which the graph ever changes
# ---------------------------------------------------------------------------


@router.post("/build", summary="Rebuild the business graph from current evidence")
async def build_graph(request: Request, body: GraphBuildRequest | None = None) -> dict:
    """
    Deterministically rebuild the graph and report exactly what changed.

    The build reads dataset profiles, the source registry, policy
    ``related_metrics``, and incident history (including the cross-dataset
    correlations C4 already measured), and derives nodes and edges from
    those records only. It invents nothing: a relationship with no
    supporting record produces no edge.

    Running it twice against unchanged evidence writes the same rows and
    retires nothing, so an operator can safely re-run it to confirm the
    graph matches the evidence.
    """
    body = body or GraphBuildRequest()
    container = getattr(request.app.state, "container", None)
    db = getattr(container, "db", None)
    if db is None:
        raise HTTPException(
            status_code=503,
            detail="No database client is configured; the business graph cannot be built.",
        )
    settings = getattr(container, "settings", None)
    principal, roles, attribution = _principal(request, body.actor_id)

    builder = BusinessGraphBuilder(
        store=_store(request),
        database_client=db,
        dataset_repo=DatasetRepository(db),
        source_repo=SourceRepository(db),
        policy_repo=PolicyRepository(db),
        intelligence=getattr(container, "dataset_intelligence", None)
        or _fallback_intelligence(db),
        incident_limit=getattr(settings, "GRAPH_BUILD_INCIDENT_LIMIT", 5000),
        min_correlation=getattr(settings, "GRAPH_MIN_CORRELATION", 0.7),
    )
    try:
        report = builder.build(retire_stale=body.retire_stale)
    except Exception as exc:  # noqa: BLE001
        logger.error("graph | build failed | %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Graph build failed: {exc}") from exc

    payload = report.as_dict()
    _audit(request, principal, "graph.build", {
        "build_id": report.build_id,
        "nodes_written": report.nodes_written,
        "edges_written": report.edges_written,
        "nodes_retired": report.nodes_retired,
        "edges_retired": report.edges_retired,
        "attribution_source": attribution,
        "roles": roles,
    })
    logger.info(
        "graph build | principal=%s (%s) | nodes=%d | edges=%d",
        principal, attribution, report.nodes_written, report.edges_written,
    )
    return {"built": True, "actor": principal, "attribution_source": attribution, **payload}


def _fallback_intelligence(db: Any) -> Any:
    """A DatasetIntelligenceService when the container has none wired.

    The composition root normally provides the SAME instance MonitorAgent
    and C4 already use; this exists so the endpoint still profiles datasets
    correctly in a minimal container (tests, a partially-wired deployment)
    rather than silently falling back to the registry's coarser
    ``metric_columns``.
    """
    from aeam.intelligence.dataset_intelligence import DatasetIntelligenceService

    return DatasetIntelligenceService(
        dataset_repo=DatasetRepository(db), schema_repo=SchemaRepository(db)
    )
