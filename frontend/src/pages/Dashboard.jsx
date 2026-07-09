import { useState, useEffect, useCallback } from "react";
import {
  UIStyles, PageHeader, Card, CardTitle, Field, Badge, ConfidenceBar,
  Skeleton, Icon, stateColor, deriveStatus, getRetrievedCount, getRecommendedAction,
  fmtTime, fmtRelative,
} from "../components/ui";

// ─── Data fetching (API contracts unchanged) ─────────────────────────────────

async function fetchStatus() {
  const res = await fetch(`/api/v1/system/status`);
  if (!res.ok) throw new Error(`Status ${res.status}`);
  return res.json();
}

async function fetchMetrics() {
  const res = await fetch(`/metrics`);
  if (!res.ok) throw new Error(`Status ${res.status}`);
  return parsePrometheusText(await res.text());
}

function parsePrometheusText(raw) {
  const out = {};
  for (const line of raw.split("\n")) {
    if (line.startsWith("#") || !line.trim()) continue;
    const idx = line.lastIndexOf(" ");
    if (idx === -1) continue;
    const key = line.slice(0, idx).trim();
    const val = parseFloat(line.slice(idx + 1).trim());
    if (!isNaN(val)) out[key] = val;
  }
  return out;
}

// ─── Small building blocks ────────────────────────────────────────────────────

function StatTile({ label, value, accent, icon, loading }) {
  return (
    <Card accent={accent} className="aeam-card-hover">
      <CardTitle icon={icon}>{label}</CardTitle>
      <div style={{ fontSize: "2.3rem", fontWeight: 800, fontFamily: "var(--font-mono)", color: accent, lineHeight: 1 }}>
        {loading ? <Skeleton width={60} height={30} /> : (value ?? "—")}
      </div>
    </Card>
  );
}

const HIGHLIGHT_KEYS = [
  "incidents_total", "active_incidents", "action_success_total",
  "action_failure_total", "investigation_duration_seconds_sum",
  "agent_execution_time_seconds_sum",
];

// ─── Page ───────────────────────────────────────────────────────────────────────

