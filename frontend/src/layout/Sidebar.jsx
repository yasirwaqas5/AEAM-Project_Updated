import { NavLink } from "react-router-dom";
import { Icon } from "../components/ui";
import { NAV_GROUPS } from "../config/nav";

/* ──────────────────────────────────────────────────────────────────────────
 * layout/Sidebar.jsx
 * Persistent, grouped, collapsible + resizable navigation. Reads NAV_GROUPS
 * so pages are declared once. Replaces the old top Navbar.
 * ────────────────────────────────────────────────────────────────────────── */

export default function Sidebar({ collapsed, onToggle, onNavigate, onResizeStart }) {
  return (
    <aside className="aeam-sidebar" aria-label="Primary navigation">
      <div className="aeam-sidebar-head">
        <NavLink to="/" className="aeam-logo" onClick={onNavigate}>
          <span className="mark">⬡</span><span>AEAM</span>
        </NavLink>
        <button className="aeam-collapse-btn" onClick={onToggle}
          aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"} title="Toggle sidebar">
          <Icon name="code" size={13} />
        </button>
      </div>

      <nav className="aeam-nav">
        {NAV_GROUPS.map((group) => (
          <div className="aeam-nav-group" key={group.label}>
            <div className="aeam-nav-group-label">{group.label}</div>
            {group.items.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.to === "/"}
                onClick={onNavigate}
                className={({ isActive }) => `aeam-nav-link${isActive ? " active" : ""}`}
                title={collapsed ? item.label : undefined}
              >
                <Icon name={item.icon} size={15} />
                <span className="lbl">{item.label}</span>
                {item.status === "soon" && <span className="aeam-nav-soon">soon</span>}
              </NavLink>
            ))}
          </div>
        ))}
      </nav>

      <div className="aeam-sidebar-foot">
        AEAM Control Plane<br />
        Enterprise Shell · A2
      </div>

      {/* Drag handle (desktop only; hidden on mobile via CSS) */}
      <div className="aeam-resize" onPointerDown={onResizeStart} role="separator"
        aria-orientation="vertical" aria-label="Resize sidebar" />
    </aside>
  );
}
