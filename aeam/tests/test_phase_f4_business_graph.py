"""
aeam/tests/test_phase_f4_business_graph.py

Phase F4 — Correlation Intelligence & Business Graph.

Acceptance criteria under test:

1. **Graph-derived correlations appear as their own advisory finding with
   disclosed edge confidence.** The Orchestrator appends a ``graph``
   finding, and every relationship in it carries the traversal path, the
   contributing edges with their individual confidences, the traversal
   depth, and the compounded path confidence.
2. **Graph queries are bounded regardless of graph size.** Asserted on a
   synthetic graph with a hub node of thousands of edges: the traversal
   reads within its budget, says it truncated, and returns the SAME answer
   on repeated runs.
3. **On a labeled multi-dataset scenario, graph-aware correlation surfaces
   a corroborating signal that pairwise C4 misses — without altering any
   deterministic decision.**
4. **The graph builds concurrency-safely.** Parallel builders converge on
   the same rows rather than duplicating them.

Plus the standing F-series invariants this phase must not breach:

* **Edge grounding.** Every edge traces to an existing record. A build over
  a corpus with no supporting evidence creates no edges — asserted per
  derivation rule, including the negative cases (a policy naming no
  metric, a correlation below threshold, a dataset with no schema).
* **Advisory boundary (AGENT-5).** The graph never reaches RuleEngine /
  StatisticalDetector / KPIAgent / ForecastAgent, never writes during an
  investigation, and — flag-off — leaves C4's output byte-identical.
* **Deterministic evolution.** Rebuilding from unchanged evidence produces
  identical rows; the graph never mutates itself.

Infrastructure: in-process only — real SQLite, real FastAPI TestClient,
deterministic fixtures (TEST-3). No LLM, no network, no live services.
"""

from __future__ import annotations

import json
import threading
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aeam.api.graph import router as graph_router
from aeam.config.settings import Settings
from aeam.intelligence.business_graph import (
    EDGES_PER_HOP_CEILING,
    MAX_DEPTH_CEILING,
    MAX_EDGES_CEILING,
    MAX_NODES_CEILING,
    BusinessGraphBuilder,
    BusinessGraphStore,
    TraversalBudget,
)
from aeam.intelligence.cross_dataset_analyzer import CrossDatasetAnalyzer
from aeam.intelligence.graph_correlation import (
    GraphCorrelationEngine,
    known_related_metrics,
)
from aeam.integrations.database import DatabaseClient
from aeam.registry.models import (
    Dataset,
    GraphEdge,
    GraphEdgeType,
    GraphNode,
    GraphNodeType,
    Policy,
    PolicyStatus,
    Schema,
    Source,
    graph_edge_id,
    graph_node_id,
    graph_node_key,
)
from aeam.registry.repositories import (
    DatasetRepository,
    GraphEdgeRepository,
    GraphNodeRepository,
    PolicyRepository,
    SchemaRepository,
    SourceRepository,
)


# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture()
def db(tmp_path):
    client = DatabaseClient(database_url=f"sqlite:///{(tmp_path / 'f4.db').as_posix()}")
    yield client
    client.dispose()


@pytest.fixture()
def store(db):
    return BusinessGraphStore(GraphNodeRepository(db), GraphEdgeRepository(db))


def _seed_incident(
    db,
    incident_id: str,
    metric: str,
    findings: list | None = None,
    timestamp: str = "2026-07-01T00:00:00Z",
) -> None:
    db.insert(
        "incidents",
        {
            "incident_id": incident_id,
            "event_id": f"evt-{incident_id}",
            "event_type": "kpi_drop",
            "metric": metric,
            "severity": "high",
            "current_value": 1.0,
            "expected_value": 2.0,
            "detection_methods": json.dumps(["rule"]),
            "timestamp": timestamp,
            "investigation_depth": 1,
            "root_cause": "seeded",
            "confidence": 0.5,
            "action_taken": False,
            "requires_human": False,
            "findings": json.dumps(findings or []),
        },
        returning_column="incident_id",
    )


def _cross_dataset_finding(
    supporting: list | None = None,
    correlations: list | None = None,
    contradicting: list | None = None,
) -> dict:
    return {
        "type": "cross_dataset",
        "data": {
            "insufficient_data": False,
            "reason": None,
            "origin_dataset_id": "ds-origin",
            "origin_dataset_name": "Origin",
            "candidates_checked": 1,
            "supporting": supporting or [],
            "contradicting": contradicting or [],
            "strong_correlations": correlations or [],
            "missing_signals": [],
        },
    }


def _seed_dataset(
    db,
    dataset_id: str,
    name: str,
    measures: list[str],
    source_id: str | None = None,
    time_column: str = "date",
) -> None:
    schema_id = f"sch-{dataset_id}"
    SchemaRepository(db).create(Schema(
        schema_id=schema_id,
        object_name=name,
        source_id=source_id,
        # The shape DatasetIntelligenceService actually profiles: `is_metric`
        # is what makes a column a measure, and a `dimension`-role timestamp
        # column is what makes the dataset comparable over calendar dates.
        columns=(
            [{"name": time_column, "type": "datetime", "role": "dimension", "is_metric": False}]
            + [{"name": m, "type": "float", "role": "metric", "is_metric": True} for m in measures]
        ),
    ))
    DatasetRepository(db).create(Dataset(
        dataset_id=dataset_id, name=name, source_id=source_id,
        schema_id=schema_id, status="indexed", metric_columns=measures,
    ))


