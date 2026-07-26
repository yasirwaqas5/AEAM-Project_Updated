import { NavLink } from "react-router-dom";
import { Icon } from "../components/ui";
import { NAV_GROUPS } from "../config/nav";
import { useAuth } from "./AuthProvider";

/* ──────────────────────────────────────────────────────────────────────────
 * layout/Sidebar.jsx
 * Persistent, grouped, collapsible + resizable navigation. Reads NAV_GROUPS
 * so pages are declared once. Replaces the old top Navbar.
 *
 * Phase E10: items whose `permission` the current roles don't grant are
 * hidden entirely (not just disabled) — role-aware navigation, same RBAC
 * vocabulary as the backend (lib/rbac.js). A group with no visible items
 * disappears too, so an analyst never sees an empty "System" heading.
 * ────────────────────────────────────────────────────────────────────────── */

export default function Sidebar({ collapsed, onToggle, onNavigate, onResizeStart }) {
  const { hasPermission } = useAuth();
  const visibleGroups = NAV_GROUPS
    .map((group) => ({
      ...group,
      items: group.items.filter((item) => {
        if (!item.permission) return true;
        const [resource, action] = item.permission.split(":");
        return hasPermission(resource, action);
      }),
    }))
    .filter((group) => group.items.length > 0);

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
        {visibleGroups.map((group) => (
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
