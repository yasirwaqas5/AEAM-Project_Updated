import { useState, useEffect, useCallback, useMemo, useRef } from "react";
import { useSearchParams } from "react-router-dom";
import {
  PageHeader, Card, Badge, SeverityBadge, Field, Button, Icon,
  fmtTime, fmtRelative,
} from "../components/ui";
import { PageContainer, SplitLayout, Panel, EmptyState, LoadingState, ErrorState } from "../components/library";
import { ReplayTimeline, ReplayGaps } from "../components/Timeline";
import { IncidentPicker } from "./Investigation";
import EvidencePanel from "../components/EvidencePanel";

/* ──────────────────────────────────────────────────────────────────────────
 * pages/Replay.jsx  (Replay Workspace)
 *
 * A step-through PLAYBACK of an already-completed investigation, built
 * entirely by REUSING, never duplicating:
 *   - GET /api/v1/replay/{id} (Phase F5) — the DURABLE data source this page
 *     previously lacked. The backend reconstructs the stage sequence from the
 *     persisted findings audit trail, so the narrative shown here IS the
 *     record rather than a second client-side derivation that could drift
 *     from it. Stage order, labels, per-stage state, honest gaps and measured
 *     durations all come from that response; this page decides only WHEN each
 *     already-recorded stage is revealed.
 *   - ReplayTimeline / ReplayGaps (components/Timeline.jsx) — the persisted
 *     timeline and gap renderers, not rebuilt here.
 *   - IncidentPicker (pages/Investigation.jsx) — the same incident-selector
 *     UI, not rebuilt.
 *   - EvidencePanel (components/EvidencePanel.jsx) — reused verbatim for a
 *     retrieval stage's evidence.
 *
 * Replay reconstructs; it never re-executes. The endpoint is read-only and
 * touches no engine, agent, action, or LLM (see aeam/api/replay.py), so
 * stepping through an incident cannot alter it — which is what makes this a
 * safe audit surface rather than the "shadow-mode re-run" this page's
 * original placeholder described.
 *
 * Three states this page must keep distinguishable, because collapsing them
 * would let a reader mistake a missing record for an empty one:
 *   - the incident has no recorded stages at all (nothing to replay);
 *   - it has stages, and some catalog stages are absent (shown as gaps with
 *     the phase that introduced them);
 *   - it has stages with no measured durations (timeline says so, never 0s).
 * ────────────────────────────────────────────────────────────────────────── */

const AUTOPLAY_INTERVAL_MS = 1800;

const fetchIncidents = async () => {
  const res = await fetch("/api/v1/incidents/");
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
};

const fetchReplay = async (incidentId) => {
  const res = await fetch(`/api/v1/replay/${encodeURIComponent(incidentId)}`);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
};

const CATEGORY_ICON = {
  decision: "target",
  evidence: "database",
  planning: "zap",
  explainability: "code",
  governance: "shield",
  actions: "zap",
  unrecognised: "layers",
};

const CATEGORY_COLOR = {
  decision: "var(--accent)",
  evidence: "var(--info)",
  planning: "var(--warn)",
  explainability: "#a78bfa",
  governance: "var(--warn)",
  actions: "var(--ok)",
  unrecognised: "var(--muted)",
};

function durationLabel(duration) {
  if (!duration?.available) return "duration not recorded";
  const seconds = duration.seconds ?? duration.stage_total_seconds;
  if (seconds == null) return "duration not recorded";
  const text = seconds < 1 ? `${Math.round(seconds * 1000)}ms` : `${Number(seconds).toFixed(2)}s`;
  return duration.scope === "stage_total" ? `${text} (stage total)` : text;
}

/* The persisted state the record itself established up to and including this
   step. Rendered as-is: the backend folds only values the entries carry, so
   there is nothing here to interpret or embellish. */
function StateAfter({ state }) {
  const keys = Object.keys(state || {}).filter((k) => !k.endsWith("_source_stage"));
  if (keys.length === 0) {
    return (
      <span style={{ color: "var(--muted)", fontStyle: "italic", fontSize: "0.78rem" }}>
        No investigation state had been recorded by this point.
      </span>
    );
  }
  return (
    <div className="aeam-grid-auto">
      {keys.map((k) => (
        <Field
          key={k}
          label={k.replace(/_/g, " ")}
          value={String(state[k])}
          mono
          title={state[`${k}_source_stage`] ? `recorded by the ${state[`${k}_source_stage`]} stage` : undefined}
        />
      ))}
    </div>
  );
}

