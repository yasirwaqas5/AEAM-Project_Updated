import { PageHeader } from "../components/ui";
import { PageContainer, Panel, DataTable, ComingSoon } from "../components/library";

const COLUMNS = [
  { key: "setting", label: "Setting" },
  { key: "scope", label: "Scope" },
  { key: "value", label: "Current" },
  { key: "source", label: "Source" },
];

const GROUPS = [
  { setting: "RAG feature flags", scope: "Retrieval", value: "hybrid · rerank · multi-query · diversity", source: ".env" },
  { setting: "Detection thresholds", scope: "Monitor", value: "—", source: ".env" },
  { setting: "Channels", scope: "Action", value: "Slack · Jira · Email", source: ".env" },
  { setting: "LLM provider", scope: "Reasoning", value: "—", source: ".env" },
];

export default function Settings() {
  return (
    <PageContainer>
      <PageHeader title="Settings" subtitle="Operator-visible configuration — every knob is .env-only today" />
      <Panel title="Configuration groups" icon="code" pad={false}
        right={<span style={{ fontSize: ".62rem", color: "var(--muted)", fontFamily: "var(--font-mono)" }}>read-only preview</span>}>
        <DataTable columns={COLUMNS} rows={GROUPS} empty="No settings surfaced." rowKey={(r) => r.setting} />
      </Panel>
      <div style={{ marginTop: "1.4rem" }}>
        <ComingSoon icon="code" title="Settings" phase="A7"
          description="Surfaces the existing configuration so operators stop editing .env by hand."
          points={[
            "Toggle RAG stages, thresholds and channels from the UI.",
            "Model / provider selection and credential-health surfacing.",
            "Backed by a config table — no framework, no backend redesign.",
          ]} />
      </div>
    </PageContainer>
  );
}
