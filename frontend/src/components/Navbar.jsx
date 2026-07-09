import { Link, useLocation } from "react-router-dom";

const NAV_LINKS = [
  { to: "/",          label: "Dashboard" },
  { to: "/incidents", label: "Incidents" },
  { to: "/agents",    label: "Agents"    },
  { to: "/trigger",   label: "Trigger"   },
];

const styles = {
  nav: {
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    padding: "0 2rem",
    height: "58px",
    background: "var(--sidebar)",
    borderBottom: "1px solid var(--border)",
    position: "sticky",
    top: 0,
    zIndex: 100,
    backdropFilter: "blur(6px)",
  },
  logo: {
    fontSize: "1rem",
    fontWeight: 700,
    letterSpacing: "0.14em",
    color: "var(--text)",
    fontFamily: "var(--font-display)",
    textDecoration: "none",
    display: "inline-flex",
    alignItems: "center",
    gap: "0.5rem",
  },
  links: {
    display: "flex",
    alignItems: "center",
    gap: "0.25rem",
  },
  link: (active) => ({
    padding: "0.4rem 0.9rem",
    borderRadius: "7px",
    fontSize: "0.82rem",
    fontWeight: active ? 600 : 400,
    letterSpacing: "0.03em",
    color: active ? "var(--accent)" : "var(--muted)",
    background: active ? "var(--accent-dim)" : "transparent",
    border: `1px solid ${active ? "rgba(0,255,163,0.25)" : "transparent"}`,
    textDecoration: "none",
    transition: "all 0.15s ease",
  }),
};

export default function Navbar() {
  const { pathname } = useLocation();

  return (
    <nav style={styles.nav}>
      {/* Logo */}
      <Link to="/" style={styles.logo}>⬡ AEAM</Link>

      {/* Links */}
      <div style={styles.links}>
        {NAV_LINKS.map(({ to, label }) => {
          const active = to === "/" ? pathname === "/" : pathname.startsWith(to);
          return (
            <Link key={to} to={to} style={styles.link(active)}>
              {label}
            </Link>
          );
        })}
      </div>
    </nav>
  );
}