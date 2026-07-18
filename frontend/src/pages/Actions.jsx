import { useState, useEffect, useCallback, useMemo } from "react";
import {
  PageHeader, Badge, SeverityBadge, Modal, Field, Icon, Button,
  fmtTime, fmtRelative, fmtMs, stateColor, actionLabel,
} from "../components/ui";
import {
  PageContainer, MetricCard, Panel, DataTable, LoadingState, ErrorState, EmptyState,
  TimelineContainer, TimelineItem,
} from "../components/library";
import AgentLogCard from "../components/AgentLogCard";
import { fetchJSON } from "./KnowledgeCenter";

/* ──────────────────────────────────────────────────────────────────────────
 * pages/Actions.jsx  (Enterprise Actions Center)
 *
 * The complete operational history of everything ActionAgent has executed —
 * built entirely from already-existing, unmodified sources:
 *   - GET /api/v1/logs/agents  -> real, DB-backed action_logs rows (the SAME
 *     endpoint + AgentLogCard the Agent Observatory's timeline already uses,
 *     reused verbatim, not duplicated). Capped at the 50 most recent rows by
 *     the endpoint itself. Overview/Metrics/table/timeline all derive from
 *     this ONE sample so every number on the page agrees with what's
 *     actually listed below it — all disclosed as "of the last N shown",
 *     never presented as a full-history total.
 *   - GET /metrics (Prometheus)  -> action_success_total / action_failure_total
 *     — true totals across ALL executions since the last backend restart
 *     (not capped at 50). Shown in a separate, clearly-labeled "Runtime
 *     Metrics" panel (same framing as Agents.jsx's own panel of the same
 *     name) rather than mixed into the primary tiles above, since a
 *     Prometheus counter can be smaller (after a restart) or larger (beyond
 *     50 rows) than the log sample — conflating the two would look like a
 *     bug rather than two honestly-different scopes.
 *   - GET /api/v1/incidents/  -> incident event_type/severity/timestamp, to
 *     show the real "Triggering Incident" context for each action.
 *
 * ActionAgent itself is untouched. The DB only ever persists status SUCCESS
 * or FAILED (see aeam/agents/action/action_agent.py) — there is no distinct
 * "SKIPPED" or "PENDING" status column. "Skipped" here means a FAILED action
 * whose validation_result the backend already recorded as SKIPPED (a
 * configuration error, as opposed to a payload-validation FAILED or a
 * transient runtime FAILED) — a real, existing field, not a fabricated
 * bucket. "Pending" is disclosed as not applicable, matching the same
 * finding already surfaced on the Agent Observatory: actions execute
 * synchronously inside incident finalization, so no pending queue exists.
 * ────────────────────────────────────────────────────────────────────────── */

// ─── Prometheus text parsing (page-local; same approach as Dashboard.jsx /
// Analytics.jsx / Agents.jsx — this repo keeps this parser page-local rather
// than as a shared module, so this follows the existing convention) ────────

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

async function fetchMetrics() {
  const res = await fetch("/metrics");
  if (!res.ok) throw new Error(`HTTP ${res.status} — /metrics`);
  return parsePrometheusText(await res.text());
}

function sumMetricSeries(metrics, baseName) {
  if (!metrics) return 0;
  return Object.entries(metrics)
    .filter(([k]) => k.startsWith(baseName))
    .reduce((sum, [, v]) => sum + v, 0);
}

// ─── Derived, honest per-log helpers ──────────────────────────────────────

const isSuccessLog = (log) => (log.status || "").toUpperCase() === "SUCCESS";
const isSkippedLog = (log) => (log.status || "").toUpperCase() === "FAILED" && (log.validation_result || "").toUpperCase() === "SKIPPED";
const isFailedLog = (log) => (log.status || "").toUpperCase() === "FAILED" && !isSkippedLog(log);

function describeResult(log) {
  if (isSuccessLog(log)) return "Executed successfully";
  if (isSkippedLog(log)) return "Not attempted — configuration error";
  if ((log.validation_result || "").toUpperCase() === "FAILED") return "Failed — payload validation rejected";
  if (isFailedLog(log)) return "Failed — runtime error after retries";
  return "Unknown";
}

