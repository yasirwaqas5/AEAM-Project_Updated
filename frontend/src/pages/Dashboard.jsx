import { useState, useEffect, useCallback, useMemo, lazy, Suspense } from "react";
import { Link } from "react-router-dom";
import {
  PageHeader, Card, CardTitle, Field, Badge, ConfidenceBar,
  Skeleton, Icon, Button, stateColor, deriveStatus, getRetrievedCount, getRecommendedAction,
  fmtTime, fmtRelative,
} from "../components/ui";
import { PageContainer } from "../components/library";
import { CountUp, Sparkline, ProgressRing } from "../components/charts";

const AgentMesh = lazy(() => import("../components/three/AgentMesh"));

/* ──────────────────────────────────────────────────────────────────────────
 * pages/Dashboard.jsx — the operational console home.
 * Same four data sources as before (system status, Prometheus metrics,
 * incidents, observability) — presentation upgraded to the flagship hero +
 * glanceable tiles. Every figure remains real; unavailable stays "N/A".
 * ────────────────────────────────────────────────────────────────────────── */

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
async function fetchObservability() {
  const res = await fetch(`/api/v1/observability/`);
  if (!res.ok) throw new Error(`Status ${res.status}`);
  return res.json();
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

const HIGHLIGHT_KEYS = [
  "incidents_total", "active_incidents", "action_success_total",
  "action_failure_total", "investigation_duration_seconds_sum",
  "agent_execution_time_seconds_sum",
];

/* 14-day incident volume from real timestamps. */
function dailyBuckets(incidents, days = 14) {
  const now = new Date();
  const counts = new Map();
  for (let d = days - 1; d >= 0; d--) {
    const day = new Date(now); day.setDate(now.getDate() - d);
    counts.set(day.toISOString().slice(0, 10), 0);
  }
  for (const inc of incidents) {
    const key = String(inc.timestamp || "").slice(0, 10);
    if (counts.has(key)) counts.set(key, counts.get(key) + 1);
  }
  return Array.from(counts.values());
}

function StatTile({ label, value, accent, icon, loading, sub, format }) {
  return (
    <Card accent={accent} className="aeam-card-hover">
      <CardTitle icon={icon}>{label}</CardTitle>
      <div style={{ fontSize: "2.1rem", fontWeight: 700, fontFamily: "var(--font-mono)", color: accent, lineHeight: 1, fontVariantNumeric: "tabular-nums", letterSpacing: "-0.02em" }}>
        {loading ? <Skeleton width={60} height={30} /> : value == null ? "—" : <CountUp value={value} format={format} />}
      </div>
      {sub && <div style={{ marginTop: ".45rem", fontSize: "var(--fs-2xs)", color: "var(--muted)" }}>{sub}</div>}
    </Card>
  );
}

export default function Dashboard() {
  const [status, setStatus] = useState(null);
  const [metrics, setMetrics] = useState(null);
  const [statusErr, setStatusErr] = useState(null);
  const [metricsErr, setMetricsErr] = useState(null);
  const [loading, setLoading] = useState(true);
  const [lastRefresh, setLastRefresh] = useState(null);
  const [incidents, setIncidents] = useState([]);
  const [observability, setObservability] = useState(null);

  const load = useCallback(async () => {
    setLoading(true); setStatusErr(null); setMetricsErr(null);
    const [s, m] = await Promise.allSettled([fetchStatus(), fetchMetrics()]);
    if (s.status === "fulfilled") setStatus(s.value); else setStatusErr(s.reason?.message ?? "Failed to fetch status");
    if (m.status === "fulfilled") setMetrics(m.value); else setMetricsErr(m.reason?.message ?? "Failed to fetch metrics");
    try {
      const r = await fetch("/api/v1/incidents/");
      if (r.ok) { const d = await r.json(); setIncidents(Array.isArray(d) ? d : []); }
    } catch { /* insight panels simply show empty */ }
    try { setObservability(await fetchObservability()); } catch { /* AI Health shows unavailable */ }
    setLoading(false);
    setLastRefresh(new Date().toLocaleTimeString());
  }, []);

  useEffect(() => { load(); const id = setInterval(load, 30_000); return () => clearInterval(id); }, [load]);

  const systemOk = status?.status === "healthy";
  const sysColor = systemOk ? "var(--ok)" : "var(--err)";
  const latest = incidents[0] || null;
  const latestStatus = deriveStatus(latest);

  const displayedMetrics = metrics
    ? Object.entries(metrics).filter(([k]) => HIGHLIGHT_KEYS.some((h) => k.startsWith(h))).slice(0, 8)
    : [];

  const aiHealth = observability?.overall_ai_health;
  const aiHealthScore = aiHealth?.available ? aiHealth.score : null;

  const trend = useMemo(() => dailyBuckets(incidents), [incidents]);
  const criticalActive = useMemo(
    () => incidents.filter((i) => ["CRITICAL", "HIGH"].includes(String(i.severity || "").toUpperCase())
      && deriveStatus(i).label !== "Resolved").length,
    [incidents],
  );

  return (
    <PageContainer max={1280}>
      <div className="aeam-page">
        <PageHeader
          title="Dashboard"
          subtitle="System overview · auto-refreshes every 30s"
          right={
            <>
              {lastRefresh && <span style={{ fontSize: "var(--fs-2xs)", color: "var(--faint)", fontFamily: "var(--font-mono)" }}>Updated {lastRefresh}</span>}
              <Button icon="activity" onClick={load} disabled={loading}>{loading ? "Loading…" : "Refresh"}</Button>
            </>
          }
        />

        {/* ── Hero: live agent mesh + platform vitals ─────────────────── */}
        <Card style={{ marginBottom: "1rem", padding: 0, overflow: "hidden" }} accent="var(--accent)">
          <div style={{ display: "grid", gridTemplateColumns: "minmax(300px, 1.15fr) minmax(260px, 1fr)", alignItems: "stretch" }}
            className="aeam-hero-grid">
            <div style={{ padding: "1.5rem 1.7rem", display: "flex", flexDirection: "column", gap: "1.1rem", justifyContent: "center" }}>
              <div>
                <div style={{ fontSize: "var(--fs-2xs)", letterSpacing: ".2em", textTransform: "uppercase", color: "var(--accent)", fontWeight: 700, marginBottom: ".5rem" }}>
                  Enterprise Agent Mesh
                </div>
                <div style={{ fontFamily: "var(--font-display)", fontSize: "var(--fs-xl)", fontWeight: 650, letterSpacing: "-0.01em", color: "var(--text)", lineHeight: 1.3 }}>
                  {loading ? "Connecting to mesh…"
                    : systemOk ? "All systems operational" : statusErr ? "Backend unreachable" : "Degraded state detected"}
                </div>
              </div>

              <div style={{ display: "flex", gap: "2rem", flexWrap: "wrap", alignItems: "center" }}>
                <ProgressRing value={aiHealthScore} size={104} sublabel="AI Health" />
                <div style={{ display: "flex", flexDirection: "column", gap: ".8rem" }}>
                  <div>
                    <div style={{ fontSize: "var(--fs-2xs)", color: "var(--muted)", textTransform: "uppercase", letterSpacing: ".12em", marginBottom: 4 }}>Incident volume · 14 days</div>
                    <Sparkline values={trend} width={190} height={40} color="var(--info)" />
                  </div>
                  <div style={{ display: "flex", gap: ".55rem", flexWrap: "wrap" }}>
                    <Badge label={systemOk ? "Operational" : "Attention"} color={sysColor} dot />
                    {criticalActive > 0 && <Badge label={`${criticalActive} high-severity open`} color="var(--warn)" />}
                    {aiHealth && !aiHealth.available && <Badge label="AI health: insufficient data" color="var(--faint)" />}
                  </div>
                </div>
              </div>
            </div>

            <div style={{ position: "relative", minHeight: 250, borderLeft: "1px solid var(--border)" }}>
              <Suspense fallback={<div style={{ height: "100%", background: "radial-gradient(circle at 50% 45%, rgba(91,157,255,.15), transparent 60%)" }} />}>
                <AgentMesh variant="dashboard" health={aiHealthScore} height="100%" />
              </Suspense>
            </div>
          </div>
        </Card>

        {/* ── Stat tiles ──────────────────────────────────────────────── */}
        <div className="aeam-grid-auto aeam-stagger" style={{ marginBottom: "1rem" }}>
          <Card accent={sysColor} className="aeam-card-hover">
            <CardTitle icon="shield">System Status</CardTitle>
            {loading
              ? <Skeleton width={110} height={26} />
              : statusErr
                ? <span style={{ color: "var(--err)", fontSize: "var(--fs-sm)", fontFamily: "var(--font-mono)" }}>{statusErr}</span>
                : <Badge label={(status?.status ?? "unknown").toUpperCase()} color={sysColor} dot />}
          </Card>
          <StatTile label="Active Incidents" value={status?.active_incidents} accent="var(--warn)" icon="alert" loading={loading} />
          <StatTile label="Agents Active" value={status?.agents_active} accent="var(--info)" icon="activity" loading={loading} />
          <StatTile label="Investigations Recorded" value={observability?.total_investigations} accent="var(--c-memory)" icon="layers" loading={loading}
            sub="persisted with full evidence trail" />
        </div>

        {/* ── Latest investigation ────────────────────────────────────── */}
        <Card accent="var(--c-plan)" style={{ marginBottom: "1rem" }}>
          <CardTitle icon="target" right={
            <span style={{ display: "inline-flex", gap: ".6rem", alignItems: "center" }}>
              {latest && <Badge label={latestStatus.label} color={latestStatus.color} dot />}
              {latest && <Link to={`/investigation?id=${encodeURIComponent(latest.incident_id)}`} style={{ textDecoration: "none" }}>
                <Button size="sm" icon="arrowr">Open</Button>
              </Link>}
            </span>
          }>Latest Investigation</CardTitle>

          {loading ? (
            <div className="aeam-grid-auto"><Skeleton height={40} /><Skeleton height={40} /><Skeleton height={40} /><Skeleton height={40} /></div>
          ) : !latest ? (
            <div style={{ color: "var(--muted)", fontSize: "var(--fs-sm)", padding: "0.5rem 0" }}>No incidents yet.</div>
          ) : (
            <div className="aeam-grid-auto" style={{ gap: "1.1rem" }}>
              <Field label="Root Cause" value={latest.root_cause || "Pending"} />
              <div style={{ display: "flex", flexDirection: "column", gap: "0.3rem" }}>
                <span style={{ fontSize: "var(--fs-2xs)", textTransform: "uppercase", letterSpacing: "0.12em", color: "var(--muted)" }}>Confidence</span>
                <ConfidenceBar value={latest.confidence} />
              </div>
              <Field label="Recommended Action" value={getRecommendedAction(latest)} />
              <Field label="Investigation Status" value={latestStatus.label} color={latestStatus.color} />
              <Field label="Retrieved Evidence" value={`${getRetrievedCount(latest)} chunks`} mono />
              <Field label="Investigation Depth" value={latest.investigation_depth ?? "—"} mono />
              <Field label="Metric" value={`${latest.event_type ?? "—"} · ${latest.metric ?? "—"}`} mono />
              <Field label="Last Updated" value={fmtTime(latest.timestamp)} title={`${fmtTime(latest.timestamp)} (${fmtRelative(latest.timestamp)})`} />
            </div>
          )}
        </Card>

        {/* ── Component health + last event ───────────────────────────── */}
        <div className="aeam-grid-2" style={{ marginBottom: "1rem" }}>
          <Card accent="var(--c-memory)">
            <CardTitle icon="layers">Component Health</CardTitle>
            {loading && <div style={{ display: "flex", flexDirection: "column", gap: "0.6rem" }}>{[1, 2, 3].map((i) => <Skeleton key={i} height={18} />)}</div>}
            {!loading && statusErr && <span style={{ color: "var(--err)", fontSize: "var(--fs-sm)" }}>{statusErr}</span>}
            {!loading && !statusErr && status && (
              <div style={{ display: "flex", flexDirection: "column", gap: "0.6rem" }}>
                {Object.entries(status)
                  .filter(([k]) => !["status", "active_incidents", "agents_active", "last_event_time"].includes(k))
                  .map(([k, v]) => (
                    <div key={k} style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                      <span style={{ fontSize: "var(--fs-sm)", color: "var(--muted)", textTransform: "capitalize" }}>{k.replace(/_/g, " ")}</span>
                      <Badge label={String(v).toUpperCase()} color={stateColor(String(v) === "true" ? "done" : String(v))} dot />
                    </div>
                  ))}
                {Object.keys(status).filter((k) => !["status", "active_incidents", "agents_active", "last_event_time"].includes(k)).length === 0 && (
                  <span style={{ fontSize: "var(--fs-sm)", color: "var(--muted)" }}>All core components nominal.</span>
                )}
              </div>
            )}
          </Card>

          <Card accent="var(--ok)">
            <CardTitle icon="clock">Last Event Time</CardTitle>
            {loading
              ? <Skeleton width={180} height={20} />
              : statusErr
                ? <span style={{ color: "var(--err)", fontSize: "var(--fs-sm)" }}>{statusErr}</span>
                : <div style={{ fontFamily: "var(--font-mono)", fontSize: "var(--fs-md)", color: "var(--text)", lineHeight: 1.6 }}>
                    {status?.last_event_time ? fmtTime(status.last_event_time) : "—"}
                    <div style={{ fontSize: "var(--fs-xs)", color: "var(--muted)", marginTop: "0.3rem" }}>{status?.last_event_time ? fmtRelative(status.last_event_time) : ""}</div>
                  </div>}
          </Card>
        </div>

        {/* ── Prometheus metrics ──────────────────────────────────────── */}
        <Card accent="var(--info)">
          <CardTitle icon="activity">Prometheus Metrics</CardTitle>
          {metricsErr && <span style={{ color: "var(--err)", fontSize: "var(--fs-sm)", fontFamily: "var(--font-mono)" }}>{metricsErr}</span>}
          {loading && <div style={{ display: "flex", flexDirection: "column", gap: "0.5rem" }}>{[1, 2, 3, 4].map((i) => <div key={i} style={{ display: "flex", justifyContent: "space-between" }}><Skeleton width={220} height={14} /><Skeleton width={50} height={14} /></div>)}</div>}
          {!loading && !metricsErr && displayedMetrics.length === 0 && <div style={{ color: "var(--muted)", fontSize: "var(--fs-sm)" }}>No metrics available — ensure /metrics is exposed.</div>}
          {!loading && !metricsErr && displayedMetrics.length > 0 && (
            <div>
              {displayedMetrics.map(([k, v]) => (
                <div key={k} style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", padding: "0.5rem 0", borderBottom: "1px solid var(--border)", fontSize: "var(--fs-sm)" }}>
                  <span style={{ color: "var(--muted)", fontFamily: "var(--font-mono)", fontSize: "var(--fs-xs)", maxWidth: "70%", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{k}</span>
                  <span style={{ fontFamily: "var(--font-mono)", color: "var(--text)", fontWeight: 600, fontVariantNumeric: "tabular-nums" }}>{typeof v === "number" ? v.toFixed(4) : v}</span>
                </div>
              ))}
            </div>
          )}
        </Card>
      </div>
    </PageContainer>
  );
}
