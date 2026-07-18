import { useState, useEffect, useCallback, useMemo } from "react";
import {
  PageHeader, Card, Badge, SeverityBadge, ConfidenceBar, Field, Button, Icon,
  fmtRelative, severityOf, getRecommendedActions, getEvidence, getRetrievedCount,
} from "../components/ui";
import { PageContainer, MetricCard, Panel, EmptyState, LoadingState, ErrorState } from "../components/library";
import EvidencePanel from "../components/EvidencePanel";

/* ──────────────────────────────────────────────────────────────────────────
 * pages/HumanReview.jsx  (Human Review Workspace)
 *
 * Reuses the existing, UNMODIFIED incidents API (GET /api/v1/incidents/ —
 * SELECT * already returns requires_human, severity, confidence, root_cause,
 * findings/evidence) and the same ui.jsx incident-shape helpers every other
 * page in this app already uses (deriveStatus, getRecommendedActions,
 * getEvidence, getRetrievedCount). No new backend endpoint, no schema change.
 *
 * Architecture note (see the Architecture Gate for this phase): the incidents
 * table has no reviewer/verdict columns and no write endpoint exists. Rather
 * than invent one, Approve / Reject / Request Changes / Escalate / Assign
 * Reviewer are implemented as REAL interactions against LOCAL, session-only
 * state — never sent to the backend, never presented as if they were. This
 * is the "UI only if backend unavailable" path the mission explicitly named
 * for Assign Reviewer, applied consistently to every verdict action so the
 * workflow is honest end-to-end: nothing here is fabricated as persisted.
 * ────────────────────────────────────────────────────────────────────────── */

const fetchIncidents = async () => {
  const res = await fetch("/api/v1/incidents/");
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
};

const VERDICTS = {
  approved:          { label: "Approved",         color: "var(--ok)",   icon: "check" },
  rejected:          { label: "Rejected",         color: "var(--err)",  icon: "x" },
  changes_requested: { label: "Changes Requested", color: "var(--warn)", icon: "alert" },
  escalated:         { label: "Escalated",        color: "var(--info)", icon: "shield" },
};

// ─── Evidence Summary (condensed — full detail via the reused EvidencePanel) ─

function EvidenceSummary({ incident, expanded, onToggle }) {
  const count = getRetrievedCount(incident);
  const top = getEvidence(incident)[0];
  return (
    <div>
      <button onClick={onToggle} style={{
        display: "flex", alignItems: "center", gap: "0.5rem", background: "none", border: "none",
        cursor: "pointer", color: "var(--muted)", fontSize: "0.76rem", padding: 0,
      }}>
        <Icon name="database" size={13} />
        {count > 0
          ? `${count} evidence chunk${count !== 1 ? "s" : ""}${top?.cause ? ` — “${top.cause}”` : ""}`
          : "No evidence retrieved for this investigation"}
        <Icon name="chevron" size={12} style={{ transform: expanded ? "rotate(180deg)" : "none" }} />
      </button>
      {expanded && (
        <div style={{ marginTop: "0.7rem", padding: "0.8rem", border: "1px solid var(--border)", borderRadius: 9, background: "rgba(255,255,255,0.015)" }}>
          <EvidencePanel incident={incident} />
        </div>
      )}
    </div>
  );
}

// ─── Verdict action bar (records to LOCAL state only — never sent anywhere) ──

