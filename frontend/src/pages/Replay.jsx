import { useState, useEffect, useCallback, useMemo, useRef } from "react";
import { useSearchParams } from "react-router-dom";
import {
  PageHeader, Card, Badge, SeverityBadge, ConfidenceBar, Field, Button, Icon,
  fmtTime, fmtRelative, severityOf, deriveStatus,
  getAuditSummary, getRecommendedActions, getEvidence, getRetrievedCount, getActionOutcome, actionLabel,
} from "../components/ui";
import { PageContainer, SplitLayout, Panel, EmptyState, LoadingState, ErrorState } from "../components/library";
import { buildStages } from "../components/Timeline";
import { IncidentPicker } from "./Investigation";
import EvidencePanel from "../components/EvidencePanel";

/* ──────────────────────────────────────────────────────────────────────────
 * pages/Replay.jsx  (Replay Workspace)
 *
 * A step-through PLAYBACK of an already-completed incident's stored
 * lifecycle — Event Received -> ... -> Final Resolution -> Replay Summary —
 * built entirely by REUSING, never duplicating:
 *   - buildStages() (components/Timeline.jsx, exported this phase) — the
 *     SAME stage-derivation rules Investigation Workspace and the Incidents
 *     "View Timeline" modal already use, so replay narrates from identical
 *     logic, not a second implementation of "what happened."
 *   - IncidentPicker (pages/Investigation.jsx, exported this phase) — the
 *     same incident-selector UI, not rebuilt.
 *   - EvidencePanel (components/EvidencePanel.jsx) — reused verbatim for the
 *     Evidence Selection step.
 *   - ui.jsx's incident-shape helpers (deriveStatus, getAuditSummary,
 *     getRecommendedActions, getEvidence, getRetrievedCount,
 *     getActionOutcome, actionLabel) — the same "Investigation helpers"
 *     every other incident-facing page in this app already uses.
 *
 * GET /api/v1/incidents/ (existing, unmodified) is the only data source —
 * no new backend endpoint. Play/Pause/Next/Previous control WHEN each
 * already-real stage is revealed; nothing about the underlying data is
 * invented, simulated, or re-executed against live agents (that would be
 * the old "shadow-mode re-run" concept this page's placeholder previously
 * described — a different, out-of-scope feature; see the Architecture Gate).
 * ────────────────────────────────────────────────────────────────────────── */

const fetchIncidents = async () => {
  const res = await fetch("/api/v1/incidents/");
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
};

const AUTOPLAY_INTERVAL_MS = 1800;

// ─── Generic stage detail (reuses the exact Timeline stage shape) ──────────

function StageDetail({ stage }) {
  if (!stage) return <Unknown />;
  const stateColorMap = { done: "var(--ok)", failed: "var(--err)", skipped: "var(--warn)", pending: "var(--warn)", idle: "var(--muted)" };
  const color = stateColorMap[stage.state] || "var(--muted)";
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "0.6rem" }}>
      <Badge label={stage.state.toUpperCase()} color={color} dot />
      <p style={{ fontSize: "0.88rem", color: "var(--text)", margin: 0, lineHeight: 1.6 }}>{stage.detail}</p>
    </div>
  );
}

function Unknown() {
  return <span style={{ color: "var(--muted)", fontStyle: "italic", fontSize: "0.82rem" }}>Not available for this incident.</span>;
}

// ─── Replay step list (mission's canonical order — derived, never duplicated) ─

