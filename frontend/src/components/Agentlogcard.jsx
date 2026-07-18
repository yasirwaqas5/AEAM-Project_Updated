import { Card, Badge, Field, Icon, Collapsible, stateColor, fmtTime, fmtRelative, fmtMs } from "./ui";

/* ──────────────────────────────────────────────────────────────────────────
 * A single agent-execution log entry, rendered as an enterprise card:
 * status badge, failure reason, execution time, retry count, validation result.
 * Consumes the /api/v1/logs/agents response shape (contract unchanged).
 * ────────────────────────────────────────────────────────────────────────── */

export default function AgentLogCard({ log }) {
  const {
    agent, incident_id, status,
    execution_time_ms, retry_count, failure_reason, validation_result,
    validation_details, timestamp,
  } = log;

  const statusColor = stateColor(status);
  const isFailed = (status ?? "").toUpperCase() === "FAILED";
  const valColor = stateColor(validation_result);

  return (
    <Card className="aeam-card-hover" style={{ borderLeft: `3px solid ${statusColor}`, padding: "1.2rem 1.4rem", display: "flex", flexDirection: "column", gap: "0.9rem" }}>
      {/* Top: agent + status badge */}
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: "0.75rem" }}>
        <div style={{ display: "flex", alignItems: "center", gap: "0.55rem", minWidth: 0 }}>
          <Icon name="zap" size={14} color="var(--muted)" />
          <span style={{ fontSize: "0.92rem", fontWeight: 600, color: "var(--text)", textTransform: "capitalize" }}>{agent ?? "—"}</span>
        </div>
        <Badge label={status || "—"} color={statusColor} dot />
      </div>

      {/* Incident id */}
      <div style={{ fontFamily: "var(--font-mono)", fontSize: "0.7rem", color: "var(--muted)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
        Incident: {incident_id ?? "—"}
      </div>

      {/* Failure reason — replaces a bare "FAILED" */}
      {(isFailed || failure_reason) && (
        <div style={{
          fontSize: "0.78rem", color: "#ff8f88", lineHeight: 1.45, wordBreak: "break-word",
          background: "rgba(255,95,87,0.08)", border: "1px solid rgba(255,95,87,0.25)",
          borderRadius: 8, padding: "0.55rem 0.75rem",
          display: "flex", gap: "0.5rem", alignItems: "flex-start",
        }}>
          <Icon name="alert" size={14} color="var(--err)" style={{ marginTop: 1 }} />
          <span><strong style={{ color: "var(--err)" }}>Reason:</strong> {failure_reason ?? "Unknown failure"}</span>
        </div>
      )}

      {/* Granular payload validation errors (e.g. Slack invalid_blocks detail) */}
      {Array.isArray(validation_details) && validation_details.length > 0 && (
        <Collapsible summary={`Payload validation details (${validation_details.length})`}>
          <ul style={{ margin: 0, paddingLeft: "1.1rem", display: "flex", flexDirection: "column", gap: "0.3rem" }}>
            {validation_details.map((d, i) => (
              <li key={i} style={{ fontSize: "0.74rem", color: "var(--muted)", fontFamily: "var(--font-mono)" }}>
                {typeof d === "string" ? d : JSON.stringify(d)}
              </li>
            ))}
          </ul>
        </Collapsible>
      )}

      {/* Meta grid */}
      <div className="aeam-grid-auto" style={{ gap: "1rem", paddingTop: "0.6rem", borderTop: "1px solid var(--border)" }}>
        <Field label="Execution Time" value={fmtMs(execution_time_ms)} mono />
        <Field label="Retry Count" value={retry_count ?? "—"} mono
          color={retry_count > 0 ? "var(--warn)" : "var(--text)"} />
        <div style={{ display: "flex", flexDirection: "column", gap: "0.3rem" }}>
          <span style={{ fontSize: "0.62rem", textTransform: "uppercase", letterSpacing: "0.12em", color: "var(--muted)" }}>Validation</span>
          {validation_result
            ? <Badge label={validation_result} color={valColor} />
            : <span style={{ fontSize: "0.85rem", color: "var(--text)" }}>—</span>}
        </div>
        <Field label="Timestamp" value={fmtTime(timestamp)} title={`${fmtTime(timestamp)} (${fmtRelative(timestamp)})`} />
      </div>
    </Card>
  );
}