export default function Dashboard() {
  const [status, setStatus] = useState(null);
  const [metrics, setMetrics] = useState(null);
  const [statusErr, setStatusErr] = useState(null);
  const [metricsErr, setMetricsErr] = useState(null);
  const [loading, setLoading] = useState(true);
  const [lastRefresh, setLastRefresh] = useState(null);
  const [incidents, setIncidents] = useState([]);

  const load = useCallback(async () => {
    setLoading(true); setStatusErr(null); setMetricsErr(null);
    const [s, m] = await Promise.allSettled([fetchStatus(), fetchMetrics()]);
    if (s.status === "fulfilled") setStatus(s.value); else setStatusErr(s.reason?.message ?? "Failed to fetch status");
    if (m.status === "fulfilled") setMetrics(m.value); else setMetricsErr(m.reason?.message ?? "Failed to fetch metrics");
    try {
      const r = await fetch("/api/v1/incidents/");
      if (r.ok) { const d = await r.json(); setIncidents(Array.isArray(d) ? d : []); }
    } catch { /* AI insight simply shows empty */ }
    setLoading(false);
    setLastRefresh(new Date().toLocaleTimeString());
  }, []);

  useEffect(() => { load(); const id = setInterval(load, 30_000); return () => clearInterval(id); }, [load]);

  const systemOk = status?.status === "healthy";
  const sysColor = systemOk ? "#00ffa3" : "#ff5f57";
  const latest = incidents[0] || null;
  const latestStatus = deriveStatus(latest);

  const displayedMetrics = metrics
    ? Object.entries(metrics).filter(([k]) => HIGHLIGHT_KEYS.some((h) => k.startsWith(h))).slice(0, 8)
    : [];

  return (
    <>
      <UIStyles />
      <div className="aeam-page">
        <PageHeader
          title="Dashboard"
          subtitle="System overview · auto-refreshes every 30s"
          right={
            <>
              {lastRefresh && <span style={{ fontSize: "0.7rem", color: "var(--muted)", fontFamily: "var(--font-mono)" }}>Updated {lastRefresh}</span>}
              <button className="aeam-btn aeam-btn-ghost" onClick={load} disabled={loading}>
                <Icon name="activity" size={13} />{loading ? "Loading…" : "Refresh"}
              </button>
            </>
          }
        />

        {/* Top stat row */}
        <div className="aeam-grid-auto" style={{ marginBottom: "1.1rem" }}>
          {/* System Status */}
          <Card accent={sysColor} className="aeam-card-hover">
            <CardTitle icon="shield">System Status</CardTitle>
            {loading
              ? <Skeleton width={110} height={26} />
              : statusErr
                ? <span style={{ color: "#ff5f57", fontSize: "0.78rem", fontFamily: "var(--font-mono)" }}>⚠ {statusErr}</span>
                : <Badge label={(status?.status ?? "unknown").toUpperCase()} color={sysColor} dot subtle={false} style={{ color: "#0b0d12" }} />}
          </Card>
          <StatTile label="Active Incidents" value={status?.active_incidents} accent="#ffb800" icon="alert" loading={loading} />
          <StatTile label="Agents Active" value={status?.agents_active} accent="#00b4ff" icon="activity" loading={loading} />
        </div>

        {/* Latest investigation — structured cards (replaces raw JSON) */}
        <Card accent="#8b5cf6" style={{ marginBottom: "1.1rem" }}>
          <CardTitle icon="target" right={
            latest && <Badge label={latestStatus.label} color={latestStatus.color} dot />
          }>Latest Investigation</CardTitle>

          {loading ? (
            <div className="aeam-grid-auto"><Skeleton height={40} /><Skeleton height={40} /><Skeleton height={40} /><Skeleton height={40} /></div>
          ) : !latest ? (
            <div style={{ color: "var(--muted)", fontSize: "0.82rem", padding: "0.5rem 0" }}>No incidents yet.</div>
          ) : (
            <>
              <div className="aeam-grid-auto" style={{ gap: "1.1rem" }}>
                <Field label="Root Cause" value={latest.root_cause || "Pending"} />
                <div style={{ display: "flex", flexDirection: "column", gap: "0.3rem" }}>
                  <span style={{ fontSize: "0.62rem", textTransform: "uppercase", letterSpacing: "0.12em", color: "var(--muted)" }}>Confidence</span>
                  <ConfidenceBar value={latest.confidence} />
                </div>
                <Field label="Recommended Action" value={getRecommendedAction(latest)} />
                <Field label="Investigation Status" value={latestStatus.label} color={latestStatus.color} />
                <Field label="Retrieved Evidence" value={`${getRetrievedCount(latest)} chunks`} mono />
                <Field label="Investigation Depth" value={latest.investigation_depth ?? "—"} mono />
                <Field label="Metric" value={`${latest.event_type ?? "—"} · ${latest.metric ?? "—"}`} mono />
                <Field label="Last Updated" value={fmtTime(latest.timestamp)} title={`${fmtTime(latest.timestamp)} (${fmtRelative(latest.timestamp)})`} />
              </div>
            </>
          )}
        </Card>

        {/* Component health + last event */}
        <div className="aeam-grid-2" style={{ marginBottom: "1.1rem" }}>
          <Card accent="#8b5cf6">
            <CardTitle icon="layers">Component Health</CardTitle>
            {loading && <div style={{ display: "flex", flexDirection: "column", gap: "0.6rem" }}>{[1, 2, 3].map((i) => <Skeleton key={i} height={18} />)}</div>}
            {!loading && statusErr && <span style={{ color: "#ff5f57", fontSize: "0.78rem" }}>⚠ {statusErr}</span>}
            {!loading && !statusErr && status && (
              <div style={{ display: "flex", flexDirection: "column", gap: "0.6rem" }}>
                {Object.entries(status)
                  .filter(([k]) => !["status", "active_incidents", "agents_active", "last_event_time"].includes(k))
                  .map(([k, v]) => (
                    <div key={k} style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                      <span style={{ fontSize: "0.78rem", color: "var(--muted)", textTransform: "capitalize" }}>{k.replace(/_/g, " ")}</span>
                      <Badge label={String(v).toUpperCase()} color={stateColor(String(v) === "true" ? "done" : String(v))} dot />
                    </div>
                  ))}
                {Object.keys(status).filter((k) => !["status", "active_incidents", "agents_active", "last_event_time"].includes(k)).length === 0 && (
                  <span style={{ fontSize: "0.78rem", color: "var(--muted)" }}>All core components nominal.</span>
                )}
              </div>
            )}
          </Card>

          <Card accent="#00ffa3">
            <CardTitle icon="clock">Last Event Time</CardTitle>
            {loading
              ? <Skeleton width={180} height={20} />
              : statusErr
                ? <span style={{ color: "#ff5f57", fontSize: "0.78rem" }}>⚠ {statusErr}</span>
                : <div style={{ fontFamily: "var(--font-mono)", fontSize: "0.86rem", color: "var(--text)", lineHeight: 1.6 }}>
                    {status?.last_event_time ? fmtTime(status.last_event_time) : "—"}
                    <div style={{ fontSize: "0.72rem", color: "var(--muted)", marginTop: "0.3rem" }}>{status?.last_event_time ? fmtRelative(status.last_event_time) : ""}</div>
                  </div>}
          </Card>
        </div>

        {/* Prometheus metrics */}
        <Card accent="#00b4ff">
          <CardTitle icon="activity">Prometheus Metrics</CardTitle>
          {metricsErr && <span style={{ color: "#ff5f57", fontSize: "0.78rem", fontFamily: "var(--font-mono)" }}>⚠ {metricsErr}</span>}
          {loading && <div style={{ display: "flex", flexDirection: "column", gap: "0.5rem" }}>{[1, 2, 3, 4].map((i) => <div key={i} style={{ display: "flex", justifyContent: "space-between" }}><Skeleton width={220} height={14} /><Skeleton width={50} height={14} /></div>)}</div>}
          {!loading && !metricsErr && displayedMetrics.length === 0 && <div style={{ color: "var(--muted)", fontSize: "0.8rem" }}>No metrics available — ensure /metrics is exposed.</div>}
          {!loading && !metricsErr && displayedMetrics.length > 0 && (
            <div>
              {displayedMetrics.map(([k, v]) => (
                <div key={k} style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", padding: "0.5rem 0", borderBottom: "1px solid var(--border)", fontSize: "0.8rem" }}>
                  <span style={{ color: "var(--muted)", fontFamily: "var(--font-mono)", fontSize: "0.74rem", maxWidth: "70%", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{k}</span>
                  <span style={{ fontFamily: "var(--font-mono)", color: "var(--text)", fontWeight: 600, fontSize: "0.8rem" }}>{typeof v === "number" ? v.toFixed(4) : v}</span>
                </div>
              ))}
            </div>
          )}
        </Card>
      </div>
    </>
  );
}
