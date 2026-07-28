import { BrowserRouter, Routes, Route, Navigate, useLocation } from "react-router-dom";
import { Suspense, lazy, useEffect } from "react";

import { ToastProvider } from "./layout/ToastHost";
import { HealthProvider } from "./layout/HealthProvider";
import { AuthProvider, useAuth } from "./layout/AuthProvider";
import ErrorBoundary from "./layout/ErrorBoundary";
import AppShell from "./layout/AppShell";
import { Icon } from "./components/ui";
import { NAV_ITEMS } from "./config/nav";

const Login = lazy(() => import("./pages/Login"));
const SsoCallback = lazy(() => import("./pages/SsoCallback"));

/* ──────────────────────────────────────────────────────────────────────────
 * App.jsx — root: design tokens + route table.
 *
 * Every route is code-split (React.lazy) so the initial bundle stays lean
 * and the 3D scenes (Welcome / Dashboard hero) never load until visited.
 * ────────────────────────────────────────────────────────────────────────── */

const Dashboard         = lazy(() => import("./pages/Dashboard"));
const Incidents         = lazy(() => import("./pages/Incidents"));
const Agents            = lazy(() => import("./pages/Agents"));
const Trigger           = lazy(() => import("./pages/Trigger"));
const Investigation     = lazy(() => import("./pages/Investigation"));
const HumanReview       = lazy(() => import("./pages/HumanReview"));
const RetrievalExplorer = lazy(() => import("./pages/RetrievalExplorer"));
const Replay            = lazy(() => import("./pages/Replay"));
const Memory            = lazy(() => import("./pages/Memory"));
const KnowledgeCenter   = lazy(() => import("./pages/KnowledgeCenter"));
const DataCenter        = lazy(() => import("./pages/DataCenter"));
const Analytics         = lazy(() => import("./pages/Analytics"));
const Actions           = lazy(() => import("./pages/Actions"));
const Settings          = lazy(() => import("./pages/Settings"));
const Admin             = lazy(() => import("./pages/Admin"));
const Welcome           = lazy(() => import("./pages/Welcome"));

/** Looks up the RBAC permission (if any) a route requires, from the single
 *  nav.js source of truth — a route's guard and its sidebar visibility can
 *  never drift apart. */
function permissionForPath(pathname) {
  const item = NAV_ITEMS.find((i) => i.to === pathname);
  return item?.permission || null;
}

/* ─── Design tokens ─────────────────────────────────────────────────────────
 * The single source of truth for the AEAM visual language.
 * Deep graphite-blue base · sapphire primary · teal/cyan intelligence accents.
 * Legacy token NAMES (--surface, --border, --accent, --ok…) are preserved so
 * every existing component restyles automatically; only VALUES changed.
 * ────────────────────────────────────────────────────────────────────────── */
