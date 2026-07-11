import { PageHeader } from "../components/ui";
import { PageContainer, MetricCard, Panel, EmptyState, ComingSoon } from "../components/library";

export default function HumanReview() {
  return (
    <PageContainer>
      <PageHeader title="Human Review Queue" subtitle="Work the escalation backlog — assign, acknowledge, approve actions, resolve" />
      <div className="aeam-grid-metrics" style={{ marginBottom: "1.4rem" }}>
        <MetricCard label="In queue" value="—" icon="alert" accent="var(--warn)" sub="need attention" />
        <MetricCard label="Assigned" value="—" icon="shield" sub="to an operator" />
        <MetricCard label="Awaiting approval" value="—" icon="check" accent="var(--info)" sub="proposed actions" />
        <MetricCard label="Resolved today" value="—" icon="target" accent="var(--ok)" sub="closed with verdict" />
      </div>
      <Panel title="Review queue" icon="shield">
        <EmptyState icon="shield" title="Queue is not wired yet"
          description="Escalated incidents will list here with assign / acknowledge / approve / resolve actions." />
      </Panel>
      <div style={{ marginTop: "1.4rem" }}>
        <ComingSoon icon="shield" title="Human Review Queue" phase="A5"
          description="Ends the escalation dead-end — most incidents currently escalate with nowhere to go."
          points={[
            "Ack / assign / resolve on incidents, plus an approve-before-act gate.",
            "Adds status, assignee and verdict columns to the existing incidents table.",
            "Operator verdicts feed the learning loop in a later phase.",
          ]} />
      </div>
    </PageContainer>
  );
}
