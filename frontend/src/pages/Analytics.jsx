import { useState, useEffect, useCallback, useMemo } from "react";
import {
  PageHeader, Card, CardTitle, Badge, Icon, Skeleton,
  SEVERITY, severityOf, deriveStatus, fmtTime, fmtRelative,
} from "../components/ui";
import { PageContainer, MetricCard, Panel, EmptyState, LoadingState, ErrorState, DataTable } from "../components/library";

/* ──────────────────────────────────────────────────────────────────────────
 * pages/Analytics.jsx  (Analytics Center)
 *
 * Every section is built from an already-existing, UNMODIFIED endpoint —
 * no new backend anywhere on this page:
 *   - GET /api/v1/incidents/            -> Incident Trends, Active vs
 *     Resolved, Severity Distribution (bucketed/derived client-side, same
 *     deriveStatus()/severityOf() helpers Dashboard/Incidents already use)
 *   - GET /api/v1/system/status         -> Current KPI snapshot, System Health
 *   - GET /metrics (Prometheus text)    -> reuses the EXACT parsePrometheusText
 *     approach already implemented in pages/Dashboard.jsx
 *   - GET /api/v1/logs/agents           -> Agent Activity, Recent Actions
 *     (real, DB-backed query against action_logs — not the mock data its
 *     own module docstring still describes)
 *   - GET /api/v1/knowledge/datasets +
 *     GET /api/v1/data-center/activation -> Business Metrics cards
 *   - GET /api/v1/observability/ (Phase D3, NEW endpoint) -> Enterprise
 *     Observability panel. Reuses the IDENTICAL incidents-table read
 *     GET /api/v1/incidents/ already performs (aeam/api/observability.py
 *     imports incidents.py's own SQL + fetch helper — no second query, no
 *     new metrics store) and reduces it to cross-incident hit rates/trends
 *     via aeam/intelligence/observability.py's ObservabilityEngine. Every
 *     figure is a real computed rate or an honest {available: false,
 *     reason} — never fabricated.
 *
 * Forecast vs Actual has no persisted per-incident forecast history anywhere
 * in the system (confirmed: no endpoint exposes LongTermMemory time series) —
 * honestly disclosed as unavailable rather than fabricated, mirroring the
 * same disclosure already used in pages/Investigation.jsx's Metric Trend panel.
 * ────────────────────────────────────────────────────────────────────────── */

// ─── Data fetching ──────────────────────────────────────────────────────────

async function fetchJSON(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`HTTP ${res.status} — ${url}`);
  return res.json();
}

