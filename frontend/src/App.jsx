import { BrowserRouter, Routes, Route } from "react-router-dom";
import { useEffect } from "react";

import Navbar    from "./components/Navbar";
import Dashboard from "./pages/Dashboard";
import Incidents from "./pages/Incidents";
import Agents    from "./pages/Agents";
import Trigger   from "./pages/Trigger";

// ─── Global styles ────────────────────────────────────────────────────────────
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

  @keyframes fadeSlideIn {
    from { opacity: 0; transform: translateY(10px); }
    to   { opacity: 1; transform: translateY(0); }
  }

  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50%       { opacity: 0.4; }
  }

  ::-webkit-scrollbar { width: 6px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
`;

// ─── Root App ─────────────────────────────────────────────────────────────────
export default function App() {
  useEffect(() => {
    const tag = document.createElement("style");
    tag.textContent = GLOBAL_CSS;
    document.head.appendChild(tag);
    return () => document.head.removeChild(tag);
  }, []);

  return (
    <BrowserRouter>
      <Navbar />
      <main style={{
        flex: 1,
        padding: "3rem",
        minHeight: "100vh",
        overflowY: "auto",
      }}>
        <Routes>
          <Route path="/"          element={<Dashboard />} />
          <Route path="/incidents" element={<Incidents />} />
          <Route path="/agents"    element={<Agents />} />
          <Route path="/trigger"   element={<Trigger />} />
        </Routes>
      </main>
    </BrowserRouter>
  );
}