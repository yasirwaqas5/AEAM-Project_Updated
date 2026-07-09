import { Badge, Collapsible, Icon, getEvidence } from "./ui";

/* ──────────────────────────────────────────────────────────────────────────
 * Evidence view — renders the retrieved RAG chunks recorded on an incident as
 * expandable cards. Uses the fields actually persisted by the RAG pipeline:
 * chunk_id, confidence (score), and the extracted cause text (preview).
 * (Retrieval `similarity`/full chunk `text` are not stored on the incident, so
 * the persisted per-cause confidence is shown as the relevance score.)
 * ────────────────────────────────────────────────────────────────────────── */

function scoreColor(v) {
  const pct = (v ?? 0) <= 1 ? (v ?? 0) * 100 : v;
  return pct >= 80 ? "#00ffa3" : pct >= 50 ? "#ffb800" : "#ff5f57";
}

function EvidenceCard({ chunk, index }) {
  const chunkId = chunk?.chunk_id ?? `chunk_${index}`;
  const score = chunk?.confidence;
  const preview = chunk?.cause ?? chunk?.text ?? "(no preview available)";
  const shortId = chunkId.length > 16 ? `${chunkId.slice(0, 14)}…` : chunkId;

  return (
    <div style={{
      border: "1px solid var(--border)", borderRadius: 10,
      background: "rgba(255,255,255,0.015)", overflow: "hidden",
    }}>
      <div style={{
        display: "flex", alignItems: "center", justifyContent: "space-between",
        padding: "0.7rem 0.9rem", borderBottom: "1px solid var(--border)", gap: "0.75rem",
      }}>
        <div style={{ display: "flex", alignItems: "center", gap: "0.55rem", minWidth: 0 }}>
          <Icon name="database" size={13} color="var(--muted)" />
          <span title={chunkId} style={{
            fontFamily: "var(--font-mono)", fontSize: "0.72rem", color: "var(--text)",
            overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
          }}>{shortId}</span>
        </div>
        {score != null && (
          <Badge label={`score ${Math.round((score <= 1 ? score * 100 : score))}%`} color={scoreColor(score)} />
        )}
      </div>

      <div style={{ padding: "0.75rem 0.9rem" }}>
        <div style={{ fontSize: "0.8rem", color: "var(--text)", lineHeight: 1.5 }}>
          {preview.length > 140 ? `${preview.slice(0, 140)}…` : preview}
        </div>

        {(preview.length > 140 || chunk?.chunk_id) && (
          <div style={{ marginTop: "0.6rem" }}>
            <Collapsible summary="Expand full chunk">
              <div style={{ display: "flex", flexDirection: "column", gap: "0.55rem", paddingTop: "0.4rem" }}>
                <div style={{ fontSize: "0.78rem", color: "var(--text)", lineHeight: 1.6 }}>{preview}</div>
                <div style={{ fontFamily: "var(--font-mono)", fontSize: "0.68rem", color: "var(--muted)", wordBreak: "break-all" }}>
                  chunk_id: {chunkId}
                </div>
              </div>
            </Collapsible>
          </div>
        )}
      </div>
    </div>
  );
}

export default function EvidencePanel({ incident }) {
  const evidence = getEvidence(incident);

  if (!evidence.length) {
    return (
      <div style={{
        textAlign: "center", padding: "2.5rem 1rem", color: "var(--muted)",
        fontSize: "0.82rem", border: "1px dashed var(--border)", borderRadius: 10,
      }}>
        <Icon name="search" size={22} color="var(--muted)" style={{ marginBottom: "0.6rem" }} />
        <div>No retrieved evidence recorded for this incident.</div>
      </div>
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "0.8rem" }}>
      <div style={{ fontSize: "0.72rem", color: "var(--muted)", letterSpacing: "0.06em" }}>
        {evidence.length} retrieved chunk{evidence.length !== 1 ? "s" : ""}
      </div>
      {evidence.map((chunk, i) => (
        <EvidenceCard key={chunk?.chunk_id ? `${chunk.chunk_id}-${i}` : i} chunk={chunk} index={i} />
      ))}
    </div>
  );
}