def _builder(db, store, **kwargs) -> BusinessGraphBuilder:
    from aeam.intelligence.dataset_intelligence import DatasetIntelligenceService

    return BusinessGraphBuilder(
        store=store,
        database_client=db,
        dataset_repo=DatasetRepository(db),
        source_repo=SourceRepository(db),
        policy_repo=PolicyRepository(db),
        intelligence=DatasetIntelligenceService(
            dataset_repo=DatasetRepository(db), schema_repo=SchemaRepository(db)
        ),
        **kwargs,
    )


# ===========================================================================
# 1. Deterministic identity — the property everything else rests on
# ===========================================================================


def test_node_and_edge_ids_are_deterministic_functions_of_their_natural_keys():
    # Two independent processes deriving the same relationship must land on
    # the same primary key, or the uniqueness constraint cannot resolve a
    # concurrent write and the graph silently duplicates.
    assert graph_node_id("metric:sales") == graph_node_id("metric:sales")
    assert graph_node_id("metric:sales") != graph_node_id("metric:latency")
    assert (
        graph_edge_id("metric:a", GraphEdgeType.CORRELATES_WITH, "metric:b")
        == graph_edge_id("metric:a", GraphEdgeType.CORRELATES_WITH, "metric:b")
    )
    # Direction and type are part of the identity.
    assert (
        graph_edge_id("metric:a", GraphEdgeType.CORRELATES_WITH, "metric:b")
        != graph_edge_id("metric:b", GraphEdgeType.CORRELATES_WITH, "metric:a")
    )
    assert (
        graph_edge_id("metric:a", GraphEdgeType.CORRELATES_WITH, "metric:b")
        != graph_edge_id("metric:a", GraphEdgeType.CO_OCCURRED_IN_INCIDENT, "metric:b")
    )


def test_node_keys_normalise_case_so_one_signal_is_one_node():
    # A dataset schema says "Sales"; an incident row says "sales". If those
    # resolved to two nodes, the graph would split the signal in half and
    # every traversal would see only one side of its history.
    assert graph_node_key(GraphNodeType.METRIC, "Sales") == graph_node_key(
        GraphNodeType.METRIC, "  sales "
    )


# ===========================================================================
# 2. Edge grounding — every edge traces to a record, and nothing else does
# ===========================================================================


def test_build_on_an_empty_platform_creates_nothing(db, store):
    report = _builder(db, store).build()
    assert report.nodes_written == 0
    assert report.edges_written == 0
    assert store.stats()["edges"] == 0


def test_structural_edges_come_from_the_registry(db, store):
    source_id = SourceRepository(db).create(Source(name="Warehouse", kind="s3"))
    _seed_dataset(db, "ds-1", "Sales Data", ["sales", "refunds"], source_id=source_id)

    _builder(db, store).build()

    # metric -> dataset -> service, all confidence 1.0 because all three are
    # recorded facts rather than inferences.
    hood = store.neighborhood(graph_node_key(GraphNodeType.METRIC, "sales"))
    by_key = {r["node_key"]: r for r in hood["related"]}
    assert by_key["dataset:ds-1"]["edges"][0]["edge_type"] == GraphEdgeType.DERIVED_FROM
    assert by_key["dataset:ds-1"]["edges"][0]["confidence"] == 1.0
    assert by_key[f"service:{source_id}"]["depth"] == 2
    # And the evidence names the record an operator can go read.
    assert by_key["dataset:ds-1"]["edges"][0]["evidence"]["dataset_id"] == "ds-1"


def test_governed_by_edges_come_only_from_declared_related_metrics(db, store):
    repo = PolicyRepository(db)
    governing = repo.create(Policy(
        related_metrics=["sales"], business_rule="Escalate large sales drops",
        source_document="policy.pdf",
    ))
    # A policy whose prose mentions sales but which declares no metric must
    # produce NO edge. Guessing the subject from the text is exactly the
    # fabrication this phase forbids.
    repo.create(Policy(
        related_metrics=[], business_rule="Sales team should review drops weekly",
        source_document="policy.pdf", raw_text="sales sales sales",
    ))

    _builder(db, store).build()

    hood = store.neighborhood(graph_node_key(GraphNodeType.METRIC, "sales"))
    policy_nodes = [r for r in hood["related"] if r["node_type"] == GraphNodeType.POLICY]
    assert [p["node_key"] for p in policy_nodes] == [f"policy:{governing}"]


def test_retired_policies_stop_governing_anything(db, store):
    PolicyRepository(db).create(Policy(
        related_metrics=["sales"], business_rule="Old rule",
        status=PolicyStatus.RETIRED,
    ))
    _builder(db, store).build()
    stats = store.stats()
    assert stats["edges_by_type"].get(GraphEdgeType.GOVERNED_BY, 0) == 0


def test_correlates_with_edges_come_from_persisted_c4_measurements(db, store):
    _seed_incident(db, "inc-1", "sales", [
        _cross_dataset_finding(correlations=[{"metric": "refunds", "correlation": -0.91,
                                              "dataset_id": "ds-2", "dataset_name": "Refunds"}])
    ])

    _builder(db, store).build()

    hood = store.neighborhood(graph_node_key(GraphNodeType.METRIC, "sales"))
    corr = next(
        r for r in hood["related"]
        if r["node_key"] == "metric:refunds" and r["edges"][0]["edge_type"] == GraphEdgeType.CORRELATES_WITH
    )
    edge = corr["edges"][0]
    # Sign is dropped (a -0.91 correlation is as strong as +0.91) but the
    # observed values are all retained so the claim stays checkable.
    assert edge["confidence"] == pytest.approx(0.91)
    assert edge["evidence"]["incident_ids"] == ["inc-1"]
    assert edge["evidence"]["max_abs_correlation"] == pytest.approx(0.91)


