const SEVERITY_STYLES = {
  CRITICAL: { color: "#dc2626", background: "#fef2f2", border: "#fecaca" },
  HIGH:     { color: "#ea580c", background: "#fff7ed", border: "#fed7aa" },
  MEDIUM:   { color: "#d97706", background: "#fffbeb", border: "#fde68a" },
  LOW:      { color: "#16a34a", background: "#f0fdf4", border: "#bbf7d0" },
};

function getSeverityStyle(severity) {
  return SEVERITY_STYLES[(severity ?? "").toUpperCase()] ?? {
    color: "#6b7280", background: "#f9fafb", border: "#e5e7eb",
  };
}

function formatConfidence(confidence) {
  if (confidence == null) return "—";
  if (confidence <= 1) return `${Math.round(confidence * 100)}%`;
  return `${Math.round(confidence)}%`;
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
  eventType: {
    fontSize: "0.95rem",
    fontWeight: 600,
    color: "#111827",
  },
  severityBadge: (sev) => ({
    fontSize: "0.7rem",
    fontWeight: 700,
    letterSpacing: "0.08em",
    textTransform: "uppercase",
    padding: "0.2rem 0.6rem",
    borderRadius: "4px",
    color: sev.color,
    background: sev.background,
    border: `1px solid ${sev.border}`,
  }),
  id: {
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

export default function IncidentCard({ incident }) {
  const {
    incident_id,
    event_type,
    severity,
    status,
    confidence,
  } = incident;

  const sevStyle = getSeverityStyle(severity);

  return (
    <div style={styles.card}>
      {/* Top: event type + severity badge */}
      <div style={styles.topRow}>
        <span style={styles.eventType}>{event_type ?? "—"}</span>
        <span style={styles.severityBadge(sevStyle)}>{severity ?? "—"}</span>
      </div>

      {/* Incident ID */}
      <div style={styles.id}>ID: {incident_id ?? "—"}</div>

      {/* Bottom: status + confidence */}
      <div style={styles.bottomRow}>
        <div style={styles.metaItem}>
          <span style={styles.metaLabel}>Status</span>
          <span style={styles.metaValue}>{status ?? "—"}</span>
        </div>
        <div style={{ ...styles.metaItem, alignItems: "flex-end" }}>
          <span style={styles.metaLabel}>Confidence</span>
          <span style={styles.metaValue}>{formatConfidence(confidence)}</span>
        </div>
      </div>
    </div>
  );
}