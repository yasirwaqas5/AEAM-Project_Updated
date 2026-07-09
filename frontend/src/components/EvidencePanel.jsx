import {
  Badge, Collapsible, Icon,
  getQueryAttempts, getRetrievedCount, getTopEvidence, getValidationStatus, fmtPct,
} from "./ui";

/* ──────────────────────────────────────────────────────────────────────────
 * Evidence view.
 *
 * When retrieval found nothing: a structured Retrieval Summary listing every
 * deterministic query attempt (original → rewritten → broadened) with its
 * threshold and retrieved count — built entirely from real, persisted
 * attempt data (aeam/agents/rag/rag_agent.py's query_attempt/query_strategy
 * fields via the orchestrator's audit_summary). Never fabricated.
 *
 * When retrieval succeeded: Top Evidence cards ranked by similarity, each
 * showing chunk_id, similarity, confidence, source, and why it was (or
 * wasn't) selected as a cited cause.
 * ────────────────────────────────────────────────────────────────────────── */

function scoreColor(pct) {
  return pct >= 80 ? "#00ffa3" : pct >= 50 ? "#ffb800" : "#ff5f57";
}

// ─── Retrieval Summary (no evidence case) ───────────────────────────────────

function RetrievalSummary({ incident }) {
  const attempts = getQueryAttempts(incident);
  const validation = getValidationStatus(incident);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
      <div style={{ display: "flex", alignItems: "center", gap: "0.5rem", color: "var(--muted)", fontSize: "0.78rem" }}>
        <Icon name="search" size={14} />
        <span style={{ fontWeight: 600, letterSpacing: "0.04em" }}>Retrieval Summary</span>
      </div>

      {attempts.length === 0 ? (
        <div style={{
          textAlign: "center", padding: "2rem 1rem", color: "var(--muted)",
          fontSize: "0.8rem", border: "1px dashed var(--border)", borderRadius: 10,
        }}>
          RAG was not invoked for this investigation.
        </div>
      ) : (
        <>
          <div style={{ display: "flex", flexDirection: "column", gap: "0.3rem" }}>
            <span style={{ fontSize: "0.62rem", textTransform: "uppercase", letterSpacing: "0.12em", color: "var(--muted)" }}>
              Original Query
            </span>
            <span style={{ fontFamily: "var(--font-mono)", fontSize: "0.78rem", color: "var(--text)" }}>
              {attempts[0]?.query || "—"}
            </span>
          </div>

          <div style={{ display: "flex", flexDirection: "column", gap: "0.6rem" }}>
            {attempts.map((a, i) => (
              <div key={i} style={{
                border: "1px solid var(--border)", borderRadius: 9,
                background: "rgba(255,255,255,0.015)", padding: "0.7rem 0.9rem",
              }}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "0.4rem" }}>
                  <span style={{ fontSize: "0.78rem", fontWeight: 600, color: "var(--text)" }}>
                    Attempt {a.attempt ?? i + 1}
                    {a.strategy && a.strategy !== "original" && (
                      <span style={{ color: "var(--muted)", fontWeight: 400 }}> — {a.strategy}</span>
                    )}
                  </span>
                  <Badge label={`Retrieved: ${a.retrieved_count ?? 0}`} color={(a.retrieved_count ?? 0) > 0 ? "#00ffa3" : "#5a5f72"} />
                </div>
                {i > 0 && a.query && (
                  <div style={{ fontFamily: "var(--font-mono)", fontSize: "0.74rem", color: "var(--text)", marginBottom: "0.3rem" }}>
                    {a.query}
                  </div>
                )}
                {a.threshold != null && (
                  <div style={{ fontSize: "0.7rem", color: "var(--muted)" }}>Threshold: {a.threshold}</div>
                )}
              </div>
            ))}
          </div>

          <div style={{
            borderTop: "1px solid var(--border)", paddingTop: "0.8rem",
            display: "flex", flexDirection: "column", gap: "0.3rem",
          }}>
            <span style={{ fontSize: "0.62rem", textTransform: "uppercase", letterSpacing: "0.12em", color: "var(--muted)" }}>
              Final Result
            </span>
            <span style={{ fontSize: "0.8rem", color: "var(--text)" }}>
              No relevant documents matched.
              {incident?.requires_human ? " Escalated to human." : ""}
            </span>
          </div>
        </>
      )}

      <div style={{
        display: "flex", alignItems: "center", gap: "0.5rem",
        fontSize: "0.74rem", color: validation.status === "PASSED" ? "#00ffa3" : "#ff8f88",
      }}>
        <Icon name={validation.status === "PASSED" ? "check" : "alert"} size={13} />
        Validation: {validation.status} — {validation.reason}
      </div>
    </div>
  );
}

