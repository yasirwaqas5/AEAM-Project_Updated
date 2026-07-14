import { useState, useEffect, useCallback, useMemo } from "react";
import { PageHeader, Card, CardTitle, Badge, Field, Icon, fmtRelative, getRetrievedCount } from "../components/ui";
import { PageContainer, MetricCard, Panel, EmptyState, LoadingState, ErrorState } from "../components/library";
import AgentLogCard from "../components/AgentLogCard";

/* ──────────────────────────────────────────────────────────────────────────
 * pages/Agents.jsx  (Agent Observatory)
 *
 * Visualizes the CURRENT runtime state of AEAM's six agents — Monitor, Rule
 * Engine, Forecast, RAG, Report, Action — from already-existing sources:
 *   - GET /metrics (Prometheus)          -> per-agent execution counts
 *     (agent_execution_time_seconds{agent="rag"|"forecast"|"report"},
 *     action_success_total / action_failure_total{action_type=...})
 *   - GET /api/v1/incidents/             -> derived last-activity timestamps,
 *     retrieved-evidence totals (RAG)
 *   - GET /api/v1/logs/agents            -> real, DB-backed action-execution
 *     log (reused verbatim via the existing AgentLogCard — unchanged)
 *   - GET /api/v1/data-center/activation -> datasets Monitor Agent is
 *     currently watching
 *   - GET /api/v1/knowledge/documents    -> knowledge-base status for RAG
 *   - GET /api/v1/system/status          -> overall system health
 *   - GET /api/v1/system/rule-engine     -> NEW, minimal, read-only endpoint
 *     added this phase (see aeam/api/system.py) — the only backend addition,
 *     justified because no existing endpoint exposed RuleEngine's curated
 *     domain list, a required field with no other honest source.
 *
 * Whatever isn't exposed anywhere (MonitorAgent's live cycle state, a
 * per-domain "last evaluation" timestamp, a "pending actions" queue that
 * doesn't architecturally exist — ActionAgent executes synchronously inside
 * finalize_incident) is disclosed as such, never fabricated.
 * ────────────────────────────────────────────────────────────────────────── */

async function fetchJSON(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`HTTP ${res.status} — ${url}`);
  return res.json();
}