function parsePrometheusText(raw) {
  // Identical approach to pages/Dashboard.jsx's parser — kept page-local
  // rather than cross-imported, matching this codebase's established
  // per-page-local-helper convention (see fetchJSON in every other page).
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

async function fetchMetrics() {
  const res = await fetch("/metrics");
  if (!res.ok) throw new Error(`HTTP ${res.status} — /metrics`);
  return parsePrometheusText(await res.text());
}

// Phase D3 — Enterprise Observability Engine. A NEW, read-only backend
// endpoint (aeam/api/observability.py) reusing the identical incidents-table
// read incidents.py already performs — no second data source, no new
// metrics store. Every field is either a real computed rate/score or an
// honest {available: false, reason} — never fabricated.
async function fetchObservability() {
  return fetchJSON("/api/v1/observability/");
}

// ─── Lightweight SVG / div charts (no charting library) ─────────────────────

function TrendBarChart({ buckets }) {
  if (!buckets.length) return <EmptyState icon="target" title="No incident history yet" tone="muted" />;
  const max = Math.max(...buckets.map((b) => b.count), 1);
  const w = 640, h = 160, padL = 28, padB = 22, barGap = 4;
  const barW = (w - padL) / buckets.length - barGap;
  return (
    <svg viewBox={`0 0 ${w} ${h}`} style={{ width: "100%", height: "auto", display: "block" }}>
      <line x1={padL} y1={h - padB} x2={w} y2={h - padB} stroke="var(--border)" strokeWidth="1" />
      <line x1={padL} y1="4" x2={padL} y2={h - padB} stroke="var(--border)" strokeWidth="1" />
      <text x="2" y="12" fontSize="9" fill="var(--muted)">{max}</text>
      <text x="2" y={h - padB} fontSize="9" fill="var(--muted)">0</text>
      {buckets.map((b, i) => {
        const barH = (b.count / max) * (h - padB - 10);
        const x = padL + i * (barW + barGap);
        const y = h - padB - barH;
        return (
          <g key={b.label}>
            <rect x={x} y={y} width={barW} height={barH} fill="var(--accent)" opacity="0.85" rx="2">
              <title>{`${b.label}: ${b.count}`}</title>
            </rect>
            {i % Math.ceil(buckets.length / 8 || 1) === 0 && (
              <text x={x + barW / 2} y={h - 6} fontSize="8" fill="var(--muted)" textAnchor="middle">{b.label}</text>
            )}
          </g>
        );
      })}
    </svg>
  );
}

function DistributionBar({ segments, total }) {
  if (!total) return <EmptyState icon="target" title="No incidents to distribute" tone="muted" />;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "0.7rem" }}>
      <div style={{ display: "flex", height: 14, borderRadius: 7, overflow: "hidden", background: "var(--border)" }}>
        {segments.filter((s) => s.count > 0).map((s) => (
          <div key={s.label} style={{ width: `${(s.count / total) * 100}%`, background: s.color }} title={`${s.label}: ${s.count}`} />
        ))}
      </div>
      <div style={{ display: "flex", flexWrap: "wrap", gap: "0.9rem" }}>
        {segments.map((s) => (
          <div key={s.label} style={{ display: "flex", alignItems: "center", gap: "0.4rem", fontSize: "0.74rem", color: "var(--muted)" }}>
            <span style={{ width: 8, height: 8, borderRadius: "50%", background: s.color, flexShrink: 0 }} />
            {s.label} <span style={{ color: "var(--text)", fontFamily: "var(--font-mono)" }}>{s.count}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

// ─── Derivation helpers (client-side, over already-fetched data) ────────────

function bucketByDay(incidents, days = 14) {
  const buckets = [];
  const now = new Date();
  for (let i = days - 1; i >= 0; i--) {
    const d = new Date(now); d.setDate(d.getDate() - i); d.setHours(0, 0, 0, 0);
    buckets.push({ date: d, label: `${d.getMonth() + 1}/${d.getDate()}`, count: 0 });
  }
  for (const inc of incidents) {
    const t = new Date(inc.timestamp);
    if (isNaN(t)) continue;
    t.setHours(0, 0, 0, 0);
    const bucket = buckets.find((b) => b.date.getTime() === t.getTime());
    if (bucket) bucket.count += 1;
  }
  return buckets;
}

function statusDistribution(incidents) {
  let active = 0, resolved = 0, failed = 0;
  for (const inc of incidents) {
    const key = deriveStatus(inc).key;
    if (key === "ESCALATED" || key === "INVESTIGATING") active += 1;
    else if (key === "FAILED") failed += 1;
    else resolved += 1; // RESOLVED, COMPLETE
  }
  return [
    { label: "Active", count: active, color: "#00b4ff" },
    { label: "Resolved", count: resolved, color: "#00ffa3" },
    { label: "Failed", count: failed, color: "#ff5f57" },
  ];
}

// Converts an ObservabilityEngine trend's `recent_values` (chronological,
// oldest-first, each a real 0..1 rate/score already computed by the
// backend) into TrendBarChart's {label, count} bucket shape — a display
// reformat only, never a new computation.
function trendToBuckets(trend) {
  if (!trend?.available) return [];
  return (trend.recent_values || []).map((v, i) => ({ label: String(i + 1), count: Math.round(v * 100) }));
}

function severityDistribution(incidents) {
  const order = ["CRITICAL", "HIGH", "MEDIUM", "LOW"];
  const counts = Object.fromEntries(order.map((k) => [k, 0]));
  for (const inc of incidents) {
    const key = (inc.severity || "").toUpperCase();
    if (key in counts) counts[key] += 1;
  }
  return order.map((k) => ({ label: k, count: counts[k], color: SEVERITY[k].color }));
}

// ─── Phase D3 building blocks ───────────────────────────────────────────────

function ObservabilityRateRow({ label, metric }) {
  const pct = metric?.available ? Math.round(metric.rate * 100) : null;
  return (
    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", padding: "0.45rem 0", borderBottom: "1px solid var(--border)" }}>
      <span style={{ fontSize: "0.78rem", color: "var(--muted)" }}>{label}</span>
      {pct != null ? (
        <span style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
          <span style={{ fontSize: "0.68rem", color: "var(--muted)", fontFamily: "var(--font-mono)" }}>
            {metric.hit_count ?? metric.resolved_count}/{metric.consulted_count ?? metric.total_with_status}
          </span>
          <Badge label={`${pct}%`} color={pct >= 70 ? "#00ffa3" : pct >= 40 ? "#ffb800" : "#ff5f57"} />
        </span>
      ) : (
        <Badge label="not available" color="var(--muted)" />
      )}
    </div>
  );
}

// ─── Page ───────────────────────────────────────────────────────────────────

export default function Analytics() {
  const [incidents, setIncidents] = useState([]);
  const [status, setStatus] = useState(null);
  const [metrics, setMetrics] = useState(null);
  const [agentLogs, setAgentLogs] = useState([]);
  const [datasets, setDatasets] = useState([]);
  const [activation, setActivation] = useState({ activated_dataset_ids: [] });
  const [observability, setObservability] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    setLoading(true); setError(null);
    const results = await Promise.allSettled([
      fetchJSON("/api/v1/incidents/"),
      fetchJSON("/api/v1/system/status"),
      fetchMetrics(),
      fetchJSON("/api/v1/logs/agents"),
      fetchJSON("/api/v1/knowledge/datasets"),
      fetchJSON("/api/v1/data-center/activation"),
      fetchObservability(),
    ]);
    const [inc, stat, met, logs, ds, act, obs] = results;
    if (inc.status === "fulfilled") setIncidents(Array.isArray(inc.value) ? inc.value : []);
    if (stat.status === "fulfilled") setStatus(stat.value);
    if (met.status === "fulfilled") setMetrics(met.value);
    if (logs.status === "fulfilled") setAgentLogs(Array.isArray(logs.value) ? logs.value : []);
    if (ds.status === "fulfilled") setDatasets(Array.isArray(ds.value) ? ds.value : []);
    if (act.status === "fulfilled") setActivation(act.value);
    if (obs.status === "fulfilled") setObservability(obs.value);
    // Only a total failure of the primary (incidents) feed is a page-level error —
    // the other panels each show their own honest per-section empty/error state.
    if (inc.status === "rejected") setError(inc.reason?.message || "Failed to load incidents");
    setLoading(false);
  }, []);

  useEffect(() => { load(); }, [load]);

  const trendBuckets = useMemo(() => bucketByDay(incidents), [incidents]);
  const statusDist = useMemo(() => statusDistribution(incidents), [incidents]);
  const severityDist = useMemo(() => severityDistribution(incidents), [incidents]);

  const agentSummary = useMemo(() => {
    const byAgent = {};
    for (const log of agentLogs) {
      const key = log.agent || "unknown";
      if (!byAgent[key]) byAgent[key] = { agent: key, total: 0, success: 0, failed: 0, totalMs: 0 };
      byAgent[key].total += 1;
      if ((log.status || "").toUpperCase() === "SUCCESS") byAgent[key].success += 1;
      else byAgent[key].failed += 1;
      byAgent[key].totalMs += Number(log.execution_time_ms) || 0;
    }
    return Object.values(byAgent).map((a) => ({ ...a, avgMs: a.total ? Math.round(a.totalMs / a.total) : 0 }));
  }, [agentLogs]);

  const recentActions = useMemo(
    () => [...agentLogs].sort((a, b) => new Date(b.timestamp) - new Date(a.timestamp)).slice(0, 8),
    [agentLogs],
  );

  const totalMonitorableMetrics = useMemo(
    () => datasets.reduce((sum, d) => sum + (d.metric_columns || []).length, 0),
    [datasets],
  );
  const activatedCount = activation?.activated_dataset_ids?.length || 0;

  const promHighlights = metrics
    ? Object.entries(metrics).filter(([k]) =>
        ["incidents_total", "active_incidents", "action_success_total", "action_failure_total",
         "investigation_duration_seconds_sum", "agent_execution_time_seconds_sum"].some((h) => k.startsWith(h)))
    : [];

  if (loading) {
    return (
      <PageContainer>
        <PageHeader title="Analytics" subtitle="Operational trends over data the system already produces" />
        <LoadingState label="Loading analytics…" rows={6} />
      </PageContainer>
    );
  }

  if (error) {
    return (
      <PageContainer>
        <PageHeader title="Analytics" subtitle="Operational trends over data the system already produces"
          right={<button className="aeam-btn aeam-btn-ghost" onClick={load}><Icon name="activity" size={13} /> Retry</button>} />
        <ErrorState message={error} onRetry={load} />
      </PageContainer>
    );
  }

  return (
    <PageContainer max={1400}>
      <PageHeader
        title="Analytics"
        subtitle="Operational trends over data the system already produces"
        right={<button className="aeam-btn aeam-btn-ghost" onClick={load} disabled={loading}>
          <Icon name="activity" size={13} /> {loading ? "Loading…" : "Refresh"}
        </button>}
      />

      {/* Current KPI snapshot */}
      <div className="aeam-grid-metrics" style={{ marginBottom: "1.4rem" }}>
        <MetricCard label="Active Incidents" value={status?.active_incidents ?? "—"} icon="alert" accent="#ffb800" />
        <MetricCard label="Agents Active" value={status?.agents_active ?? "—"} icon="activity" accent="#00b4ff" />
        <MetricCard label="Total Incidents" value={incidents.length} icon="branch" />
        <MetricCard label="System Status" value={(status?.status || "unknown").toUpperCase()}
          icon="shield" accent={status?.status === "healthy" ? "#00ffa3" : "#ff5f57"} />
      </div>

      <div className="aeam-grid-2" style={{ marginBottom: "1.4rem" }}>
        {/* Incident Trends */}
        <Panel title="Incident Trends (14 days)" icon="branch">
          <TrendBarChart buckets={trendBuckets} />
        </Panel>

        {/* Active vs Resolved */}
        <Panel title="Active vs Resolved" icon="activity">
          <DistributionBar segments={statusDist} total={incidents.length} />
        </Panel>

        {/* Severity Distribution */}
        <Panel title="Severity Distribution" icon="alert">
          <DistributionBar segments={severityDist} total={incidents.length} />
        </Panel>

        {/* Forecast vs Actual — honestly unavailable */}
        <Panel title="Forecast vs Actual" icon="target">
          <EmptyState icon="target" title="Not available yet" tone="muted"
            description="No per-incident forecast history is persisted anywhere in the system today — only whether a forecast deviation fired (see the incident timeline's Forecast Analysis stage). Charting a real forecast-vs-actual trend needs a metric-history endpoint, which does not exist yet." />
        </Panel>
      </div>

      <div className="aeam-grid-2" style={{ marginBottom: "1.4rem" }}>
        {/* System Health summary */}
        <Panel title="System Health" icon="shield">
          {status ? (
            <div style={{ display: "flex", flexDirection: "column", gap: "0.6rem" }}>
              <div style={{ display: "flex", justifyContent: "space-between" }}>
                <span style={{ fontSize: "0.78rem", color: "var(--muted)" }}>Overall status</span>
                <Badge label={(status.status || "unknown").toUpperCase()} color={status.status === "healthy" ? "#00ffa3" : "#ff5f57"} dot />
              </div>
              {Object.entries(status)
                .filter(([k]) => !["status", "active_incidents", "agents_active", "last_event_time"].includes(k))
                .map(([k, v]) => (
                  <div key={k} style={{ display: "flex", justifyContent: "space-between" }}>
                    <span style={{ fontSize: "0.78rem", color: "var(--muted)", textTransform: "capitalize" }}>{k.replace(/_/g, " ")}</span>
                    <Badge label={String(v).toUpperCase()} color={String(v) === "true" || v === "healthy" ? "#00ffa3" : "var(--muted)"} dot />
                  </div>
                ))}
              {Object.keys(status).filter((k) => !["status", "active_incidents", "agents_active", "last_event_time"].includes(k)).length === 0 && (
                <span style={{ fontSize: "0.78rem", color: "var(--muted)" }}>All core components nominal.</span>
              )}
              <div style={{ display: "flex", justifyContent: "space-between" }}>
                <span style={{ fontSize: "0.78rem", color: "var(--muted)" }}>Last event</span>
                <span style={{ fontSize: "0.78rem", fontFamily: "var(--font-mono)" }}>{fmtRelative(status.last_event_time)}</span>
              </div>
            </div>
          ) : <EmptyState icon="shield" title="System status unavailable" tone="muted" />}
        </Panel>

        {/* Prometheus metrics already exposed */}
        <Panel title="Prometheus Metrics" icon="activity"
          right={<span style={{ fontSize: "0.62rem", color: "var(--muted)", fontFamily: "var(--font-mono)" }}>GET /metrics</span>}>
          {metrics === null ? (
            <EmptyState icon="activity" title="Metrics unavailable" description="Could not reach /metrics." tone="muted" />
          ) : promHighlights.length === 0 ? (
            <EmptyState icon="activity" title="No matching series yet" tone="muted" />
          ) : (
            <div>
              {promHighlights.map(([k, v]) => (
                <div key={k} style={{ display: "flex", justifyContent: "space-between", padding: "0.45rem 0", borderBottom: "1px solid var(--border)" }}>
                  <span style={{ fontSize: "0.72rem", color: "var(--muted)", fontFamily: "var(--font-mono)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", maxWidth: "70%" }}>{k}</span>
                  <span style={{ fontSize: "0.78rem", fontFamily: "var(--font-mono)", color: "var(--text)", fontWeight: 600 }}>{v.toFixed(2)}</span>
                </div>
              ))}
            </div>
          )}
        </Panel>
      </div>

      {/* Business Metrics cards */}
      <div style={{ marginBottom: "1.4rem" }}>
        <Panel title="Business Metrics" icon="database" pad={false}>
          <div className="aeam-grid-metrics" style={{ padding: "1.1rem 1.25rem" }}>
            <MetricCard label="Registered Datasets" value={datasets.length} icon="layers" />
            <MetricCard label="Monitorable Metrics" value={totalMonitorableMetrics} icon="target"
              sub="measure columns discovered across datasets" />
            <MetricCard label="Activated for Monitoring" value={activatedCount} icon="activity" accent="#00ffa3" />
            <MetricCard label="Inactive Datasets" value={Math.max(0, datasets.length - activatedCount)} icon="database" accent="var(--muted)" />
          </div>
        </Panel>
      </div>

      {/* Phase D3 — Enterprise Observability Engine */}
      <div style={{ marginBottom: "1.4rem" }}>
        <Panel title="Enterprise Observability" icon="shield"
          right={<span style={{ fontSize: "0.62rem", color: "var(--muted)", fontFamily: "var(--font-mono)" }}>GET /api/v1/observability/</span>}>
          {!observability ? (
            <EmptyState icon="shield" title="Observability data unavailable" tone="muted" />
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: "1.1rem" }}>
              <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: "0.75rem" }}>
                <div>
                  <span style={{ fontSize: "0.62rem", textTransform: "uppercase", letterSpacing: "0.12em", color: "var(--muted)" }}>
                    Overall AI Health ({observability.total_investigations} investigation{observability.total_investigations === 1 ? "" : "s"})
                  </span>
                  <div style={{ fontSize: "1.6rem", fontWeight: 800, fontFamily: "var(--font-mono)", color: observability.overall_ai_health?.available ? "#00ffa3" : "var(--muted)" }}>
                    {observability.overall_ai_health?.available ? `${Math.round(observability.overall_ai_health.score * 100)}%` : "N/A"}
                  </div>
                </div>
                <span style={{ fontSize: "0.68rem", color: "var(--muted)", maxWidth: 420 }}>{observability.overall_ai_health_formula}</span>
              </div>

              <div className="aeam-grid-2" style={{ gap: "1.1rem" }}>
                <div>
                  <span style={{ fontSize: "0.62rem", textTransform: "uppercase", letterSpacing: "0.12em", color: "var(--muted)" }}>Evidence Source Rates</span>
                  <div style={{ marginTop: "0.4rem" }}>
                    <ObservabilityRateRow label="Memory Hit Rate" metric={observability.memory_hit_rate} />
                    <ObservabilityRateRow label="Policy Hit Rate" metric={observability.policy_hit_rate} />
                    <ObservabilityRateRow label="Retrieval Success Rate" metric={observability.retrieval_success_rate} />
                    <ObservabilityRateRow label="Cross-Dataset Usage" metric={observability.cross_dataset_usage_rate} />
                    <ObservabilityRateRow label="Adaptive Detection Usage" metric={observability.adaptive_detection_usage_rate} />
                    <ObservabilityRateRow label="Investigation Success Rate" metric={observability.investigation_success_rate} />
                  </div>
                </div>

                <div style={{ display: "flex", flexDirection: "column", gap: "0.9rem" }}>
                  <div>
                    <span style={{ fontSize: "0.62rem", textTransform: "uppercase", letterSpacing: "0.12em", color: "var(--muted)" }}>
                      Execution Plan Confidence Trend
                    </span>
                    {observability.execution_plan_confidence_trend?.available ? (
                      <>
                        <div style={{ marginTop: "0.3rem" }}><TrendBarChart buckets={trendToBuckets(observability.execution_plan_confidence_trend)} /></div>
                        <span style={{ fontSize: "0.68rem", color: "var(--muted)" }}>
                          avg {Math.round(observability.execution_plan_confidence_trend.average * 100)}% · {observability.execution_plan_confidence_trend.direction}
                        </span>
                      </>
                    ) : <EmptyState icon="target" title="Not available" tone="muted" description={observability.execution_plan_confidence_trend?.reason} />}
                  </div>
                  <div>
                    <span style={{ fontSize: "0.62rem", textTransform: "uppercase", letterSpacing: "0.12em", color: "var(--muted)" }}>
                      AI Evaluation Trend
                    </span>
                    {observability.ai_evaluation_trend?.available ? (
                      <>
                        <div style={{ marginTop: "0.3rem" }}><TrendBarChart buckets={trendToBuckets(observability.ai_evaluation_trend)} /></div>
                        <span style={{ fontSize: "0.68rem", color: "var(--muted)" }}>
                          avg {Math.round(observability.ai_evaluation_trend.average * 100)}% · {observability.ai_evaluation_trend.direction}
                        </span>
                      </>
                    ) : <EmptyState icon="target" title="Not available" tone="muted" description={observability.ai_evaluation_trend?.reason} />}
                  </div>
                </div>
              </div>

              <div style={{
                display: "flex", alignItems: "flex-start", gap: "0.5rem",
                padding: "0.55rem 0.8rem", border: "1px dashed var(--border)", borderRadius: 8,
                color: "var(--muted)", fontSize: "0.7rem",
              }}>
                <Icon name="alert" size={12} color="var(--muted)" style={{ marginTop: "0.1rem", flexShrink: 0 }} />
                <span>
                  {observability.investigation_duration?.available
                    ? `Investigation duration: ${observability.investigation_duration.average}s average.`
                    : `Investigation duration: ${observability.investigation_duration?.reason || "not available"}`}
                </span>
              </div>
            </div>
          )}
        </Panel>
      </div>

      <div className="aeam-grid-2">
        {/* Agent Activity summary */}
        <Panel title="Agent Activity" icon="layers" pad={false}>
          <DataTable
            columns={[
              { key: "agent", label: "Agent" },
              { key: "total", label: "Runs", align: "right" },
              { key: "success", label: "Success", align: "right", render: (r) => <span style={{ color: "var(--ok)" }}>{r.success}</span> },
              { key: "failed", label: "Failed", align: "right", render: (r) => <span style={{ color: r.failed ? "var(--err)" : "var(--muted)" }}>{r.failed}</span> },
              { key: "avgMs", label: "Avg Duration", align: "right", render: (r) => `${r.avgMs}ms` },
            ]}
            rows={agentSummary}
            rowKey={(r) => r.agent}
            empty="No agent activity recorded yet."
          />
        </Panel>

        {/* Recent Actions summary */}
        <Panel title="Recent Actions" icon="zap" pad={false}>
          <DataTable
            columns={[
              { key: "agent", label: "Action" },
              { key: "status", label: "Status", render: (r) => <Badge label={r.status} color={(r.status || "").toUpperCase() === "SUCCESS" ? "#00ffa3" : "#ff5f57"} dot /> },
              { key: "timestamp", label: "When", render: (r) => fmtRelative(r.timestamp) },
            ]}
            rows={recentActions}
            rowKey={(r, i) => `${r.incident_id || "action"}-${i}`}
            empty="No recent actions recorded yet."
          />
        </Panel>
      </div>
    </PageContainer>
  );
}
