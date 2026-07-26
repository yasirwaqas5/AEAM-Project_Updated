import { useState, useEffect, useCallback, useMemo } from "react";
import {
  PageHeader, Card, Badge, SeverityBadge, ConfidenceBar, Field, Button, Icon,
  fmtRelative, severityOf, getRecommendedActions, getEvidence, getRetrievedCount,
} from "../components/ui";
import { PageContainer, MetricCard, Panel, EmptyState, LoadingState, ErrorState } from "../components/library";
import EvidencePanel from "../components/EvidencePanel";
import { fetchJSON } from "./KnowledgeCenter";

/* ──────────────────────────────────────────────────────────────────────────
 * pages/HumanReview.jsx  (Human Review Workspace)
 *
 * Phase E9 — this workspace is now PERSISTED. Until E9 the incidents table
 * had no verdict columns and no write endpoint existed, so every verdict was
 * recorded in browser session state and the page said so in a banner. That
 * banner is gone because it stopped being true: verdicts go to
 * aeam/api/review.py, survive restart, carry the acting principal, and — for
 * an approval that completes the chain — actually release the runbook steps
 * the Orchestrator withheld.
 *
 * Two backend surfaces, both new in E9 and both read verbatim (no derived
 * governance state is computed in the browser — the chain, the tier, and
 * what executed are the server's answers):
 *   GET  /api/v1/review/queue      — incidents holding withheld actions
 *   GET  /api/v1/review/verdicts   — persisted verdict history
 *   POST /api/v1/review/incidents/{id}/{approve|reject|verdict}
 *
 * The incidents API (GET /api/v1/incidents/) is still read, unmodified, for
 * the evidence/confidence/root-cause detail each queue row displays — the
 * queue says WHAT is gated, the incident record says what the investigation
 * found. Incidents that predate this phase simply never appear in the queue.
 * ────────────────────────────────────────────────────────────────────────── */

const fetchIncidents = () => fetchJSON("/api/v1/incidents/");
const fetchQueue = () => fetchJSON("/api/v1/review/queue");
const fetchVerdicts = () => fetchJSON("/api/v1/review/verdicts?limit=50");

const submitVerdict = (incidentId, verdict, note) =>
  fetchJSON(`/api/v1/review/incidents/${incidentId}/verdict`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ verdict, note: note || null }),
  });

const VERDICTS = {
  approved:          { label: "Approved",          color: "var(--ok)",   icon: "check" },
  rejected:          { label: "Rejected",          color: "var(--err)",  icon: "x" },
  changes_requested: { label: "Changes Requested", color: "var(--warn)", icon: "alert" },
  escalated:         { label: "Escalated",         color: "var(--info)", icon: "shield" },
};

// Verdicts that leave the approval chain exactly where it was. Labelled as
// such in the UI so a reviewer is never led to believe "Request Changes"
// approved or rejected anything.
const NON_DECIDING = new Set(["changes_requested", "escalated"]);

// ─── Approval chain (server-provided; never inferred here) ──────────────────

function ApprovalChain({ approval }) {
  const tiers = approval.required_tiers || [];
  const current = approval.current_tier ?? 0;
  if (tiers.length === 0) return null;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "0.4rem" }}>
      <span style={{ fontSize: "0.62rem", textTransform: "uppercase", letterSpacing: "0.12em", color: "var(--muted)" }}>
        Approval Chain {tiers.length > 1 ? `(${tiers.length} tiers, in order)` : "(single tier)"}
      </span>
      <div style={{ display: "flex", alignItems: "center", flexWrap: "wrap", gap: "0.35rem" }}>
        {tiers.map((tier, i) => {
          const done = i < current;
          const active = i === current && approval.status === "pending";
          const color = done ? "var(--ok)" : active ? "var(--warn)" : "var(--muted)";
          return (
            <span key={`${tier}-${i}`} style={{ display: "flex", alignItems: "center", gap: "0.35rem" }}>
              {i > 0 && <Icon name="chevron" size={11} style={{ transform: "rotate(-90deg)", opacity: 0.5 }} />}
              <span style={{
                display: "inline-flex", alignItems: "center", gap: "0.3rem",
                fontSize: "0.72rem", color, fontWeight: active ? 600 : 400,
                padding: "0.15rem 0.5rem", borderRadius: 999,
                border: `1px solid color-mix(in srgb, ${color} 40%, transparent)`,
                background: active ? `color-mix(in srgb, ${color} 10%, transparent)` : "transparent",
              }}>
                {done && <Icon name="check" size={11} />}
                {tier}
              </span>
            </span>
          );
        })}
      </div>
    </div>
  );
}