function StageCard({ stage, incident }) {
  if (!stage) return null;
  const color = CATEGORY_COLOR[stage.category] || "var(--muted)";
  const showEvidence = stage.key === "rag";
  return (
    <Card accent={color}>
      <div style={{ display: "flex", alignItems: "center", gap: "0.6rem", marginBottom: "0.8rem", flexWrap: "wrap" }}>
        <Icon name={CATEGORY_ICON[stage.category] || "layers"} size={16} color={color} />
        <span style={{ fontSize: "1rem", fontWeight: 700, color: "var(--text)" }}>{stage.label}</span>
        <Badge label={stage.category} color={color} dot />
        {stage.occurrences_total > 1 && (
          <Badge label={`pass ${stage.occurrence} of ${stage.occurrences_total}`} color="var(--muted)" />
        )}
        <span style={{ marginLeft: "auto", fontSize: "0.72rem", fontFamily: "var(--font-mono)", color: "var(--muted)" }}>
          {durationLabel(stage.duration)}
        </span>
      </div>

      {!stage.recognised && (
        <div style={{
          fontSize: "0.7rem", color: "var(--warn)", border: "1px solid var(--border)",
          borderRadius: 8, padding: "0.45rem 0.65rem", marginBottom: "0.8rem",
        }}>
          <Icon name="alert" size={12} color="var(--warn)" /> This stage was recorded with a type
          this console build does not recognise ({stage.declared_type || "untyped"}). Its payload is shown unchanged.
        </div>
      )}

      <p style={{ fontSize: "0.88rem", color: "var(--text)", margin: 0, lineHeight: 1.6 }}>{stage.summary}</p>

      <div style={{ marginTop: "1rem" }}>
        <span style={{ fontSize: "0.62rem", textTransform: "uppercase", letterSpacing: "0.12em", color: "var(--muted)" }}>
          State recorded by this point
        </span>
        <div style={{ marginTop: "0.4rem" }}><StateAfter state={stage.state_after} /></div>
      </div>

      {showEvidence && incident && (
        <div style={{ marginTop: "1rem" }}>
          <span style={{ fontSize: "0.62rem", textTransform: "uppercase", letterSpacing: "0.12em", color: "var(--muted)" }}>
            Retrieved evidence
          </span>
          <div style={{ marginTop: "0.4rem" }}><EvidencePanel incident={incident} /></div>
        </div>
      )}

      <details style={{ marginTop: "1rem" }}>
        <summary style={{ cursor: "pointer", fontSize: "0.72rem", color: "var(--muted)" }}>
          Persisted output for this stage (verbatim)
        </summary>
        <pre style={{
          marginTop: "0.5rem", fontSize: "0.68rem", color: "var(--muted)", overflowX: "auto",
          background: "rgba(255,255,255,0.02)", border: "1px solid var(--border)",
          borderRadius: 8, padding: "0.6rem", maxHeight: 320,
        }}>{JSON.stringify(stage.outputs, null, 2)}</pre>
      </details>
    </Card>
  );
}

// ─── Playback controls ──────────────────────────────────────────────────────