function VerdictBar({ onRecord }) {
  const [pendingVerdict, setPendingVerdict] = useState(null);
  const [reviewer, setReviewer] = useState("Operator");
  const [note, setNote] = useState("");

  if (pendingVerdict) {
    const v = VERDICTS[pendingVerdict];
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: "0.6rem", padding: "0.8rem", border: `1px solid color-mix(in srgb, ${v.color} 33%, transparent)`, borderRadius: 9, background: `color-mix(in srgb, ${v.color} 6%, transparent)` }}>
        <div style={{ display: "flex", alignItems: "center", gap: "0.5rem", fontSize: "0.78rem", color: v.color, fontWeight: 600 }}>
          <Icon name={v.icon} size={14} /> Confirm: {v.label}
        </div>
        <input value={reviewer} onChange={(e) => setReviewer(e.target.value)} placeholder="Reviewer name"
          style={{ background: "var(--bg)", border: "1px solid var(--border)", borderRadius: 6, padding: "0.4rem 0.6rem", fontSize: "0.78rem", color: "var(--text)" }} />
        <textarea value={note} onChange={(e) => setNote(e.target.value)} placeholder="Optional note…" rows={2}
          style={{ background: "var(--bg)", border: "1px solid var(--border)", borderRadius: 6, padding: "0.4rem 0.6rem", fontSize: "0.78rem", color: "var(--text)", fontFamily: "inherit", resize: "vertical" }} />
        <div style={{ display: "flex", gap: "0.5rem" }}>
          <Button variant="primary" onClick={() => { onRecord(pendingVerdict, reviewer, note); setPendingVerdict(null); setNote(""); }}>
            Confirm {v.label}
          </Button>
          <Button onClick={() => setPendingVerdict(null)}>Cancel</Button>
        </div>
      </div>
    );
  }

  return (
    <div style={{ display: "flex", flexWrap: "wrap", gap: "0.5rem" }}>
      <Button icon="check" onClick={() => setPendingVerdict("approved")}>Approve</Button>
      <Button icon="x" onClick={() => setPendingVerdict("rejected")}>Reject</Button>
      <Button icon="alert" onClick={() => setPendingVerdict("changes_requested")}>Request Changes</Button>
      <Button icon="shield" onClick={() => setPendingVerdict("escalated")}>Escalate</Button>
    </div>
  );
}

// ─── Review queue card ───────────────────────────────────────────────────────

function ReviewCard({ incident, onRecord }) {
  const [evidenceOpen, setEvidenceOpen] = useState(false);
  const sev = severityOf(incident.severity);
  const recommended = getRecommendedActions(incident);

  return (
    <Card accent={sev.color} style={{ display: "flex", flexDirection: "column", gap: "0.9rem" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: "0.5rem" }}>
        <div style={{ display: "flex", alignItems: "center", gap: "0.6rem" }}>
          <SeverityBadge severity={incident.severity} />
          <span style={{ fontFamily: "var(--font-mono)", fontSize: "0.85rem", fontWeight: 600, color: "var(--text)" }}>
            {incident.event_type || "—"}
          </span>
          <span style={{ fontSize: "0.72rem", color: "var(--muted)" }}>· {incident.metric || "—"}</span>
        </div>
        <span style={{ fontSize: "0.68rem", color: "var(--muted)", fontFamily: "var(--font-mono)" }}>{fmtRelative(incident.timestamp)}</span>
      </div>

      <div className="aeam-grid-auto">
        <Field label="Root Cause" value={incident.root_cause || "Pending"} />
        <div style={{ display: "flex", flexDirection: "column", gap: "0.3rem" }}>
          <span style={{ fontSize: "0.62rem", textTransform: "uppercase", letterSpacing: "0.12em", color: "var(--muted)" }}>Confidence</span>
          <ConfidenceBar value={incident.confidence} />
        </div>
        <Field label="Recommended Action" value={recommended.join("; ")} />
      </div>

      <EvidenceSummary incident={incident} expanded={evidenceOpen} onToggle={() => setEvidenceOpen((v) => !v)} />

      <VerdictBar onRecord={(verdict, reviewer, note) => onRecord(incident, verdict, reviewer, note)} />
    </Card>
  );
}

// ─── Session Review History (explicitly labeled non-persisted) ─────────────

function ReviewHistory({ entries }) {
  if (entries.length === 0) {
    return (
      <EmptyState icon="clock" title="No review history yet"
        description="Review decisions are not yet persisted by the backend — nothing to show until Approve/Reject/Request Changes/Escalate is used in this session." />
    );
  }
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "0.6rem" }}>
      {entries.map((e, i) => {
        const v = VERDICTS[e.verdict];
        return (
          <div key={i} style={{ display: "flex", flexDirection: "column", gap: "0.3rem", padding: "0.7rem 0.9rem", border: "1px solid var(--border)", borderRadius: 9 }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: "0.5rem" }}>
              <div style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
                <Badge label={v.label} color={v.color} dot />
                <span style={{ fontSize: "0.78rem", color: "var(--text)", fontFamily: "var(--font-mono)" }}>{e.incident.event_type} · {e.incident.metric}</span>
              </div>
              <span style={{ fontSize: "0.68rem", color: "var(--muted)" }}>{fmtRelative(e.at)} by {e.reviewer}</span>
            </div>
            {e.note && <div style={{ fontSize: "0.76rem", color: "var(--muted)" }}>{e.note}</div>}
          </div>
        );
      })}
    </div>
  );
}

