import { PageHeader } from "../components/ui";
import { PageContainer, MetricCard, Panel, EmptyState, ComingSoon } from "../components/library";

export default function Admin() {
  return (
    <PageContainer>
      <PageHeader title="Admin" subtitle="Users, roles, audit trail and policies — the governance surface" />
      <div className="aeam-grid-metrics" style={{ marginBottom: "1.4rem" }}>
        <MetricCard label="Users" value="—" icon="shield" sub="with access" />
        <MetricCard label="Roles" value="—" icon="layers" sub="defined" />
        <MetricCard label="Audit events" value="—" icon="clock" sub="recorded" />
      </div>
      <Panel title="Audit trail" icon="shield">
        <EmptyState icon="shield" title="Governance not active yet"
          description="A durable, queryable audit trail and role management surface once SecurityMiddleware is fully enabled." />
      </Panel>
      <div style={{ marginTop: "1.4rem" }}>
        <ComingSoon icon="shield" title="Admin" phase="A7"
          description="Activates the JWT / RBAC middleware that already exists but is bypassed in development."
          points={[
            "User + role management over the existing RBAC.",
            "Durable, queryable audit log (beyond per-request logging).",
            "Approval policies and retention rules — deterministic, not LLM-driven.",
          ]} />
      </div>
    </PageContainer>
  );
}
