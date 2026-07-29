import { Badge, Icon, getBusinessGraphData } from "./ui";

/* ──────────────────────────────────────────────────────────────────────────
 * Business Graph view (Phase F4).
 *
 * Renders what the platform ALREADY KNOWS relates to this incident's
 * metric, read from the persisted business graph during the investigation.
 * See aeam/intelligence/graph_correlation.py: this is advisory evidence
 * only, never capable of altering a deterministic RuleEngine /
 * StatisticalDetector / KPIAgent / ForecastAgent outcome.
 *
 * Complementary to — not a replacement for — the Cross-Dataset panel next
 * to it. C4 scans the CURRENTLY-ACTIVATED datasets pairwise, right now.
 * The graph reports relationships measured in PAST investigations, so it
 * can surface a metric whose dataset is no longer activated and can reach
 * two hops out. Both are shown because they answer different questions.
 *
 * Four honest states, deliberately distinguished rather than collapsed
 * into one "nothing here":
 * - Never consulted for this investigation (an older incident, or the
 *   graph was disabled at the time) — getBusinessGraphData() is null.
 * - Consulted, but the graph holds no node for this metric — available is
 *   false, with the real reason shown (usually: no build has run since
 *   this metric first appeared).
 * - Consulted, metric present, but nothing connected to it yet.
 * - Consulted, relationships found — grouped by entity type, each entry
 *   showing its traversal depth, the edges walked with their individual
 *   confidences, and the compounded path confidence.
 *
 * Every group discloses provenance: nothing here is asserted without the
 * path and edges that support it, and a truncated traversal says so
 * rather than presenting a partial neighbourhood as complete.
 * ────────────────────────────────────────────────────────────────────────── */

const EMPTY_BOX = {
  textAlign: "center", padding: "2rem 1rem", color: "var(--muted)",
  fontSize: "0.8rem", border: "1px dashed var(--border)", borderRadius: 10,
};

function PathTrace({ entry }) {
  const path = entry.path || [];
  const edges = entry.edges || [];
  if (path.length < 2) return null;
  return (
    <div style={{
      fontSize: "0.66rem", color: "var(--muted)", fontFamily: "var(--font-mono)",
      display: "flex", flexWrap: "wrap", alignItems: "center", gap: "0.3rem",
      marginTop: "0.35rem",
    }}>
      {path.map((key, i) => (
        <span key={`${key}-${i}`} style={{ display: "inline-flex", alignItems: "center", gap: "0.3rem" }}>
          <span style={{ color: i === 0 ? "var(--text)" : "var(--muted)" }}>{key}</span>
          {i < edges.length && (
            <span style={{ color: "var(--faint)" }}>
              &nbsp;—[{edges[i].edge_type} {Number(edges[i].confidence).toFixed(2)}]→&nbsp;
            </span>
          )}
        </span>
      ))}
    </div>
  );
}

function RelatedRow({ entry }) {
  const color = entry.direct ? "var(--info)" : "var(--muted)";
  return (
    <div style={{
      padding: "0.55rem 0.8rem", border: "1px solid var(--border)", borderRadius: 8,
      background: "rgba(255,255,255,0.015)",
    }}>
      <div style={{
        display: "flex", alignItems: "center", justifyContent: "space-between",
        gap: "0.75rem", flexWrap: "wrap",
      }}>
        <div style={{ display: "flex", alignItems: "center", gap: "0.5rem", minWidth: 0 }}>
          <Icon name="branch" size={12} color="var(--muted)" />
          <span style={{ fontSize: "0.78rem", color: "var(--text)", fontWeight: 600 }}>
            {entry.label || entry.node_key}
          </span>
        </div>
        <div style={{ display: "flex", gap: "0.4rem", flexWrap: "wrap", alignItems: "center" }}>
          {entry.relation && <Badge label={entry.relation} color="var(--muted)" />}
          <Badge label={`depth ${entry.depth}`} color={color} />
          {entry.path_confidence != null && (
            <Badge label={`conf ${Number(entry.path_confidence).toFixed(2)}`} color={color} />
          )}
        </div>
      </div>
      <PathTrace entry={entry} />
      {(entry.edges || []).some((e) => e.observation_count > 1) && (
        <div style={{ fontSize: "0.66rem", color: "var(--faint)", marginTop: "0.3rem" }}>
          strongest edge observed in{" "}
          {Math.max(...(entry.edges || []).map((e) => e.observation_count || 1))} investigation(s)
        </div>
      )}
    </div>
  );
}

