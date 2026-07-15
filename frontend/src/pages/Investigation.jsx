import { useState, useEffect, useCallback, useMemo } from "react";
import { useSearchParams } from "react-router-dom";
import {
  PageHeader, Card, Badge, SeverityBadge, ConfidenceBar, Field, Button, Icon,
  fmtTime, fmtRelative, severityOf, deriveStatus,
  getAuditSummary, getRetrievedCount, getRecommendedActions, getActionOutcome,
  actionLabel, getValidationStatus, getEvidence, parseMaybeJSON,
} from "../components/ui";
import {
  PageContainer, SplitLayout, Panel, EmptyState, LoadingState, ErrorState,
  TimelineContainer, MetricCard,
} from "../components/library";
import { SearchBox } from "./KnowledgeCenter";
import Timeline from "../components/Timeline";
import EvidencePanel from "../components/EvidencePanel";
import MemoryPanel from "../components/MemoryPanel";
import PolicyMatchPanel from "../components/PolicyMatchPanel";
import CrossDatasetPanel from "../components/CrossDatasetPanel";

/* ──────────────────────────────────────────────────────────────────────────
 * pages/Investigation.jsx  (Investigation Workspace)
 *
 * The unified "why" view — composes ONLY existing pieces:
 *   - GET /api/v1/incidents/ (existing, unmodified, SELECT * — already
 *     returns every field this page reads; no detail endpoint added)
 *   - Timeline (components/Timeline.jsx — extended additively this phase
 *     with finer detection/reasoning stages, all existing stages untouched)
 *   - EvidencePanel (components/EvidencePanel.jsx — reused verbatim)
 *   - MemoryPanel (components/MemoryPanel.jsx, Phase C1) — Enterprise
 *     Memory's similar-resolved-incidents recall, kept as its own panel
 *     deliberately separate from Evidence: knowledge documents vs. past
 *     incidents must never be merged into one indistinguishable list.
 *   - PolicyMatchPanel (components/PolicyMatchPanel.jsx, Phase C3) —
 *     "Matched Enterprise Policies", the Policy Registry's advisory
 *     findings for this investigation. A THIRD, structurally distinct
 *     evidence source (type: "policy") — never merged with RAG or Memory,
 *     and never capable of overriding a deterministic RuleEngine decision.
 *   - CrossDatasetPanel (components/CrossDatasetPanel.jsx, Phase C4) —
 *     "Cross-Dataset Analysis", correlated signals from OTHER activated
 *     datasets (type: "cross_dataset"). A FOURTH, structurally distinct
 *     evidence source — advisory only, never a second MonitorAgent/
 *     RuleEngine/ForecastAgent.
 *   - ui.jsx's existing incident-shape helpers (getAuditSummary,
 *     getRecommendedActions, getActionOutcome, getValidationStatus,
 *     getMemoryData/getMemoryMatches, getPolicyMatchData/getPolicyMatches,
 *     getCrossDatasetData, ...)
 *
 * No new backend endpoint. No orchestration logic duplicated — every
 * derived value here is read directly from fields Orchestrator.
 * finalize_incident() already persists.
 * ────────────────────────────────────────────────────────────────────────── */

const fetchIncidents = async () => {
  const res = await fetch("/api/v1/incidents/");
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
};

// ─── Metric comparison (honest — real numbers, no fabricated trend line) ───

