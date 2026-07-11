import { PageHeader } from "../components/ui";
import { PageContainer, Panel, EmptyState, ComingSoon } from "../components/library";

export default function Replay() {
  return (
    <PageContainer>
      <PageHeader title="Replay Center" subtitle="Re-run a past event through the current pipeline in shadow mode — no side effects" />
      <Panel title="Replayable events" icon="play">
        <EmptyState icon="play" title="Nothing selected"
          description="Pick a historical event to dry-run it through the current rules, retrieval and runbooks, then diff then-vs-now." />
      </Panel>
      <div style={{ marginTop: "1.4rem" }}>
        <ComingSoon icon="play" title="Replay Center" phase="C"
          description="Validate rule and runbook changes against real historical events before they go live."
          points={[
            "Event payloads are already persisted — replay needs no new storage.",
            "Dry-run mode suppresses all outbound actions.",
            "Diff view compares the original outcome to the current pipeline’s.",
          ]} />
      </div>
    </PageContainer>
  );
}
