/* ──────────────────────────────────────────────────────────────────────────
 * config/nav.js
 * Single source of truth for the Enterprise UI Shell navigation.
 * Drives the Sidebar, the router (App.jsx), and TopBar breadcrumbs — so a
 * page is added in exactly one place. Icon names map to ui.jsx's Icon set.
 *
 * status: "live"  → real, implemented page
 *         "soon"  → placeholder wired now, business logic lands in `phase`
 *
 * permission: "resource:action" (Phase E10) — the RBAC grant (see
 *   lib/rbac.js, lockstep with aeam/security/rbac.py) required to see and
 *   enter this page. `undefined` means every authenticated role may see it
 *   (e.g. Dashboard). Sidebar filters items the current roles don't grant;
 *   App.jsx's route guard enforces the same check server-side-honestly —
 *   navigating directly to a hidden URL still gets a 403 page, not the
 *   page itself.
 * ────────────────────────────────────────────────────────────────────────── */

export const NAV_GROUPS = [
  {
    label: "Overview",
    items: [
      { to: "/",          label: "Dashboard", icon: "activity", status: "live" },
      { to: "/analytics", label: "Analytics", icon: "target",   status: "live", permission: "logs:view" },
      // Full-bleed startup-sequence showcase (renders outside the shell) —
      // for demos, interviews and reviews. Dashboard stays the default.
      { to: "/welcome",   label: "Welcome Tour", icon: "sparkle", status: "live" },
    ],
  },
  {
    label: "Investigate",
    items: [
      { to: "/incidents",     label: "Incidents",          icon: "alert",  status: "live", permission: "incidents:view" },
      { to: "/investigation", label: "Investigation",      icon: "branch", status: "live", permission: "incidents:view" },
      { to: "/human-review",  label: "Human Review",       icon: "shield", status: "live", permission: "incidents:view" },
      { to: "/retrieval",     label: "Retrieval Explorer", icon: "search", status: "live", permission: "admin:config" },
      { to: "/replay",        label: "Replay",             icon: "play",   status: "live", permission: "incidents:view" },
      { to: "/memory",        label: "Memory",             icon: "layers", status: "live", permission: "documents:search" },
    ],
  },
  {
    label: "Knowledge & Data",
    items: [
      { to: "/knowledge", label: "Knowledge Center", icon: "database", status: "live", permission: "documents:search" },
      { to: "/data",      label: "Data Center",      icon: "database", status: "live", permission: "admin:config" },
    ],
  },
  {
    label: "Operate",
    items: [
      { to: "/agents",  label: "Agents",  icon: "layers", status: "live", permission: "logs:view" },
      { to: "/actions", label: "Actions", icon: "zap",    status: "live", permission: "actions:execute" },
      { to: "/trigger", label: "Trigger", icon: "bolt",   status: "live", permission: "kpis:trigger" },
    ],
  },
  {
    label: "System",
    items: [
      { to: "/settings", label: "Settings", icon: "code",   status: "live", permission: "admin:config" },
      { to: "/admin",    label: "Admin",    icon: "shield", status: "soon", phase: "A7", permission: "admin:config" },
    ],
  },
];

/** Flat list of every nav item, tagged with its group label. */
export const NAV_ITEMS = NAV_GROUPS.flatMap((g) =>
  g.items.map((it) => ({ ...it, group: g.label })),
);

/** Resolve the active nav item for a pathname (longest-prefix match). */
export function matchNav(pathname) {
  if (pathname === "/") return NAV_ITEMS.find((i) => i.to === "/");
  return NAV_ITEMS
    .filter((i) => i.to !== "/" && pathname.startsWith(i.to))
    .sort((a, b) => b.to.length - a.to.length)[0] || null;
}