function MetricComparisonChart({ current, expected }) {
  if (current == null || expected == null) {
    return <EmptyState icon="target" title="No metric values recorded" tone="muted" />;
  }
  const max = Math.max(Math.abs(current), Math.abs(expected), 1) * 1.15;
  const curPct = Math.min(100, (Math.abs(current) / max) * 100);
  const expPct = Math.min(100, (Math.abs(expected) / max) * 100);
  const deviationPct = expected !== 0 ? ((current - expected) / Math.abs(expected)) * 100 : null;
  const rows = [
    { label: "Current", value: current, pct: curPct, color: "var(--err)" },
    { label: "Expected (baseline)", value: expected, pct: expPct, color: "var(--info)" },
  ];
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "0.9rem" }}>
      {rows.map((r) => (
        <div key={r.label}>
          <div style={{ display: "flex", justifyContent: "space-between", fontSize: "0.72rem", color: "var(--muted)", marginBottom: "0.3rem" }}>
            <span>{r.label}</span>
            <span style={{ fontFamily: "var(--font-mono)", color: "var(--text)" }}>{r.value.toLocaleString(undefined, { maximumFractionDigits: 2 })}</span>
          </div>
          <div style={{ height: 10, background: "var(--border)", borderRadius: 5, overflow: "hidden" }}>
            <div style={{ width: `${r.pct}%`, height: "100%", background: r.color, borderRadius: 5, transition: "width 0.4s ease" }} />
          </div>
        </div>
      ))}
      {deviationPct != null && (
        <div style={{ fontSize: "0.72rem", color: "var(--muted)" }}>
          Deviation: <span style={{ color: deviationPct < 0 ? "var(--err)" : "var(--ok)", fontFamily: "var(--font-mono)" }}>
            {deviationPct > 0 ? "+" : ""}{deviationPct.toFixed(1)}%
          </span> from baseline
        </div>
      )}
      <div style={{ fontSize: "0.66rem", color: "var(--muted)", fontStyle: "italic" }}>
        Forecast-vs-actual trend requires per-incident forecast history, not currently persisted —
        showing the real detection-time baseline comparison instead.
      </div>
    </div>
  );
}

// ─── Investigation Summary ───────────────────────────────────────────────────

function InvestigationSummary({ incident }) {
  const status = deriveStatus(incident);
  const audit = getAuditSummary(incident);
  const sev = severityOf(incident.severity);
  const deviationPct = incident.expected_value && incident.expected_value !== 0
    ? Math.abs(((incident.current_value - incident.expected_value) / incident.expected_value) * 100)
    : null;

  return (
    <Card accent={sev.color} style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: "0.6rem" }}>
        <div style={{ display: "flex", alignItems: "center", gap: "0.7rem" }}>
          <SeverityBadge severity={incident.severity} />
          <span style={{ fontFamily: "var(--font-mono)", fontSize: "0.9rem", fontWeight: 700, color: "var(--text)" }}>
            {incident.event_type || "—"}
          </span>
        </div>
        <Badge label={status.label} color={status.color} dot />
      </div>

      <div className="aeam-grid-auto">
        <Field label="Root Cause" value={incident.root_cause || "Pending"} />
        <div style={{ display: "flex", flexDirection: "column", gap: "0.3rem" }}>
          <span style={{ fontSize: "0.62rem", textTransform: "uppercase", letterSpacing: "0.12em", color: "var(--muted)" }}>Confidence</span>
          <ConfidenceBar value={incident.confidence ?? audit?.top_confidence} />
        </div>
        <Field label="Affected Metric" value={incident.metric || "—"} mono />
        <Field label="Business Impact"
          value={deviationPct != null
            ? `${deviationPct.toFixed(1)}% deviation from baseline (${incident.severity || "unknown"} severity)`
            : "Not quantifiable from persisted data"} />
      </div>
    </Card>
  );
}

// ─── Reasoning Panel ──────────────────────────────────────────────────────────

