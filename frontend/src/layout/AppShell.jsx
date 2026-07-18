import { useState, useEffect, useCallback, useRef } from "react";
import { useLocation } from "react-router-dom";
import { UIStyles } from "../components/ui";
import { ShellStyles } from "../components/library";
import Sidebar from "./Sidebar";
import TopBar from "./TopBar";
import StatusBar from "./StatusBar";
import CommandPalette from "./CommandPalette";

/* ──────────────────────────────────────────────────────────────────────────
 * layout/AppShell.jsx
 * The single reusable application shell. Owns: sidebar collapse/resize state,
 * the mobile drawer, and the CSS-grid frame (sidebar | topbar/workspace/status).
 * Every route renders as {children} inside ONE workspace — no duplicated layout.
 * ────────────────────────────────────────────────────────────────────────── */

const LS_COLLAPSED = "aeam.sidebar.collapsed";
const LS_WIDTH = "aeam.sidebar.width";
const MIN_W = 190, MAX_W = 340;

export default function AppShell({ children }) {
  const [collapsed, setCollapsed] = useState(() => localStorage.getItem(LS_COLLAPSED) === "1");
  const [width, setWidth] = useState(() => {
    const w = parseInt(localStorage.getItem(LS_WIDTH), 10);
    return Number.isFinite(w) ? Math.min(MAX_W, Math.max(MIN_W, w)) : 248;
  });
  const [mobileOpen, setMobileOpen] = useState(false);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const draggingRef = useRef(false);
  const { pathname } = useLocation();

  // Persist collapse; close the mobile drawer on route change.
  useEffect(() => { localStorage.setItem(LS_COLLAPSED, collapsed ? "1" : "0"); }, [collapsed]);
  useEffect(() => { setMobileOpen(false); }, [pathname]);

  // Global hotkeys: Ctrl/Cmd+K anywhere, "/" outside form fields.
  useEffect(() => {
    const onKey = (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
        e.preventDefault(); setPaletteOpen((o) => !o);
      } else if (e.key === "/" && !/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName || "") &&
        !document.activeElement?.isContentEditable) {
        e.preventDefault(); setPaletteOpen(true);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // Sidebar resize (pointer drag on the handle).
  const onResizeStart = useCallback((e) => {
    if (collapsed) return;
    e.preventDefault();
    draggingRef.current = true;
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";

    const onMove = (ev) => {
      if (!draggingRef.current) return;
      const next = Math.min(MAX_W, Math.max(MIN_W, ev.clientX));
      setWidth(next);
    };
    const onUp = () => {
      draggingRef.current = false;
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      setWidth((w) => { localStorage.setItem(LS_WIDTH, String(w)); return w; });
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  }, [collapsed]);

  return (
    <div
      className="aeam-shell"
      data-collapsed={collapsed ? "true" : "false"}
      data-mobile-open={mobileOpen ? "true" : "false"}
      style={!collapsed ? { "--sidebar-w": `${width}px` } : undefined}
    >
      <UIStyles />
      <ShellStyles />

      <a className="aeam-skip" href="#aeam-main">Skip to content</a>
      <CommandPalette open={paletteOpen} onClose={() => setPaletteOpen(false)} />

      <Sidebar
        collapsed={collapsed}
        onToggle={() => setCollapsed((c) => !c)}
        onNavigate={() => setMobileOpen(false)}
        onResizeStart={onResizeStart}
      />

      {mobileOpen && <div className="aeam-backdrop" onClick={() => setMobileOpen(false)} aria-hidden="true" />}

      <div className="aeam-maincol">
        <TopBar onHamburger={() => setMobileOpen((o) => !o)} onSearch={() => setPaletteOpen(true)} />
        <main id="aeam-main" className="aeam-workspace">{children}</main>
        <StatusBar />
      </div>
    </div>
  );
}
