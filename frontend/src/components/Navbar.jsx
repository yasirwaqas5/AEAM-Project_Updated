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
    height: "56px",
    background: "#ffffff",
    borderBottom: "1px solid #e5e7eb",
    position: "sticky",
    top: 0,
    zIndex: 100,
  },
  logo: {
    fontSize: "1rem",
    fontWeight: 700,
    letterSpacing: "0.08em",
    color: "#111827",
    textDecoration: "none",
  },
  links: {
    display: "flex",
    alignItems: "center",
    gap: "0.25rem",
  },
  link: (active) => ({
    padding: "0.4rem 0.9rem",
    borderRadius: "6px",
    fontSize: "0.85rem",
    fontWeight: active ? 600 : 400,
    color: active ? "#111827" : "#6b7280",
    background: active ? "#f3f4f6" : "transparent",
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