// ─── Top Evidence (has evidence case) ───────────────────────────────────────

function EvidenceCard({ item }) {
  const chunkId = item.chunk_id ?? "unknown";
  const shortId = chunkId.length > 16 ? `${chunkId.slice(0, 14)}…` : chunkId;
  const simPct = item.similarity != null ? Math.round((item.similarity <= 1 ? item.similarity * 100 : item.similarity)) : null;

  return (
    <div style={{
      border: "1px solid var(--border)", borderRadius: 10,
      background: "rgba(255,255,255,0.015)", overflow: "hidden",
    }}>
      <div style={{
        display: "flex", alignItems: "center", justifyContent: "space-between",
        padding: "0.7rem 0.9rem", borderBottom: "1px solid var(--border)", gap: "0.75rem", flexWrap: "wrap",
      }}>
        <div style={{ display: "flex", alignItems: "center", gap: "0.55rem", minWidth: 0 }}>
          <Icon name="database" size={13} color="var(--muted)" />
          <span title={chunkId} style={{
            fontFamily: "var(--font-mono)", fontSize: "0.72rem", color: "var(--text)",
            overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
          }}>{shortId}</span>
        </div>
        <div style={{ display: "flex", gap: "0.4rem", flexWrap: "wrap" }}>
          {simPct != null && <Badge label={`similarity ${simPct}%`} color={scoreColor(simPct)} />}
          {item.confidence != null && <Badge label={`confidence ${fmtPct(item.confidence)}`} color={scoreColor(item.confidence <= 1 ? item.confidence * 100 : item.confidence)} />}
        </div>
      </div>

      <div style={{ padding: "0.75rem 0.9rem", display: "flex", flexDirection: "column", gap: "0.5rem" }}>
        <div style={{ fontSize: "0.8rem", color: "var(--text)", lineHeight: 1.5 }}>
          {(item.preview || "").length > 140 ? `${item.preview.slice(0, 140)}…` : (item.preview || "(no preview)")}
        </div>

        <div style={{ display: "flex", gap: "1.2rem", flexWrap: "wrap", fontSize: "0.7rem", color: "var(--muted)" }}>
          {item.source && <span>Source: {item.source}</span>}
          <span style={{ color: item.cited ? "#00ffa3" : "var(--muted)" }}>{item.reasonSelected}</span>
        </div>

        <Collapsible summary="Expand full chunk">
          <div style={{ display: "flex", flexDirection: "column", gap: "0.55rem", paddingTop: "0.4rem" }}>
            <div style={{ fontSize: "0.78rem", color: "var(--text)", lineHeight: 1.6 }}>{item.preview}</div>
            <div style={{ fontFamily: "var(--font-mono)", fontSize: "0.68rem", color: "var(--muted)", wordBreak: "break-all" }}>
              chunk_id: {chunkId}
            </div>
          </div>
        </Collapsible>
      </div>
    </div>
  );
}

// ─── Panel ──────────────────────────────────────────────────────────────────

export default function EvidencePanel({ incident }) {
  const retrievedCount = getRetrievedCount(incident);
  const topEvidence = getTopEvidence(incident);

  if (retrievedCount === 0 || topEvidence.length === 0) {
    return <RetrievalSummary incident={incident} />;
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "0.8rem" }}>
      <div style={{ fontSize: "0.72rem", color: "var(--muted)", letterSpacing: "0.06em" }}>
        Top Evidence — {topEvidence.length} chunk{topEvidence.length !== 1 ? "s" : ""}, ranked by similarity
      </div>
      {topEvidence.map((item, i) => (
        <EvidenceCard key={item.chunk_id ? `${item.chunk_id}-${i}` : i} item={item} />
      ))}
    </div>
  );
}
