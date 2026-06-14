import { useState, useEffect, useCallback } from "react";

const API_BASE = import.meta.env.VITE_API_BASE ?? "http://localhost:8080";

// ─── Data fetching ────────────────────────────────────────────────────────────

async function fetchStatus() {
  const res = await fetch(`${API_BASE}/api/v1/system/status`);
  if (!res.ok) throw new Error(`Status ${res.status}`);
  return res.json();
}

async function fetchMetrics() {
  const res = await fetch(`${API_BASE}/metrics`);
  if (!res.ok) throw new Error(`Status ${res.status}`);
  const text = await res.text();
  return parsePrometheusText(text);
}

/** Parse Prometheus text exposition into { metricName: value } */
function parsePrometheusText(raw) {
  const out = {};
  for (const line of raw.split("\n")) {
    if (line.startsWith("#") || !line.trim()) continue;
    const spaceIdx = line.lastIndexOf(" ");
    if (spaceIdx === -1) continue;
    const key = line.slice(0, spaceIdx).trim();
    const val = parseFloat(line.slice(spaceIdx + 1).trim());
    if (!isNaN(val)) out[key] = val;
  }
  return out;
}

// ─── Styles ───────────────────────────────────────────────────────────────────

const S = {
  page: {
    animation: "fadeSlideIn 0.35s ease forwards",
  },
  header: {
    marginBottom: "2.5rem",
  },
  title: {
    fontSize: "1.75rem",
    fontWeight: 700,
    fontFamily: "var(--font-display)",
    color: "var(--text)",
    margin: 0,
    lineHeight: 1.2,
  },
  subtitle: {
    margin: "0.4rem 0 0",
    color: "var(--muted)",
    fontSize: "0.82rem",
    letterSpacing: "0.04em",
  },
  grid3: {
    display: "grid",
    gridTemplateColumns: "repeat(3, 1fr)",
    gap: "1.25rem",
    marginBottom: "1.75rem",
  },
  grid2: {
    display: "grid",
    gridTemplateColumns: "1fr 1fr",
    gap: "1.25rem",
  },
  card: {
    background: "var(--surface)",
    border: "1px solid var(--border)",
    borderRadius: "12px",
    padding: "1.5rem",
    position: "relative",
    overflow: "hidden",
  },
  cardTitle: {
    fontSize: "0.68rem",
    textTransform: "uppercase",
    letterSpacing: "0.14em",
    color: "var(--muted)",
    marginBottom: "0.85rem",
  },
  badge: (ok) => ({
    display: "inline-flex",
    alignItems: "center",
    gap: "0.4rem",
    fontSize: "0.78rem",
    fontWeight: 600,
    color: ok ? "#00ffa3" : "#ff5f57",
    background: ok ? "rgba(0,255,163,0.08)" : "rgba(255,95,87,0.10)",
    border: `1px solid ${ok ? "rgba(0,255,163,0.25)" : "rgba(255,95,87,0.25)"}`,
    borderRadius: "20px",
    padding: "0.3rem 0.75rem",
    letterSpacing: "0.06em",
  }),
  dot: (ok) => ({
    width: "7px",
    height: "7px",
    borderRadius: "50%",
    background: ok ? "#00ffa3" : "#ff5f57",
    boxShadow: `0 0 8px ${ok ? "#00ffa3" : "#ff5f57"}`,
    flexShrink: 0,
  }),
  bigNum: (accent = "var(--accent)") => ({
    fontSize: "2.5rem",
    fontWeight: 800,
    fontFamily: "var(--font-mono)",
    color: accent,
    lineHeight: 1,
    marginBottom: "0.3rem",
  }),
  numLabel: {
    fontSize: "0.72rem",
    color: "var(--muted)",
    letterSpacing: "0.08em",
    textTransform: "uppercase",
  },
  accentBar: (color) => ({
    position: "absolute",
    top: 0, left: 0,
    width: "100%", height: "2px",
    background: color,
  }),
  metricRow: {
    display: "flex",
    justifyContent: "space-between",
    alignItems: "baseline",
    padding: "0.5rem 0",
    borderBottom: "1px solid var(--border)",
    fontSize: "0.8rem",
  },
  metricKey: {
    color: "var(--muted)",
    fontFamily: "var(--font-mono)",
    fontSize: "0.75rem",
    maxWidth: "70%",
    overflow: "hidden",
    textOverflow: "ellipsis",
    whiteSpace: "nowrap",
  },
  metricVal: {
    fontFamily: "var(--font-mono)",
    color: "var(--text)",
    fontWeight: 600,
    fontSize: "0.8rem",
  },
  error: {
    color: "#ff5f57",
    fontSize: "0.78rem",
    fontFamily: "var(--font-mono)",
    padding: "0.5rem 0",
  },
  refreshBtn: {
    fontSize: "0.72rem",
    color: "var(--muted)",
    background: "none",
    border: "1px solid var(--border)",
    borderRadius: "6px",
    padding: "0.3rem 0.65rem",
    cursor: "pointer",
    letterSpacing: "0.06em",
    transition: "color 0.15s, border-color 0.15s",
  },
  headerRow: {
    display: "flex",
    alignItems: "flex-end",
    justifyContent: "space-between",
    marginBottom: "2.5rem",
  },
  timestamp: {
    fontSize: "0.7rem",
    color: "var(--muted)",
    fontFamily: "var(--font-mono)",
  },
};