def test_a_weak_correlation_produces_no_edge_at_all(db, store):
    _seed_incident(db, "inc-1", "sales", [
        _cross_dataset_finding(correlations=[{"metric": "noise", "correlation": 0.31}])
    ])
    _builder(db, store).build()
    assert store.stats()["edges_by_type"].get(GraphEdgeType.CORRELATES_WITH, 0) == 0


def test_correlation_confidence_is_the_mean_across_every_observation(db, store):
    # THIS is what "correlation compounds" means: C4 measures the pair once
    # per incident and forgets it; the graph keeps every observation and
    # reports the mean with the count that produced it.
    for i, r in enumerate([0.8, 0.9, 1.0]):
        _seed_incident(
            db, f"inc-{i}", "sales",
            [_cross_dataset_finding(correlations=[{"metric": "refunds", "correlation": r}])],
            timestamp=f"2026-07-0{i + 1}T00:00:00Z",
        )

    _builder(db, store).build()

    hood = store.neighborhood(graph_node_key(GraphNodeType.METRIC, "sales"))
    edge = next(
        e for r in hood["related"] for e in r["edges"]
        if e["edge_type"] == GraphEdgeType.CORRELATES_WITH
    )
    assert edge["confidence"] == pytest.approx(0.9)
    assert edge["observation_count"] == 3
    assert edge["evidence"]["incident_count"] == 3


def test_a_malformed_findings_row_never_fails_the_build(db, store):
    db.execute(
        "INSERT INTO incidents (incident_id, metric, timestamp, findings) "
        "VALUES ('bad', 'sales', '2026-07-01T00:00:00Z', 'not json at all')"
    )
    _seed_incident(db, "good", "latency_ms", [])
    report = _builder(db, store).build()
    assert report.incidents_scanned == 2
    assert store.get_node("metric:sales") is not None


def test_an_unreadable_evidence_source_is_recorded_not_hidden(db, store):
    # A graph built from three of four sources is a different object from
    # one built from all four, and the report must say which it is.
    builder = BusinessGraphBuilder(store=store, database_client=db)
    report = builder.build()
    reasons = {s["source"] for s in report.skipped_sources}
    assert {"dataset_registry", "policy_registry"} <= reasons


# ===========================================================================
# 3. Deterministic evolution
# ===========================================================================


def test_rebuilding_from_unchanged_evidence_is_idempotent(db, store):
    _seed_dataset(db, "ds-1", "Sales", ["sales"])
    _seed_incident(db, "inc-1", "sales", [
        _cross_dataset_finding(correlations=[{"metric": "refunds", "correlation": 0.85}])
    ])
    builder = _builder(db, store)

    first = builder.build()
    time.sleep(0.01)
    second = builder.build()

    assert (first.nodes_written, first.edges_written) == (second.nodes_written, second.edges_written)
    assert second.nodes_retired == 0 and second.edges_retired == 0
    assert first.nodes_by_type == second.nodes_by_type
    assert first.edges_by_type == second.edges_by_type


def test_first_seen_survives_a_rebuild_while_last_seen_advances(db, store):
    _seed_dataset(db, "ds-1", "Sales", ["sales"])
    builder = _builder(db, store)
    builder.build()
    original = store.get_node("metric:sales")

    time.sleep(0.01)
    builder.build()
    refreshed = store.get_node("metric:sales")

    assert refreshed.first_seen_at == original.first_seen_at
    assert refreshed.last_seen_at > original.last_seen_at


def test_an_edge_whose_evidence_disappears_is_retired(db, store):
    policy_id = PolicyRepository(db).create(Policy(
        related_metrics=["sales"], business_rule="Governs sales",
    ))
    builder = _builder(db, store)
    builder.build()
    assert store.get_node(f"policy:{policy_id}") is not None

    # The evidence is corrected: the policy no longer declares that metric.
    PolicyRepository(db).update(policy_id, {"related_metrics": []})
    time.sleep(0.01)
    report = builder.build()

    assert report.edges_retired >= 1
    assert store.stats()["edges_by_type"].get(GraphEdgeType.GOVERNED_BY, 0) == 0


def test_retire_stale_false_layers_without_removing(db, store):
    PolicyRepository(db).create(Policy(related_metrics=["sales"], business_rule="Governs sales"))
    builder = _builder(db, store)
    builder.build()
    before = store.stats()["edges"]

    time.sleep(0.01)
    report = builder.build(retire_stale=False)

    assert report.edges_retired == 0
    assert store.stats()["edges"] == before


# ===========================================================================
# 4. Bounded traversal — the E6 guarantee, on a genuinely large graph
# ===========================================================================


@pytest.fixture(scope="module")
def large_graph(tmp_path_factory):
    """A hub metric with 3,000 neighbours, each also linked to a leaf.

    Deliberately far larger than every budget in the module, so a query
    that is NOT bounded will visibly blow past its limit rather than
    passing by accident on a small fixture.

    Module-scoped and never written to by the tests that use it: building
    it once costs one pass instead of one per assertion, and every test
    below reads only.
    """
    path = tmp_path_factory.mktemp("f4-large") / "large.db"
    client = DatabaseClient(database_url=f"sqlite:///{path.as_posix()}")
    node_repo, edge_repo = GraphNodeRepository(client), GraphEdgeRepository(client)
    hub = graph_node_key(GraphNodeType.METRIC, "hub")
    node_repo.upsert(GraphNode(node_key=hub, node_type=GraphNodeType.METRIC, label="hub"))
    for i in range(3000):
        key = graph_node_key(GraphNodeType.METRIC, f"m{i}")
        node_repo.upsert(GraphNode(node_key=key, node_type=GraphNodeType.METRIC, label=f"m{i}"))
        edge_repo.upsert(GraphEdge(
            source_key=hub, target_key=key, edge_type=GraphEdgeType.CORRELATES_WITH,
            confidence=0.5 + (i % 50) / 100.0, observation_count=1,
        ))
    yield BusinessGraphStore(node_repo, edge_repo)
    client.dispose()