function PlaybackControls({ index, total, playing, onPlay, onPause, onNext, onPrev, onReset, onJump }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "0.8rem" }}>
      <div style={{ display: "flex", alignItems: "center", gap: "0.5rem", flexWrap: "wrap" }}>
        <Button icon={playing ? "x" : "play"} variant="primary" onClick={playing ? onPause : onPlay} disabled={total === 0}>
          {playing ? "Pause" : "Play"}
        </Button>
        <Button icon="chevron" onClick={onPrev} disabled={index === 0}>Previous</Button>
        <Button icon="chevron" onClick={onNext} disabled={index >= total - 1}>Next</Button>
        <Button icon="activity" onClick={onReset}>Reset</Button>
        <span style={{ fontSize: "0.72rem", color: "var(--muted)", fontFamily: "var(--font-mono)", marginLeft: "auto" }}>
          {total === 0 ? "No recorded stages" : `Stage ${index + 1} of ${total}`}
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
  const [replay, setReplay] = useState(null);
  const [replayError, setReplayError] = useState(null);
  const [replayLoading, setReplayLoading] = useState(false);
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

  // The reconstruction is fetched per incident — the page never derives the
  // stage sequence itself, so what it shows can never disagree with the record.
  useEffect(() => {
    if (!selected?.incident_id) { setReplay(null); return; }
    let cancelled = false;
    setReplayLoading(true); setReplayError(null);
    fetchReplay(selected.incident_id)
      .then((data) => { if (!cancelled) { setReplay(data); setStepIndex(0); setPlaying(false); } })
      .catch((e) => { if (!cancelled) { setReplay(null); setReplayError(e.message); } })
      .finally(() => { if (!cancelled) setReplayLoading(false); });
    return () => { cancelled = true; };
  }, [selected?.incident_id]);

  const handleSelect = (id) => { setSearchParams({ id }); setStepIndex(0); setPlaying(false); };

  const stages = replay?.stages || [];

  useEffect(() => {
    if (!playing || stages.length === 0) return;
    timerRef.current = setInterval(() => {
      setStepIndex((i) => {
        if (i >= stages.length - 1) { setPlaying(false); return i; }
        return i + 1;
      });
    }, AUTOPLAY_INTERVAL_MS);
    return () => clearInterval(timerRef.current);
  }, [playing, stages.length]);

  const jump = (i) => setStepIndex(Math.max(0, Math.min(Math.max(stages.length - 1, 0), i)));

  if (loading) {
    return (
      <PageContainer>
        <PageHeader title="Replay Workspace" subtitle="Step through a recorded investigation — reconstructed from the persisted audit trail, never re-executed" />
        <LoadingState label="Loading incidents…" rows={5} />
      </PageContainer>
    );
  }

  if (error) {
    return (
      <PageContainer>
        <PageHeader title="Replay Workspace" subtitle="Step through a recorded investigation — reconstructed from the persisted audit trail, never re-executed"
          right={<Button icon="activity" onClick={load}>Retry</Button>} />
        <ErrorState message={error} onRetry={load} />
      </PageContainer>
    );
  }

  return (
    <PageContainer max={1400}>
      <PageHeader
        title="Replay Workspace"
        subtitle="Step through a recorded investigation — reconstructed from the persisted audit trail, never re-executed"
        right={<Button icon="activity" onClick={load} disabled={loading}>{loading ? "Loading…" : "Refresh"}</Button>}
      />

      {incidents.length === 0 && (
        <EmptyState icon="play" title="No incidents to replay"
          description="Trigger an anomaly (POST /api/v1/trigger or run_simulation.py) to have an incident here." />
      )}

      {incidents.length > 0 && (
        <SplitLayout
          ratio="280px 1fr"
          left={
            <Panel title="Incidents" icon="play" pad={false} style={{ padding: "1rem" }}>
              <IncidentPicker incidents={incidents} selectedId={selected?.incident_id} onSelect={handleSelect} search={search} onSearch={setSearch} />
            </Panel>
          }
          right={
            !selected ? (
              <EmptyState icon="play" title="Select an incident" description="Choose an incident from the list to replay its recorded investigation." />
            ) : replayLoading ? (
              <LoadingState label="Reconstructing investigation…" rows={4} />
            ) : replayError ? (
              <ErrorState message={replayError} onRetry={() => handleSelect(selected.incident_id)} />
            ) : (
              <div style={{ display: "flex", flexDirection: "column", gap: "1.2rem" }}>
                <Panel title="Incident" icon="bolt">
                  <div className="aeam-grid-auto">
                    <Field label="Event Type" value={replay?.event_type || "—"} mono />
                    <Field label="Metric" value={replay?.metric || "—"} mono />
                    <Field label="Severity" value={<SeverityBadge severity={replay?.severity} />} />
                    <Field label="Recorded" value={fmtTime(replay?.recorded_at)} title={fmtRelative(replay?.recorded_at)} />
                    <Field label="Recorded Stages" value={String(replay?.total_stages ?? 0)} mono />
                  </div>
                  {replay?.findings_readable === false && (
                    <div style={{
                      marginTop: "0.8rem", fontSize: "0.72rem", color: "var(--err)",
                      border: "1px solid var(--border)", borderRadius: 8, padding: "0.5rem 0.7rem",
                    }}>
                      <Icon name="alert" size={12} color="var(--err)" /> This incident's findings record could not be
                      decoded, so no stages could be reconstructed. Nothing has been inferred in its place.
                    </div>
                  )}
                </Panel>

                <Panel title="Playback" icon="play">
                  <PlaybackControls
                    index={stepIndex} total={stages.length} playing={playing}
                    onPlay={() => setPlaying(true)} onPause={() => setPlaying(false)}
                    onNext={() => jump(stepIndex + 1)} onPrev={() => jump(stepIndex - 1)}
                    onReset={() => { setStepIndex(0); setPlaying(false); }}
                    onJump={jump}
                  />
                </Panel>

                {stages.length === 0 ? (
                  <EmptyState icon="layers" title="No recorded stages"
                    description="This incident's persisted findings contain no investigation stages. Replay shows nothing rather than reconstructing a sequence that was never recorded." />
                ) : (
                  <StageCard stage={stages[Math.min(stepIndex, stages.length - 1)]} incident={selected} />
                )}

                {replay?.timeline && (
                  <Panel title="Timeline — measured durations only" icon="activity">
                    <ReplayTimeline timeline={replay.timeline} />
                  </Panel>
                )}

                {replay?.gaps?.length > 0 && (
                  <Panel title={`Historical gaps (${replay.gaps.length})`} icon="alert">
                    <ReplayGaps gaps={replay.gaps} />
                  </Panel>
                )}
              </div>
            )
          }
        />
      )}
    </PageContainer>
  );
}