function buildReplaySteps(incident) {
  const stages = buildStages(incident);
  const byKey = Object.fromEntries(stages.map((s) => [s.key, s]));
  const outcome = getActionOutcome(incident);
  const recommended = getRecommendedActions(incident);
  const status = deriveStatus(incident);
  const audit = getAuditSummary(incident);
  const retrieved = getRetrievedCount(incident);
  const evidence = getEvidence(incident);

  return [
    {
      id: "event_received", label: "Event Received", icon: "bolt",
      render: () => (
        <div className="aeam-grid-auto">
          <Field label="Event Type" value={incident.event_type || "—"} mono />
          <Field label="Received" value={fmtTime(incident.timestamp)} title={fmtRelative(incident.timestamp)} />
          <Field label="Severity" value={<SeverityBadge severity={incident.severity} />} />
        </div>
      ),
    },
    {
      id: "dataset_trigger", label: "Dataset / Trigger", icon: "database",
      render: () => (
        <div className="aeam-grid-auto">
          <Field label="Metric" value={incident.metric || "—"} mono />
          <Field label="Current Value" value={incident.current_value ?? "—"} mono />
          <Field label="Expected Value" value={incident.expected_value ?? "—"} mono />
        </div>
      ),
    },
    { id: "rule_evaluation", label: "Rule Evaluation", icon: "shield", render: () => <StageDetail stage={byKey.rule_evaluation} /> },
    { id: "statistical_detection", label: "Statistical Detection", icon: "activity", render: () => <StageDetail stage={byKey.statistical_analysis} /> },
    { id: "forecast_evaluation", label: "Forecast Evaluation", icon: "target", render: () => <StageDetail stage={byKey.forecast_analysis} /> },
    { id: "rag_retrieval", label: "RAG Retrieval", icon: "database", render: () => <StageDetail stage={byKey.rag} /> },
    {
      id: "evidence_selection", label: "Evidence Selection", icon: "database",
      render: () => (
        <div style={{ display: "flex", flexDirection: "column", gap: "0.8rem" }}>
          <StageDetail stage={byKey.retrieved_evidence} />
          {retrieved > 0 && <EvidencePanel incident={incident} />}
        </div>
      ),
    },
    { id: "llm_reasoning", label: "LLM Reasoning", icon: "code", render: () => <StageDetail stage={byKey.llm_reasoning} /> },
    { id: "validation", label: "Validation", icon: "shield", render: () => <StageDetail stage={byKey.validation} /> },
    { id: "human_review", label: "Human Review", icon: "shield", render: () => <StageDetail stage={byKey.human_review} /> },
    {
      id: "actions_executed", label: "Actions Executed", icon: "zap",
      render: () => (
        <div style={{ display: "flex", flexDirection: "column", gap: "0.8rem" }}>
          <StageDetail stage={byKey.action} />
          <div className="aeam-grid-auto">
            {["jira", "slack", "email"].map((k) => (
              <Field key={k} label={k[0].toUpperCase() + k.slice(1)}
                value={<Badge label={byKey[k]?.state?.toUpperCase() || "—"} color={
                  byKey[k]?.state === "done" ? "var(--ok)" : byKey[k]?.state === "failed" ? "var(--err)" : "var(--muted)"
                } dot />}
                title={byKey[k]?.detail} />
            ))}
          </div>
          <div>
            <span style={{ fontSize: "0.62rem", textTransform: "uppercase", letterSpacing: "0.12em", color: "var(--muted)" }}>Recommended</span>
            <p style={{ fontSize: "0.82rem", color: "var(--text)", margin: "0.3rem 0 0" }}>{recommended.join("; ") || "—"}</p>
          </div>
        </div>
      ),
    },
    {
      id: "final_resolution", label: "Final Resolution", icon: "check",
      render: () => (
        <div className="aeam-grid-auto">
          <Badge label={status.label} color={status.color} dot />
          <Field label="Root Cause" value={incident.root_cause || "Pending"} />
          <div style={{ display: "flex", flexDirection: "column", gap: "0.3rem" }}>
            <span style={{ fontSize: "0.62rem", textTransform: "uppercase", letterSpacing: "0.12em", color: "var(--muted)" }}>Confidence</span>
            <ConfidenceBar value={incident.confidence ?? audit?.top_confidence} />
          </div>
          <Field label="Escalated" value={incident.requires_human ? "Yes" : "No"} />
        </div>
      ),
    },
    {
      id: "replay_summary", label: "Replay Summary", icon: "layers",
      render: () => {
        const totalStages = stages.length;
        const firedStages = stages.filter((s) => s.state === "done").length;
        return (
          <div style={{ display: "flex", flexDirection: "column", gap: "0.8rem" }}>
            <p style={{ fontSize: "0.9rem", color: "var(--text)", lineHeight: 1.7, margin: 0 }}>
              This <strong>{incident.event_type || "incident"}</strong> ({incident.metric || "—"}) was received {fmtRelative(incident.timestamp)} and reached
              a final status of <strong style={{ color: status.color }}>{status.label}</strong> after {incident.investigation_depth ?? "—"} investigation pass(es),
              with {evidence.length} evidence chunk{evidence.length !== 1 ? "s" : ""} retrieved and {outcome.executed.length} action{outcome.executed.length !== 1 ? "s" : ""} executed.
            </p>
            <div className="aeam-grid-auto">
              <Field label="Stages Completed" value={`${firedStages} / ${totalStages}`} mono />
              <Field label="Actions Executed" value={outcome.executed.map(actionLabel).join(", ") || "None"} />
              <Field label="Actions Skipped" value={outcome.skipped.length} mono />
              <Field label="Business Impact" value={
                incident.expected_value && incident.expected_value !== 0
                  ? `${Math.abs(((incident.current_value - incident.expected_value) / incident.expected_value) * 100).toFixed(1)}% deviation`
                  : "Not quantifiable"
              } />
            </div>
          </div>
        );
      },
    },
  ];
}

// ─── Playback controls ──────────────────────────────────────────────────────