def test_traversal_respects_its_edge_budget_on_a_large_graph(large_graph):
    budget = TraversalBudget.clamped(max_depth=3, max_nodes=1000, max_edges=50)
    result = large_graph.neighborhood("metric:hub", budget=budget)

    assert result["edges_traversed"] <= 50
    assert result["truncated"] is True
    assert result["truncation_reason"] == "edge_budget_exhausted"


def test_traversal_respects_its_node_budget_on_a_large_graph(large_graph):
    budget = TraversalBudget.clamped(max_depth=3, max_nodes=10, max_edges=5000)
    result = large_graph.neighborhood("metric:hub", budget=budget)

    assert result["nodes_visited"] <= 10
    assert len(result["related"]) <= 10
    assert result["truncated"] is True


def test_a_bounded_traversal_is_deterministic_across_runs(large_graph):
    # A truncated answer that varied run to run would make the finding
    # unreproducible, and an operator could never confirm what an
    # investigation actually saw.
    budget = TraversalBudget.clamped(max_depth=2, max_nodes=20, max_edges=40)
    first = large_graph.neighborhood("metric:hub", budget=budget)
    second = large_graph.neighborhood("metric:hub", budget=budget)
    assert [r["node_key"] for r in first["related"]] == [r["node_key"] for r in second["related"]]


def test_a_bounded_traversal_keeps_the_strongest_edges_first(large_graph):
    budget = TraversalBudget.clamped(max_depth=1, max_nodes=5, max_edges=5)
    result = large_graph.neighborhood("metric:hub", budget=budget)
    confidences = [r["path_confidence"] for r in result["related"]]
    assert confidences == sorted(confidences, reverse=True)
    assert confidences[0] >= 0.9


def test_a_caller_cannot_talk_past_the_hard_ceilings():
    budget = TraversalBudget.clamped(max_depth=9999, max_nodes=10**9, max_edges=10**9,
                                     min_confidence=7.5)
    assert budget.max_depth == MAX_DEPTH_CEILING
    assert budget.max_nodes == MAX_NODES_CEILING
    assert budget.max_edges == MAX_EDGES_CEILING
    assert budget.min_confidence == 1.0


def test_no_single_hop_reads_more_than_the_per_hop_ceiling(large_graph, monkeypatch):
    seen: list[int] = []
    original = GraphEdgeRepository.list_touching

    def _spy(self, node_keys, *, limit, **kwargs):
        seen.append(limit)
        return original(self, node_keys, limit=limit, **kwargs)

    monkeypatch.setattr(GraphEdgeRepository, "list_touching", _spy)
    large_graph.neighborhood(
        "metric:hub", budget=TraversalBudget.clamped(max_edges=MAX_EDGES_CEILING)
    )
    assert seen and max(seen) <= EDGES_PER_HOP_CEILING


def test_traversal_of_a_missing_node_says_so_rather_than_returning_empty(store):
    result = store.neighborhood("metric:never_seen")
    assert result["origin_found"] is False
    assert result["related"] == []


def test_min_confidence_filters_edges_out_of_the_traversal(db, store):
    node_repo, edge_repo = GraphNodeRepository(db), GraphEdgeRepository(db)
    for key in ("metric:a", "metric:b", "metric:c"):
        node_repo.upsert(GraphNode(node_key=key, node_type=GraphNodeType.METRIC, label=key.split(":")[1]))
    edge_repo.upsert(GraphEdge(source_key="metric:a", target_key="metric:b",
                               edge_type=GraphEdgeType.CORRELATES_WITH, confidence=0.95))
    edge_repo.upsert(GraphEdge(source_key="metric:a", target_key="metric:c",
                               edge_type=GraphEdgeType.CORRELATES_WITH, confidence=0.72))

    result = store.neighborhood("metric:a", budget=TraversalBudget.clamped(min_confidence=0.9))
    assert [r["node_key"] for r in result["related"]] == ["metric:b"]


# ===========================================================================
# 5. Explainability — no opaque conclusions
# ===========================================================================


def test_every_relationship_discloses_its_path_edges_confidences_and_depth(db, store):
    node_repo, edge_repo = GraphNodeRepository(db), GraphEdgeRepository(db)
    for key, label in (("metric:a", "a"), ("metric:b", "b"), ("metric:c", "c")):
        node_repo.upsert(GraphNode(node_key=key, node_type=GraphNodeType.METRIC, label=label))
    edge_repo.upsert(GraphEdge(source_key="metric:a", target_key="metric:b",
                               edge_type=GraphEdgeType.CORRELATES_WITH, confidence=0.8,
                               observation_count=3, evidence={"incident_ids": ["i1"]}))
    edge_repo.upsert(GraphEdge(source_key="metric:b", target_key="metric:c",
                               edge_type=GraphEdgeType.CORRELATES_WITH, confidence=0.5,
                               observation_count=1))

    result = store.neighborhood("metric:a", budget=TraversalBudget.clamped(max_depth=2))
    two_hop = next(r for r in result["related"] if r["node_key"] == "metric:c")

    assert two_hop["depth"] == 2
    assert two_hop["path"] == ["metric:a", "metric:b", "metric:c"]
    assert [e["confidence"] for e in two_hop["edges"]] == [0.8, 0.5]
    # A two-hop claim is genuinely weaker than either hop alone, and the
    # compounded number says so instead of reporting the last edge's 0.5.
    assert two_hop["path_confidence"] == pytest.approx(0.4)
    assert two_hop["edges"][0]["observation_count"] == 3
    assert two_hop["edges"][0]["evidence"] == {"incident_ids": ["i1"]}


