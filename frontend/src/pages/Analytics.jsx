import { PageHeader } from "../components/ui";
import { PageContainer, GraphPlaceholder, ComingSoon } from "../components/library";

export default function Analytics() {
  return (
    <PageContainer>
      <PageHeader title="Analytics" subtitle="Operational trends over data the system already produces" />
      <div className="aeam-grid-2" style={{ marginBottom: "1.4rem" }}>
        <GraphPlaceholder title="Action success rate" note="from action_success_total / action_failure_total" />
        <GraphPlaceholder title="Forecast vs actual" note="from ForecastAgent yhat + confidence bands" />
        <GraphPlaceholder title="Incident heatmap" note="metric × severity × time" />
        <GraphPlaceholder title="Investigation funnel" note="depth · validation · escalation rate" />
      </div>
      <ComingSoon icon="target" title="Analytics" phase="B-series"
        description="Charts over the existing Postgres tables and Prometheus counters — no new datastore."
        points={[
          "Action success / failure, LLM latency and cost-per-incident.",
          "Forecast-vs-actual with confidence bands (the forecast trust view).",
          "Incident heatmap and the investigation funnel (resolve vs escalate).",
        ]} />
    </PageContainer>
  );
}
