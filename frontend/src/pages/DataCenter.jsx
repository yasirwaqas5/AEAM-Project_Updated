import { PageHeader } from "../components/ui";
import { PageContainer, Panel, DataTable, ComingSoon } from "../components/library";

const COLUMNS = [
  { key: "name", label: "Connection" },
  { key: "kind", label: "Type" },
  { key: "metrics", label: "Metrics", align: "right" },
  { key: "health", label: "Health" },
  { key: "synced", label: "Last synced" },
];

export default function DataCenter() {
  return (
    <PageContainer>
      <PageHeader title="Data Center" subtitle="Connect databases and spreadsheets; discover the business metrics AEAM should watch" />
      <Panel title="Connected sources" icon="database" pad={false}
        right={<span style={{ fontSize: ".62rem", color: "var(--muted)", fontFamily: "var(--font-mono)" }}>read-only preview</span>}>
        <DataTable columns={COLUMNS} rows={[]} empty="No data sources connected — schema discovery and safe SQL arrive in Phase B." />
      </Panel>
      <div style={{ marginTop: "1.4rem" }}>
        <ComingSoon icon="database" title="Structured Data Center" phase="B"
          description="Feeds the already-built KPI, Statistical, Forecast and Rule agents a live metric stream."
          points={[
            "Read-only connection catalog + schema / table / column discovery.",
            "Deterministic, allow-listed safe SQL — never free-form LLM SQL against production.",
            "Metric baselining → Prophet forecast → rule / anomaly evaluation → incidents.",
            "Turns the dormant detection loop into a running autonomous pipeline.",
          ]} />
      </div>
    </PageContainer>
  );
}