// ─── Withheld actions (what the gate is actually holding back) ──────────────

function WithheldActions({ approval }) {
  const pending = approval.pending_actions || [];
  if (pending.length === 0) {
    return (
      <div style={{ fontSize: "0.74rem", color: "var(--muted)" }}>
        No runbook steps were withheld — notifications already dispatched.
      </div>
    );
  }
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "0.35rem" }}>
      <span style={{ fontSize: "0.62rem", textTransform: "uppercase", letterSpacing: "0.12em", color: "var(--muted)" }}>
        Withheld Until Approved ({pending.length})
      </span>
      <div style={{ display: "flex", flexWrap: "wrap", gap: "0.35rem" }}>
        {pending.map((step) => (
          <Badge key={step} label={step} color="var(--warn)" dot />
        ))}
      </div>
    </div>
  );
}

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

// ─── Verdict action bar (persisted — POSTs to the review API) ───────────────

function VerdictBar({ approval, onSubmit, busy, error }) {
  const [pendingVerdict, setPendingVerdict] = useState(null);
  const [note, setNote] = useState("");

  const awaiting = approval.awaiting_tier;

  if (pendingVerdict) {
    const v = VERDICTS[pendingVerdict];
    const deciding = !NON_DECIDING.has(pendingVerdict);
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: "0.6rem", padding: "0.8rem", border: `1px solid color-mix(in srgb, ${v.color} 33%, transparent)`, borderRadius: 9, background: `color-mix(in srgb, ${v.color} 6%, transparent)` }}>
        <div style={{ display: "flex", alignItems: "center", gap: "0.5rem", fontSize: "0.78rem", color: v.color, fontWeight: 600 }}>
          <Icon name={v.icon} size={14} /> Confirm: {v.label}
          {awaiting && <span style={{ fontWeight: 400, color: "var(--muted)" }}>· as {awaiting}</span>}
        </div>
        <div style={{ fontSize: "0.72rem", color: "var(--muted)" }}>
          {deciding
            ? (pendingVerdict === "approved"
                ? ((approval.remaining_tiers || []).length > 1
                    ? `Advances the chain to ${(approval.remaining_tiers || [])[1]}. Nothing executes until every tier has approved.`
                    : "This is the final tier — the withheld runbook steps will execute now.")
                : "Halts the chain permanently. No withheld step will ever execute for this incident.")
            : "Recorded with your identity; the approval chain stays exactly where it is."}
        </div>
        <textarea value={note} onChange={(e) => setNote(e.target.value)} placeholder="Optional note…" rows={2}
          style={{ background: "var(--bg)", border: "1px solid var(--border)", borderRadius: 6, padding: "0.4rem 0.6rem", fontSize: "0.78rem", color: "var(--text)", fontFamily: "inherit", resize: "vertical" }} />
        {error && <div style={{ fontSize: "0.74rem", color: "var(--err)" }}>{error}</div>}
        <div style={{ display: "flex", gap: "0.5rem" }}>
          <Button variant="primary" disabled={busy}
            onClick={async () => {
              const ok = await onSubmit(pendingVerdict, note);
              if (ok) { setPendingVerdict(null); setNote(""); }
            }}>
            {busy ? "Submitting…" : `Confirm ${v.label}`}
          </Button>
          <Button onClick={() => setPendingVerdict(null)} disabled={busy}>Cancel</Button>
        </div>
      </div>
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "0.4rem" }}>
      <div style={{ display: "flex", flexWrap: "wrap", gap: "0.5rem" }}>
        <Button icon="check" onClick={() => setPendingVerdict("approved")}>Approve</Button>
        <Button icon="x" onClick={() => setPendingVerdict("rejected")}>Reject</Button>
        <Button icon="alert" onClick={() => setPendingVerdict("changes_requested")}>Request Changes</Button>
        <Button icon="shield" onClick={() => setPendingVerdict("escalated")}>Escalate</Button>
      </div>
      {error && <div style={{ fontSize: "0.74rem", color: "var(--err)" }}>{error}</div>}
    </div>
  );
}