const GLOBAL_CSS = `
  :root {
    /* — Color · base surfaces — */
    --bg:        #0a0e14;
    --bg-raise:  #0d1219;
    --sidebar:   #0c1018;
    --surface:   #10151e;
    --surface-2: #151b26;
    --surface-3: #1a2230;
    --border:    #1c2432;
    --border-2:  #27314464;
    --border-hi: rgba(148,179,219,.16);

    /* — Color · text — */
    --text:   #e8edf6;
    --text-2: #b9c3d6;
    --muted:  #8a94a9;
    --faint:  #5d6679;

    /* — Color · accent system — */
    --accent:        #5b9dff;
    --accent-2:      #2dd4bf;
    --glow:          #38bdf8;
    --accent-dim:    rgba(91,157,255,.10);
    --accent-border: rgba(91,157,255,.35);

    /* — Color · status — */
    --ok:   #34d399;  --ok-dim:   rgba(52,211,153,.10);
    --warn: #fbbf24;  --warn-dim: rgba(251,191,36,.10);
    --err:  #f87171;  --err-dim:  rgba(248,113,113,.10);
    --info: #38bdf8;  --info-dim: rgba(56,189,248,.10);

    /* — Color · intelligence engines — */
    --c-memory:    #a78bfa;
    --c-policy:    #5b9dff;
    --c-cross:     #2dd4bf;
    --c-adaptive:  #fbbf24;
    --c-retrieval: #38bdf8;
    --c-plan:      #f472b6;
    --c-eval:      #34d399;
    --c-observe:   #94a3b8;
    --c-forecast:  #c084fc;

    /* — Typography — */
    --font-display: "Segoe UI Variable Display","SF Pro Display",Inter,"Segoe UI",system-ui,sans-serif;
    --font-body:    "Segoe UI Variable Text","SF Pro Text",Inter,"Segoe UI",system-ui,sans-serif;
    --font-mono:    "Cascadia Mono","SFMono-Regular",ui-monospace,"JetBrains Mono",Consolas,monospace;
    --fs-2xs: .6875rem;  /* 11px — floor */
    --fs-xs:  .75rem;    /* 12px */
    --fs-sm:  .8125rem;  /* 13px */
    --fs-md:  .875rem;   /* 14px */
    --fs-lg:  1rem;      /* 16px */
    --fs-xl:  1.25rem;   /* 20px */
    --fs-2xl: 1.75rem;   /* 28px */

    /* — Spacing (4px scale) — */
    --sp-1: 4px; --sp-2: 8px; --sp-3: 12px; --sp-4: 16px;
    --sp-5: 20px; --sp-6: 24px; --sp-8: 32px; --sp-10: 40px; --sp-12: 48px;

    /* — Radius — */
    --r-sm: 6px; --r-md: 10px; --r-lg: 14px; --r-xl: 20px;
    --radius: 12px;

    /* — Elevation — */
    --e1: 0 1px 2px rgba(2,6,12,.4);
    --e2: 0 2px 6px rgba(2,6,12,.35), 0 10px 28px rgba(2,6,12,.28);
    --e3: 0 4px 14px rgba(2,6,12,.42), 0 20px 56px rgba(2,6,12,.34);
    --e4: 0 8px 28px rgba(2,6,12,.5), 0 36px 90px rgba(2,6,12,.42);
    --edge: inset 0 1px 0 rgba(255,255,255,.045);

    /* — Glass — */
    --glass-bg:   rgba(12,16,24,.72);
    --glass-blur: saturate(1.5) blur(16px);

    /* — Motion — */
    --t-fast: 120ms; --t-med: 200ms; --t-slow: 320ms;
    --ease-out:    cubic-bezier(.215,.61,.355,1);
    --ease-spring: cubic-bezier(.34,1.56,.64,1);
  }

  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  html, body, #root {
    height: 100%;
    background: var(--bg);
    color: var(--text);
    font-family: var(--font-body);
    font-size: 15px;
    -webkit-font-smoothing: antialiased;
    text-rendering: optimizeLegibility;
  }

  a { color: inherit; }
  ::selection { background: rgba(91,157,255,.28); }

  /* Keyboard focus — visible, consistent, never on mouse click. */
  :focus { outline: none; }
  :focus-visible {
    outline: 2px solid var(--glow);
    outline-offset: 2px;
    border-radius: 4px;
  }

  ::-webkit-scrollbar { width: 10px; height: 10px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb {
    background: var(--surface-3); border-radius: 6px;
    border: 2px solid var(--bg); background-clip: padding-box;
  }
  ::-webkit-scrollbar-thumb:hover { background: #2c3648; background-clip: padding-box; border: 2px solid var(--bg); }

  @media (prefers-reduced-motion: reduce) {
    *, *::before, *::after {
      animation-duration: .01ms !important;
      animation-iteration-count: 1 !important;
      transition-duration: .01ms !important;
    }
  }
`;

function RouteFallback() {
  return (
    <div style={{ display: "flex", alignItems: "center", justifyContent: "center", height: "60vh", gap: 10 }}>
      <span className="aeam-spinner" aria-hidden="true" />
      <span style={{ color: "var(--muted)", fontSize: "var(--fs-sm)", fontFamily: "var(--font-mono)" }}>Loading module…</span>
    </div>
  );
}

