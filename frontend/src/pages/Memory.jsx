import { useState, useEffect, useCallback, useMemo, lazy, Suspense } from "react";
import { Link } from "react-router-dom";
import { PageHeader, Badge, Button, Modal, Icon, fmtRelative, getMemoryMatches, getMemoryData } from "../components/ui";
import { PageContainer, Panel, DataTable, MetricCard, EmptyState, LoadingState, ErrorState } from "../components/library";
import { CountUp } from "../components/charts";
import { useAuth } from "../layout/AuthProvider";
import { useToast } from "../layout/ToastHost";

const NodeGraph = lazy(() => import("../components/three/NodeGraph"));

/* ──────────────────────────────────────────────────────────────────────────
 * pages/Memory.jsx — Enterprise Memory Center.
 *
 * Shows the REAL organizational memory at work (Phase C1): which live
 * investigations recalled which past resolved incidents, with true
 * similarity scores — read from the same /api/v1/incidents/ findings the
 * Investigation Workspace uses, plus the Observability engine's real
 * memory hit-rate. No fabricated graph, no invented "embeddings browser":
 * direct vector-collection browsing would need a new backend endpoint
 * (listed honestly below).
 * ────────────────────────────────────────────────────────────────────────── */

/* ─── Phase E12 (MEM-4): memory curation ────────────────────────────────────
 * Organizational memory had no correction path: a memory recorded from a
 * wrong root cause kept surfacing as evidence in every future investigation,
 * with no way to withdraw or fix it. This modal is that path.
 *
 * Both operations REQUIRE a reason — the control will not submit without one,
 * because an unattributed, unexplained deletion from an audited store is
 * exactly what MEM-4 exists to prevent. The acting principal comes from the
 * session, server-side; nothing here can spoof it.
 * ────────────────────────────────────────────────────────────────────────── */
function CurateMemoryModal({ incidentId, currentRootCause, onClose, onDone }) {
  const [mode, setMode] = useState("correct");   // 'correct' | 'expunge'
  const [rootCause, setRootCause] = useState(currentRootCause || "");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const toast = useToast();

  async function submit() {
    if (!reason.trim()) {
      setError("A reason is required — memory corrections are never unexplained.");
      return;
    }
    if (mode === "correct" && !rootCause.trim()) {
      setError("Enter the corrected root cause.");
      return;
    }
    setBusy(true); setError(null);
    try {
      const path = mode === "expunge"
        ? "/api/v1/knowledge/curate/memory/expunge"
        : "/api/v1/knowledge/curate/memory/correct";
      const body = mode === "expunge"
        ? { incident_id: incidentId, reason: reason.trim() }
        : { incident_id: incidentId, corrections: { root_cause: rootCause.trim() }, reason: reason.trim() };

      const res = await fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        const detail = await res.json().catch(() => ({}));
        throw new Error(detail.detail || `HTTP ${res.status}`);
      }
      toast.success(
        mode === "expunge" ? "Memory withdrawn" : "Memory corrected",
        mode === "expunge"
          ? "This incident no longer surfaces as evidence in new investigations."
          : "Re-embedded from the corrected text, so recall now matches the corrected wording.",
      );
      onDone?.();
      onClose();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal title="Curate organizational memory" icon="shield" onClose={onClose} maxWidth={560}>
      <div style={{ display: "flex", flexDirection: "column", gap: "0.9rem" }}>
        <div style={{ fontSize: "0.7rem", color: "var(--muted)", fontFamily: "var(--font-mono)" }}>
          incident {incidentId}
        </div>

        <div style={{ display: "flex", gap: "0.4rem" }}>
          {[["correct", "Correct"], ["expunge", "Withdraw"]].map(([key, label]) => (
            <button key={key} type="button" onClick={() => { setMode(key); setError(null); }} style={{
              fontSize: "0.7rem", letterSpacing: "0.06em", textTransform: "uppercase",
              background: mode === key ? "var(--accent-dim)" : "none",
              border: `1px solid ${mode === key ? "var(--accent-border)" : "var(--border)"}`,
              color: mode === key ? "var(--accent)" : "var(--muted)",
              borderRadius: 6, padding: "0.32rem 0.75rem", cursor: "pointer",
            }}>{label}</button>
          ))}
        </div>

        <p style={{ fontSize: "0.68rem", color: "var(--muted)", lineHeight: 1.5 }}>
          {mode === "correct"
            ? "The memory is rewritten and re-embedded from the corrected text — not merely relabelled — so future recall matches the corrected wording rather than the original. The correction is stored with the entry, so anyone who recalls it later can see it was corrected, by whom, and why."
            : "The memory is permanently removed from recall. Future investigations will not surface this incident as evidence. The incident record itself is untouched — only its organizational-memory entry is withdrawn."}
        </p>

        {mode === "correct" && (
          <label style={{ display: "flex", flexDirection: "column", gap: "0.3rem" }}>
            <span style={{ fontSize: "0.62rem", textTransform: "uppercase", letterSpacing: "0.12em", color: "var(--muted)" }}>
              Corrected root cause
            </span>
            <textarea value={rootCause} onChange={(e) => setRootCause(e.target.value)} rows={2}
              style={{
                width: "100%", resize: "vertical", fontSize: "var(--fs-xs)",
                background: "var(--surface-2)", color: "var(--text)",
                border: "1px solid var(--border)", borderRadius: "var(--r-md)", padding: "var(--sp-2)",
              }} />
          </label>
        )}

        <label style={{ display: "flex", flexDirection: "column", gap: "0.3rem" }}>
          <span style={{ fontSize: "0.62rem", textTransform: "uppercase", letterSpacing: "0.12em", color: "var(--muted)" }}>
            Reason (required)
          </span>
          <textarea value={reason} onChange={(e) => setReason(e.target.value)} rows={3}
            placeholder="Why is this memory being changed? Recorded verbatim in the audit trail with your identity."
            style={{
              width: "100%", resize: "vertical", fontSize: "var(--fs-xs)",
              background: "var(--surface-2)", color: "var(--text)",
              border: "1px solid var(--border)", borderRadius: "var(--r-md)", padding: "var(--sp-2)",
            }} />
        </label>

        {error && (
          <div role="alert" style={{ color: "var(--err)", fontSize: "0.7rem", display: "flex", gap: "0.4rem", alignItems: "center" }}>
            <Icon name="alert" size={12} color="var(--err)" /> {error}
          </div>
        )}

        <div style={{ display: "flex", gap: "0.5rem", justifyContent: "flex-end" }}>
          <Button onClick={onClose} disabled={busy}>Cancel</Button>
          <Button variant="primary" onClick={submit} disabled={busy}>
            {busy ? "Working…" : mode === "expunge" ? "Withdraw memory" : "Apply correction"}
          </Button>
        </div>
      </div>
    </Modal>
  );
}

