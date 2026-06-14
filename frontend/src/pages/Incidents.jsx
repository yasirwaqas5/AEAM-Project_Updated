import { useState, useEffect, useCallback } from "react";

const API_BASE = import.meta.env.VITE_API_BASE ?? "http://localhost:8080";

// ─── Data fetching ────────────────────────────────────────────────────────────

async function fetchIncidents() {
  const res = await fetch(`${API_BASE}/api/v1/incidents/`);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

// ─── Severity config ──────────────────────────────────────────────────────────

const SEVERITY = {
  CRITICAL: { color: "#ff5f57", bg: "rgba(255,95,87,0.08)",  border: "rgba(255,95,87,0.25)",  rank: 4 },
  HIGH:     { color: "#ffb800", bg: "rgba(255,184,0,0.08)",  border: "rgba(255,184,0,0.25)",  rank: 3 },
  MEDIUM:   { color: "#00b4ff", bg: "rgba(0,180,255,0.08)",  border: "rgba(0,180,255,0.25)",  rank: 2 },
  LOW:      { color: "#00ffa3", bg: "rgba(0,255,163,0.08)",  border: "rgba(0,255,163,0.25)",  rank: 1 },
};

const STATUS = {
  COMPLETE:    { color: "#00ffa3", label: "Complete"    },
  INVESTIGATING:{ color: "#00b4ff", label: "Investigating"},
  ESCALATED:   { color: "#ff5f57", label: "Escalated"   },
  PENDING:     { color: "#ffb800", label: "Pending"     },
};

function getSeverity(key) {
  return SEVERITY[(key ?? "").toUpperCase()] ?? { color: "#5a5f72", bg: "rgba(90,95,114,0.08)", border: "rgba(90,95,114,0.25)", rank: 0 };
}

function getStatus(key) {
  return STATUS[(key ?? "").toUpperCase()] ?? { color: "#5a5f72", label: key ?? "Unknown" };
}

// ─── Styles ───────────────────────────────────────────────────────────────────

const S = {
  page: { animation: "fadeSlideIn 0.35s ease forwards" },
  headerRow: {
    display: "flex",
    alignItems: "flex-end",
    justifyContent: "space-between",
    marginBottom: "2.5rem",
  },
  title: {
    fontSize: "1.75rem", fontWeight: 700,
    fontFamily: "var(--font-display)", color: "var(--text)",
    margin: 0, lineHeight: 1.2,
  },
  subtitle: {
    margin: "0.4rem 0 0", color: "var(--muted)",
    fontSize: "0.82rem", letterSpacing: "0.04em",
  },
  controls: {
    display: "flex", gap: "0.75rem", alignItems: "center",
  },
  filterBtn: (active) => ({
    fontSize: "0.72rem", letterSpacing: "0.08em",
    background: active ? "var(--accent-dim)" : "none",
    border: `1px solid ${active ? "rgba(0,255,163,0.4)" : "var(--border)"}`,
    color: active ? "var(--accent)" : "var(--muted)",
    borderRadius: "6px", padding: "0.3rem 0.7rem",
    cursor: "pointer", textTransform: "uppercase",
    transition: "all 0.15s",
  }),
  refreshBtn: {
    fontSize: "0.72rem", color: "var(--muted)",
    background: "none", border: "1px solid var(--border)",
    borderRadius: "6px", padding: "0.3rem 0.65rem",
    cursor: "pointer", letterSpacing: "0.06em",
    transition: "color 0.15s, border-color 0.15s",
  },
  list: {
    display: "flex", flexDirection: "column", gap: "0.85rem",
  },
  empty: {
    border: "1px dashed var(--border)", borderRadius: "12px",
    padding: "3rem", textAlign: "center",
    color: "var(--muted)", fontSize: "0.82rem", letterSpacing: "0.06em",
  },
  error: {
    background: "rgba(255,95,87,0.08)", border: "1px solid rgba(255,95,87,0.25)",
    borderRadius: "10px", padding: "1rem 1.25rem",
    color: "#ff5f57", fontSize: "0.8rem", fontFamily: "var(--font-mono)",
  },
  count: {
    fontSize: "0.72rem", color: "var(--muted)",
    letterSpacing: "0.06em", marginBottom: "1.25rem",
  },
};

// ─── Skeleton ─────────────────────────────────────────────────────────────────

function Skeleton({ width = "100%", height = 16, style = {} }) {
  return (
    <div style={{
      width, height,
      background: "var(--border)",
      borderRadius: "4px",
      animation: "skeletonPulse 1.2s ease-in-out infinite",
      ...style,
    }} />
  );
}

function CardSkeleton() {
  return (
    <div style={{
      background: "var(--surface)", border: "1px solid var(--border)",
      borderRadius: "12px", padding: "1.25rem 1.5rem",
      display: "flex", flexDirection: "column", gap: "0.75rem",
    }}>
      <div style={{ display: "flex", justifyContent: "space-between" }}>
        <Skeleton width={160} height={14} />
        <Skeleton width={70} height={22} style={{ borderRadius: "20px" }} />
      </div>
      <Skeleton height={12} width="60%" />
      <div style={{ display: "flex", gap: "1rem" }}>
        <Skeleton width={80} height={10} />
        <Skeleton width={100} height={10} />
        <Skeleton width={80} height={10} />
      </div>
    </div>
  );
}

// ─── IncidentCard ─────────────────────────────────────────────────────────────

function IncidentCard({ incident }) {
  const {
    incident_id, event_type, severity,
    status, confidence, root_cause,
  } = incident;

  const sev = getSeverity(severity);
  const sta = getStatus(status);
  const confidencePct = confidence != null ? Math.round(confidence * 100) : null;

  return (
    <div style={{
      background: "var(--surface)",
      border: "1px solid var(--border)",
      borderLeft: `3px solid ${sev.color}`,
      borderRadius: "12px",
      padding: "1.25rem 1.5rem",
      transition: "border-color 0.15s, box-shadow 0.15s",
      cursor: "default",
    }}
      onMouseEnter={(e) => {
        e.currentTarget.style.borderColor = sev.color;
        e.currentTarget.style.boxShadow = `0 0 0 1px ${sev.color}33`;
      }}
      onMouseLeave={(e) => {
        e.currentTarget.style.borderColor = "var(--border)";
        e.currentTarget.style.boxShadow = "none";
      }}
    >
      {/* Top row */}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "0.75rem" }}>
        <div style={{ display: "flex", alignItems: "center", gap: "0.75rem" }}>
          {/* Severity badge */}
          <span style={{
            fontSize: "0.65rem", fontWeight: 700,
            letterSpacing: "0.1em", textTransform: "uppercase",
            background: sev.bg, border: `1px solid ${sev.border}`,
            color: sev.color, borderRadius: "4px",
            padding: "0.2rem 0.55rem",
          }}>
            {severity ?? "—"}
          </span>

          {/* Event type */}
          <span style={{
            fontFamily: "var(--font-mono)", fontSize: "0.8rem",
            color: "var(--text)", fontWeight: 600,
          }}>
            {event_type ?? "—"}
          </span>
        </div>

        {/* Status pill */}
        <span style={{
          fontSize: "0.7rem", letterSpacing: "0.08em",
          color: sta.color,
          background: `${sta.color}15`,
          border: `1px solid ${sta.color}40`,
          borderRadius: "20px", padding: "0.25rem 0.65rem",
          fontWeight: 600,
        }}>
          {sta.label}
        </span>
      </div>

      {/* Incident ID */}
      <div style={{
        fontFamily: "var(--font-mono)", fontSize: "0.72rem",
        color: "var(--muted)", marginBottom: "0.85rem",
        letterSpacing: "0.04em",
        overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
      }}>
        ID: {incident_id ?? "—"}
      </div>

      {/* Root cause */}
      {root_cause && (
        <div style={{
          fontSize: "0.78rem", color: "var(--muted)",
          borderLeft: "2px solid var(--border)",
          paddingLeft: "0.75rem", marginBottom: "0.85rem",
          lineHeight: 1.5,
        }}>
          {root_cause}
        </div>
      )}

      {/* Footer meta */}
      <div style={{
        display: "flex", alignItems: "center", gap: "1.5rem",
        marginTop: "0.25rem",
      }}>
        {/* Confidence bar */}
        {confidencePct != null && (
          <div style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
            <div style={{
              width: "80px", height: "4px",
              background: "var(--border)", borderRadius: "2px",
              overflow: "hidden",
            }}>
              <div style={{
                height: "100%",
                width: `${confidencePct}%`,
                background: confidencePct >= 80 ? "#00ffa3" : confidencePct >= 50 ? "#ffb800" : "#ff5f57",
                borderRadius: "2px",
                transition: "width 0.4s ease",
              }} />
            </div>
            <span style={{
              fontFamily: "var(--font-mono)", fontSize: "0.7rem",
              color: "var(--muted)",
            }}>
              {confidencePct}%
            </span>
          </div>
        )}

        <span style={{ fontSize: "0.7rem", color: "var(--muted)", letterSpacing: "0.06em" }}>
          Confidence
        </span>
      </div>
    </div>
  );
}

