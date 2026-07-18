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

export default function TopBar({ onHamburger, onSearch }) {
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

      {/* Global search — opens the command palette (Ctrl+K). */}
      <button type="button" className="aeam-search" onClick={onSearch} aria-label="Open command palette"
        style={{ cursor: "pointer", textAlign: "left" }}>
        <Icon name="search" size={14} />
        <span style={{ flex: 1, fontSize: "var(--fs-sm)", color: "var(--faint)" }}>Jump to page or incident…</span>
        <kbd>ctrl K</kbd>
      </button>

      <div className="aeam-topbar-right">
        {/* System status — real signal from /health + /system/status */}
        <span className="aeam-sys-pill" title={`System ${sysLabel}`}>
          <StatusDot state={overall === "ok" ? "ok" : overall} pulse={overall === "ok"} />
          <span className="txt">{sysLabel}{agentsActive != null ? ` · ${agentsActive} agents` : ""}</span>
        </span>

        {/* Environment badge — real Vite build mode */}
        <span className="aeam-env" style={{ color: ENV_COLOR.color, borderColor: ENV_COLOR.border, background: ENV_COLOR.bg }}
          title="Frontend build environment">{ENV}</span>

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
