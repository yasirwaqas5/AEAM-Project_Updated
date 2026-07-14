import { Badge, Icon, getMemoryData, getMemoryMatches, fmtPct, fmtRelative } from "./ui";

/* ──────────────────────────────────────────────────────────────────────────
 * Enterprise Memory view (Phase C1).
 *
 * Renders similar RESOLVED INCIDENTS the Enterprise Memory Engine recalled
 * for this investigation — deliberately a separate component from
 * EvidencePanel.jsx, never merged into the same list. Knowledge documents
 * (RAG chunks) answer "what does our documentation say"; Enterprise Memory
 * answers "has AEAM seen something like this before, and what happened."
 *
 * Three honest states:
 * - Memory was never consulted for this investigation (older incident, or
 *   memory engine unavailable at the time) — getMemoryData() is null.
 * - Memory was consulted and found nothing similar — matches is [].
 * - Memory was consulted and found similar past incidents — render them,
 *   ranked by the SAME similarity score the retrieval pipeline computed
 *   (never invented).
 * ────────────────────────────────────────────────────────────────────────── */

function scoreColor(pct) {
  return pct >= 80 ? "#00ffa3" : pct >= 50 ? "#ffb800" : "#ff5f57";
}

const RESOLUTION_COLOR = {
  RESOLVED: "#00ffa3",
  COMPLETE: "#5a5f72",
  ESCALATED: "#ff5f57",
  FAILED: "#ff5f57",
};

function MemoryCard({ match }) {
  const simPct = match.similarity != null
    ? Math.round(match.similarity <= 1 ? match.similarity * 100 : match.similarity)
    : null;
  const shortId = match.incident_id ? (match.incident_id.length > 16 ? `${match.incident_id.slice(0, 14)}…` : match.incident_id) : "unknown";

  return (
    <div style={{
      border: "1px solid var(--border)", borderRadius: 10,
      background: "rgba(255,255,255,0.015)", overflow: "hidden",
    }}>
      <div style={{
        display: "flex", alignItems: "center", justifyContent: "space-between",
        padding: "0.7rem 0.9rem", borderBottom: "1px solid var(--border)", gap: "0.75rem", flexWrap: "wrap",
      }}>
        <div style={{ display: "flex", alignItems: "center", gap: "0.55rem", minWidth: 0 }}>
          <Icon name="layers" size={13} color="var(--muted)" />
          <span title={match.incident_id} style={{
            fontFamily: "var(--font-mono)", fontSize: "0.72rem", color: "var(--text)",
            overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
          }}>{shortId}</span>
        </div>
        <div style={{ display: "flex", gap: "0.4rem", flexWrap: "wrap" }}>
          {simPct != null && <Badge label={`similarity ${simPct}%`} color={scoreColor(simPct)} />}
          {match.resolution_status && (
            <Badge label={match.resolution_status} color={RESOLUTION_COLOR[match.resolution_status] || "var(--muted)"} />
          )}
        </div>
      </div>

      <div style={{ padding: "0.75rem 0.9rem", display: "flex", flexDirection: "column", gap: "0.5rem" }}>
        <div style={{ fontSize: "0.8rem", color: "var(--text)", lineHeight: 1.5 }}>
          {match.incident_summary || "(no summary recorded)"}
        </div>

        <div style={{ display: "flex", flexDirection: "column", gap: "0.3rem" }}>
          <span style={{ fontSize: "0.62rem", textTransform: "uppercase", letterSpacing: "0.1em", color: "var(--muted)" }}>Root Cause</span>
          <span style={{ fontSize: "0.78rem", color: match.root_cause ? "var(--text)" : "var(--muted)", fontStyle: match.root_cause ? "normal" : "italic" }}>
            {match.root_cause || "Not determined for this past incident."}
          </span>
        </div>

        <div style={{ display: "flex", gap: "1.2rem", flexWrap: "wrap", fontSize: "0.7rem", color: "var(--muted)" }}>
          {match.category && <span>Category: {match.category}</span>}
          {match.triggered_metric && <span>Metric: {match.triggered_metric}</span>}
          {match.confidence != null && <span>Confidence: {fmtPct(match.confidence)}</span>}
          {match.timestamp && <span>{fmtRelative(match.timestamp)}</span>}
        </div>
      </div>
    </div>
  );
}

export default function MemoryPanel({ incident }) {
  const data = getMemoryData(incident);
  const matches = getMemoryMatches(incident);

  if (data === null) {
    return (
      <div style={{
        textAlign: "center", padding: "2rem 1rem", color: "var(--muted)",
        fontSize: "0.8rem", border: "1px dashed var(--border)", borderRadius: 10,
      }}>
        Enterprise Memory was not consulted for this investigation.
      </div>
    );
  }

  if (matches.length === 0) {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: "0.8rem" }}>
        <div style={{ display: "flex", alignItems: "center", gap: "0.5rem", color: "var(--muted)", fontSize: "0.78rem" }}>
          <Icon name="layers" size={14} />
          <span style={{ fontWeight: 600, letterSpacing: "0.04em" }}>Enterprise Memory</span>
        </div>
        <div style={{
          textAlign: "center", padding: "2rem 1rem", color: "var(--muted)",
          fontSize: "0.8rem", border: "1px dashed var(--border)", borderRadius: 10,
        }}>
          No similar resolved incidents found in Enterprise Memory.
        </div>
        {data.query && (
          <div style={{ fontSize: "0.68rem", color: "var(--muted)", fontFamily: "var(--font-mono)" }}>
            Searched: "{data.query}"
          </div>
        )}
      </div>
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "0.8rem" }}>
      <div style={{ fontSize: "0.72rem", color: "var(--muted)", letterSpacing: "0.06em" }}>
        {matches.length} similar resolved incident{matches.length !== 1 ? "s" : ""}, ranked by similarity
      </div>
      {matches.map((m, i) => (
        <MemoryCard key={m.incident_id ? `${m.incident_id}-${i}` : i} match={m} />
      ))}
    </div>
  );
}
