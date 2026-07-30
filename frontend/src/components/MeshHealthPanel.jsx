import { Badge, Icon } from "./ui";

/* ──────────────────────────────────────────────────────────────────────────
 * Mesh Health panel (Phase F6).
 *
 * Renders GET /api/v1/mesh/health — the Supervisor Agent's observation of the
 * mesh as a whole. See aeam/agents/supervisor/supervisor_agent.py: the
 * Supervisor observes and recommends; it never coordinates, executes, or
 * restarts anything, and this panel is correspondingly read-only. There is
 * deliberately no action button here — one would hand the Supervisor the
 * coordination authority ARCH-1 reserves for the Orchestrator.
 *
 * Four honest states, kept distinguishable rather than collapsed into a
 * single "all good":
 *  - the report was never fetched;
 *  - oversight is disabled (SUPERVISOR_AGENT_ENABLED=false) — which is NOT
 *    the same as a healthy mesh;
 *  - oversight ran and no health component was computable (score withheld
 *    with its reason, never a placeholder number);
 *  - oversight ran and found issues, each shown with the metrics behind it.
 * ────────────────────────────────────────────────────────────────────────── */

const SEVERITY_COLOR = {
  critical: "var(--err)",
  warning: "var(--warn)",
  info: "var(--info)",
};

const EMPTY_BOX = {
  textAlign: "center", padding: "1.6rem 1rem", color: "var(--muted)",
  fontSize: "0.8rem", border: "1px dashed var(--border)", borderRadius: 10,
};

function ScoreRing({ score }) {
  const pct = Math.round(score * 100);
  const color = pct >= 80 ? "var(--ok)" : pct >= 50 ? "var(--warn)" : "var(--err)";
  return (
    <div style={{ display: "flex", alignItems: "baseline", gap: "0.4rem" }}>
      <span style={{ fontSize: "1.6rem", fontWeight: 700, color, fontFamily: "var(--font-mono)" }}>{pct}%</span>
      <span style={{ fontSize: "0.68rem", color: "var(--muted)" }}>mesh health</span>
    </div>
  );
}

function IssueRow({ issue }) {
  const color = SEVERITY_COLOR[issue.severity] || "var(--muted)";
  const evidence = issue.evidence || {};
  const source = evidence.source;
  return (
    <div style={{
      border: "1px solid var(--border)", borderRadius: 8, padding: "0.55rem 0.75rem",
      background: "rgba(255,255,255,0.015)", display: "flex", flexDirection: "column", gap: "0.35rem",
    }}>
      <div style={{ display: "flex", alignItems: "center", gap: "0.5rem", flexWrap: "wrap" }}>
        <Badge label={issue.severity} color={color} dot />
        <span style={{ fontSize: "0.8rem", fontWeight: 600, color: "var(--text)" }}>{issue.agent}</span>
        <span style={{ fontSize: "0.66rem", fontFamily: "var(--font-mono)", color: "var(--faint)" }}>{issue.kind}</span>
      </div>
      <span style={{ fontSize: "0.74rem", color: "var(--text)", lineHeight: 1.5 }}>{issue.reason}</span>
      {/* Every issue must show the metrics that produced it — an anomaly
          without evidence is an opinion. */}
      <div style={{ display: "flex", gap: "0.4rem", flexWrap: "wrap" }}>
        {Object.entries(evidence)
          .filter(([k, v]) => k !== "source" && v != null)
          .map(([k, v]) => (
            <span key={k} style={{
              fontSize: "0.64rem", fontFamily: "var(--font-mono)", color: "var(--muted)",
              border: "1px solid var(--border)", borderRadius: 6, padding: "0.1rem 0.4rem",
            }}>{k}={String(v)}</span>
          ))}
      </div>
      {source && (
        <span style={{ fontSize: "0.62rem", color: "var(--faint)", fontStyle: "italic" }}>source: {source}</span>
      )}
      {issue.recommended_escalation && (
        <span style={{ fontSize: "0.68rem", color: "var(--muted)", lineHeight: 1.5 }}>
          <Icon name="alert" size={10} color="var(--muted)" /> {issue.recommended_escalation}
        </span>
      )}
    </div>
  );
}