// ─── Incidents page ───────────────────────────────────────────────────────────

const SEVERITY_FILTERS = ["ALL", "CRITICAL", "HIGH", "MEDIUM", "LOW"];

export default function Incidents() {
  const [incidents, setIncidents] = useState([]);
  const [loading, setLoading]   = useState(true);
  const [error, setError]       = useState(null);
  const [filter, setFilter]     = useState("ALL");

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await fetchIncidents();
      setIncidents(Array.isArray(data) ? data : []);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const displayed = filter === "ALL"
    ? incidents
    : incidents.filter((i) => (i.severity ?? "").toUpperCase() === filter);

  // Sort by severity rank descending
  const sorted = [...displayed].sort((a, b) => {
    const ra = getSeverity(a.severity).rank;
    const rb = getSeverity(b.severity).rank;
    return rb - ra;
  });

  return (
    <>
      <style>{`
        @keyframes skeletonPulse {
          0%, 100% { opacity: 1; }
          50%       { opacity: 0.4; }
        }
      `}</style>

      <div style={S.page}>
        {/* Header */}
        <div style={S.headerRow}>
          <div>
            <h1 style={S.title}>Incidents</h1>
            <p style={S.subtitle}>All processed anomaly incidents</p>
          </div>
          <div style={S.controls}>
            <button style={S.refreshBtn} onClick={load} disabled={loading}>
              {loading ? "Loading…" : "↻ Refresh"}
            </button>
          </div>
        </div>

        {/* Severity filters */}
        <div style={{ display: "flex", gap: "0.5rem", marginBottom: "1.5rem", flexWrap: "wrap" }}>
          {SEVERITY_FILTERS.map((f) => (
            <button key={f} style={S.filterBtn(filter === f)} onClick={() => setFilter(f)}>
              {f}
            </button>
          ))}
        </div>

        {/* Count */}
        {!loading && !error && (
          <div style={S.count}>
            {sorted.length} incident{sorted.length !== 1 ? "s" : ""}
            {filter !== "ALL" && ` · filtered by ${filter}`}
          </div>
        )}

        {/* Error */}
        {error && (
          <div style={S.error}>⚠ Failed to load incidents: {error}</div>
        )}

        {/* Skeletons */}
        {loading && (
          <div style={S.list}>
            {[1, 2, 3].map((i) => <CardSkeleton key={i} />)}
          </div>
        )}

        {/* Incident list */}
        {!loading && !error && sorted.length === 0 && (
          <div style={S.empty}>
            {filter === "ALL"
              ? "No incidents recorded yet."
              : `No ${filter} incidents found.`}
          </div>
        )}

        {!loading && !error && sorted.length > 0 && (
          <div style={S.list}>
            {sorted.map((inc) => (
              <IncidentCard key={inc.incident_id ?? Math.random()} incident={inc} />
            ))}
          </div>
        )}
      </div>
    </>
  );
}