function parsePrometheusText(raw) {
  // Same approach as pages/Dashboard.jsx / pages/Analytics.jsx, kept page-local.
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

// ─── Prometheus label lookups ────────────────────────────────────────────────

function metricFor(metrics, baseName, labelKey, labelValue) {
  if (!metrics) return null;
  const key = Object.keys(metrics).find(
    (k) => k.startsWith(baseName) && k.includes(`${labelKey}="${labelValue}"`),
  );
  return key ? metrics[key] : null;
}

function sumMetricSeries(metrics, baseName) {
  if (!metrics) return 0;
  return Object.entries(metrics)
    .filter(([k]) => k.startsWith(baseName))
    .reduce((sum, [, v]) => sum + v, 0);
}

// ─── Small building blocks ────────────────────────────────────────────────────

function Unavailable({ label }) {
  return <span style={{ color: "var(--muted)", fontStyle: "italic", fontSize: "0.8rem" }}>{label || "not exposed via API"}</span>;
}

function AgentOverviewCard({ name, icon, color, status, lastActivity, health }) {
  return (
    <Card accent={color} className="aeam-card-hover">
      <CardTitle icon={icon}>{name}</CardTitle>
      <div style={{ display: "flex", flexDirection: "column", gap: "0.5rem", marginTop: "0.4rem" }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <span style={{ fontSize: "0.72rem", color: "var(--muted)" }}>Status</span>
          {status.known ? <Badge label={status.label} color={status.color} dot /> : <Unavailable />}
        </div>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <span style={{ fontSize: "0.72rem", color: "var(--muted)" }}>Last Activity</span>
          {lastActivity ? <span style={{ fontSize: "0.76rem", fontFamily: "var(--font-mono)", color: "var(--text)" }}>{fmtRelative(lastActivity)}</span> : <Unavailable />}
        </div>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <span style={{ fontSize: "0.72rem", color: "var(--muted)" }}>Health</span>
          {health.known ? <Badge label={health.label} color={health.color} dot /> : <Unavailable />}
        </div>
      </div>
    </Card>
  );
}

// ─── Page ───────────────────────────────────────────────────────────────────

export default function Agents() {
  const [incidents, setIncidents] = useState([]);
  const [status, setStatus] = useState(null);
  const [metrics, setMetrics] = useState(null);
  const [agentLogs, setAgentLogs] = useState([]);
  const [documents, setDocuments] = useState([]);
  const [activation, setActivation] = useState({ activated_dataset_ids: [] });
  const [ruleEngine, setRuleEngine] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    setLoading(true); setError(null);
    const results = await Promise.allSettled([
      fetchJSON("/api/v1/incidents/"),
      fetchJSON("/api/v1/system/status"),
      fetchMetrics(),
      fetchJSON("/api/v1/logs/agents"),
      fetchJSON("/api/v1/knowledge/documents"),
      fetchJSON("/api/v1/data-center/activation"),
      fetchJSON("/api/v1/system/rule-engine"),
    ]);
    const [inc, stat, met, logs, docs, act, rules] = results;
    if (inc.status === "fulfilled") setIncidents(Array.isArray(inc.value) ? inc.value : []);
    if (stat.status === "fulfilled") setStatus(stat.value);
    if (met.status === "fulfilled") setMetrics(met.value);
    if (logs.status === "fulfilled") setAgentLogs(Array.isArray(logs.value) ? logs.value : []);
    if (docs.status === "fulfilled") setDocuments(Array.isArray(docs.value) ? docs.value : []);
    if (act.status === "fulfilled") setActivation(act.value);
    if (rules.status === "fulfilled") setRuleEngine(rules.value);
    if (inc.status === "rejected") setError(inc.reason?.message || "Failed to load incidents");
    setLoading(false);
  }, []);

  useEffect(() => { load(); }, [load]);

  // ── Derived, honest per-agent facts ──────────────────────────────────────

  const forecastCount = useMemo(() => metricFor(metrics, "agent_execution_time_seconds_count", "agent", "forecast"), [metrics]);
  const ragCount = useMemo(() => metricFor(metrics, "agent_execution_time_seconds_count", "agent", "rag"), [metrics]);
  const reportCount = useMemo(() => metricFor(metrics, "agent_execution_time_seconds_count", "agent", "report"), [metrics]);
  const actionSuccess = useMemo(() => sumMetricSeries(metrics, "action_success_total"), [metrics]);
  const actionFailure = useMemo(() => sumMetricSeries(metrics, "action_failure_total"), [metrics]);
  const actionTotal = actionSuccess + actionFailure;
  const successRate = actionTotal > 0 ? Math.round((actionSuccess / actionTotal) * 100) : null;

  const lastForecastIncident = useMemo(
    () => incidents.find((i) => {
      try { return JSON.parse(i.detection_methods || "[]").includes("FORECAST"); }
      catch { return Array.isArray(i.detection_methods) && i.detection_methods.includes("FORECAST"); }
    }),
    [incidents],
  );
  const lastRagIncident = useMemo(() => incidents.find((i) => getRetrievedCount(i) > 0), [incidents]);
  const mostRecentIncident = incidents[0]; // API already orders newest-first
  const mostRecentAction = agentLogs[0]; // already ordered DESC by the backend

  const totalRetrievedChunks = useMemo(() => incidents.reduce((sum, i) => sum + getRetrievedCount(i), 0), [incidents]);
  const indexedDocs = useMemo(() => documents.filter((d) => d.status === "indexed").length, [documents]);

  if (loading) {
    return (
      <PageContainer>
        <PageHeader title="Agent Observatory" subtitle="Runtime state of AEAM's autonomous agents" />
        <LoadingState label="Loading agent runtime state…" rows={6} />
      </PageContainer>
    );
  }

  if (error) {
    return (
      <PageContainer>
        <PageHeader title="Agent Observatory" subtitle="Runtime state of AEAM's autonomous agents"
          right={<button className="aeam-btn aeam-btn-ghost" onClick={load}><Icon name="activity" size={13} /> Retry</button>} />
        <ErrorState message={error} onRetry={load} />
      </PageContainer>
    );
  }

  return (
    <PageContainer max={1400}>
      <PageHeader
        title="Agent Observatory"
        subtitle="Runtime state of AEAM's autonomous agents — Monitor, Rule Engine, Forecast, RAG, Report, Action"
        right={<button className="aeam-btn aeam-btn-ghost" onClick={load} disabled={loading}>
          <Icon name="activity" size={13} /> {loading ? "Loading…" : "Refresh"}
        </button>}
      />

      {/* 1. Agent Overview cards */}
      <div className="aeam-grid-auto" style={{ marginBottom: "1.4rem" }}>
        <AgentOverviewCard name="Monitor Agent" icon="activity" color="#00b4ff"
          status={{ known: false }}
          lastActivity={null}
          health={{ known: activation.activated_dataset_ids?.length >= 0, label: `${activation.activated_dataset_ids?.length || 0} dataset(s) watched`, color: "var(--ok)" }} />
        <AgentOverviewCard name="Rule Engine" icon="shield" color="#8b5cf6"
          status={{ known: !!ruleEngine, label: ruleEngine ? `${ruleEngine.count} domain(s) loaded` : "unknown", color: "var(--ok)" }}
          lastActivity={null}
          health={{ known: !!ruleEngine, label: "Configured", color: "var(--ok)" }} />
        <AgentOverviewCard name="Forecast Agent" icon="target" color="#ffb800"
          status={{ known: forecastCount != null, label: forecastCount != null ? `${forecastCount} executed` : "unknown", color: forecastCount > 0 ? "var(--ok)" : "var(--muted)" }}
          lastActivity={lastForecastIncident?.timestamp}
          health={{ known: forecastCount != null, label: forecastCount > 0 ? "Active" : "No invocations yet", color: forecastCount > 0 ? "var(--ok)" : "var(--muted)" }} />
        <AgentOverviewCard name="RAG Agent" icon="database" color="#00ffa3"
          status={{ known: ragCount != null, label: ragCount != null ? `${ragCount} requests` : "unknown", color: ragCount > 0 ? "var(--ok)" : "var(--muted)" }}
          lastActivity={lastRagIncident?.timestamp}
          health={{ known: true, label: `${indexedDocs}/${documents.length} docs indexed`, color: "var(--ok)" }} />
        <AgentOverviewCard name="Report Agent" icon="code" color="#00b4ff"
          status={{ known: reportCount != null, label: reportCount != null ? `${reportCount} generated` : "unknown", color: reportCount > 0 ? "var(--ok)" : "var(--muted)" }}
          lastActivity={mostRecentIncident?.timestamp}
          health={{ known: true, label: "Configured", color: "var(--ok)" }} />
        <AgentOverviewCard name="Action Agent" icon="zap" color="#ff5f57"
          status={{ known: actionTotal > 0, label: `${actionTotal} run(s)`, color: actionFailure > 0 ? "var(--warn)" : "var(--ok)" }}
          lastActivity={mostRecentAction?.timestamp}
          health={{ known: successRate != null, label: successRate != null ? `${successRate}% success` : "unknown", color: successRate >= 80 ? "var(--ok)" : successRate != null ? "var(--warn)" : "var(--muted)" }} />
      </div>

      {/* 2. Per-agent panels */}
      <div className="aeam-grid-2" style={{ marginBottom: "1.4rem" }}>
        <Panel title="Monitor Agent" icon="activity">
          <div className="aeam-grid-auto">
            <Field label="Current State" value={<Unavailable />} />
            <Field label="Last Monitoring Cycle" value={<Unavailable label="not exposed via API — MonitorAgent's cycle state has no runtime endpoint" />} />
            <Field label="Datasets Being Monitored" value={activation.activated_dataset_ids?.length || 0} />
          </div>
          {activation.activated_dataset_ids?.length > 0 && (
            <div style={{ marginTop: "0.6rem", display: "flex", flexWrap: "wrap", gap: "0.4rem" }}>
              {activation.activated_dataset_ids.map((id) => (
                <Badge key={id} label={id.slice(0, 8)} color="var(--muted)" />
              ))}
            </div>
          )}
        </Panel>

        <Panel title="Rule Engine" icon="shield">
          <div className="aeam-grid-auto">
            <Field label="Rules Loaded" value={ruleEngine ? ruleEngine.count : <Unavailable />} />
            <Field label="Last Evaluation" value={<Unavailable label="not tracked — per-domain evaluation timestamps are not persisted" />} />
          </div>
          {ruleEngine?.loaded_domains?.length > 0 && (
            <div style={{ marginTop: "0.6rem" }}>
              <span style={{ fontSize: "0.62rem", textTransform: "uppercase", letterSpacing: "0.12em", color: "var(--muted)" }}>Domains</span>
              <div style={{ display: "flex", flexWrap: "wrap", gap: "0.4rem", marginTop: "0.4rem" }}>
                {ruleEngine.loaded_domains.map((d) => <Badge key={d} label={d} color="#8b5cf6" />)}
              </div>
            </div>
          )}
        </Panel>

        <Panel title="Forecast Agent" icon="target">
          <div className="aeam-grid-auto">
            <Field label="Forecasts Executed" value={forecastCount ?? <Unavailable />} mono />
            <Field label="Last Forecast" value={lastForecastIncident ? fmtRelative(lastForecastIncident.timestamp) : <Unavailable label="no forecast-deviation incident recorded" />} />
            <Field label="Availability" value="Configured (always constructed at startup)" />
          </div>
        </Panel>

        <Panel title="RAG Agent" icon="database">
          <div className="aeam-grid-auto">
            <Field label="Retrieval Requests" value={ragCount ?? <Unavailable />} mono />
            <Field label="Retrieved Chunks (total, across incidents)" value={totalRetrievedChunks} mono />
            <Field label="Knowledge Status" value={`${indexedDocs} / ${documents.length} document(s) indexed`} />
          </div>
        </Panel>

        <Panel title="Report Agent" icon="code">
          <div className="aeam-grid-auto">
            <Field label="Reports Generated" value={reportCount ?? <Unavailable />} mono />
            <Field label="Latest Report" value={mostRecentIncident
              ? <span title="Approximated by the most recent incident — report content itself is not separately persisted">{fmtRelative(mostRecentIncident.timestamp)}</span>
              : <Unavailable />} />
            <Field label="Status" value="Configured (always constructed at startup)" />
          </div>
        </Panel>

        <Panel title="Action Agent" icon="zap">
          <div className="aeam-grid-auto">
            <Field label="Executed Actions" value={actionSuccess} mono color="var(--ok)" />
            <Field label="Failed Actions" value={actionFailure} mono color={actionFailure > 0 ? "var(--err)" : "var(--text)"} />
            <Field label="Pending Actions" value={<Unavailable label="not applicable — actions execute synchronously within incident finalization; no pending queue exists" />} />
            <Field label="Success Rate" value={successRate != null ? `${successRate}%` : "—"} mono />
          </div>
        </Panel>
      </div>

      {/* 3. Agent Activity Timeline (reused verbatim: real DB-backed action_logs + AgentLogCard) */}
      <div style={{ marginBottom: "1.4rem" }}>
        <Panel title="Agent Activity Timeline" icon="clock"
          right={<span style={{ fontSize: "0.62rem", color: "var(--muted)", fontFamily: "var(--font-mono)" }}>action-dispatch steps (jira / slack / email / diagnostics / monitoring)</span>}>
          {agentLogs.length === 0 ? (
            <EmptyState icon="clock" title="No agent executions logged yet" />
          ) : (
            <div className="aeam-grid-2">
              {agentLogs.slice(0, 8).map((log, i) => <AgentLogCard key={i} log={log} />)}
            </div>
          )}
        </Panel>
      </div>

      <div className="aeam-grid-2" style={{ marginBottom: "1.4rem" }}>
        {/* 4. Runtime Metrics */}
        <Panel title="Runtime Metrics" icon="activity" right={<span style={{ fontSize: "0.62rem", color: "var(--muted)", fontFamily: "var(--font-mono)" }}>GET /metrics</span>}>
          {metrics === null ? (
            <EmptyState icon="activity" title="Metrics unavailable" tone="muted" />
          ) : (
            <div>
              {[
                ["Forecast invocations", forecastCount],
                ["RAG invocations", ragCount],
                ["Report invocations", reportCount],
                ["Actions succeeded", actionSuccess],
                ["Actions failed", actionFailure],
              ].map(([label, v]) => (
                <div key={label} style={{ display: "flex", justifyContent: "space-between", padding: "0.45rem 0", borderBottom: "1px solid var(--border)" }}>
                  <span style={{ fontSize: "0.76rem", color: "var(--muted)" }}>{label}</span>
                  <span style={{ fontSize: "0.78rem", fontFamily: "var(--font-mono)", color: "var(--text)", fontWeight: 600 }}>{v ?? "—"}</span>
                </div>
              ))}
            </div>
          )}
        </Panel>

        {/* 5. Overall System Health */}
        <Panel title="Overall System Health" icon="shield">
          {status ? (
            <div style={{ display: "flex", flexDirection: "column", gap: "0.6rem" }}>
              <div style={{ display: "flex", justifyContent: "space-between" }}>
                <span style={{ fontSize: "0.78rem", color: "var(--muted)" }}>Status</span>
                <Badge label={(status.status || "unknown").toUpperCase()} color={status.status === "healthy" ? "#00ffa3" : "#ff5f57"} dot />
              </div>
              <div style={{ display: "flex", justifyContent: "space-between" }}>
                <span style={{ fontSize: "0.78rem", color: "var(--muted)" }}>Active Incidents</span>
                <span style={{ fontSize: "0.78rem", fontFamily: "var(--font-mono)" }}>{status.active_incidents}</span>
              </div>
              <div style={{ display: "flex", justifyContent: "space-between" }}>
                <span style={{ fontSize: "0.78rem", color: "var(--muted)" }}>Agents Active</span>
                <span style={{ fontSize: "0.78rem", fontFamily: "var(--font-mono)" }} title="Static configuration count (registered agent types) — not a live per-agent health check.">
                  {status.agents_active} <Icon name="alert" size={10} style={{ verticalAlign: "middle", opacity: 0.6 }} />
                </span>
              </div>
              <div style={{ fontSize: "0.66rem", color: "var(--muted)", fontStyle: "italic" }}>
                "Agents Active" is a static registered-agent-types count, not live per-agent health — see the panels above for what's actually observable per agent.
              </div>
              <div style={{ display: "flex", justifyContent: "space-between" }}>
                <span style={{ fontSize: "0.78rem", color: "var(--muted)" }}>Last Event</span>
                <span style={{ fontSize: "0.78rem", fontFamily: "var(--font-mono)" }}>{fmtRelative(status.last_event_time)}</span>
              </div>
            </div>
          ) : <EmptyState icon="shield" title="System status unavailable" tone="muted" />}
        </Panel>
      </div>
    </PageContainer>
  );
}
