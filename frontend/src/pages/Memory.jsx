import { useState, useEffect, useCallback, useMemo, lazy, Suspense } from "react";
import { Link } from "react-router-dom";
import { PageHeader, Badge, Button, fmtRelative, getMemoryMatches, getMemoryData } from "../components/ui";
import { PageContainer, Panel, DataTable, MetricCard, EmptyState, LoadingState, ErrorState } from "../components/library";
import { CountUp } from "../components/charts";

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

export default function Memory() {
  const [incidents, setIncidents] = useState([]);
  const [obs, setObs] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

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
              ]}
              rows={recalls}
            />
          </Panel>

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
          </p>
        </>
      )}
    </PageContainer>
  );
}