// ─── Page ───────────────────────────────────────────────────────────────────

export default function HumanReview() {
  const [incidents, setIncidents] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [reviewed, setReviewed] = useState({}); // { [incident_id]: reviewHistoryEntry }

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

  const recordVerdict = useCallback((incident, verdict, reviewer, note) => {
    setReviewed((prev) => ({
      ...prev,
      [incident.incident_id]: { incident, verdict, reviewer: reviewer || "Operator", note, at: new Date().toISOString() },
    }));
  }, []);

  const needsReview = useMemo(
    () => incidents.filter((i) => i.requires_human && !reviewed[i.incident_id]),
    [incidents, reviewed],
  );
  const historyEntries = useMemo(
    () => Object.values(reviewed).sort((a, b) => new Date(b.at) - new Date(a.at)),
    [reviewed],
  );
  const escalatedCount = useMemo(() => incidents.filter((i) => i.requires_human).length, [incidents]);
  const avgConfidence = useMemo(() => {
    const withConf = needsReview.filter((i) => i.confidence != null);
    if (!withConf.length) return null;
    return withConf.reduce((s, i) => s + i.confidence, 0) / withConf.length;
  }, [needsReview]);

  if (loading) {
    return (
      <PageContainer>
        <PageHeader title="Human Review Queue" subtitle="Work the escalation backlog — assign, approve, reject, request changes, or escalate" />
        <LoadingState label="Loading the review queue…" rows={5} />
      </PageContainer>
    );
  }

  if (error) {
    return (
      <PageContainer>
        <PageHeader title="Human Review Queue" subtitle="Work the escalation backlog — assign, approve, reject, request changes, or escalate"
          right={<Button icon="activity" onClick={load}>Retry</Button>} />
        <ErrorState message={error} onRetry={load} />
      </PageContainer>
    );
  }

  return (
    <PageContainer max={1100}>
      <PageHeader
        title="Human Review Queue"
        subtitle="Work the escalation backlog — assign, approve, reject, request changes, or escalate"
        right={<Button icon="activity" onClick={load} disabled={loading}>{loading ? "Loading…" : "Refresh"}</Button>}
      />

      <div style={{
        display: "flex", alignItems: "center", gap: "0.6rem", marginBottom: "1.2rem",
        padding: "0.7rem 1rem", border: "1px solid var(--border)", borderRadius: 9,
        background: "rgba(0,180,255,0.06)", fontSize: "0.76rem", color: "var(--muted)",
      }}>
        <Icon name="alert" size={14} color="var(--info)" />
        Review decisions below are recorded for <strong style={{ color: "var(--text)" }}>this browser session only</strong> — the
        incidents API has no reviewer/verdict write endpoint yet, so nothing here is sent to or persisted by the backend.
      </div>

      <div className="aeam-grid-metrics" style={{ marginBottom: "1.4rem" }}>
        <MetricCard label="In Queue" value={needsReview.length} icon="alert" accent="var(--warn)" sub="need attention" />
        <MetricCard label="Escalated (total)" value={escalatedCount} icon="shield" sub="requires_human = true" />
        <MetricCard label="Avg Confidence (pending)" value={avgConfidence != null ? `${Math.round(avgConfidence * (avgConfidence <= 1 ? 100 : 1))}%` : "—"} icon="check" accent="var(--info)" />
        <MetricCard label="Reviewed This Session" value={historyEntries.length} icon="target" accent="var(--ok)" sub="not persisted" />
      </div>

      <div style={{ marginBottom: "1.4rem" }}>
        <Panel title="Review Queue" icon="shield" pad={needsReview.length === 0}>
          {needsReview.length === 0 ? (
            <EmptyState icon="shield" title="No incidents currently need review"
              description="Escalated incidents (requires_human = true) will appear here." />
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: "0.9rem", padding: "1.1rem" }}>
              {needsReview.map((inc) => (
                <ReviewCard key={inc.incident_id} incident={inc} onRecord={recordVerdict} />
              ))}
            </div>
          )}
        </Panel>
      </div>

      <Panel title="Review History (this session)" icon="clock">
        <ReviewHistory entries={historyEntries} />
      </Panel>
    </PageContainer>
  );
}
