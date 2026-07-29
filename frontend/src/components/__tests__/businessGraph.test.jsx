import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { getBusinessGraphData } from "../ui";
import BusinessGraphPanel from "../BusinessGraphPanel";

/* ──────────────────────────────────────────────────────────────────────────
 * Phase F4 — the console half of "graph-derived correlations appear as
 * their own advisory finding with disclosed edge confidence".
 *
 * The backend suite proves the finding is produced and bounded. These prove
 * it is SHOWN, and shown honestly — which for a graph means three distinct
 * states never collapsing into one another:
 *
 *   1. never consulted (an older incident, or the flag was off)
 *   2. consulted, but the graph has no node for this metric
 *   3. consulted, node present, relationships found
 *
 * A UI that rendered all three as "no relationships" would let an operator
 * read "we checked and found nothing" out of "we never checked" — exactly
 * the conflation COMPAT-1 exists to prevent.
 *
 * They also prove the disclosure requirements hold in the rendered output:
 * no relationship appears without its traversal depth and path confidence,
 * and a truncated traversal is labelled as truncated rather than presented
 * as a complete neighbourhood.
 * ────────────────────────────────────────────────────────────────────────── */

function incidentWith(data) {
  return { findings: [{ type: "graph", data }] };
}

const AVAILABLE = {
  available: true,
  reason: null,
  origin_key: "metric:checkout_latency",
  origin_label: "checkout_latency",
  budget: { max_depth: 2, max_nodes: 100, max_edges: 300, min_confidence: 0 },
  truncated: false,
  truncation_reason: null,
  depth_reached: 2,
  nodes_visited: 3,
  edges_traversed: 4,
  correlated_metrics: [
    {
      node_key: "metric:payment_errors",
      node_type: "metric",
      label: "payment_errors",
      depth: 1,
      relation: "correlates_with",
      path: ["metric:checkout_latency", "metric:payment_errors"],
      edges: [
        {
          edge_type: "correlates_with",
          confidence: 0.88,
          observation_count: 4,
          from: "metric:checkout_latency",
          to: "metric:payment_errors",
        },
      ],
      edge_confidences: [0.88],
      path_confidence: 0.88,
      direct: true,
    },
  ],
  governing_policies: [],
  related_datasets: [],
  related_services: [],
  prior_incidents: [],
  related_total: 1,
};

describe("getBusinessGraphData", () => {
  it("returns null for an incident recorded before F4", () => {
    expect(getBusinessGraphData({ findings: [{ type: "cross_dataset", data: {} }] })).toBeNull();
  });

  it("returns null for an incident with no findings at all", () => {
    expect(getBusinessGraphData({})).toBeNull();
    expect(getBusinessGraphData(null)).toBeNull();
  });

  it("reads the graph finding when one is present", () => {
    const data = getBusinessGraphData(incidentWith(AVAILABLE));
    expect(data.origin_key).toBe("metric:checkout_latency");
    expect(data.correlated_metrics).toHaveLength(1);
  });

  it("takes the LAST graph finding when an investigation recorded more than one", () => {
    const incident = {
      findings: [
        { type: "graph", data: { ...AVAILABLE, related_total: 1 } },
        { type: "graph", data: { ...AVAILABLE, related_total: 9 } },
      ],
    };
    expect(getBusinessGraphData(incident).related_total).toBe(9);
  });
});

describe("BusinessGraphPanel", () => {
  it("says the graph was never consulted rather than showing an empty result", () => {
    render(<BusinessGraphPanel incident={{ findings: [] }} />);
    expect(screen.getByText(/was not consulted/i)).toBeTruthy();
  });

  it("distinguishes 'metric absent from the graph' and shows the real reason", () => {
    render(
      <BusinessGraphPanel
        incident={incidentWith({
          available: false,
          reason: "The business graph holds no node for metric 'checkout_latency'.",
          correlated_metrics: [],
        })}
      />
    );
    expect(screen.getByText(/No graph relationships available/i)).toBeTruthy();
    expect(screen.getByText(/holds no node for metric/i)).toBeTruthy();
  });

  it("distinguishes 'present but unconnected' from both of the above", () => {
    render(
      <BusinessGraphPanel
        incident={incidentWith({
          ...AVAILABLE,
          correlated_metrics: [],
          related_total: 0,
          depth_reached: 0,
          nodes_visited: 1,
          edges_traversed: 0,
        })}
      />
    );
    expect(screen.getByText(/nothing is connected to it yet/i)).toBeTruthy();
  });

  it("renders a relationship with its depth, confidence and traversal path", () => {
    render(<BusinessGraphPanel incident={incidentWith(AVAILABLE)} />);
    expect(screen.getByText("payment_errors")).toBeTruthy();
    expect(screen.getByText("correlates_with")).toBeTruthy();
    expect(screen.getByText("depth 1")).toBeTruthy();
    expect(screen.getByText("conf 0.88")).toBeTruthy();
    // The path is what makes the claim checkable, so it must be rendered —
    // once in the traversal header and once as the path's first hop.
    expect(screen.getAllByText("metric:checkout_latency").length).toBeGreaterThan(1);
    // And how many investigations backed the strongest edge.
    expect(screen.getByText(/observed in 4 investigation/i)).toBeTruthy();
  });

  it("labels a truncated traversal instead of implying it was complete", () => {
    render(
      <BusinessGraphPanel
        incident={incidentWith({
          ...AVAILABLE,
          truncated: true,
          truncation_reason: "edge_budget_exhausted",
        })}
      />
    );
    expect(screen.getByText(/Traversal stopped early/i)).toBeTruthy();
    expect(screen.getByText(/edge_budget_exhausted/i)).toBeTruthy();
  });
});
