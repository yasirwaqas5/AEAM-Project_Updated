import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import { useEffect } from "react";

import { ToastProvider } from "./layout/ToastHost";
import { HealthProvider } from "./layout/HealthProvider";
import AppShell from "./layout/AppShell";

// Live pages (existing business logic — unchanged)
import Dashboard from "./pages/Dashboard";
import Incidents from "./pages/Incidents";
import Agents    from "./pages/Agents";
import Trigger   from "./pages/Trigger";

// Shell placeholder pages (wired now, business logic in later phases)
import Investigation     from "./pages/Investigation";
import HumanReview       from "./pages/HumanReview";
import RetrievalExplorer from "./pages/RetrievalExplorer";
import Replay            from "./pages/Replay";
import Memory            from "./pages/Memory";
import KnowledgeCenter   from "./pages/KnowledgeCenter";
import DataCenter        from "./pages/DataCenter";
import Analytics         from "./pages/Analytics";
import Actions           from "./pages/Actions";
import Settings          from "./pages/Settings";
import Admin             from "./pages/Admin";

// ─── Base design tokens + global resets (unchanged palette) ───────────────────
// The Enterprise Shell extends these tokens (--surface-2, --ok/--warn/--err/…)
// in components/library.jsx::ShellStyles. The base names below are preserved so
// every existing ui.jsx primitive keeps rendering identically.
const GLOBAL_CSS = `
  :root {
    --bg:         #0b0d12;
    --sidebar:    #0e1016;
    --surface:    #13161f;
    --border:     #1e2230;
    --text:       #e8eaf0;
    --muted:      #5a5f72;
    --accent:     #00ffa3;
    --accent-dim: rgba(0, 255, 163, 0.08);
    --font-display: 'DM Mono', 'Fira Code', monospace;
    --font-body:    'DM Sans', 'Segoe UI', sans-serif;
    --font-mono:    'DM Mono', 'Fira Code', monospace;
  }

  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  html, body, #root {
    height: 100%;
    background: var(--bg);
    color: var(--text);
    font-family: var(--font-body);
    font-size: 15px;
    -webkit-font-smoothing: antialiased;
  }

  a { color: inherit; }

  ::-webkit-scrollbar { width: 8px; height: 8px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 4px; }
  ::-webkit-scrollbar-thumb:hover { background: #2c3142; }
`;

// ─── Route table (mirrors config/nav.js) ──────────────────────────────────────
function AppRoutes() {
  return (
    <Routes>
      <Route path="/"              element={<Dashboard />} />
      <Route path="/analytics"     element={<Analytics />} />
      <Route path="/incidents"     element={<Incidents />} />
      <Route path="/investigation" element={<Investigation />} />
      <Route path="/human-review"  element={<HumanReview />} />
      <Route path="/retrieval"     element={<RetrievalExplorer />} />
      <Route path="/replay"        element={<Replay />} />
      <Route path="/memory"        element={<Memory />} />
      <Route path="/knowledge"     element={<KnowledgeCenter />} />
      <Route path="/data"          element={<DataCenter />} />
      <Route path="/agents"        element={<Agents />} />
      <Route path="/actions"       element={<Actions />} />
      <Route path="/trigger"       element={<Trigger />} />
      <Route path="/settings"      element={<Settings />} />
      <Route path="/admin"         element={<Admin />} />
      {/* Unknown routes fall back to the Dashboard. */}
      <Route path="*"              element={<Navigate to="/" replace />} />
    </Routes>
  );
}

// ─── Root ─────────────────────────────────────────────────────────────────────
export default function App() {
  useEffect(() => {
    const tag = document.createElement("style");
    tag.textContent = GLOBAL_CSS;
    document.head.appendChild(tag);
    return () => document.head.removeChild(tag);
  }, []);

  return (
    <ToastProvider>
      <HealthProvider>
        <BrowserRouter>
          <AppShell>
            <AppRoutes />
          </AppShell>
        </BrowserRouter>
      </HealthProvider>
    </ToastProvider>
  );
}
