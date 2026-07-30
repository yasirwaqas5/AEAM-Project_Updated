import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import MeshHealthPanel from "../MeshHealthPanel";
import { buildMeshLive } from "../ui";
import { ENGINES } from "../three/AgentMesh";

/* ──────────────────────────────────────────────────────────────────────────
 * Phase F6 — the console half of "the Supervisor surfaces mesh health and a
 * behaviour anomaly, and both agents appear in the console mesh".
 *
 * The backend suite proves the report is built from observed telemetry only.
 * These prove the console does not undo that:
 *
 *  - "oversight disabled" must never render as a healthy mesh. Those are
 *    opposite facts and a single "all good" would conflate them.
 *  - a withheld score must show its reason, never a placeholder number.
 *  - every anomaly must render the metrics behind it — an anomaly without
 *    evidence is an opinion.
 *  - the panel must offer NO action control, because the Supervisor has no
 *    coordination authority (ARCH-1).
 * ────────────────────────────────────────────────────────────────────────── */

const HEALTHY = {
  supervisor_enabled: true,
  reason: null,
  roster: ["orchestrator", "planning", "supervisor"],
  agents: [
    {
      agent: "monitor",
      scope: "thread_worker",
      heartbeat: { instrumented: true, state: "live", age_seconds: 2 },
      executions: { observed: true, count: 12 },
      participation: { measured: false },
    },
    {
      agent: "planning",
      scope: "request_scoped",
      heartbeat: { instrumented: true, state: "idle", age_seconds: 900 },
      executions: { observed: true, count: 3 },
      participation: { measured: false },
    },
  ],
  issues: [],
  recommended_escalations: [],
  mesh_health: {
    available: true,
    score: 0.85,
    components: {},
    formula: "unweighted mean of the 2 computable component(s): agent_activity, heartbeat_health.",
    issue_counts: { critical: 0, warning: 0, info: 0 },
    reason: null,
  },
};

const WITH_ANOMALY = {
  ...HEALTHY,
  issues: [
    {
      agent: "monitor",
      kind: "stale_heartbeat",
      severity: "critical",
      reason: "monitor last reported 400s ago, beyond the 120s staleness threshold.",
      evidence: {
        heartbeat_age_seconds: 400,
        stale_after_seconds: 120,
        source: "HeartbeatTracker (Phase E7)",
      },
      recommended_escalation: "Escalate to an operator to inspect the monitor thread.",
    },
  ],
  mesh_health: { ...HEALTHY.mesh_health, issue_counts: { critical: 1, warning: 0, info: 0 } },
};

describe("MeshHealthPanel", () => {
  it("says oversight was not queried rather than implying health", () => {
    render(<MeshHealthPanel mesh={null} />);
    expect(screen.getByText(/has not been queried/i)).toBeTruthy();
  });

  it("distinguishes disabled oversight from a healthy mesh", () => {
    render(
      <MeshHealthPanel
        mesh={{
          supervisor_enabled: false,
          reason: "SupervisorAgent is not wired (SUPERVISOR_AGENT_ENABLED=false).",
        }}
      />
    );
    expect(screen.getByText(/Mesh oversight is disabled/i)).toBeTruthy();
    expect(screen.getByText(/SUPERVISOR_AGENT_ENABLED=false/)).toBeTruthy();
    expect(screen.queryByText(/mesh health/i)).toBeNull();
  });

  it("renders the score with the formula that produced it", () => {
    render(<MeshHealthPanel mesh={HEALTHY} />);
    expect(screen.getByText("85%")).toBeTruthy();
    expect(screen.getByText(/unweighted mean of the 2 computable/)).toBeTruthy();
  });

  it("shows the reason when no score is computable, never a placeholder", () => {
    render(
      <MeshHealthPanel
        mesh={{
          ...HEALTHY,
          mesh_health: {
            available: false,
            score: null,
            components: {},
            formula: null,
            issue_counts: { critical: 0, warning: 0, info: 0 },
            reason: "No health component is computable.",
          },
        }}
      />
    );
    expect(screen.getByText(/not computable/i)).toBeTruthy();
    expect(screen.getByText(/No health component is computable/)).toBeTruthy();
    expect(screen.queryByText("0%")).toBeNull();
  });

  it("states plainly when no anomaly was observed", () => {
    render(<MeshHealthPanel mesh={HEALTHY} />);
    expect(screen.getByText(/No behaviour anomalies observed/i)).toBeTruthy();
  });

  it("renders an anomaly with its affected agent, reason, evidence and source", () => {
    render(<MeshHealthPanel mesh={WITH_ANOMALY} />);
    expect(screen.getByText("stale_heartbeat")).toBeTruthy();
    expect(screen.getByText(/beyond the 120s staleness threshold/)).toBeTruthy();
    expect(screen.getByText("heartbeat_age_seconds=400")).toBeTruthy();
    expect(screen.getByText("stale_after_seconds=120")).toBeTruthy();
    expect(screen.getByText(/source: HeartbeatTracker/)).toBeTruthy();
    expect(screen.getByText(/Escalate to an operator/)).toBeTruthy();
  });

  it("renders a request-scoped agent as idle, not stale", () => {
    render(<MeshHealthPanel mesh={HEALTHY} />);
    expect(screen.getByText("idle")).toBeTruthy();
    expect(screen.queryByText("stale")).toBeNull();
  });

  it("offers no action control — the Supervisor has no coordination authority", () => {
    const { container } = render(<MeshHealthPanel mesh={WITH_ANOMALY} />);
    expect(container.querySelectorAll("button").length).toBe(0);
    expect(screen.getByText(/Advisory only/i)).toBeTruthy();
  });
});

describe("console mesh membership", () => {
  it("includes both agents formalized in F6", () => {
    const keys = ENGINES.map((e) => e.key);
    expect(keys).toContain("supervisor");
    expect(keys).toContain("plan");
    const planning = ENGINES.find((e) => e.key === "plan");
    expect(planning.label).toBe("Planning Agent");
    const supervisor = ENGINES.find((e) => e.key === "supervisor");
    // The node's own description must not claim coordination.
    expect(supervisor.purpose).toMatch(/never coordinates/i);
  });
});

describe("buildMeshLive supervisor node", () => {
  it("reports unknown when the mesh report was not fetched", () => {
    const live = buildMeshLive([], null, null);
    expect(live.supervisor.state).toBe("idle");
    expect(live.supervisor.lastActivity).toMatch(/not queried/i);
  });

  it("reports disabled oversight distinctly from an observed mesh", () => {
    const live = buildMeshLive([], null, { supervisor_enabled: false });
    expect(live.supervisor.lastActivity).toMatch(/disabled/i);
  });

  it("reports an observed mesh with its issue count and health", () => {
    const live = buildMeshLive([], null, WITH_ANOMALY);
    expect(live.supervisor.state).toBe("active");
    expect(live.supervisor.lastActivity).toMatch(/1 issue/);
    expect(live.supervisor.health).toBe("85% mesh health");
  });

  it("reports zero anomalies as a real observation, not an absence", () => {
    const live = buildMeshLive([], null, HEALTHY);
    expect(live.supervisor.lastActivity).toMatch(/no anomalies/i);
  });

  it("stays backward compatible when called without the mesh argument", () => {
    const live = buildMeshLive([], null);
    expect(live.supervisor).toBeTruthy();
    expect(live.monitor).toBeTruthy();
  });
});