def test_the_engine_finding_carries_full_provenance_per_entry(db, store):
    _seed_incident(db, "inc-1", "sales", [
        _cross_dataset_finding(correlations=[{"metric": "refunds", "correlation": 0.9}])
    ])
    _builder(db, store).build()

    finding = GraphCorrelationEngine(store).analyze("sales")

    assert finding["available"] is True
    entry = finding["correlated_metrics"][0]
    for required in ("path", "edges", "edge_confidences", "depth", "path_confidence", "relation"):
        assert required in entry, f"{required} must be disclosed on every graph finding entry"
    assert entry["edge_confidences"] == [0.9]
    # The budget the traversal ran under is part of the disclosure: a
    # reader cannot judge "nothing else was found" without it.
    assert finding["budget"]["max_depth"] >= 1


def test_the_engine_says_when_the_metric_is_absent_rather_than_reporting_nothing_found(store):
    finding = GraphCorrelationEngine(store).analyze("never_seen")
    assert finding["available"] is False
    assert "no node for metric" in finding["reason"]
    assert finding["correlated_metrics"] == []


def test_the_engine_groups_related_entities_by_type(db, store):
    source_id = SourceRepository(db).create(Source(name="Warehouse", kind="s3"))
    _seed_dataset(db, "ds-1", "Sales", ["sales"], source_id=source_id)
    PolicyRepository(db).create(Policy(related_metrics=["sales"], business_rule="Governs sales"))
    _seed_incident(db, "inc-1", "sales", [])
    _builder(db, store).build()

    finding = GraphCorrelationEngine(
        store, budget=TraversalBudget.clamped(max_depth=2)
    ).analyze("sales")

    assert [d["node_key"] for d in finding["related_datasets"]] == ["dataset:ds-1"]
    assert len(finding["governing_policies"]) == 1
    assert len(finding["prior_incidents"]) == 1
    assert len(finding["related_services"]) == 1


def test_engine_failure_degrades_to_an_honest_unavailable(store, monkeypatch):
    def _boom(*_a, **_k):
        raise RuntimeError("storage exploded")

    monkeypatch.setattr(BusinessGraphStore, "neighborhood", _boom)
    finding = GraphCorrelationEngine(store).analyze("sales")
    assert finding["available"] is False
    assert "storage exploded" in finding["reason"]


# ===========================================================================
# 6. The correlation uplift — what graph-aware sees that pairwise cannot
# ===========================================================================


class _StubActivation:
    def __init__(self, ids): self._ids = ids
    def list_activated_dataset_ids(self): return list(self._ids)


class _StubKPISource:
    def __init__(self, rows_by_dataset): self._rows = rows_by_dataset
    def fetch_rows(self, dataset_id): return list(self._rows.get(dataset_id, []))


def _labeled_scenario(db, store):
    """A deliberately labeled multi-dataset scenario.

    ``sales`` (the incident metric) lives in ds-origin. ``supply_delay``
    lives in ds-supply, which shares NO metric name and NO dimension column
    with the origin — so C4's pairwise scan files it under the generic
    ``activated_dataset`` relation. It is also currently NORMAL, which is
    precisely why pairwise C4 discards it entirely.

    But three past investigations measured a strong sales↔supply_delay
    correlation. That is a corroborating signal, and only the graph has it.
    """
    # Deliberately DIFFERENT time-column names so the two datasets share no
    # dimension either — otherwise C4 would relate them structurally and the
    # test would prove nothing about the graph.
    _seed_dataset(db, "ds-origin", "Sales", ["sales"], time_column="date")
    _seed_dataset(db, "ds-supply", "Supply", ["supply_delay"], time_column="event_timestamp")
    for i in range(3):
        _seed_incident(
            db, f"inc-{i}", "sales",
            [_cross_dataset_finding(correlations=[{"metric": "supply_delay", "correlation": 0.87}])],
            timestamp=f"2026-07-0{i + 1}T00:00:00Z",
        )
    _builder(db, store).build()


def test_graph_surfaces_a_corroborating_signal_pairwise_c4_misses(db, store):
    _labeled_scenario(db, store)

    rows = {
        "ds-origin": [{"date": f"2026-07-0{i + 1}", "sales": v} for i, v in enumerate([100, 101, 99, 40])],
        # Flat: no anomaly at all, so C4 has nothing to report about it.
        "ds-supply": [{"event_timestamp": f"2026-07-0{i + 1}", "supply_delay": 5.0} for i in range(4)],
    }
    activation = _StubActivation(["ds-origin", "ds-supply"])
    from aeam.intelligence.dataset_intelligence import DatasetIntelligenceService

    intelligence = DatasetIntelligenceService(
        dataset_repo=DatasetRepository(db), schema_repo=SchemaRepository(db)
    )

    pairwise = CrossDatasetAnalyzer(
        dataset_activation=activation, intelligence=intelligence,
        kpi_source=_StubKPISource(rows),
    ).analyze("sales")

    # Pairwise C4: nothing corroborating. supply_delay is normal and shares
    # no structure, so it is genuinely (and correctly) omitted.
    assert pairwise["supporting"] == []
    assert pairwise["contradicting"] == []

    # The graph, reading measurements C4 itself made in past incidents,
    # surfaces the relationship — with the evidence that backs it.
    graph_finding = GraphCorrelationEngine(store).analyze("sales")
    corroborating = [
        m for m in graph_finding["correlated_metrics"] if m["label"] == "supply_delay"
    ]
    assert len(corroborating) == 1
    assert corroborating[0]["relation"] == GraphEdgeType.CORRELATES_WITH
    assert corroborating[0]["edges"][0]["observation_count"] == 3


