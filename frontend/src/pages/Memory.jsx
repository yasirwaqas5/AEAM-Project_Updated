import { PageHeader } from "../components/ui";
import { PageContainer, Panel, EmptyState, ComingSoon } from "../components/library";

export default function Memory() {
  return (
    <PageContainer>
      <PageHeader title="Memory Center" subtitle="Browse embedded incident history — “have we seen this before?”" />
      <Panel title="Similar incidents" icon="layers">
        <EmptyState icon="layers" title="Incident memory is not active yet"
          description="Incidents are persisted but not embedded — the LongTermMemory vector client is a no-op today." />
      </Panel>
      <div style={{ marginTop: "1.4rem" }}>
        <ComingSoon icon="layers" title="Memory Center" phase="C"
          description="Activates the existing (no-op) vector memory so past incidents become retrievable evidence."
          points={[
            "Point LongTermMemory’s vector client at the existing Qdrant.",
            "Similar-incident search and resolution reuse.",
            "The compounding data moat — AEAM cites its own resolved history.",
          ]} />
      </div>
    </PageContainer>
  );
}