function PlaybackControls({ index, total, playing, onPlay, onPause, onNext, onPrev, onReset, onJump }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "0.8rem" }}>
      <div style={{ display: "flex", alignItems: "center", gap: "0.5rem", flexWrap: "wrap" }}>
        <Button icon={playing ? "x" : "play"} variant="primary" onClick={playing ? onPause : onPlay}>
          {playing ? "Pause" : "Play"}
        </Button>
        <Button icon="chevron" onClick={onPrev} disabled={index === 0}>Previous</Button>
        <Button icon="chevron" onClick={onNext} disabled={index === total - 1}>Next</Button>
        <Button icon="activity" onClick={onReset}>Reset</Button>
        <span style={{ fontSize: "0.72rem", color: "var(--muted)", fontFamily: "var(--font-mono)", marginLeft: "auto" }}>
          Stage {index + 1} of {total}
        </span>
      </div>
      <div style={{ display: "flex", gap: "3px" }}>
        {Array.from({ length: total }).map((_, i) => (
          <button key={i} onClick={() => onJump(i)} title={`Jump to stage ${i + 1}`} style={{
            flex: 1, height: 6, border: "none", borderRadius: 3, cursor: "pointer", padding: 0,
            background: i <= index ? "var(--accent)" : "var(--border)", opacity: i <= index ? 1 : 0.6,
          }} />
        ))}
      </div>
    </div>
  );
}

// ─── Page ───────────────────────────────────────────────────────────────────

export default function Replay() {
  const [incidents, setIncidents] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [search, setSearch] = useState("");
  const [searchParams, setSearchParams] = useSearchParams();
  const [stepIndex, setStepIndex] = useState(0);
  const [playing, setPlaying] = useState(false);
  const timerRef = useRef(null);

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
    return byId || [...incidents].sort((a, b) => new Date(b.timestamp) - new Date(a.timestamp))[0];
  }, [incidents, selectedId]);

  const handleSelect = (id) => { setSearchParams({ id }); setStepIndex(0); setPlaying(false); };

  const steps = useMemo(() => (selected ? buildReplaySteps(selected) : []), [selected]);

  // Autoplay
  useEffect(() => {
    if (!playing || !steps.length) return;
    timerRef.current = setInterval(() => {
      setStepIndex((i) => {
        if (i >= steps.length - 1) { setPlaying(false); return i; }
        return i + 1;
      });
    }, AUTOPLAY_INTERVAL_MS);
    return () => clearInterval(timerRef.current);
  }, [playing, steps.length]);

  const jump = (i) => setStepIndex(Math.max(0, Math.min(steps.length - 1, i)));

  if (loading) {
    return (
      <PageContainer>
        <PageHeader title="Replay Workspace" subtitle="Step through a completed incident's lifecycle — detection to resolution" />
        <LoadingState label="Loading incidents…" rows={5} />
      </PageContainer>
    );
  }

  if (error) {
    return (
      <PageContainer>
        <PageHeader title="Replay Workspace" subtitle="Step through a completed incident's lifecycle — detection to resolution"
          right={<Button icon="activity" onClick={load}>Retry</Button>} />
        <ErrorState message={error} onRetry={load} />
      </PageContainer>
    );
  }

  return (
    <PageContainer max={1400}>
      <PageHeader
        title="Replay Workspace"
        subtitle="Step through a completed incident's lifecycle — detection to resolution, from stored data (no re-execution against live agents)"
        right={<Button icon="activity" onClick={load} disabled={loading}>{loading ? "Loading…" : "Refresh"}</Button>}
      />

      {!loading && !error && incidents.length === 0 && (
        <EmptyState icon="play" title="No incidents to replay"
          description="Trigger an anomaly (POST /api/v1/trigger or run_simulation.py) to have an incident here." />
      )}

      {!loading && !error && incidents.length > 0 && (
        <SplitLayout
          ratio="280px 1fr"
          left={
            <Panel title="Incidents" icon="play" pad={false} style={{ padding: "1rem" }}>
              <IncidentPicker incidents={incidents} selectedId={selected?.incident_id} onSelect={handleSelect} search={search} onSearch={setSearch} />
            </Panel>
          }
          right={
            selected ? (
              <div style={{ display: "flex", flexDirection: "column", gap: "1.2rem" }}>
                <Panel title="Replay Timeline" icon="play">
                  <PlaybackControls
                    index={stepIndex} total={steps.length} playing={playing}
                    onPlay={() => setPlaying(true)} onPause={() => setPlaying(false)}
                    onNext={() => jump(stepIndex + 1)} onPrev={() => jump(stepIndex - 1)}
                    onReset={() => { setStepIndex(0); setPlaying(false); }}
                    onJump={jump}
                  />
                </Panel>

                <Card accent="var(--info)">
                  <div style={{ display: "flex", alignItems: "center", gap: "0.6rem", marginBottom: "1rem" }}>
                    <Icon name={steps[stepIndex]?.icon || "play"} size={16} color="var(--accent)" />
                    <span style={{ fontSize: "1rem", fontWeight: 700, color: "var(--text)" }}>{steps[stepIndex]?.label}</span>
                  </div>
                  {steps[stepIndex]?.render()}
                </Card>
              </div>
            ) : (
              <EmptyState icon="play" title="Select an incident" description="Choose an incident from the list to replay its lifecycle." />
            )
          }
        />
      )}
    </PageContainer>
  );
}