function statusOf(log) {
  if (isSkippedLog(log)) return "SKIPPED";
  return (log.status || "UNKNOWN").toUpperCase();
}

// ─── Small building blocks ─────────────────────────────────────────────────

function FilterGroup({ options, value, onChange }) {
  return (
    <div style={{ display: "flex", gap: "0.5rem", flexWrap: "wrap" }}>
      {options.map((opt) => {
        const active = value === opt;
        return (
          <button key={opt} onClick={() => onChange(opt)} style={{
            fontSize: "0.7rem", letterSpacing: "0.06em", textTransform: "uppercase",
            background: active ? "var(--accent-dim)" : "none",
            border: `1px solid ${active ? "var(--accent-border)" : "var(--border)"}`,
            color: active ? "var(--accent)" : "var(--muted)",
            borderRadius: 6, padding: "0.3rem 0.75rem", cursor: "pointer", transition: "all 0.15s",
          }}>{opt}</button>
        );
      })}
    </div>
  );
}

function IncidentChip({ incidentId, incident }) {
  if (!incidentId) return <span style={{ color: "var(--muted)" }}>—</span>;
  return (
    <div style={{ display: "flex", alignItems: "center", gap: "0.4rem", minWidth: 0 }}>
      <span style={{ fontFamily: "var(--font-mono)", fontSize: "0.72rem", color: "var(--text)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
        title={incidentId}>{incidentId.slice(0, 8)}</span>
      {incident?.severity ? <SeverityBadge severity={incident.severity} /> : (
        <span style={{ fontSize: "0.62rem", color: "var(--muted)", fontStyle: "italic" }}>details unavailable</span>
      )}
    </div>
  );
}

// ─── Page ───────────────────────────────────────────────────────────────────

export default function Actions() {
  const [logs, setLogs] = useState([]);
  const [incidents, setIncidents] = useState([]);
  const [metrics, setMetrics] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [statusFilter, setStatusFilter] = useState("ALL");
  const [typeFilter, setTypeFilter] = useState("ALL");
  const [detailLog, setDetailLog] = useState(null);

  const load = useCallback(async () => {
    setLoading(true); setError(null);
    const results = await Promise.allSettled([
      fetchJSON("/api/v1/logs/agents"),
      fetchJSON("/api/v1/incidents/"),
      fetchMetrics(),
    ]);
    const [logsRes, incRes, metRes] = results;
    if (logsRes.status === "fulfilled") setLogs(Array.isArray(logsRes.value) ? logsRes.value : []);
    else setError(logsRes.reason?.message || "Failed to load action logs");
    if (incRes.status === "fulfilled") setIncidents(Array.isArray(incRes.value) ? incRes.value : []);
    if (metRes.status === "fulfilled") setMetrics(metRes.value);
    setLoading(false);
  }, []);

  useEffect(() => { load(); }, [load]);

  const incidentsById = useMemo(() => new Map(incidents.map((i) => [i.incident_id, i])), [incidents]);

  // ── Primary Overview + Metrics: derived from the SAME action_logs sample as
  // the table/timeline below (up to the 50 most recent rows), so every tile
  // on this page agrees with what's actually visible — no cross-source
  // mismatch between an Overview count and the list right underneath it. ──
  const actionSuccess = useMemo(() => logs.filter(isSuccessLog).length, [logs]);
  const actionFailure = useMemo(() => logs.filter(isFailedLog).length, [logs]);
  const skippedCount = useMemo(() => logs.filter(isSkippedLog).length, [logs]);
  const actionTotal = logs.length;
  const successRate = actionTotal > 0 ? (actionSuccess / actionTotal) * 100 : null;
  const failureRate = actionTotal > 0 ? (actionFailure / actionTotal) * 100 : null;

  const mostUsedAction = useMemo(() => {
    const counts = {};
    for (const l of logs) if (l.agent) counts[l.agent] = (counts[l.agent] || 0) + 1;
    let best = null;
    for (const [type, total] of Object.entries(counts)) {
      if (!best || total > best.total) best = { type, total };
    }
    return best;
  }, [logs]);

  const avgExecutionMs = useMemo(() => {
    const withDuration = logs.filter((l) => typeof l.execution_time_ms === "number");
    if (withDuration.length === 0) return null;
    return withDuration.reduce((sum, l) => sum + l.execution_time_ms, 0) / withDuration.length;
  }, [logs]);

  const retriedCount = useMemo(() => logs.filter((l) => (l.retry_count ?? 0) > 0).length, [logs]);
  const seenActionTypes = useMemo(
    () => [...new Set(logs.map((l) => l.agent).filter(Boolean))].sort(),
    [logs],
  );

  // ── Supplementary Prometheus counters — true all-executions-since-last-
  // restart totals (not capped at 50), shown separately and clearly labeled
  // so they're never mistaken for the (consistent, table-matching) figures
  // above. Same GET /metrics source + parsing approach as Agents.jsx's own
  // "Runtime Metrics" panel. ──
  const promSuccess = useMemo(() => sumMetricSeries(metrics, "action_success_total"), [metrics]);
  const promFailure = useMemo(() => sumMetricSeries(metrics, "action_failure_total"), [metrics]);

  const filteredLogs = useMemo(() => logs.filter((l) => {
    if (statusFilter === "SUCCESS" && !isSuccessLog(l)) return false;
    if (statusFilter === "FAILED" && !isFailedLog(l)) return false;
    if (statusFilter === "SKIPPED" && !isSkippedLog(l)) return false;
    if (typeFilter !== "ALL" && l.agent !== typeFilter) return false;
    return true;
  }), [logs, statusFilter, typeFilter]);

  const columns = [
    {
      key: "agent", label: "Action Type",
      render: (r) => (
        <div>
          <div style={{ fontWeight: 600 }}>{actionLabel(r.agent)}</div>
          <div style={{ fontSize: "0.66rem", color: "var(--muted)", fontFamily: "var(--font-mono)" }}>{r.agent}</div>
        </div>
      ),
    },
    { key: "incident_id", label: "Triggering Incident", render: (r) => <IncidentChip incidentId={r.incident_id} incident={incidentsById.get(r.incident_id)} /> },
    { key: "status", label: "Status", render: (r) => <Badge label={statusOf(r)} color={stateColor(statusOf(r))} dot /> },
    { key: "execution_time_ms", label: "Execution Time", align: "right", render: (r) => <span style={{ fontFamily: "var(--font-mono)" }}>{fmtMs(r.execution_time_ms)}</span> },
    { key: "retry_count", label: "Retries", align: "right", render: (r) => <span style={{ color: (r.retry_count ?? 0) > 0 ? "var(--warn)" : "var(--text)" }}>{r.retry_count ?? "—"}</span> },
    { key: "timestamp", label: "Executed", render: (r) => <span title={fmtTime(r.timestamp)}>{fmtRelative(r.timestamp)}</span> },
    { key: "view", label: "", render: (r) => <Button icon="search" onClick={() => setDetailLog(r)}>View</Button> },
  ];

  if (loading) {
    return (
      <PageContainer>
        <PageHeader title="Actions Center" subtitle="Everything AEAM did to the outside world — Slack, Jira, email, webhooks, diagnostics, monitoring" />
        <LoadingState label="Loading action history…" rows={6} />
      </PageContainer>
    );
  }

  if (error) {
    return (
      <PageContainer>
        <PageHeader title="Actions Center" subtitle="Everything AEAM did to the outside world"
          right={<Button icon="activity" onClick={load}>Retry</Button>} />
        <ErrorState message={error} onRetry={load} />
      </PageContainer>
    );
  }

  return (
    <PageContainer max={1400}>
      <PageHeader
        title="Actions Center"
        subtitle="The complete operational history of actions AEAM's Action Agent has executed — Slack, Jira, email, webhooks, diagnostics, monitoring"
        right={<Button icon="activity" onClick={load} disabled={loading}>{loading ? "Loading…" : "Refresh"}</Button>}
      />

      {logs.length === 0 ? (
        <EmptyState icon="zap" title="No actions executed yet"
          description="ActionAgent runs synchronously during incident finalization — trigger an event via /api/v1/trigger or run_simulation.py to populate this view." />
      ) : (
        <>
          {/* 1. Action Overview — of the last `logs.length` (<=50) executions shown below */}
          <div className="aeam-grid-auto" style={{ marginBottom: "1.4rem" }}>
            <MetricCard label="Total Actions" icon="zap" value={actionTotal} sub={`most recent ${actionTotal} executions`} />
            <MetricCard label="Successful" icon="check" value={actionSuccess} accent="var(--ok, var(--ok))" />
            <MetricCard label="Failed" icon="alert" value={actionFailure} accent={actionFailure > 0 ? "var(--err)" : undefined} />
            <MetricCard label="Skipped" icon="x" value={skippedCount} sub="config error (validation_result=SKIPPED)" accent="var(--warn)" />
            <MetricCard label="Pending" icon="clock" value="N/A" sub="actions execute synchronously — no pending queue exists" />
          </div>

          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "0.9rem", flexWrap: "wrap", gap: "0.9rem" }}>
            <div style={{ display: "flex", flexDirection: "column", gap: "0.4rem" }}>
              <span style={{ fontSize: "0.62rem", textTransform: "uppercase", letterSpacing: "0.12em", color: "var(--muted)" }}>Status</span>
              <FilterGroup options={["ALL", "SUCCESS", "FAILED", "SKIPPED"]} value={statusFilter} onChange={setStatusFilter} />
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: "0.4rem" }}>
              <span style={{ fontSize: "0.62rem", textTransform: "uppercase", letterSpacing: "0.12em", color: "var(--muted)" }}>Action Type</span>
              <FilterGroup options={["ALL", ...seenActionTypes]} value={typeFilter} onChange={setTypeFilter} />
            </div>
          </div>

          {/* 2. Recent Executions */}
          <div style={{ marginBottom: "1.4rem" }}>
            <Panel title="Recent Executions" icon="zap" pad={false}
              right={<span style={{ fontSize: "0.62rem", color: "var(--muted)", fontFamily: "var(--font-mono)" }}>
                {filteredLogs.length} of last {logs.length} shown — reads action_logs
              </span>}>
              <DataTable columns={columns} rows={filteredLogs} rowKey={(r, i) => `${r.incident_id}-${r.timestamp}-${i}`}
                empty="No actions match this filter." />
            </Panel>
          </div>

          {/* 4. Action Timeline */}
          <div style={{ marginBottom: "1.4rem" }}>
            <Panel title="Action Timeline" icon="clock"
              right={<span style={{ fontSize: "0.62rem", color: "var(--muted)", fontFamily: "var(--font-mono)" }}>
                most recent {Math.min(15, filteredLogs.length)} of {filteredLogs.length} filtered
              </span>}>
              {filteredLogs.length === 0 ? (
                <EmptyState icon="clock" title="No actions match this filter" tone="muted" />
              ) : (
                <TimelineContainer>
                  {filteredLogs.slice(0, 15).map((log, i) => (
                    <TimelineItem key={i} color={stateColor(statusOf(log))} title={`${actionLabel(log.agent)} — ${statusOf(log)}`} time={fmtRelative(log.timestamp)}>
                      <div style={{ display: "flex", flexDirection: "column", gap: "0.3rem" }}>
                        <IncidentChip incidentId={log.incident_id} incident={incidentsById.get(log.incident_id)} />
                        <span style={{ fontSize: "0.72rem", color: "var(--muted)" }}>{describeResult(log)} · {fmtMs(log.execution_time_ms)}</span>
                        {log.failure_reason && <span style={{ fontSize: "0.72rem", color: "#ff8f88" }}>{log.failure_reason}</span>}
                      </div>
                    </TimelineItem>
                  ))}
                </TimelineContainer>
              )}
            </Panel>
          </div>

          {/* 6. Action Metrics + 7. Retry information */}
          <div className="aeam-grid-2" style={{ marginBottom: "1.4rem" }}>
            <Panel title="Action Metrics" icon="target"
              right={<span style={{ fontSize: "0.62rem", color: "var(--muted)", fontFamily: "var(--font-mono)" }}>of last {logs.length} shown</span>}>
              <div className="aeam-grid-auto">
                <Field label="Success Rate" value={successRate != null ? `${successRate.toFixed(1)}%` : "—"} mono />
                <Field label="Failure Rate" value={failureRate != null ? `${failureRate.toFixed(1)}%` : "—"} mono color={failureRate > 0 ? "var(--err)" : undefined} />
                <Field label="Most Used Action" value={mostUsedAction ? `${actionLabel(mostUsedAction.type)} (${mostUsedAction.total})` : "—"} />
                <Field label="Average Execution Time" value={avgExecutionMs != null ? fmtMs(avgExecutionMs) : "—"} mono />
              </div>
            </Panel>

            <Panel title="Retry Information" icon="activity"
              right={<span style={{ fontSize: "0.62rem", color: "var(--muted)", fontFamily: "var(--font-mono)" }}>of last {logs.length} shown</span>}>
              <div className="aeam-grid-auto">
                <Field label="Actions Retried" value={retriedCount} mono color={retriedCount > 0 ? "var(--warn)" : undefined} />
                <Field label="Never Retried" value={logs.length - retriedCount} mono />
              </div>
              <div style={{ fontSize: "0.66rem", color: "var(--muted)", fontStyle: "italic", marginTop: "0.6rem" }}>
                Retry count is per-execution (exponential backoff, max 3 attempts) — see each entry's detail for its exact count.
              </div>
            </Panel>
          </div>

          {/* Supplementary Prometheus counters — true all-time-since-restart
              totals, kept visually separate from the log-sample figures above
              (same framing as the Agent Observatory's "Runtime Metrics" panel). */}
          <div style={{ marginBottom: "1.4rem" }}>
            <Panel title="Runtime Metrics (Prometheus)" icon="activity"
              right={<span style={{ fontSize: "0.62rem", color: "var(--muted)", fontFamily: "var(--font-mono)" }}>GET /metrics</span>}>
              {metrics === null ? (
                <EmptyState icon="activity" title="Metrics unavailable" tone="muted" />
              ) : (
                <>
                  <div>
                    {[
                      ["Actions succeeded", promSuccess],
                      ["Actions failed", promFailure],
                    ].map(([label, v]) => (
                      <div key={label} style={{ display: "flex", justifyContent: "space-between", padding: "0.45rem 0", borderBottom: "1px solid var(--border)" }}>
                        <span style={{ fontSize: "0.76rem", color: "var(--muted)" }}>{label}</span>
                        <span style={{ fontSize: "0.78rem", fontFamily: "var(--font-mono)", color: "var(--text)", fontWeight: 600 }}>{v}</span>
                      </div>
                    ))}
                  </div>
                  <div style={{ fontSize: "0.66rem", color: "var(--muted)", fontStyle: "italic", marginTop: "0.6rem" }}>
                    Counted since the last backend restart, across every execution — not capped at 50, and not necessarily equal
                    to the log-sample figures above (this counter resets on restart; the log table does not).
                  </div>
                </>
              )}
            </Panel>
          </div>
        </>
      )}

      {/* 3. Per Action Details */}
      {detailLog && (
        <Modal title="Action Execution Detail" icon="zap" onClose={() => setDetailLog(null)}>
          <AgentLogCard log={detailLog} />
          <div style={{ marginTop: "0.9rem", fontSize: "0.78rem", color: "var(--muted)" }}>
            <strong style={{ color: "var(--text)" }}>Result:</strong> {describeResult(detailLog)}
          </div>
        </Modal>
      )}
    </PageContainer>
  );
}