def test_graph_aware_c4_labels_a_known_relative_that_pairwise_calls_generic(db, store):
    _labeled_scenario(db, store)
    rows = {
        "ds-origin": [{"date": f"2026-07-0{i + 1}", "sales": v} for i, v in enumerate([100, 101, 99, 40])],
        "ds-supply": [{"event_timestamp": f"2026-07-0{i + 1}", "supply_delay": 5.0} for i in range(4)],
    }
    from aeam.intelligence.dataset_intelligence import DatasetIntelligenceService

    kwargs = dict(
        dataset_activation=_StubActivation(["ds-origin", "ds-supply"]),
        intelligence=DatasetIntelligenceService(
            dataset_repo=DatasetRepository(db), schema_repo=SchemaRepository(db)
        ),
        kpi_source=_StubKPISource(rows),
    )

    pairwise = CrossDatasetAnalyzer(**kwargs).analyze("sales")
    graph_aware = CrossDatasetAnalyzer(**kwargs, graph_store=store).analyze("sales")

    # Same measurements either way — the graph adds knowledge, not numbers.
    assert graph_aware["supporting"] == pairwise["supporting"]
    # But the dataset C4 could only call "activated_dataset" is now
    # recognised as a known relative, with the traversal that says so.
    entry = next(e for e in graph_aware["contradicting"] if e["dataset_id"] == "ds-supply")
    assert entry["relation"].startswith("graph_")
    assert entry["graph_relation"]["matched_metric"] == "supply_delay"
    assert entry["graph_relation"]["edges"][0]["confidence"] == pytest.approx(0.87)
    assert "supply_delay" in graph_aware["graph_known_metrics"]


def test_known_related_metrics_ignores_structural_and_governance_edges(db, store):
    # derived_from and governed_by describe structure and governance. They
    # say nothing about whether two signals MOVE TOGETHER, so following
    # them here would let "both live in the same dataset" masquerade as a
    # behavioural relationship.
    source_id = SourceRepository(db).create(Source(name="Warehouse", kind="s3"))
    _seed_dataset(db, "ds-1", "Sales", ["sales", "unrelated_measure"], source_id=source_id)
    PolicyRepository(db).create(Policy(related_metrics=["sales"], business_rule="Governs sales"))
    _builder(db, store).build()

    known = known_related_metrics(store, "sales")
    assert "unrelated_measure" not in known


def test_known_related_metrics_degrades_to_empty_on_failure(store, monkeypatch):
    def _boom(*_a, **_k):
        raise RuntimeError("nope")

    monkeypatch.setattr(BusinessGraphStore, "neighborhood", _boom)
    assert known_related_metrics(store, "sales") == {}


# ===========================================================================
# 7. Advisory boundary (AGENT-5) — the regression that matters most
# ===========================================================================


def test_flag_off_cross_dataset_output_is_key_for_key_identical(db, store):
    _labeled_scenario(db, store)
    rows = {
        "ds-origin": [{"date": f"2026-07-0{i + 1}", "sales": v} for i, v in enumerate([100, 101, 99, 40])],
        "ds-supply": [{"event_timestamp": f"2026-07-0{i + 1}", "supply_delay": 5.0} for i in range(4)],
    }
    from aeam.intelligence.dataset_intelligence import DatasetIntelligenceService

    kwargs = dict(
        dataset_activation=_StubActivation(["ds-origin", "ds-supply"]),
        intelligence=DatasetIntelligenceService(
            dataset_repo=DatasetRepository(db), schema_repo=SchemaRepository(db)
        ),
        kpi_source=_StubKPISource(rows),
    )

    # graph_store=None is the flag-off posture, and must reproduce Phase C4
    # exactly — same keys, same values. This is the F4 rollback guarantee.
    without_graph = CrossDatasetAnalyzer(**kwargs).analyze("sales")
    explicit_none = CrossDatasetAnalyzer(**kwargs, graph_store=None).analyze("sales")

    assert without_graph == explicit_none
    assert "graph_aware" not in without_graph
    assert "graph_known_metrics" not in without_graph


def test_the_graph_modules_never_import_the_deterministic_engines():
    # The strongest form of "the graph never overrides a deterministic
    # decision" is that it cannot reach one. A graph module that imported
    # RuleEngine or StatisticalDetector could grow that capability later
    # without anyone noticing; this fails the moment one does.
    from pathlib import Path

    forbidden = ("RuleEngine", "StatisticalDetector", "KPIAgent", "ForecastAgent",
                 "DecisionEngine", "ActionAgent")
    for module in ("business_graph.py", "graph_correlation.py"):
        source = (Path(__file__).resolve().parents[1] / "intelligence" / module).read_text(
            encoding="utf-8"
        )
        imports = [ln for ln in source.splitlines() if ln.startswith(("import ", "from "))]
        for line in imports:
            for name in forbidden:
                assert name not in line, f"{module} must not import {name}: {line!r}"


