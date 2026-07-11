import { PageHeader } from "../components/ui";
import { PageContainer, MetricCard, Panel, DataTable, ComingSoon } from "../components/library";

const COLUMNS = [
  { key: "doc", label: "Document" },
  { key: "source", label: "Source" },
  { key: "version", label: "Version" },
  { key: "chunks", label: "Chunks", align: "right" },
  { key: "freshness", label: "Freshness" },
  { key: "status", label: "Status" },
];

export default function KnowledgeCenter() {
  return (
    <PageContainer>
      <PageHeader title="Knowledge Center" subtitle="Manage the corpus the retrieval stack depends on — documents, versions, freshness" />
      <div className="aeam-grid-metrics" style={{ marginBottom: "1.4rem" }}>
        <MetricCard label="Documents" value="—" icon="database" sub="registered sources" />
        <MetricCard label="Chunks" value="—" icon="layers" sub="in vector index" />
        <MetricCard label="Sources" value="—" icon="branch" sub="connected systems" />
        <MetricCard label="Stale" value="—" icon="clock" accent="var(--warn)" sub="past review-by" />
      </div>
      <Panel title="Document library" icon="database" pad={false}
        right={<span style={{ fontSize: ".62rem", color: "var(--muted)", fontFamily: "var(--font-mono)" }}>read-only preview</span>}>
        <DataTable columns={COLUMNS} rows={[]} empty="No documents ingested yet — runtime ingestion arrives in Phase A3." />
      </Panel>
      <div style={{ marginTop: "1.4rem" }}>
        <ComingSoon icon="database" title="Knowledge Center" phase="A3"
          description="Runtime ingestion, versioning and freshness over the existing embedding + Qdrant pipeline."
          points={[
            "Runtime ingestion API + BM25 rebuild hook (today ingestion is startup-only).",
            "Document/source registry: doc → version → source → owner → review-by.",
            "Supersede-and-delete on re-ingest so changed docs never orphan old vectors.",
            "Chunk viewer, coverage heatmap and near-duplicate detection.",
          ]} />
      </div>
    </PageContainer>
  );
}
