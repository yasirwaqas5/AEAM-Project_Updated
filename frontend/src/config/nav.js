/* ──────────────────────────────────────────────────────────────────────────
 * config/nav.js
 * Single source of truth for the Enterprise UI Shell navigation.
 * Drives the Sidebar, the router (App.jsx), and TopBar breadcrumbs — so a
 * page is added in exactly one place. Icon names map to ui.jsx's Icon set.
 *
 * status: "live"  → real, implemented page
 *         "soon"  → placeholder wired now, business logic lands in `phase`
 * ────────────────────────────────────────────────────────────────────────── */

export const NAV_GROUPS = [
  {
    label: "Overview",
    items: [
      { to: "/",          label: "Dashboard", icon: "activity", status: "live" },
      { to: "/analytics", label: "Analytics", icon: "target",   status: "live" },
    ],
  },
  {
    label: "Investigate",
    items: [
      { to: "/incidents",     label: "Incidents",          icon: "alert",  status: "live" },
      { to: "/investigation", label: "Investigation",      icon: "branch", status: "live" },
      { to: "/human-review",  label: "Human Review",       icon: "shield", status: "live" },
      { to: "/retrieval",     label: "Retrieval Explorer", icon: "search", status: "soon", phase: "A6" },
      { to: "/replay",        label: "Replay",             icon: "play",   status: "live" },
      { to: "/memory",        label: "Memory",             icon: "layers", status: "soon", phase: "C" },
    ],
  },
  {
    label: "Knowledge & Data",
    items: [
      { to: "/knowledge", label: "Knowledge Center", icon: "database", status: "live" },
      { to: "/data",      label: "Data Center",      icon: "database", status: "live" },
    ],
  },
  {
    label: "Operate",
    items: [
      { to: "/agents",  label: "Agents",  icon: "layers", status: "live" },
      { to: "/actions", label: "Actions", icon: "zap",    status: "soon", phase: "A5" },
      { to: "/trigger", label: "Trigger", icon: "bolt",   status: "live" },
    ],
  },
  {
    label: "System",
    items: [
      { to: "/settings", label: "Settings", icon: "code",   status: "soon", phase: "A7" },
      { to: "/admin",    label: "Admin",    icon: "shield", status: "soon", phase: "A7" },
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