def test_the_engine_has_no_method_that_writes_to_the_graph():
    # The Orchestrator's stage must be able to READ only. An engine with a
    # write path could grow the graph from its own output, which is how an
    # advisory source quietly becomes a self-reinforcing one.
    write_names = {"build", "upsert_node", "upsert_edge", "sweep_stale", "write", "save"}
    assert not (write_names & set(dir(GraphCorrelationEngine)))


def test_an_investigation_never_writes_to_the_graph(db, store):
    _seed_incident(db, "inc-1", "sales", [
        _cross_dataset_finding(correlations=[{"metric": "refunds", "correlation": 0.9}])
    ])
    _builder(db, store).build()
    before = store.stats()

    for _ in range(3):
        GraphCorrelationEngine(store).analyze("sales")

    assert store.stats() == before


# ---------------------------------------------------------------------------
# End-to-end: a real Orchestrator investigation over a real graph
# ---------------------------------------------------------------------------


class _RecordingLTM:
    """Captures the finalized incident payload the Orchestrator persists."""

    def __init__(self):
        self.recorded = None

    def record_incident(self, payload):
        self.recorded = payload
        return payload.get("incident_id", "fake-id")

    def get_metric_history(self, *_a, **_k):
        return []


def _orchestrator(business_graph_engine=None):
    from aeam.agents.orchestrator.orchestrator import Orchestrator
    from aeam.core.event_bus import EventBus
    from aeam.core.event_models import Event

    settings = Settings(
        DATABASE_URL="sqlite:///:memory:", REDIS_URL="redis://localhost:6379/0",
        VECTOR_DB_URL="http://localhost", ENVIRONMENT="development", LLM_ENABLED=False,
    )
    from aeam.agents.orchestrator.decision_engine import DecisionEngine
    from aeam.agents.orchestrator.evaluation_engine import EvaluationEngine

    ltm = _RecordingLTM()
    orch = Orchestrator(
        event_bus=EventBus(),
        decision_engine=DecisionEngine(settings=settings),
        evaluation_engine=EvaluationEngine(settings=settings),
        long_term_memory=ltm,
        settings=settings,
        business_graph_engine=business_graph_engine,
    )
    event = Event(
        event_id="1", event_type="KPI_DROP", metric="sales", severity="HIGH",
        current_value=40, expected_value=100, detection_methods=["rule"],
        timestamp="2026-07-05T00:00:00Z",
    )
    return orch, ltm, event


def test_end_to_end_investigation_appends_the_graph_finding(db, store):
    _labeled_scenario(db, store)
    orch, ltm, event = _orchestrator(GraphCorrelationEngine(store))

    orch.handle_event(event)

    findings = ltm.recorded["findings"]
    graph_findings = [f for f in findings if f.get("type") == "graph"]
    assert len(graph_findings) == 1, "the graph stage must run exactly once per incident"
    data = graph_findings[0]["data"]
    assert data["available"] is True
    # And the relationship it surfaced carries its evidence, end to end.
    supply = next(m for m in data["correlated_metrics"] if m["label"] == "supply_delay")
    assert supply["edges"][0]["observation_count"] == 3


def test_end_to_end_the_graph_finding_changes_no_deterministic_output(db, store):
    _labeled_scenario(db, store)

    without, ltm_without, event = _orchestrator(None)
    without.handle_event(event)
    with_graph, ltm_with, event2 = _orchestrator(GraphCorrelationEngine(store))
    with_graph.handle_event(event2)

    baseline, graphed = ltm_without.recorded, ltm_with.recorded

    # The graph adds one evidence entry and NOTHING else. Every field the
    # deterministic path produces is identical.
    assert {f.get("type") for f in graphed["findings"]} - {
        f.get("type") for f in baseline["findings"]
    } == {"graph"}
    for field in ("severity", "root_cause", "confidence", "action_taken", "requires_human"):
        assert graphed.get(field) == baseline.get(field), (
            f"{field} changed when the graph was enabled — the graph is advisory "
            "and must never alter a deterministic outcome"
        )


def test_end_to_end_a_broken_graph_never_breaks_an_investigation(db, store):
    class _Broken:
        def analyze(self, metric):
            raise RuntimeError("boom")

    orch, ltm, event = _orchestrator(_Broken())
    orch.handle_event(event)  # must not raise

    graph_finding = next(f for f in ltm.recorded["findings"] if f.get("type") == "graph")
    # Honest degradation with the real reason, not a crash and not a
    # fabricated empty neighbourhood.
    assert graph_finding["data"]["available"] is False
    assert "boom" in graph_finding["data"]["reason"]


def test_orchestrator_wires_the_graph_like_every_other_advisory_source():
    from aeam.agents.orchestrator.orchestrator import Orchestrator

    assert "business_graph_engine" in Orchestrator.__init__.__code__.co_varnames
    assert hasattr(Orchestrator, "_has_graph_finding")
    # The stage lives inside the SAME _investigate() the other advisory
    # sources run in — not a second coordinator, not a parallel path.
    assert "graph" in Orchestrator._investigate.__code__.co_consts


def test_orchestrator_skips_the_graph_stage_entirely_when_unwired(db):
    # Flag off => business_graph_engine is None => no graph finding at all,
    # and the investigation is byte-identical to F3's.
    orch, ltm, event = _orchestrator(None)
    orch.handle_event(event)
    assert [f for f in ltm.recorded["findings"] if f.get("type") == "graph"] == []


# ===========================================================================
# 8. Concurrency (ARCH-8)
# ===========================================================================