function Section({ title, icon, color, entries }) {
  if (!entries || entries.length === 0) return null;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "0.4rem" }}>
      <div style={{ display: "flex", alignItems: "center", gap: "0.4rem" }}>
        <Icon name={icon} size={13} color={color} />
        <span style={{
          fontSize: "0.68rem", textTransform: "uppercase", letterSpacing: "0.1em",
          color: "var(--muted)", fontWeight: 700,
        }}>
          {title} ({entries.length})
        </span>
      </div>
      {entries.map((e, i) => <RelatedRow key={e.node_key ? `${e.node_key}-${i}` : i} entry={e} />)}
    </div>
  );
}

export default function BusinessGraphPanel({ incident }) {
  const data = getBusinessGraphData(incident);

  if (data === null) {
    return (
      <div style={EMPTY_BOX}>
        The Business Graph was not consulted for this investigation.
      </div>
    );
  }

  if (!data.available) {
    return (
      <div style={EMPTY_BOX}>
        <Icon name="alert" size={16} style={{ marginBottom: "0.4rem", opacity: 0.7 }} /><br />
        No graph relationships available for this metric.
        <div style={{ marginTop: "0.4rem", fontSize: "0.72rem" }}>{data.reason}</div>
      </div>
    );
  }

  const metrics = data.correlated_metrics || [];
  const policies = data.governing_policies || [];
  const datasets = data.related_datasets || [];
  const services = data.related_services || [];
  const incidents = data.prior_incidents || [];
  const nothingFound =
    metrics.length === 0 && policies.length === 0 && datasets.length === 0 &&
    services.length === 0 && incidents.length === 0;
  const budget = data.budget || {};

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
      <div style={{ fontSize: "0.72rem", color: "var(--muted)" }}>
        Traversed from <strong style={{ color: "var(--text)" }}>{data.origin_key}</strong> —{" "}
        {data.nodes_visited ?? 0} node(s) and {data.edges_traversed ?? 0} edge(s) read, to depth{" "}
        {data.depth_reached ?? 0} of {budget.max_depth ?? "?"}
      </div>

      {data.truncated && (
        <div style={{
          fontSize: "0.7rem", color: "var(--warn)", border: "1px solid var(--border)",
          borderRadius: 8, padding: "0.5rem 0.7rem",
        }}>
          <Icon name="alert" size={12} color="var(--warn)" />{" "}
          Traversal stopped early ({data.truncation_reason}) — more relationships may exist
          beyond the query budget.
        </div>
      )}

      {nothingFound && (
        <div style={{
          textAlign: "center", padding: "1.4rem 1rem", color: "var(--muted)",
          fontSize: "0.78rem", border: "1px dashed var(--border)", borderRadius: 10,
        }}>
          This metric is in the graph, but nothing is connected to it yet.
        </div>
      )}

      <Section title="Correlated Metrics" icon="branch" color="var(--info)" entries={metrics} />
      <Section title="Governing Policies" icon="shield" color="var(--accent)" entries={policies} />
      <Section title="Prior Incidents" icon="layers" color="var(--warn)" entries={incidents} />
      <Section title="Datasets" icon="database" color="var(--muted)" entries={datasets} />
      <Section title="Source Systems" icon="database" color="var(--muted)" entries={services} />
    </div>
  );
}
