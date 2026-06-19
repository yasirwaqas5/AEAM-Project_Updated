const STATUS_STYLES = {
  SUCCESS: { color: "#16a34a", background: "#f0fdf4", border: "#bbf7d0" },
  RUNNING: { color: "#2563eb", background: "#eff6ff", border: "#bfdbfe" },
  FAILED:  { color: "#dc2626", background: "#fef2f2", border: "#fecaca" },
  PENDING: { color: "#d97706", background: "#fffbeb", border: "#fde68a" },
};

function getStatusStyle(status) {
  return STATUS_STYLES[(status ?? "").toUpperCase()] ?? {
    color: "#6b7280", background: "#f9fafb", border: "#e5e7eb",
  };
}

function formatTimestamp(ts) {
  if (!ts) return "—";
  try {
    return new Date(ts).toLocaleString("en-IN", {
      dateStyle: "medium",
      timeStyle: "short",
    });
  } catch {
    return ts;
  }
}

function formatMs(ms) {
  if (ms == null) return "—";
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(2)}s`;
}

const styles = {
  card: {
    background: "#ffffff",
    border: "1px solid #e5e7eb",
    borderRadius: "10px",
    padding: "1.25rem 1.5rem",
    boxShadow: "0 1px 3px rgba(0,0,0,0.06)",
    display: "flex",
    flexDirection: "column",
    gap: "0.75rem",
  },
  topRow: {
    display: "flex",
    justifyContent: "space-between",
    alignItems: "center",
  },
  agentName: {
    fontSize: "0.95rem",
    fontWeight: 600,
    color: "#111827",
  },
  statusBadge: (st) => ({
    fontSize: "0.7rem",
    fontWeight: 700,
    letterSpacing: "0.08em",
    textTransform: "uppercase",
    padding: "0.2rem 0.6rem",
    borderRadius: "4px",
    color: st.color,
    background: st.background,
    border: `1px solid ${st.border}`,
  }),
  incidentId: {
    fontSize: "0.72rem",
    color: "#9ca3af",
    fontFamily: "monospace",
    overflow: "hidden",
    textOverflow: "ellipsis",
    whiteSpace: "nowrap",
  },
  bottomRow: {
    display: "flex",
    justifyContent: "space-between",
    alignItems: "center",
    paddingTop: "0.5rem",
    borderTop: "1px solid #f3f4f6",
  },
  metaItem: {
    display: "flex",
    flexDirection: "column",
    gap: "0.2rem",
  },
  metaLabel: {
    fontSize: "0.65rem",
    textTransform: "uppercase",
    letterSpacing: "0.1em",
    color: "#9ca3af",
  },
  metaValue: {
    fontSize: "0.82rem",
    fontWeight: 500,
    color: "#374151",
  },
};

export default function AgentLogCard({ log }) {
  const {
    agent,
    incident_id,
    status,
    execution_time_ms,
    timestamp,
  } = log;

  const stStyle = getStatusStyle(status);

  return (
    <div style={styles.card}>
      {/* Top: agent name + status badge */}
      <div style={styles.topRow}>
        <span style={styles.agentName}>{agent ?? "—"}</span>
        <span style={styles.statusBadge(stStyle)}>{status ?? "—"}</span>
      </div>

      {/* Incident ID */}
      <div style={styles.incidentId}>
        Incident: {incident_id ?? "—"}
      </div>

      {/* Bottom: execution time + timestamp */}
      <div style={styles.bottomRow}>
        <div style={styles.metaItem}>
          <span style={styles.metaLabel}>Exec Time</span>
          <span style={styles.metaValue}>{formatMs(execution_time_ms)}</span>
        </div>
        <div style={{ ...styles.metaItem, alignItems: "flex-end" }}>
          <span style={styles.metaLabel}>Timestamp</span>
          <span style={styles.metaValue}>{formatTimestamp(timestamp)}</span>
        </div>
      </div>
    </div>
  );
}