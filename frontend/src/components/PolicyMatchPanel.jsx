import { Badge, Icon, getPolicyMatchData, getPolicyMatches } from "./ui";

/* ──────────────────────────────────────────────────────────────────────────
 * Matched Enterprise Policies view (Phase C3).
 *
 * Renders the structured business policies the Enterprise Policy Registry
 * matched to THIS investigation — deliberately a separate component from
 * both EvidencePanel.jsx (Knowledge Documents) and MemoryPanel.jsx
 * (Enterprise Memory / past incidents), never merged into either. A policy
 * answers "what business rule applies here"; it is ADVISORY evidence only
 * — see aeam/intelligence/policy_registry.py and Orchestrator.investigate():
 * a matched policy never overrides, suppresses, or triggers a deterministic
 * RuleEngine decision.
 *
 * Three honest states, matching the same convention MemoryPanel already
 * established:
 * - The Policy Registry was never consulted for this investigation (older
 *   incident, or the registry was unavailable at the time) —
 *   getPolicyMatchData() is null.
 * - It was consulted and found nothing relevant — matches is [].
 * - It was consulted and found relevant policies — render them, each
 *   labelled with exactly which match tier found it ("metric" = exact
 *   deterministic match on the incident's metric; "semantic" = embedding
 *   similarity fallback, shown with its real score, never invented).
 * ────────────────────────────────────────────────────────────────────────── */

const REASON_LABEL = { metric: "Metric match", semantic: "Semantic match" };
const REASON_COLOR = { metric: "var(--ok)", semantic: "var(--info)" };

function PolicyMatchCard({ match }) {
  const label = match.business_rule || match.condition || "(unlabeled policy)";
  const simPct = match.similarity != null ? Math.round(match.similarity * 100) : null;

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
          <Icon name="shield" size={13} color="var(--muted)" />
          <span style={{ fontSize: "0.82rem", color: "var(--text)", fontWeight: 600 }}>{label}</span>
        </div>
        <div style={{ display: "flex", gap: "0.4rem", flexWrap: "wrap" }}>
          <Badge label={REASON_LABEL[match.match_reason] || match.match_reason || "unknown"} color={REASON_COLOR[match.match_reason] || "var(--muted)"} />
          {simPct != null && <Badge label={`similarity ${simPct}%`} color="var(--muted)" />}
          {match.priority && <Badge label={match.priority} color={
            match.priority === "critical" || match.priority === "high" ? "var(--err)" : match.priority === "medium" ? "var(--warn)" : "var(--muted)"
          } />}
        </div>
      </div>

      <div style={{ padding: "0.75rem 0.9rem", display: "flex", flexDirection: "column", gap: "0.5rem" }}>
        {match.condition && (
          <div style={{ display: "flex", flexDirection: "column", gap: "0.2rem" }}>
            <span style={{ fontSize: "0.62rem", textTransform: "uppercase", letterSpacing: "0.1em", color: "var(--muted)" }}>Condition</span>
            <span style={{ fontSize: "0.78rem", color: "var(--text)", fontFamily: "var(--font-mono)" }}>{match.condition}</span>
          </div>
        )}
        {match.actions?.length > 0 && (
          <div style={{ display: "flex", flexDirection: "column", gap: "0.2rem" }}>
            <span style={{ fontSize: "0.62rem", textTransform: "uppercase", letterSpacing: "0.1em", color: "var(--muted)" }}>Actions</span>
            <span style={{ fontSize: "0.78rem", color: "var(--text)" }}>{match.actions.join(", ")}</span>
          </div>
        )}
        <div style={{ display: "flex", gap: "1.2rem", flexWrap: "wrap", fontSize: "0.7rem", color: "var(--muted)" }}>
          {match.department && <span>Department: {match.department}</span>}
          {match.role && <span>Role: {match.role}</span>}
          {match.time_constraint && <span>Time: {match.time_constraint}</span>}
          {match.approval_required != null && <span>Approval required: {match.approval_required ? "Yes" : "No"}</span>}
          {match.related_metrics?.length > 0 && <span>Metrics: {match.related_metrics.join(", ")}</span>}
        </div>
        <div style={{ display: "flex", gap: "1rem", fontSize: "0.66rem", color: "var(--muted)", paddingTop: "0.4rem", borderTop: "1px solid var(--border)" }}>
          <span>Source: {match.source_document || "—"}</span>
          {match.source_chunk && <span title={match.source_chunk}>Chunk: {match.source_chunk.slice(0, 12)}…</span>}
          <span title={match.policy_id}>Policy ID: {match.policy_id ? `${match.policy_id.slice(0, 8)}…` : "—"}</span>
        </div>
      </div>
    </div>
  );
}

export default function PolicyMatchPanel({ incident }) {
  const data = getPolicyMatchData(incident);
  const matches = getPolicyMatches(incident);

  if (data === null) {
    return (
      <div style={{
        textAlign: "center", padding: "2rem 1rem", color: "var(--muted)",
        fontSize: "0.8rem", border: "1px dashed var(--border)", borderRadius: 10,
      }}>
        Enterprise Policy Registry was not consulted for this investigation.
      </div>
    );
  }

  if (matches.length === 0) {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: "0.8rem" }}>
        <div style={{
          textAlign: "center", padding: "2rem 1rem", color: "var(--muted)",
          fontSize: "0.8rem", border: "1px dashed var(--border)", borderRadius: 10,
        }}>
          No matched enterprise policies for this investigation.
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
        {matches.length} matched enterprise polic{matches.length !== 1 ? "ies" : "y"} — advisory evidence, never overriding a deterministic rule
      </div>
      {matches.map((m, i) => (
        <PolicyMatchCard key={m.policy_id ? `${m.policy_id}-${i}` : i} match={m} />
      ))}
    </div>
  );
}
