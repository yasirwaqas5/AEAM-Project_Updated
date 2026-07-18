import { Link } from "react-router-dom";
import { PageHeader, Icon, Button } from "../components/ui";
import { PageContainer, Panel, EmptyState } from "../components/library";

/* ──────────────────────────────────────────────────────────────────────────
 * pages/Admin.jsx — governance surface.
 * Honest status page: configuration management (Phase D5) is live and lives
 * in Settings; user/role management and a durable audit trail require the
 * (existing, dev-bypassed) SecurityMiddleware to be enforced plus new
 * backend endpoints — stated plainly, no fabricated counts.
 * ────────────────────────────────────────────────────────────────────────── */

const CAPABILITIES = [
  {
    icon: "sliders", title: "Configuration management", state: "live",
    desc: "Every intelligence-engine threshold, weight and limit is manageable from the Settings page — read, validate, update and reset, persisted to the environment.",
    action: <Link to="/settings" style={{ textDecoration: "none" }}><Button size="sm" icon="arrowr">Open Settings</Button></Link>,
  },
  {
    icon: "shield", title: "Authentication & RBAC", state: "exists — bypassed in development",
    desc: "JWT and role checks exist in SecurityMiddleware but are intentionally bypassed while ENVIRONMENT=development. Enforcement is a deployment decision, not a missing feature.",
  },
  {
    icon: "clock", title: "Durable audit trail", state: "not built",
    desc: "Every investigation already persists its full evidence trail per incident; a queryable, cross-cutting operator-action audit log requires new backend endpoints.",
  },
];

const STATE_COLORS = { live: "var(--ok)" };

export default function Admin() {
  return (
    <PageContainer>
      <PageHeader title="Administration" subtitle="Governance status — what is enforced, what exists, what is not built yet" />
      <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }} className="aeam-stagger">
        {CAPABILITIES.map((c) => (
          <Panel key={c.title} title={c.title} icon={c.icon}
            right={<span style={{
              fontSize: "var(--fs-2xs)", fontWeight: 700, textTransform: "uppercase", letterSpacing: ".1em",
              color: STATE_COLORS[c.state] || "var(--warn)",
            }}>{c.state}</span>}>
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: "1rem", flexWrap: "wrap" }}>
              <p style={{ margin: 0, color: "var(--muted)", fontSize: "var(--fs-sm)", lineHeight: 1.65, maxWidth: "72ch" }}>{c.desc}</p>
              {c.action}
            </div>
          </Panel>
        ))}
      </div>
      <div style={{ marginTop: "1.4rem" }}>
        <EmptyState icon="shield" title="User & role management"
          description="Surfaces here once SecurityMiddleware enforcement is enabled outside development and user/role endpoints exist. No placeholder data is shown in the meantime." />
      </div>
    </PageContainer>
  );
}
