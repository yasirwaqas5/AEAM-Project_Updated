import { useLocation } from "react-router-dom";
import { Icon } from "../components/ui";
import { StatusDot } from "../components/library";
import { matchNav } from "../config/nav";
import { useHealth } from "./HealthProvider";

/* ──────────────────────────────────────────────────────────────────────────
 * layout/TopBar.jsx
 * Global search (UI only), notifications, current user, system status pill,
 * environment badge, current workspace, breadcrumbs. All chrome — no logic.
 * ────────────────────────────────────────────────────────────────────────── */

// Environment reflects how the FRONTEND was built (Vite mode). Binding this to
// the backend ENVIRONMENT needs a proxied endpoint that exposes it — a Phase A3
// backend task. This is honest (real build signal), not fabricated.
const ENV = (import.meta.env && import.meta.env.MODE) === "production" ? "PROD" : "DEV";
const ENV_COLOR = ENV === "PROD"
  ? { color: "var(--err)", border: "rgba(255,95,87,.35)", bg: "rgba(255,95,87,.08)" }
  : { color: "var(--warn)", border: "rgba(255,184,0,.35)", bg: "rgba(255,184,0,.08)" };

function BellIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9" />
      <path d="M13.73 21a2 2 0 0 1-3.46 0" />
    </svg>
  );
}

export default function TopBar({ onHamburger }) {
  const { pathname } = useLocation();
  const active = matchNav(pathname);
  const { overall, agentsActive } = useHealth();

  const crumbGroup = active?.group;
  const crumbPage = active?.label || "—";
  const sysColor = { ok: "var(--ok)", degraded: "var(--warn)", error: "var(--err)" }[overall] || "var(--muted)";
  const sysLabel = { ok: "Operational", degraded: "Degraded", error: "Unreachable" }[overall] || "Unknown";

  return (
    <header className="aeam-topbar">
      <button className="aeam-hamburger" onClick={onHamburger} aria-label="Open navigation">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><path d="M3 6h18M3 12h18M3 18h18"/></svg>
      </button>

      {/* Breadcrumbs: Home / Group / Page */}
      <nav className="aeam-crumbs" aria-label="Breadcrumb">
        <span>AEAM</span>
        {crumbGroup && <><span className="sep">/</span><span>{crumbGroup}</span></>}
        <span className="sep">/</span><span className="cur">{crumbPage}</span>
      </nav>

      {/* Global search — UI only */}
      <label className="aeam-search">
        <Icon name="search" size={14} />
        <input placeholder="Search incidents, knowledge, actions…" aria-label="Global search"
          onKeyDown={(e) => { if (e.key === "Enter") e.preventDefault(); }} />
        <kbd>/</kbd>
      </label>

      <div className="aeam-topbar-right">
        {/* Current workspace */}
        <span className="aeam-sys-pill" title="Current workspace">
          <Icon name="layers" size={12} /><span className="txt">Production</span>
        </span>

        {/* System status */}
        <span className="aeam-sys-pill" title={`System ${sysLabel}`}>
          <StatusDot state={overall === "ok" ? "ok" : overall} pulse={overall === "ok"} />
          <span className="txt">{sysLabel}{agentsActive != null ? ` · ${agentsActive} agents` : ""}</span>
        </span>

        {/* Environment badge */}
        <span className="aeam-env" style={{ color: ENV_COLOR.color, borderColor: ENV_COLOR.border, background: ENV_COLOR.bg }}
          title="Frontend build environment">{ENV}</span>

        {/* Notifications */}
        <button className="aeam-icon-btn" aria-label="Notifications" title="Notifications">
          <BellIcon />
          <span className="badge">0</span>
        </button>

        {/* Current user */}
        <span className="aeam-user" title="Signed-in operator">
          <span className="aeam-avatar">OP</span>
          <span className="aeam-user-meta">
            <span className="aeam-user-name">Operator</span>
            <span className="aeam-user-role">SRE · read/write</span>
          </span>
        </span>
      </div>
    </header>
  );
}