export default function Memory() {
  const [incidents, setIncidents] = useState([]);
  const [obs, setObs] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  // Phase E12: the memory entry currently being curated, if any.
  const [curating, setCurating] = useState(null);
  const { hasPermission } = useAuth();
  const canCurate = hasPermission("admin", "config");

  const load = useCallback(async () => {
    setLoading(true); setError(null);
    try {
      const r = await fetch("/api/v1/incidents/");
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const d = await r.json();
      setIncidents(Array.isArray(d) ? d : []);
      try {
        const o = await fetch("/api/v1/observability/");
        if (o.ok) setObs(await o.json());
      } catch { /* hit-rate tiles show N/A */ }
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => { load(); }, [load]);

  /* Flatten every real recall: (investigation → recalled past incident). */
  const recalls = useMemo(() => {
    const rows = [];
    for (const inc of incidents) {
      for (const m of getMemoryMatches(inc)) {
        rows.push({
          key: `${inc.incident_id}:${m.incident_id}`,
          when: inc.timestamp,
          investigation: `${inc.event_type || "—"} · ${inc.metric || "—"}`,
          incident_id: inc.incident_id,
          recalled_id: m.incident_id,
          similarity: m.similarity,
          past_root_cause: m.root_cause,
          resolution_status: m.resolution_status,
        });
      }
    }
    return rows.sort((a, b) => (b.similarity ?? 0) - (a.similarity ?? 0));
  }, [incidents]);

  const consultedCount = useMemo(
    () => incidents.filter((i) => getMemoryData(i) != null).length,
    [incidents],
  );

  const hitRate = obs?.memory_hit_rate;

  return (
    <PageContainer>
      <PageHeader
        title="Memory Center"
        subtitle="Organizational memory — past resolved incidents recalled as evidence for new investigations"
        right={<Button icon="activity" onClick={load} disabled={loading}>{loading ? "Loading…" : "Refresh"}</Button>}
      />

      {loading && <LoadingState label="Loading memory activity…" rows={4} />}
      {error && <ErrorState message={error} onRetry={load} />}

      {!loading && !error && (
        <>
          <div className="aeam-grid-metrics aeam-stagger" style={{ marginBottom: "1.2rem" }}>
            <MetricCard label="Investigations Recorded" icon="database"
              value={obs?.total_investigations != null ? <CountUp value={obs.total_investigations} /> : "N/A"}
              sub="each finalized incident is embedded into memory" />
            <MetricCard label="Memory Consultations" icon="layers"
              value={<CountUp value={consultedCount} />} sub="investigations that queried memory" />
            <MetricCard label="Recalls Made" icon="branch" accent="var(--c-memory)"
              value={<CountUp value={recalls.length} />} sub="past incidents surfaced as evidence" />
            <MetricCard label="Memory Hit Rate" icon="target"
              accent={hitRate?.available ? "var(--ok)" : "var(--muted)"}
              value={hitRate?.available ? `${Math.round(hitRate.rate * 100)}%` : "N/A"}
              sub={hitRate?.available ? `${hitRate.hit_count}/${hitRate.consulted_count} consultations found a match` : hitRate?.reason || "unavailable"} />
          </div>

          {recalls.length > 0 && (
            <Panel title="Recall Graph — investigations linked to the past incidents they cited" icon="branch"
              pad={false} style={{ marginBottom: "1.2rem" }}
              right={<span style={{ fontSize: "var(--fs-2xs)", color: "var(--faint)", fontFamily: "var(--font-mono)" }}>hover a node · edge strength = similarity</span>}>
              <Suspense fallback={<div style={{ height: 320, background: "radial-gradient(circle at 50% 45%, rgba(167,139,250,.12), transparent 60%)" }} />}>
                <NodeGraph height={320} layout="orbit"
                  nodes={(() => {
                    const byId = new Map();
                    for (const inc of incidents) {
                      byId.set(inc.incident_id, {
                        id: inc.incident_id, color: "#5b9dff", size: 0.1,
                        label: `${inc.event_type || "incident"} · ${inc.metric || "—"}`,
                      });
                    }
                    const nodes = [];
                    const used = new Set();
                    for (const r of recalls) {
                      for (const id of [r.incident_id, r.recalled_id]) {
                        if (!id || used.has(id)) continue;
                        used.add(id);
                        nodes.push(byId.get(id) || {
                          id, color: "#a78bfa", size: 0.08,
                          label: `past incident ${String(id).slice(0, 8)}…`,
                        });
                      }
                    }
                    return nodes;
                  })()}
                  edges={recalls.map((r) => ({ from: r.incident_id, to: r.recalled_id, weight: r.similarity ?? 0.4 }))}
                />
              </Suspense>
            </Panel>
          )}

          <Panel title="Recall Activity — strongest matches first" icon="layers" pad={false}>
            <DataTable
              empty="No memory recalls yet — memory matches appear here once an investigation finds a similar past incident."
              rowKey={(r) => r.key}
              columns={[
                {
                  key: "investigation", label: "Investigation",
                  render: (r) => (
                    <Link to={`/investigation?id=${encodeURIComponent(r.incident_id)}`}
                      style={{ color: "var(--accent)", textDecoration: "none", fontWeight: 600 }}>
                      {r.investigation}
                    </Link>
                  ),
                },
                { key: "when", label: "When", render: (r) => <span style={{ color: "var(--muted)" }}>{fmtRelative(r.when)}</span> },
                {
                  key: "similarity", label: "Similarity",
                  render: (r) => r.similarity == null ? "—" : (
                    <span style={{ fontFamily: "var(--font-mono)", color: r.similarity >= 0.7 ? "var(--ok)" : "var(--text-2)" }}>
                      {(r.similarity * 100).toFixed(1)}%
                    </span>
                  ),
                },
                {
                  key: "recalled_id", label: "Recalled Incident",
                  render: (r) => (
                    <Link to={`/investigation?id=${encodeURIComponent(r.recalled_id)}`}
                      style={{ color: "var(--text-2)", fontFamily: "var(--font-mono)", fontSize: "var(--fs-xs)" }}
                      title={r.recalled_id}>
                      {String(r.recalled_id || "").slice(0, 8)}…
                    </Link>
                  ),
                },
                { key: "past_root_cause", label: "Past Root Cause", render: (r) => r.past_root_cause || <span style={{ color: "var(--faint)" }}>not recorded</span> },
                {
                  key: "resolution_status", label: "Outcome",
                  render: (r) => r.resolution_status
                    ? <Badge label={r.resolution_status} color={r.resolution_status === "RESOLVED" ? "var(--ok)" : "var(--warn)"} />
                    : "—",
                },
                // Phase E12 (MEM-4): the correction path. Only rendered for a
                // session holding admin:config — the backend enforces that
                // regardless; hiding it just avoids offering a 403.
                ...(canCurate ? [{
                  key: "curate", label: "Curate",
                  render: (r) => (
                    <Button size="sm" icon="shield"
                      title="Correct or withdraw this memory"
                      onClick={() => setCurating({
                        incidentId: r.recalled_id, rootCause: r.past_root_cause,
                      })}>
                      Curate
                    </Button>
                  ),
                }] : []),
              ]}
              rows={recalls}
            />
          </Panel>

          {curating && (
            <CurateMemoryModal
              incidentId={curating.incidentId}
              currentRootCause={curating.rootCause}
              onClose={() => setCurating(null)}
              onDone={load}
            />
          )}

          {recalls.length === 0 && consultedCount > 0 && (
            <div style={{ marginTop: "1rem" }}>
              <EmptyState icon="layers" title="Memory consulted, no similar incidents found yet"
                description={`${consultedCount} investigation(s) queried organizational memory but found nothing above the similarity threshold — expected while the incident corpus is young. Matches compound as resolved incidents accumulate.`} />
            </div>
          )}

          <p style={{ marginTop: "1.1rem", fontSize: "var(--fs-xs)", color: "var(--faint)", lineHeight: 1.6 }}>
            Direct browsing of the underlying vector collection (aeam_incident_memories) requires a
            dedicated backend endpoint that does not exist yet — everything above is real recall
            activity read from persisted investigations.
            {canCurate
              ? " Memory entries surfaced above can be corrected or withdrawn; every curation records who acted, why, and when in the audit trail."
              : " Correcting or withdrawing a memory entry is an administrator action and is not available to your role."}
          </p>
        </>
      )}
    </PageContainer>
  );
}
