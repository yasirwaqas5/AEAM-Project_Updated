import { useState, useEffect, useCallback, useMemo } from "react";
import { useSearchParams } from "react-router-dom";
import {
  PageHeader, Badge, SeverityBadge, Field, Icon, Button, Collapsible,
  fmtRelative, fmtMs, fmtPct, severityOf,
  getQueryAttempts, getTopEvidence, getRetrievedCount,
} from "../components/ui";
import {
  PageContainer, SplitLayout, MetricCard, Panel, EmptyState, LoadingState, ErrorState,
} from "../components/library";
import { SearchBox } from "./KnowledgeCenter";
import { IncidentPicker } from "./Investigation";
import EvidencePanel from "../components/EvidencePanel";

/* ──────────────────────────────────────────────────────────────────────────
 * pages/RetrievalExplorer.jsx  (Enterprise Retrieval Explorer)
 *
 * Explains exactly how AEAM's RAG pipeline retrieved evidence — composed
 * ONLY from already-existing pieces, two genuinely different scopes shown
 * side by side and never conflated:
 *
 *   - HISTORICAL evidence — what actually happened for this incident at
 *     investigation time. Read straight from the incident row via ui.jsx's
 *     existing helpers (getQueryAttempts/getTopEvidence/getRetrievedCount)
 *     and rendered with EvidencePanel (components/EvidencePanel.jsx),
 *     reused verbatim, unmodified.
 *   - LIVE trace — an on-demand re-run of the incident's original query
 *     through GET /api/v1/debug/retrieval (aeam/api/retrieval_debug.py,
 *     developer-only, already exists, never modified), showing every
 *     pipeline stage (expansion, dense, BM25, RRF fusion, reranking,
 *     diversity) via the SAME RetrievalDebugTracer that already replays the
 *     real production components. Explicitly labeled as a live re-trace
 *     against the CURRENT index/corpus — it can legitimately differ from
 *     the historical evidence above if anything changed since the incident,
 *     and the UI says so rather than implying they're the same thing.
 *
 * RAGAgent does not persist the assembled prompt or the final context string
 * sent to the LLM (aeam/agents/rag/rag_agent.py's findings dict has no such
 * field — only `raw_llm_response`, the LLM's reply, is kept). Prompt Context
 * and Final Context sections are therefore shown as explicitly unavailable,
 * never reconstructed/fabricated.
 * ────────────────────────────────────────────────────────────────────────── */

async function fetchJSON(url, options) {
  const res = await fetch(url, options);
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try { detail = (await res.json())?.detail || detail; } catch { /* ignore */ }
    throw new Error(detail);
  }
  return res.json();
}

const fetchIncidents = () => fetchJSON("/api/v1/incidents/");
const fetchTrace = (query, topK) =>
  fetchJSON(`/api/v1/debug/retrieval/?query=${encodeURIComponent(query)}&top_k=${topK}`);

// ─── Small building blocks ─────────────────────────────────────────────────

function scoreColor(pct) {
  return pct >= 80 ? "#00ffa3" : pct >= 50 ? "#ffb800" : "#ff5f57";
}

/** Compact chunk row for LIVE TRACE stages — distinct shape from EvidencePanel's
 *  incident-evidence cards (chunk_id/source/text_preview/*_score/final_rank),
 *  so it is genuinely new rendering, not a duplicate of EvidenceCard. */
function TraceChunkRow({ chunk, scoreLabel, scoreValue }) {
  const shortId = chunk.chunk_id ? (chunk.chunk_id.length > 16 ? `${chunk.chunk_id.slice(0, 14)}…` : chunk.chunk_id) : "unknown";
  return (
    <div style={{
      display: "flex", alignItems: "center", justifyContent: "space-between", gap: "0.75rem",
      padding: "0.5rem 0.7rem", border: "1px solid var(--border)", borderRadius: 8,
      background: "rgba(255,255,255,0.015)",
    }}>
      <div style={{ display: "flex", alignItems: "center", gap: "0.55rem", minWidth: 0 }}>
        <Icon name="database" size={12} color="var(--muted)" />
        <span title={chunk.chunk_id} style={{ fontFamily: "var(--font-mono)", fontSize: "0.7rem", color: "var(--text)" }}>{shortId}</span>
        {chunk.source && <span style={{ fontSize: "0.68rem", color: "var(--muted)" }}>· {chunk.source}</span>}
      </div>
      {scoreValue != null && <Badge label={`${scoreLabel} ${typeof scoreValue === "number" ? scoreValue.toFixed(3) : scoreValue}`} color="var(--info, #00b4ff)" />}
    </div>
  );
}