function Forbidden() {
  const { pathname } = useLocation();
  return (
    <div style={{ display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", height: "60vh", gap: "var(--sp-3)", textAlign: "center" }}>
      <Icon name="shield" size={28} color="var(--err)" />
      <h2 style={{ fontSize: "var(--fs-lg)", color: "var(--text)" }}>Access denied</h2>
      <p style={{ color: "var(--muted)", fontSize: "var(--fs-sm)", maxWidth: 420 }}>
        Your role does not grant access to <code>{pathname}</code>. Contact an administrator
        if you believe this is incorrect.
      </p>
    </div>
  );
}

/** Route-level enforcement of the same permission Sidebar uses to decide
 *  visibility — reaching a hidden URL directly gets a 403 page, not the
 *  page itself (Phase E10 acceptance: "an analyst cannot see or invoke
 *  admin configuration"). */
function RequirePermission({ children }) {
  const { pathname } = useLocation();
  const { hasPermission } = useAuth();
  const permission = permissionForPath(pathname);
  if (permission) {
    const [resource, action] = permission.split(":");
    if (!hasPermission(resource, action)) return <Forbidden />;
  }
  return children;
}

/** Redirects unauthenticated visitors to /login, remembering where they
 *  were headed so Login can send them back after signing in. Renders
 *  nothing while the boot-time dev-token probe is still in flight, so a
 *  page never flashes the login screen during a dev-posture auto-login. */
function RequireAuth({ children }) {
  const { isAuthenticated, booting } = useAuth();
  const location = useLocation();
  if (booting) return <RouteFallback />;
  if (!isAuthenticated) return <Navigate to="/login" replace state={{ from: location }} />;
  return children;
}

function AppRoutes() {
  return (
    <Suspense fallback={<RouteFallback />}>
      <Routes>
        <Route path="/"              element={<Dashboard />} />
        <Route path="/analytics"     element={<RequirePermission><Analytics /></RequirePermission>} />
        <Route path="/incidents"     element={<RequirePermission><Incidents /></RequirePermission>} />
        <Route path="/investigation" element={<RequirePermission><Investigation /></RequirePermission>} />
        <Route path="/human-review"  element={<RequirePermission><HumanReview /></RequirePermission>} />
        <Route path="/retrieval"     element={<RequirePermission><RetrievalExplorer /></RequirePermission>} />
        <Route path="/replay"        element={<RequirePermission><Replay /></RequirePermission>} />
        <Route path="/memory"        element={<RequirePermission><Memory /></RequirePermission>} />
        <Route path="/knowledge"     element={<RequirePermission><KnowledgeCenter /></RequirePermission>} />
        <Route path="/data"          element={<RequirePermission><DataCenter /></RequirePermission>} />
        <Route path="/agents"        element={<RequirePermission><Agents /></RequirePermission>} />
        <Route path="/actions"       element={<RequirePermission><Actions /></RequirePermission>} />
        <Route path="/trigger"       element={<RequirePermission><Trigger /></RequirePermission>} />
        <Route path="/settings"      element={<RequirePermission><Settings /></RequirePermission>} />
        <Route path="/admin"         element={<RequirePermission><Admin /></RequirePermission>} />
        <Route path="*"              element={<Navigate to="/" replace />} />
      </Routes>
    </Suspense>
  );
}

export default function App() {
  useEffect(() => {
    const tag = document.createElement("style");
    tag.textContent = GLOBAL_CSS;
    document.head.appendChild(tag);
    return () => document.head.removeChild(tag);
  }, []);

  return (
    <ToastProvider>
      <BrowserRouter>
        <AuthProvider>
          <HealthProvider>
            <ErrorBoundary>
              <Routes>
                {/* The Welcome Experience renders OUTSIDE the shell — a full-bleed
                    startup sequence for demos/presentations. The console (every
                    other route) keeps the operational AppShell. */}
                <Route path="/welcome" element={
                  <Suspense fallback={<RouteFallback />}><Welcome /></Suspense>
                } />
                <Route path="/login" element={
                  <Suspense fallback={<RouteFallback />}><Login /></Suspense>
                } />
                {/* Phase E13: the OIDC redirect target. Unauthenticated by
                    necessity — it is where the token arrives — so it sits
                    outside RequireAuth alongside /login. */}
                <Route path="/auth/callback" element={
                  <Suspense fallback={<RouteFallback />}><SsoCallback /></Suspense>
                } />
                <Route path="*" element={
                  <RequireAuth>
                    <AppShell><AppRoutes /></AppShell>
                  </RequireAuth>
                } />
              </Routes>
            </ErrorBoundary>
          </HealthProvider>
        </AuthProvider>
      </BrowserRouter>
    </ToastProvider>
  );
}
