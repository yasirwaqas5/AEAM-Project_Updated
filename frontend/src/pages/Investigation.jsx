import { PageHeader } from "../components/ui";
import { PageContainer, SplitLayout, Panel, EmptyState, TimelineContainer, ComingSoon } from "../components/library";

export default function Investigation() {
  return (
    <PageContainer>
      <PageHeader title="Investigation Workspace" subtitle="The unified “why” view — detection → decision → evidence → action as one causal chain" />
      <SplitLayout
        ratio="1.4fr 1fr"
        left={
          <Panel title="Causal chain" icon="branch">
            <TimelineContainer>
              <EmptyState icon="branch" title="Open an incident to investigate"
                description="Select an incident to walk its rule breach, retrieved evidence, reasoning and executed actions in one place." />
            </TimelineContainer>
          </Panel>
        }
        right={
          <Panel title="Cited evidence" icon="database">
            <EmptyState icon="database" title="No evidence loaded"
              description="Grounded, chunk-level citations for the active investigation appear here." />
          </Panel>
        }
      />
      <div style={{ marginTop: "1.4rem" }}>
        <ComingSoon icon="branch" title="Investigation Workspace" phase="A4"
          description="Composes the existing EvidencePanel + Timeline + audit_summary into a single causal narrative."
          points={[
            "Reuses the existing Evidence and Timeline components — no new backend.",
            "Reads the consolidated audit_summary already persisted per incident.",
            "Adds a similar-incident sidebar once Memory (Phase C) is live.",
          ]} />
      </div>
    </PageContainer>
  );
}