// ─── Review queue card ───────────────────────────────────────────────────────

function ReviewCard({ approval, incident, onSubmit }) {
  const [evidenceOpen, setEvidenceOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const sev = severityOf(approval.severity || incident?.severity);
  const recommended = incident ? getRecommendedActions(incident) : [];

  const handle = useCallback(async (verdict, note) => {
    setBusy(true); setError(null);
    try {
      await onSubmit(approval.incident_id, verdict, note);
      return true;
    } catch (e) {
      setError(e.message);
      return false;
    } finally {
      setBusy(false);
    }
  }, [approval.incident_id, onSubmit]);

  return (
    <Card accent={sev.color} style={{ display: "flex", flexDirection: "column", gap: "0.9rem" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: "0.5rem" }}>
        <div style={{ display: "flex", alignItems: "center", gap: "0.6rem" }}>
          <SeverityBadge severity={approval.severity || incident?.severity} />
          <span style={{ fontFamily: "var(--font-mono)", fontSize: "0.85rem", fontWeight: 600, color: "var(--text)" }}>
            {approval.event_type || incident?.event_type || "—"}
          </span>
          <span style={{ fontSize: "0.72rem", color: "var(--muted)" }}>· {approval.metric || incident?.metric || "—"}</span>
        </div>
        <span style={{ fontSize: "0.68rem", color: "var(--muted)", fontFamily: "var(--font-mono)" }}>{fmtRelative(approval.created_at)}</span>
      </div>

      <div className="aeam-grid-auto">
        <Field label="Root Cause" value={incident?.root_cause || "Pending"} />
        <div style={{ display: "flex", flexDirection: "column", gap: "0.3rem" }}>
          <span style={{ fontSize: "0.62rem", textTransform: "uppercase", letterSpacing: "0.12em", color: "var(--muted)" }}>Confidence</span>
          <ConfidenceBar value={incident?.confidence} />
        </div>
        <Field label="Recommended Action" value={recommended.join("; ")} />
      </div>

      <ApprovalChain approval={approval} />
      <WithheldActions approval={approval} />

      {incident && (
        <EvidenceSummary incident={incident} expanded={evidenceOpen} onToggle={() => setEvidenceOpen((v) => !v)} />
      )}

      <VerdictBar approval={approval} onSubmit={handle} busy={busy} error={error} />
    </Card>
  );
}

// ─── Persisted verdict history ──────────────────────────────────────────────

function ReviewHistory({ entries }) {
  if (entries.length === 0) {
    return (
      <EmptyState icon="clock" title="No verdicts recorded yet"
        description="Approve / Reject / Request Changes / Escalate decisions appear here once cast — they are persisted, attributed, and survive restart." />
    );
  }
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "0.6rem" }}>
      {entries.map((e) => {
        const v = VERDICTS[e.verdict] || { label: e.verdict, color: "var(--muted)" };
        const tier = e.tier_label ? `${e.tier_label} (tier ${(e.tier ?? 0) + 1})` : `tier ${(e.tier ?? 0) + 1}`;
        return (
          <div key={e.verdict_id} style={{ display: "flex", flexDirection: "column", gap: "0.3rem", padding: "0.7rem 0.9rem", border: "1px solid var(--border)", borderRadius: 9 }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: "0.5rem" }}>
              <div style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
                <Badge label={v.label} color={v.color} dot />
                <span style={{ fontSize: "0.78rem", color: "var(--text)", fontFamily: "var(--font-mono)" }}>{e.incident_id}</span>
                <span style={{ fontSize: "0.7rem", color: "var(--muted)" }}>· {tier}</span>
              </div>
              <span style={{ fontSize: "0.68rem", color: "var(--muted)" }}>
                {fmtRelative(e.created_at)} by {e.reviewer_id}
                {e.attribution_source && e.attribution_source !== "jwt" && (
                  <span style={{ color: "var(--warn)" }}> · identity: {e.attribution_source}</span>
                )}
              </span>
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
  const [queue, setQueue] = useState([]);
  const [enforced, setEnforced] = useState(true);
  const [incidents, setIncidents] = useState([]);
  const [verdicts, setVerdicts] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    setLoading(true); setError(null);
    try {
      const [queueData, verdictData, incidentData] = await Promise.all([
        fetchQueue(), fetchVerdicts(), fetchIncidents(),
      ]);
      setQueue(Array.isArray(queueData?.queue) ? queueData.queue : []);
      setEnforced(queueData?.enforced !== false);
      setVerdicts(Array.isArray(verdictData?.verdicts) ? verdictData.verdicts : []);
      setIncidents(Array.isArray(incidentData) ? incidentData : []);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  // Cast a verdict, then re-read from the server. The page never guesses the
  // resulting state — whether the chain advanced, halted, or released
  // execution is the review API's answer, not a local optimistic update.
  const castVerdict = useCallback(async (incidentId, verdict, note) => {
    await submitVerdict(incidentId, verdict, note);
    await load();
  }, [load]);

  const incidentsById = useMemo(() => {
    const map = {};
    for (const inc of incidents) map[inc.incident_id] = inc;
    return map;
  }, [incidents]);

  const escalatedCount = useMemo(
    () => incidents.filter((i) => i.requires_human).length, [incidents],
  );
  const withheldCount = useMemo(
    () => queue.reduce((sum, a) => sum + (a.pending_actions?.length || 0), 0), [queue],
  );
  const multiTierCount = useMemo(
    () => queue.filter((a) => (a.required_tiers || []).length > 1).length, [queue],
  );

  const header = (
    <PageHeader
      title="Human Review Queue"
      subtitle="Approve, reject, request changes, or escalate — verdicts are persisted, attributed, and gate execution"
      right={<Button icon="activity" onClick={load} disabled={loading}>{loading ? "Loading…" : "Refresh"}</Button>}
    />
  );

  if (loading) {
    return <PageContainer>{header}<LoadingState label="Loading the review queue…" rows={5} /></PageContainer>;
  }

  if (error) {
    return <PageContainer>{header}<ErrorState message={error} onRetry={load} /></PageContainer>;
  }

  return (
    <PageContainer max={1100}>
      {header}

      {!enforced && (
        <div style={{
          display: "flex", alignItems: "center", gap: "0.6rem", marginBottom: "1.2rem",
          padding: "0.7rem 1rem", border: "1px solid var(--border)", borderRadius: 9,
          background: "rgba(255,180,0,0.07)", fontSize: "0.76rem", color: "var(--muted)",
        }}>
          <Icon name="alert" size={14} color="var(--warn)" />
          Approval enforcement is <strong style={{ color: "var(--text)" }}>disabled</strong> on this
          deployment (HUMAN_APPROVAL_ENFORCED=false), so runbook steps execute without waiting for a
          verdict. Verdicts cast here are still persisted and attributed — but they are not gating anything.
        </div>
      )}

      <div className="aeam-grid-metrics" style={{ marginBottom: "1.4rem" }}>
        <MetricCard label="Awaiting Approval" value={queue.length} icon="alert" accent="var(--warn)" sub="gated incidents" />
        <MetricCard label="Actions Withheld" value={withheldCount} icon="shield" accent="var(--err)" sub="will run on approval" />
        <MetricCard label="Multi-Tier Chains" value={multiTierCount} icon="target" sub="need every tier, in order" />
        <MetricCard label="Escalated (total)" value={escalatedCount} icon="activity" accent="var(--info)" sub="requires_human = true" />
      </div>

      <div style={{ marginBottom: "1.4rem" }}>
        <Panel title="Review Queue" icon="shield" pad={queue.length === 0}>
          {queue.length === 0 ? (
            <EmptyState icon="shield" title="Nothing is waiting on a human"
              description="Incidents appear here only when their execution plan required approval and runbook steps were actually withheld." />
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: "0.9rem", padding: "1.1rem" }}>
              {queue.map((approval) => (
                <ReviewCard
                  key={approval.approval_id}
                  approval={approval}
                  incident={incidentsById[approval.incident_id]}
                  onSubmit={castVerdict}
                />
              ))}
            </div>
          )}
        </Panel>
      </div>

      <Panel title="Verdict History" icon="clock">
        <ReviewHistory entries={verdicts} />
      </Panel>
    </PageContainer>
  );
}
