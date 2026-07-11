import { PageHeader, Icon } from "../components/ui";
import { PageContainer, Panel, GraphPlaceholder, ComingSoon } from "../components/library";

export default function RetrievalExplorer() {
  return (
    <PageContainer>
      <PageHeader title="Retrieval Explorer" subtitle="Trace the retrieval pipeline stage by stage — the debug API already exists" />
      <Panel title="Query" icon="search">
        <div className="aeam-search" style={{ maxWidth: "none", opacity: .6, cursor: "not-allowed" }}>
          <Icon name="search" size={14} />
          <input placeholder="Trace a query through dense · BM25 · RRF · rerank · diversity…" disabled aria-label="Retrieval query" />
        </div>
        <p style={{ margin: ".8rem 0 0", fontSize: ".72rem", color: "var(--muted)" }}>
          Wires to the existing <code style={{ fontFamily: "var(--font-mono)" }}>GET /api/v1/debug/retrieval</code> endpoint.
        </p>
      </Panel>
      <div style={{ marginTop: "1.4rem" }}>
        <GraphPlaceholder title="Stage survival" height={200} note="which chunks each stage kept or dropped" />
      </div>
      <div style={{ marginTop: "1.4rem" }}>
        <ComingSoon icon="search" title="Retrieval Explorer" phase="A6"
          description="A UI over the retrieval debug endpoint that already returns per-stage traces — the cheapest visible win."
          points={[
            "Expanded queries, per-stage result columns, per-chunk scores.",
            "Stage-survival view: why a chunk won or was dropped.",
            "Side-by-side A/B across the RAG feature flags.",
          ]} />
      </div>
    </PageContainer>
  );
}
