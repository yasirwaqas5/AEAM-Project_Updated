import { PageHeader } from "../components/ui";
import { PageContainer, Panel, DataTable, ComingSoon } from "../components/library";

const COLUMNS = [
  { key: "action", label: "Action" },
  { key: "incident", label: "Incident" },
  { key: "status", label: "Status" },
  { key: "retries", label: "Retries", align: "right" },
  { key: "when", label: "Executed" },
];

export default function Actions() {
  return (
    <PageContainer>
      <PageHeader title="Action History" subtitle="Everything AEAM did to the outside world — Slack, Jira, diagnostics, monitoring" />
      <Panel title="Executed actions" icon="zap" pad={false}
        right={<span style={{ fontSize: ".62rem", color: "var(--muted)", fontFamily: "var(--font-mono)" }}>reads action_logs</span>}>
        <DataTable columns={COLUMNS} rows={[]} empty="Action history view wires to the existing action_logs table in Phase A5." />
      </Panel>
      <div style={{ marginTop: "1.4rem" }}>
        <ComingSoon icon="zap" title="Action History" phase="A5"
          description="A read view over the action_logs table the Action Agent already writes."
          points={[
            "Status, idempotency key, retry count and incident link per action.",
            "Only actions ActionAgent confirmed SUCCESS are shown as executed.",
            "Adds an approval trail once the approval gate lands.",
          ]} />
      </div>
    </PageContainer>
  );
}