function ReasoningPanel({ incident }) {
  const evidence = getEvidence(incident);
  const recommended = getRecommendedActions(incident);
  const validation = getValidationStatus(incident);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
      <div>
        <div style={{ fontSize: "0.64rem", textTransform: "uppercase", letterSpacing: "0.13em", color: "var(--muted)", fontWeight: 700, marginBottom: "0.5rem" }}>
          LLM Explanation
        </div>
        {incident.root_cause ? (
          <p style={{ fontSize: "0.82rem", color: "var(--text)", lineHeight: 1.6, margin: 0 }}>{incident.root_cause}</p>
        ) : (
          <div style={{ fontSize: "0.78rem", color: "var(--muted)" }}>
            {validation.status === "SKIPPED" ? "RAG was not invoked for this investigation." : "No reasoning was produced."}
          </div>
        )}
      </div>

      {evidence.length > 0 && (
        <div>
          <div style={{ fontSize: "0.64rem", textTransform: "uppercase", letterSpacing: "0.13em", color: "var(--muted)", fontWeight: 700, marginBottom: "0.5rem" }}>
            Structured Reasoning ({evidence.length} contributing cause{evidence.length !== 1 ? "s" : ""})
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: "0.5rem" }}>
            {evidence.map((c, i) => (
              <div key={`${c.chunk_id || "evidence"}-${i}`} style={{ border: "1px solid var(--border)", borderRadius: 8, padding: "0.6rem 0.8rem", fontSize: "0.76rem" }}>
                <div style={{ color: "var(--text)" }}>{c.cause || c.reason || "—"}</div>
                {c.confidence != null && (
                  <div style={{ color: "var(--muted)", marginTop: "0.2rem", fontSize: "0.68rem" }}>confidence {Math.round((c.confidence <= 1 ? c.confidence * 100 : c.confidence))}%</div>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      <div>
        <div style={{ fontSize: "0.64rem", textTransform: "uppercase", letterSpacing: "0.13em", color: "var(--muted)", fontWeight: 700, marginBottom: "0.5rem" }}>
          Recommendations
        </div>
        {recommended.length > 0 ? (
          <ul style={{ margin: 0, paddingLeft: "1.1rem", display: "flex", flexDirection: "column", gap: "0.35rem" }}>
            {recommended.map((r, i) => <li key={i} style={{ fontSize: "0.78rem", color: "var(--text)" }}>{r}</li>)}
          </ul>
        ) : <div style={{ fontSize: "0.78rem", color: "var(--muted)" }}>No recommendations generated.</div>}
      </div>
    </div>
  );
}

// ─── Actions Panel ────────────────────────────────────────────────────────────

function ActionsPanel({ incident }) {
  const outcome = getActionOutcome(incident);
  const audit = getAuditSummary(incident);
  const recommended = audit?.recommended_actions || [];
  const pending = recommended.filter(
    (r) => !outcome.executed.some((e) => actionLabel(e).toLowerCase().includes(r.toLowerCase().split(" ")[0]))
  );

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
      <div>
        <div style={{ fontSize: "0.64rem", textTransform: "uppercase", letterSpacing: "0.13em", color: "var(--ok)", fontWeight: 700, marginBottom: "0.5rem" }}>
          Executed ({outcome.executed.length})
        </div>
        {outcome.executed.length > 0 ? (
          <div style={{ display: "flex", flexWrap: "wrap", gap: "0.4rem" }}>
            {outcome.executed.map((a) => <Badge key={a} label={actionLabel(a)} color="var(--ok)" dot />)}
          </div>
        ) : <div style={{ fontSize: "0.76rem", color: "var(--muted)" }}>No actions executed yet.</div>}
      </div>

      <div>
        <div style={{ fontSize: "0.64rem", textTransform: "uppercase", letterSpacing: "0.13em", color: "var(--warn)", fontWeight: 700, marginBottom: "0.5rem" }}>
          Skipped ({outcome.skipped.length})
        </div>
        {outcome.skipped.length > 0 ? (
          <div style={{ display: "flex", flexDirection: "column", gap: "0.4rem" }}>
            {outcome.skipped.map((s, i) => (
              <div key={i} style={{ fontSize: "0.74rem", color: "var(--muted)" }}>
                <span style={{ color: "var(--text)" }}>{actionLabel(s.action)}</span> — {s.reason}
              </div>
            ))}
          </div>
        ) : <div style={{ fontSize: "0.76rem", color: "var(--muted)" }}>Nothing skipped.</div>}
      </div>

      {pending.length > 0 && (
        <div>
          <div style={{ fontSize: "0.64rem", textTransform: "uppercase", letterSpacing: "0.13em", color: "var(--info)", fontWeight: 700, marginBottom: "0.5rem" }}>
            Pending ({pending.length})
          </div>
          <div style={{ display: "flex", flexWrap: "wrap", gap: "0.4rem" }}>
            {pending.map((r, i) => <Badge key={i} label={r} color="var(--info)" />)}
          </div>
        </div>
      )}

      <div>
        <div style={{ fontSize: "0.64rem", textTransform: "uppercase", letterSpacing: "0.13em", color: "var(--muted)", fontWeight: 700, marginBottom: "0.5rem" }}>
          Human Approval
        </div>
        <Badge
          label={incident.requires_human ? "Escalated — awaiting human review" : "Not required"}
          color={incident.requires_human ? "var(--warn)" : "var(--muted)"}
          dot
        />
      </div>
    </div>
  );
}

// ─── Incident picker (left rail) ─────────────────────────────────────────────

// Exported (additive — no existing behaviour changed) so pages/Replay.jsx can
// reuse the SAME incident-selector UI instead of duplicating it.
export function IncidentPicker({ incidents, selectedId, onSelect, search, onSearch }) {
  const needle = search.trim().toLowerCase();
  const filtered = useMemo(
    () => (needle
      ? incidents.filter((i) => (i.event_type || "").toLowerCase().includes(needle) || (i.metric || "").toLowerCase().includes(needle))
      : incidents),
    [incidents, needle],
  );

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "0.8rem" }}>
      <SearchBox value={search} onChange={onSearch} placeholder="Search incidents…" />
      <div style={{ display: "flex", flexDirection: "column", gap: "0.4rem", maxHeight: "70vh", overflowY: "auto" }}>
        {filtered.length === 0 && (
          <div style={{ fontSize: "0.76rem", color: "var(--muted)", padding: "1rem 0", textAlign: "center" }}>No incidents found.</div>
        )}
        {filtered.map((inc) => {
          const active = inc.incident_id === selectedId;
          const sev = severityOf(inc.severity);
          return (
            <button key={inc.incident_id} onClick={() => onSelect(inc.incident_id)} style={{
              textAlign: "left", background: active ? "var(--accent-dim)" : "var(--surface)",
              borderTop: `1px solid ${active ? "rgba(0,255,163,0.4)" : "var(--border)"}`,
              borderRight: `1px solid ${active ? "rgba(0,255,163,0.4)" : "var(--border)"}`,
              borderBottom: `1px solid ${active ? "rgba(0,255,163,0.4)" : "var(--border)"}`,
              borderLeft: `3px solid ${sev.color}`, borderRadius: 8, padding: "0.6rem 0.75rem", cursor: "pointer",
            }}>
              <div style={{ display: "flex", justifyContent: "space-between", gap: "0.5rem" }}>
                <span style={{ fontSize: "0.76rem", fontWeight: 600, color: "var(--text)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {inc.event_type || "—"}
                </span>
                <Badge label={inc.severity || "—"} color={sev.color} />
              </div>
              <div style={{ fontSize: "0.68rem", color: "var(--muted)", marginTop: "0.2rem" }}>
                {inc.metric || "—"} · {fmtRelative(inc.timestamp)}
              </div>
            </button>
          );
        })}
      </div>
    </div>
  );
}

// ─── Page ───────────────────────────────────────────────────────────────────

export default function Investigation() {
  const [incidents, setIncidents] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [search, setSearch] = useState("");
  const [searchParams, setSearchParams] = useSearchParams();

  const load = useCallback(async () => {
    setLoading(true); setError(null);
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

  const selectedId = searchParams.get("id");
  const selected = useMemo(() => {
    if (!incidents.length) return null;
    const byId = selectedId && incidents.find((i) => i.incident_id === selectedId);
    return byId || [...incidents].sort((a, b) => severityOf(b.severity).rank - severityOf(a.severity).rank)[0];
  }, [incidents, selectedId]);

  const handleSelect = (id) => setSearchParams({ id });

  const status = selected ? deriveStatus(selected) : null;

  return (
    <PageContainer max={1500}>
      <PageHeader
        title="Investigation Workspace"
        subtitle="The unified “why” view — detection → decision → evidence → action as one causal chain"
        right={<Button icon="activity" onClick={load} disabled={loading}>{loading ? "Loading…" : "Refresh"}</Button>}
      />

      {loading && <LoadingState label="Loading incidents…" rows={4} />}
      {error && <ErrorState message={error} onRetry={load} />}

      {!loading && !error && incidents.length === 0 && (
        <EmptyState icon="branch" title="No incidents yet"
          description="Trigger an anomaly (POST /api/v1/trigger or run_simulation.py) to see an investigation here." />
      )}

      {!loading && !error && incidents.length > 0 && (
        <SplitLayout
          ratio="280px 1fr"
          left={
            <Panel title="Incidents" icon="branch" pad={false} style={{ padding: "1rem" }}>
              <IncidentPicker incidents={incidents} selectedId={selected?.incident_id} onSelect={handleSelect} search={search} onSearch={setSearch} />
            </Panel>
          }
          right={
            selected ? (
              <div style={{ display: "flex", flexDirection: "column", gap: "1.2rem" }}>
                <InvestigationSummary incident={selected} />

                <div className="aeam-grid-metrics">
                  <MetricCard label="Current Stage" value={status.label} icon="branch" />
                  <MetricCard label="Status" value={status.label} icon="activity" accent={status.color} />
                  <MetricCard label="Investigation Depth" value={selected.investigation_depth ?? "—"} icon="layers" />
                  <MetricCard label="Processing Duration" value="not tracked / incident" icon="clock"
                    sub="see investigation_duration (Prometheus)" />
                </div>

                <SplitLayout
                  ratio="1.5fr 1fr"
                  left={
                    <div style={{ display: "flex", flexDirection: "column", gap: "1.2rem" }}>
                      <Panel title="Causal Chain" icon="branch">
                        <TimelineContainer>
                          <Timeline incident={selected} />
                        </TimelineContainer>
                      </Panel>
                      <Panel title="Metric Trend — Current vs Expected" icon="target">
                        <MetricComparisonChart current={selected.current_value} expected={selected.expected_value} />
                      </Panel>
                      <Panel title="Incident Metadata" icon="code">
                        <div className="aeam-grid-auto">
                          <Field label="Incident ID" value={selected.incident_id} mono title={selected.incident_id} />
                          <Field label="Event ID" value={selected.event_id} mono title={selected.event_id} />
                          <Field label="Created" value={fmtTime(selected.timestamp)} title={selected.timestamp} />
                          <Field label="Detection Methods" value={
                            (() => {
                              const dm = parseMaybeJSON(selected.detection_methods);
                              const list = Array.isArray(dm) ? dm : (Array.isArray(selected.detection_methods) ? selected.detection_methods : []);
                              return list.join(", ") || "—";
                            })()
                          } />
                        </div>
                      </Panel>
                    </div>
                  }
                  right={
                    <div style={{ display: "flex", flexDirection: "column", gap: "1.2rem" }}>
                      <Panel title="Evidence" icon="database"><EvidencePanel incident={selected} /></Panel>
                      <Panel title="Enterprise Memory" icon="layers"><MemoryPanel incident={selected} /></Panel>
                      <Panel title="Matched Enterprise Policies" icon="shield"><PolicyMatchPanel incident={selected} /></Panel>
                      <Panel title="Cross-Dataset Analysis" icon="branch"><CrossDatasetPanel incident={selected} /></Panel>
                      <Panel title="Reasoning" icon="code"><ReasoningPanel incident={selected} /></Panel>
                      <Panel title="Actions" icon="zap"><ActionsPanel incident={selected} /></Panel>
                    </div>
                  }
                />
              </div>
            ) : (
              <EmptyState icon="branch" title="Select an incident" description="Choose an incident from the list to investigate." />
            )
          }
        />
      )}
    </PageContainer>
  );
}