// ─── Sub-components ───────────────────────────────────────────────────────────

function Card({ children, accentColor, style = {} }) {
  return (
    <div style={{ ...S.card, ...style }}>
      {accentColor && <div style={S.accentBar(accentColor)} />}
      {children}
    </div>
  );
}

function StatusBadge({ status }) {
  const ok = status === "healthy";
  return (
    <span style={S.badge(ok)}>
      <span style={S.dot(ok)} />
      {(status ?? "unknown").toUpperCase()}
    </span>
  );
}

function StatCard({ label, value, accent, loading }) {
  return (
    <Card accentColor={accent}>
      <div style={S.cardTitle}>{label}</div>
      <div style={S.bigNum(accent)}>
        {loading ? <Skeleton width={60} /> : (value ?? "—")}
      </div>
    </Card>
  );
}

function Skeleton({ width = 80, height = 16 }) {
  return (
    <div style={{
      width, height,
      background: "var(--border)",
      borderRadius: "4px",
      animation: "skeletonPulse 1.2s ease-in-out infinite",
    }} />
  );
}

function MetricsCard({ metrics, loading, error }) {
  const HIGHLIGHT_KEYS = [
    "incidents_total",
    "active_incidents",
    "action_success_total",
    "action_failure_total",
    "investigation_duration_seconds_sum",
    "agent_execution_time_seconds_sum",
  ];

  const displayed = metrics
    ? Object.entries(metrics)
        .filter(([k]) => HIGHLIGHT_KEYS.some((h) => k.startsWith(h)))
        .slice(0, 8)
    : [];

  return (
    <Card accentColor="#00b4ff" style={{ gridColumn: "1 / -1" }}>
      <div style={S.cardTitle}>Prometheus Metrics</div>

      {error && <div style={S.error}>⚠ {error}</div>}
      {loading && (
        <div style={{ display: "flex", flexDirection: "column", gap: "0.5rem" }}>
          {[1, 2, 3, 4].map((i) => (
            <div key={i} style={{ display: "flex", justifyContent: "space-between" }}>
              <Skeleton width={220} height={14} />
              <Skeleton width={50} height={14} />
            </div>
          ))}
        </div>
      )}
      {!loading && !error && displayed.length === 0 && (
        <div style={{ color: "var(--muted)", fontSize: "0.8rem" }}>
          No metrics available — ensure /metrics is exposed.
        </div>
      )}
      {!loading && !error && displayed.length > 0 && (
        <div>
          {displayed.map(([k, v]) => (
            <div style={S.metricRow} key={k}>
              <span style={S.metricKey}>{k}</span>
              <span style={S.metricVal}>{typeof v === "number" ? v.toFixed(4) : v}</span>
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}

// ─── Dashboard page ───────────────────────────────────────────────────────────

export default function Dashboard() {
  const [status, setStatus] = useState(null);
  const [metrics, setMetrics] = useState(null);
  const [statusErr, setStatusErr] = useState(null);
  const [metricsErr, setMetricsErr] = useState(null);
  const [loading, setLoading] = useState(true);
  const [lastRefresh, setLastRefresh] = useState(null);

  const load = useCallback(async () => {
    setLoading(true);
    setStatusErr(null);
    setMetricsErr(null);

    const [statusResult, metricsResult] = await Promise.allSettled([
      fetchStatus(),
      fetchMetrics(),
    ]);

    if (statusResult.status === "fulfilled") {
      setStatus(statusResult.value);
    } else {
      setStatusErr(statusResult.reason?.message ?? "Failed to fetch status");
    }

    if (metricsResult.status === "fulfilled") {
      setMetrics(metricsResult.value);
    } else {
      setMetricsErr(metricsResult.reason?.message ?? "Failed to fetch metrics");
    }

    setLoading(false);
    setLastRefresh(new Date().toLocaleTimeString());
  }, []);

  // Initial load + auto-refresh every 30s
  useEffect(() => {
    load();
    const id = setInterval(load, 30_000);
    return () => clearInterval(id);
  }, [load]);

  const systemOk = status?.status === "healthy";

  return (
    <>
      <style>{`
        @keyframes skeletonPulse {
          0%, 100% { opacity: 1; }
          50%       { opacity: 0.4; }
        }
        button.refresh-btn:hover {
          color: var(--accent) !important;
          border-color: var(--accent) !important;
        }
      `}</style>

      <div style={S.page}>
        {/* Header */}
        <div style={S.headerRow}>
          <div>
            <h1 style={S.title}>Dashboard</h1>
            <p style={S.subtitle}>System overview · auto-refreshes every 30s</p>
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: "1rem" }}>
            {lastRefresh && (
              <span style={S.timestamp}>Updated {lastRefresh}</span>
            )}
            <button
              className="refresh-btn"
              style={S.refreshBtn}
              onClick={load}
              disabled={loading}
            >
              {loading ? "Loading…" : "↻ Refresh"}
            </button>
          </div>
        </div>

        {/* Status + stat cards */}
        <div style={S.grid3}>
          {/* System status */}
          <Card accentColor={systemOk ? "#00ffa3" : "#ff5f57"}>
            <div style={S.cardTitle}>System Status</div>
            {loading
              ? <Skeleton width={90} height={28} />
              : statusErr
                ? <div style={S.error}>⚠ {statusErr}</div>
                : <StatusBadge status={status?.status} />
            }
          </Card>

          {/* Active incidents */}
          <StatCard
            label="Active Incidents"
            value={status?.active_incidents}
            accent="#ffb800"
            loading={loading}
          />

          {/* Agents active */}
          <StatCard
            label="Agents Active"
            value={status?.agents_active}
            accent="#00b4ff"
            loading={loading}
          />
        </div>

        {/* Checks + last event */}
        <div style={S.grid2}>
          {/* Component health checks */}
          <Card accentColor="#8b5cf6">
            <div style={S.cardTitle}>Component Health</div>
            {loading && (
              <div style={{ display: "flex", flexDirection: "column", gap: "0.6rem" }}>
                {[1, 2, 3, 4].map((i) => <Skeleton key={i} height={18} />)}
              </div>
            )}
            {!loading && statusErr && <div style={S.error}>⚠ {statusErr}</div>}
            {!loading && !statusErr && status && (
              <div style={{ display: "flex", flexDirection: "column", gap: "0.5rem" }}>
                {Object.entries(status).filter(([k]) =>
                  !["status", "active_incidents", "agents_active", "last_event_time"].includes(k)
                ).map(([k, v]) => (
                  <div key={k} style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                    <span style={{ fontSize: "0.78rem", color: "var(--muted)", textTransform: "capitalize" }}>
                      {k.replace(/_/g, " ")}
                    </span>
                    <StatusBadge status={String(v)} />
                  </div>
                ))}
              </div>
            )}
          </Card>

          {/* Last event time */}
          <Card accentColor="#00ffa3">
            <div style={S.cardTitle}>Last Event Time</div>
            {loading
              ? <Skeleton width={180} height={20} />
              : statusErr
                ? <div style={S.error}>⚠ {statusErr}</div>
                : (
                  <div style={{ fontFamily: "var(--font-mono)", fontSize: "0.88rem", color: "var(--text)", lineHeight: 1.6 }}>
                    {status?.last_event_time
                      ? new Date(status.last_event_time).toLocaleString()
                      : "—"}
                  </div>
                )
            }
          </Card>
        </div>

        {/* Metrics */}
        <div style={{ ...S.grid2, marginTop: "1.25rem" }}>
          <MetricsCard metrics={metrics} loading={loading} error={metricsErr} />
        </div>
      </div>
    </>
  );
}