function StageCard({ title, icon, count, timingMs, note, chunks, scoreKey, scoreLabel }) {
  return (
    <div style={{ border: "1px solid var(--border)", borderRadius: 10, padding: "0.9rem", display: "flex", flexDirection: "column", gap: "0.6rem" }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
        <div style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
          <Icon name={icon} size={14} color="var(--muted)" />
          <span style={{ fontSize: "0.82rem", fontWeight: 600, color: "var(--text)" }}>{title}</span>
        </div>
        <Badge label={`${count} chunk${count !== 1 ? "s" : ""}`} color={count > 0 ? "var(--accent)" : "var(--muted)"} />
      </div>
      {timingMs != null && <div style={{ fontSize: "0.7rem", color: "var(--muted)", fontFamily: "var(--font-mono)" }}>{fmtMs(timingMs)}</div>}
      {note && <div style={{ fontSize: "0.72rem", color: "var(--muted)", fontStyle: "italic" }}>{note}</div>}
      {chunks && chunks.length > 0 && (
        <Collapsible summary={`Show ${chunks.length} chunk${chunks.length !== 1 ? "s" : ""}`}>
          <div style={{ display: "flex", flexDirection: "column", gap: "0.4rem", paddingTop: "0.3rem" }}>
            {chunks.map((c, i) => (
              <TraceChunkRow key={c.chunk_id || i} chunk={c} scoreLabel={scoreLabel} scoreValue={c[scoreKey]} />
            ))}
          </div>
        </Collapsible>
      )}
    </div>
  );
}

function Unavailable({ children }) {
  return (
    <div style={{
      display: "flex", alignItems: "flex-start", gap: "0.5rem", fontSize: "0.76rem", color: "var(--muted)",
      fontStyle: "italic", padding: "0.7rem 0.9rem", border: "1px dashed var(--border)", borderRadius: 8,
    }}>
      <Icon name="alert" size={13} style={{ marginTop: 1, opacity: 0.7 }} />
      <span>{children}</span>
    </div>
  );
}

// ─── Page ───────────────────────────────────────────────────────────────────

export default function RetrievalExplorer() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [incidents, setIncidents] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [search, setSearch] = useState("");

  const [trace, setTrace] = useState(null);
  const [traceLoading, setTraceLoading] = useState(false);
  const [traceError, setTraceError] = useState(null);
  const [topK, setTopK] = useState(5);

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
  const selectIncident = (id) => { setSearchParams({ id }); setTrace(null); setTraceError(null); };

  const incident = useMemo(() => incidents.find((i) => i.incident_id === selectedId) || null, [incidents, selectedId]);
  const queryAttempts = useMemo(() => (incident ? getQueryAttempts(incident) : []), [incident]);
  const originalQuery = queryAttempts[0]?.query || null;
  const retrievedCount = incident ? getRetrievedCount(incident) : 0;
  const topEvidence = useMemo(() => (incident ? getTopEvidence(incident) : []), [incident]);
  const sources = useMemo(() => [...new Set(topEvidence.map((e) => e.source).filter(Boolean))], [topEvidence]);
  const avgSimilarity = useMemo(() => {
    const sims = topEvidence.map((e) => e.similarity).filter((s) => typeof s === "number");
    if (sims.length === 0) return null;
    return sims.reduce((a, b) => a + b, 0) / sims.length;
  }, [topEvidence]);

  const runTrace = useCallback(async () => {
    if (!originalQuery) return;
    setTraceLoading(true); setTraceError(null); setTrace(null);
    try {
      const data = await fetchTrace(originalQuery, topK);
      setTrace(data);
    } catch (e) {
      setTraceError(e.message);
    } finally {
      setTraceLoading(false);
    }
  }, [originalQuery, topK]);

  const stageSurvivalCounts = useMemo(() => {
    if (!trace?.stage_survival) return null;
    const counts = { fusion: 0, reranker: 0, evidence_diversity: 0, survived: 0 };
    for (const s of trace.stage_survival) {
      if (s.removed_at_stage) counts[s.removed_at_stage] = (counts[s.removed_at_stage] || 0) + 1;
      else counts.survived += 1;
    }
    return counts;
  }, [trace]);

  if (loading) {
    return (
      <PageContainer max={1400}>
        <PageHeader title="Retrieval Explorer" subtitle="Trace exactly how AEAM's RAG pipeline retrieved evidence for an investigation" />
        <LoadingState label="Loading incidents…" rows={5} />
      </PageContainer>
    );
  }

  if (error) {
    return (
      <PageContainer max={1400}>
        <PageHeader title="Retrieval Explorer" subtitle="Trace exactly how AEAM's RAG pipeline retrieved evidence for an investigation"
          right={<Button icon="activity" onClick={load}>Retry</Button>} />
        <ErrorState message={error} onRetry={load} />
      </PageContainer>
    );
  }

  return (
    <PageContainer max={1400}>
      <PageHeader title="Retrieval Explorer" subtitle="Trace exactly how AEAM's RAG pipeline retrieved evidence for an investigation" />

      <SplitLayout ratio="320px 1fr" left={
        <Panel title="Incidents" icon="search" pad={false}>
          <div style={{ padding: "0.9rem" }}>
            <IncidentPicker incidents={incidents} selectedId={selectedId} onSelect={selectIncident} search={search} onSearch={setSearch} />
          </div>
        </Panel>
      } right={!incident ? (
        <EmptyState icon="search" title="Select an incident" description="Choose an incident on the left to explore how its evidence was retrieved." />
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: "1.4rem" }}>

          {/* 1. Retrieval Overview */}
          <Panel title="Retrieval Overview" icon="search">
            <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
              <div>
                <span style={{ fontSize: "0.62rem", textTransform: "uppercase", letterSpacing: "0.12em", color: "var(--muted)" }}>Query</span>
                <div style={{ fontFamily: "var(--font-mono)", fontSize: "0.86rem", color: "var(--text)", marginTop: "0.25rem" }}>
                  {originalQuery || "—"}
                </div>
              </div>
              <div style={{ display: "flex", alignItems: "center", gap: "0.6rem", flexWrap: "wrap" }}>
                <span style={{ fontSize: "0.62rem", textTransform: "uppercase", letterSpacing: "0.12em", color: "var(--muted)" }}>Investigation</span>
                <SeverityBadge severity={incident.severity} />
                <span style={{ fontSize: "0.78rem", color: "var(--text)" }}>{incident.event_type || "—"}</span>
                <span style={{ fontFamily: "var(--font-mono)", fontSize: "0.7rem", color: "var(--muted)" }}>{incident.incident_id}</span>
                <span style={{ fontSize: "0.7rem", color: "var(--muted)" }}>{fmtRelative(incident.timestamp)}</span>
              </div>
              <div className="aeam-grid-auto">
                <MetricCard label="Retrieved Chunks" icon="database" value={retrievedCount} />
                <MetricCard label="Sources" icon="layers" value={sources.length} />
                <MetricCard label="Average Similarity" icon="target" value={avgSimilarity != null ? fmtPct(avgSimilarity) : "—"} />
              </div>
            </div>
          </Panel>

          {/* 2. Query Details */}
          <Panel title="Query Details" icon="code">
            {queryAttempts.length === 0 ? (
              <Unavailable>RAG was not invoked for this investigation — there are no query attempts to show.</Unavailable>
            ) : (
              <div style={{ display: "flex", flexDirection: "column", gap: "0.6rem" }}>
                {queryAttempts.map((a, i) => (
                  <div key={i} style={{ border: "1px solid var(--border)", borderRadius: 9, padding: "0.7rem 0.9rem", background: "rgba(255,255,255,0.015)" }}>
                    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "0.35rem" }}>
                      <span style={{ fontSize: "0.78rem", fontWeight: 600, color: "var(--text)" }}>
                        Attempt {a.attempt ?? i + 1}{a.strategy && a.strategy !== "original" ? ` — ${a.strategy}` : ""}
                      </span>
                      <Badge label={`Retrieved: ${a.retrieved_count ?? 0}`} color={(a.retrieved_count ?? 0) > 0 ? "#00ffa3" : "#5a5f72"} />
                    </div>
                    <div style={{ fontFamily: "var(--font-mono)", fontSize: "0.78rem", color: "var(--text)" }}>{a.query || "—"}</div>
                    {a.threshold != null && <div style={{ fontSize: "0.68rem", color: "var(--muted)", marginTop: "0.3rem" }}>Similarity threshold: {a.threshold}</div>}
                  </div>
                ))}
              </div>
            )}
          </Panel>

          {/* 3. Retrieval Pipeline (live trace) */}
          <Panel title="Retrieval Pipeline" icon="branch"
            right={<span style={{ fontSize: "0.62rem", color: "var(--muted)", fontFamily: "var(--font-mono)" }}>GET /api/v1/debug/retrieval</span>}>
            <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
              <div style={{
                fontSize: "0.72rem", color: "var(--muted)", fontStyle: "italic",
                border: "1px dashed var(--border)", borderRadius: 8, padding: "0.6rem 0.8rem",
              }}>
                This re-runs the query above against the CURRENT index via the developer-only retrieval debug tracer —
                it replays the real production pipeline components live, so results can legitimately differ from the
                historical evidence recorded at investigation time if the corpus or RAG settings changed since.
              </div>

              {!originalQuery ? (
                <Unavailable>No original query recorded for this incident — nothing to trace.</Unavailable>
              ) : (
                <div style={{ display: "flex", alignItems: "center", gap: "0.7rem", flexWrap: "wrap" }}>
                  <Button icon="branch" variant="primary" onClick={runTrace} disabled={traceLoading}>
                    {traceLoading ? "Tracing…" : "Trace Retrieval Pipeline"}
                  </Button>
                  <Field label="Top K" value={
                    <input type="number" min={1} max={50} value={topK}
                      onChange={(e) => setTopK(Math.max(1, Math.min(50, Number(e.target.value) || 1)))}
                      style={{
                        width: 60, background: "var(--surface)", border: "1px solid var(--border)",
                        borderRadius: 6, color: "var(--text)", padding: "0.2rem 0.4rem", fontFamily: "var(--font-mono)",
                      }} />
                  } />
                </div>
              )}

              {traceLoading && <LoadingState label="Replaying the retrieval pipeline for this query…" rows={4} />}
              {traceError && <ErrorState message={traceError} onRetry={runTrace} />}

              {trace && (
                <>
                  <div className="aeam-grid-2" style={{ gap: "0.8rem" }}>
                    <StageCard title="Query" icon="search" count={trace.expanded_queries?.length ?? 0}
                      timingMs={trace.timings_ms?.query_expansion_ms}
                      note={trace.expanded_queries?.length > 1 ? "Expanded into multiple variants (LLM query expansion)." : "No query expansion — single variant."}
                      chunks={null} />
                    <StageCard title="Embedding + Dense Search" icon="target" count={trace.dense_results?.length ?? 0}
                      timingMs={trace.timings_ms?.embedding_search_ms}
                      note="Qdrant vector similarity search (embedding happens as part of this stage — the trace does not expose a separate embedding-only step)."
                      chunks={trace.dense_results} scoreKey="dense_similarity" scoreLabel="sim" />
                    <StageCard title="BM25" icon="layers" count={trace.bm25_results?.length ?? 0}
                      timingMs={trace.timings_ms?.bm25_search_ms}
                      note={(trace.bm25_results?.length ?? 0) === 0 ? "No BM25 results — hybrid retrieval may be disabled (RAG_HYBRID_ENABLED)." : undefined}
                      chunks={trace.bm25_results} scoreKey="bm25_score" scoreLabel="bm25" />
                    <StageCard title="Hybrid Merge (RRF)" icon="branch" count={trace.rrf_fused?.length ?? 0}
                      timingMs={trace.timings_ms?.rrf_fusion_ms}
                      chunks={trace.rrf_fused} scoreKey="rrf_score" scoreLabel="rrf" />
                    <StageCard title="Reranking" icon="code" count={trace.reranked?.length ?? 0}
                      timingMs={trace.timings_ms?.reranking_ms}
                      note={trace.reranked === trace.rrf_fused ? "Reranker disabled — pass-through from hybrid merge." : undefined}
                      chunks={trace.reranked} scoreKey="rerank_score" scoreLabel="rerank" />
                    <StageCard title="Selected Evidence" icon="check" count={trace.final_chunks?.length ?? 0}
                      timingMs={trace.timings_ms?.diversity_ms}
                      note="Exactly what RAGAgent receives, after evidence-diversity filtering."
                      chunks={trace.final_chunks} scoreKey="similarity" scoreLabel="sim" />
                  </div>

                  {/* 7. Retrieval Statistics */}
                  <Panel title="Retrieval Statistics" icon="activity">
                    <div className="aeam-grid-auto">
                      {Object.entries(trace.timings_ms || {}).map(([k, v]) => (
                        <Field key={k} label={k.replace(/_ms$/, "").replace(/_/g, " ")} value={fmtMs(v)} mono />
                      ))}
                    </div>
                    {stageSurvivalCounts && (
                      <div style={{ marginTop: "0.9rem" }}>
                        <span style={{ fontSize: "0.62rem", textTransform: "uppercase", letterSpacing: "0.12em", color: "var(--muted)" }}>
                          Where chunks were dropped
                        </span>
                        <div className="aeam-grid-auto" style={{ marginTop: "0.4rem" }}>
                          <Field label="Survived every stage" value={stageSurvivalCounts.survived} mono color="#00ffa3" />
                          <Field label="Dropped at fusion" value={stageSurvivalCounts.fusion} mono />
                          <Field label="Dropped by reranker" value={stageSurvivalCounts.reranker} mono />
                          <Field label="Dropped by diversity filter" value={stageSurvivalCounts.evidence_diversity} mono />
                        </div>
                      </div>
                    )}
                  </Panel>

                  {/* 5. Prompt Context */}
                  <Panel title="Prompt Context" icon="code">
                    <Unavailable>
                      Not persisted. RAGAgent assembles the LLM prompt at investigation time (see
                      <code style={{ fontFamily: "var(--font-mono)" }}> _assemble_prompt</code> in aeam/agents/rag/rag_agent.py)
                      but does not store the assembled prompt text anywhere — only the retrieved chunks (above) and the
                      LLM's raw response are retained.
                    </Unavailable>
                  </Panel>

                  {/* 6. Final Context supplied to the LLM */}
                  <Panel title="Final Context Supplied to the LLM" icon="database">
                    <Unavailable>
                      Not separately available. The underlying chunk text is shown above in each stage and in Retrieved
                      Chunks below — but the exact, formatted context string RAGAgent concatenated into the prompt is
                      not stored, so it is not reconstructed here to avoid showing something that may not byte-match
                      what was actually sent.
                    </Unavailable>
                  </Panel>
                </>
              )}
            </div>
          </Panel>

          {/* 4. Retrieved Chunks — historical, as recorded at investigation time */}
          <Panel title="Retrieved Chunks" icon="database"
            right={<span style={{ fontSize: "0.62rem", color: "var(--muted)", fontFamily: "var(--font-mono)" }}>as recorded at investigation time</span>}>
            <EvidencePanel incident={incident} />
          </Panel>
        </div>
      )} />
    </PageContainer>
  );
}