def test_concurrent_upserts_of_the_same_edge_converge_to_one_row(db, store):
    node_repo, edge_repo = GraphNodeRepository(db), GraphEdgeRepository(db)
    node_repo.upsert(GraphNode(node_key="metric:a", node_type=GraphNodeType.METRIC, label="a"))
    node_repo.upsert(GraphNode(node_key="metric:b", node_type=GraphNodeType.METRIC, label="b"))
    errors: list[Exception] = []

    def _write():
        try:
            for _ in range(10):
                edge_repo.upsert(GraphEdge(
                    source_key="metric:a", target_key="metric:b",
                    edge_type=GraphEdgeType.CORRELATES_WITH, confidence=0.8,
                ))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_write) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert store.stats()["edges"] == 1


def test_concurrent_builds_converge_rather_than_duplicating(db, store):
    _seed_dataset(db, "ds-1", "Sales", ["sales", "refunds"])
    _seed_incident(db, "inc-1", "sales", [
        _cross_dataset_finding(correlations=[{"metric": "refunds", "correlation": 0.9}])
    ])
    errors: list[Exception] = []

    def _build():
        try:
            _builder(db, store).build()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_build) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    concurrent_stats = store.stats()

    # Deterministic ids mean four racing builders write the SAME rows, and
    # the timestamp-based sweep means none of them deletes another's work.
    # So the graph four builders produced must be the graph one produces.
    single_report = _builder(db, store).build()
    assert single_report.nodes_retired == 0, "a racing build left an orphan row behind"
    assert single_report.edges_retired == 0
    assert store.stats() == concurrent_stats
    assert store.get_node("metric:sales") is not None
    assert store.get_node("metric:refunds") is not None


def test_a_sweep_never_deletes_a_concurrent_builders_rows(db, store):
    # The sweep key is last_seen_at, not build id, precisely so a row
    # another builder just wrote is newer than this build's cutoff.
    node_repo = GraphNodeRepository(db)
    node_repo.upsert(GraphNode(node_key="metric:written_now", node_type=GraphNodeType.METRIC,
                               label="written_now"))
    cutoff = "2020-01-01T00:00:00+00:00"
    nodes_removed, _ = store.sweep_stale(cutoff)
    assert nodes_removed == 0
    assert store.get_node("metric:written_now") is not None


# ===========================================================================
# 9. API surface
# ===========================================================================


@pytest.fixture()
def client(db):
    class _Container:
        pass

    container = _Container()
    container.db = db
    container.settings = Settings(
        DATABASE_URL="sqlite:///:memory:", REDIS_URL="redis://localhost:6379/0",
        VECTOR_DB_URL="http://localhost", ENVIRONMENT="development",
        BUSINESS_GRAPH_ENABLED=True,
    )
    container.audit_logger = None

    app = FastAPI()
    app.include_router(graph_router)
    app.state.container = container
    return TestClient(app)


def test_api_stats_distinguishes_disabled_from_empty(client):
    body = client.get("/api/v1/graph/stats").json()
    assert body["enabled"] is True
    assert body["nodes"] == 0 and body["edges"] == 0
    assert set(body["node_types"]) == GraphNodeType.ALL
    assert set(body["edge_types"]) == GraphEdgeType.ALL


def test_api_build_is_deterministic_and_reports_what_it_did(client, db):
    _seed_dataset(db, "ds-1", "Sales", ["sales"])
    first = client.post("/api/v1/graph/build", json={}).json()
    second = client.post("/api/v1/graph/build", json={}).json()

    assert first["built"] is True
    assert first["nodes_written"] == second["nodes_written"]
    assert second["nodes_retired"] == 0 and second["edges_retired"] == 0


def test_api_neighborhood_reports_the_budget_it_actually_ran_under(client, db):
    _seed_dataset(db, "ds-1", "Sales", ["sales"])
    client.post("/api/v1/graph/build", json={})

    body = client.get("/api/v1/graph/neighborhood?metric=sales&max_depth=9999").json()
    assert body["origin_found"] is True
    assert body["budget"]["max_depth"] == MAX_DEPTH_CEILING


def test_api_neighborhood_requires_a_target(client):
    assert client.get("/api/v1/graph/neighborhood").status_code == 400


def test_api_rejects_an_unknown_edge_type(client):
    resp = client.get("/api/v1/graph/neighborhood?metric=sales&edge_type=invented_relation")
    assert resp.status_code == 400
    assert "Unknown edge_type" in resp.json()["detail"]


def test_api_node_search_is_always_bounded(client, db):
    for i in range(120):
        _seed_dataset(db, f"ds-{i}", f"Dataset {i}", [f"m{i}"])
    client.post("/api/v1/graph/build", json={})

    body = client.get("/api/v1/graph/nodes?q=&limit=25").json()
    assert len(body["nodes"]) <= 25
    # And no parameter combination returns the whole graph.
    assert client.get("/api/v1/graph/nodes?limit=100000").status_code == 422


def test_api_rbac_separates_reads_from_the_privileged_build():
    from aeam.middleware.security_middleware import _ENDPOINT_RBAC_MAP

    entries = {p: (r, a) for p, r, a in _ENDPOINT_RBAC_MAP if p.startswith("/api/v1/graph")}
    assert entries["/api/v1/graph/stats"] == ("documents", "search")
    assert entries["/api/v1/graph/neighborhood"] == ("documents", "search")
    # Anything else under /graph — including a write surface added later —
    # falls through to the strictest tier by default (SEC-1).
    assert entries["/api/v1/graph"] == ("admin", "config")
    # And the read prefixes must be listed BEFORE the catch-all, or
    # longest-prefix resolution would grade a read as a write.
    order = [p for p, _r, _a in _ENDPOINT_RBAC_MAP if p.startswith("/api/v1/graph")]
    assert order.index("/api/v1/graph") == len(order) - 1