function AgentRow({ agent }) {
  const hb = agent.heartbeat || {};
  const stateColor =
    hb.state === "live" ? "var(--ok)"
    : hb.state === "stale" ? "var(--err)"
    : hb.state === "idle" ? "var(--muted)"
    : "var(--warn)";
  return (
    <div style={{
      display: "flex", alignItems: "center", justifyContent: "space-between", gap: "0.6rem",
      padding: "0.4rem 0.7rem", border: "1px solid var(--border)", borderRadius: 8, flexWrap: "wrap",
    }}>
      <span style={{ fontSize: "0.78rem", fontWeight: 600, color: "var(--text)" }}>
        {agent.agent}
        <span style={{ fontSize: "0.64rem", color: "var(--faint)", fontWeight: 400 }}> · {agent.scope}</span>
      </span>
      <div style={{ display: "flex", gap: "0.35rem", flexWrap: "wrap", alignItems: "center" }}>
        <Badge label={hb.state || "unknown"} color={stateColor} dot />
        <span style={{ fontSize: "0.66rem", fontFamily: "var(--font-mono)", color: "var(--muted)" }}>
          {agent.executions?.observed
            ? `${agent.executions.count} exec`
            : "exec not instrumented"}
        </span>
        {agent.participation?.measured && (
          <span style={{ fontSize: "0.66rem", fontFamily: "var(--font-mono)", color: "var(--muted)" }}>
            {Math.round((agent.participation.rate ?? 0) * 100)}% contribution
          </span>
        )}
      </div>
    </div>
  );
}

export default function MeshHealthPanel({ mesh }) {
  if (!mesh) {
    return <div style={EMPTY_BOX}>Mesh oversight has not been queried.</div>;
  }

  if (mesh.supervisor_enabled === false) {
    return (
      <div style={EMPTY_BOX}>
        <Icon name="alert" size={16} style={{ marginBottom: "0.4rem", opacity: 0.7 }} /><br />
        Mesh oversight is disabled.
        <div style={{ marginTop: "0.4rem", fontSize: "0.72rem" }}>{mesh.reason}</div>
      </div>
    );
  }

  const health = mesh.mesh_health || {};
  const issues = mesh.issues || [];
  const agents = mesh.agents || [];
  const counts = health.issue_counts || {};

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: "0.8rem", flexWrap: "wrap" }}>
        {health.available
          ? <ScoreRing score={health.score} />
          : <span style={{ fontSize: "0.78rem", color: "var(--muted)" }}>Mesh health not computable</span>}
        <div style={{ display: "flex", gap: "0.4rem", flexWrap: "wrap" }}>
          {["critical", "warning", "info"].map((s) => (
            <Badge key={s} label={`${counts[s] ?? 0} ${s}`} color={SEVERITY_COLOR[s]} dot />
          ))}
        </div>
      </div>

      {/* The formula is disclosed so nobody has to trust the number. */}
      {health.available
        ? <span style={{ fontSize: "0.66rem", color: "var(--faint)", lineHeight: 1.5 }}>{health.formula}</span>
        : <span style={{ fontSize: "0.7rem", color: "var(--muted)", lineHeight: 1.5 }}>{health.reason}</span>}

      <div>
        <span style={{ fontSize: "0.62rem", textTransform: "uppercase", letterSpacing: "0.12em", color: "var(--muted)" }}>
          Roster ({agents.length})
        </span>
        <div style={{ display: "flex", flexDirection: "column", gap: "0.35rem", marginTop: "0.4rem" }}>
          {agents.map((a) => <AgentRow key={a.agent} agent={a} />)}
        </div>
      </div>

      <div>
        <span style={{ fontSize: "0.62rem", textTransform: "uppercase", letterSpacing: "0.12em", color: "var(--muted)" }}>
          Observed anomalies ({issues.length})
        </span>
        <div style={{ marginTop: "0.4rem" }}>
          {issues.length === 0 ? (
            <div style={EMPTY_BOX}>No behaviour anomalies observed in the current telemetry.</div>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: "0.4rem" }}>
              {issues.map((issue, i) => <IssueRow key={`${issue.agent}-${issue.kind}-${i}`} issue={issue} />)}
            </div>
          )}
        </div>
      </div>

      <span style={{ fontSize: "0.62rem", color: "var(--faint)", fontStyle: "italic", lineHeight: 1.5 }}>
        Advisory only — the Supervisor observes and recommends. The Orchestrator remains the
        single coordinator; acting on a recommendation is an operator decision.
      </span>
    </div>
  );
}
