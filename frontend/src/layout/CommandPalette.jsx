import { useState, useEffect, useRef, useMemo, useCallback } from "react";
import { useNavigate } from "react-router-dom";
import { AnimatePresence, motion } from "framer-motion";
import { Icon, SeverityBadge, fmtRelative } from "../components/ui";
import { NAV_ITEMS } from "../config/nav";

/* ──────────────────────────────────────────────────────────────────────────
 * layout/CommandPalette.jsx — Ctrl+K / "/" global navigator.
 * Two live sections: pages (from nav.js — the single source of truth) and
 * recent incidents (fetched from the real /api/v1/incidents/ on open;
 * navigates to the Investigation Workspace pre-selected via ?id=).
 * Fully keyboard driven: ↑/↓ move, Enter opens, Esc closes.
 * ────────────────────────────────────────────────────────────────────────── */

export default function CommandPalette({ open, onClose }) {
  const navigate = useNavigate();
  const [query, setQuery] = useState("");
  const [incidents, setIncidents] = useState([]);
  const [cursor, setCursor] = useState(0);
  const inputRef = useRef(null);
  const listRef = useRef(null);

  useEffect(() => {
    if (!open) return;
    setQuery(""); setCursor(0);
    inputRef.current?.focus();
    // Live incidents, fetched fresh each time the palette opens.
    fetch("/api/v1/incidents/")
      .then((r) => (r.ok ? r.json() : []))
      .then((d) => setIncidents(Array.isArray(d) ? d.slice(0, 30) : []))
      .catch(() => setIncidents([]));
  }, [open]);

  const q = query.trim().toLowerCase();

  const results = useMemo(() => {
    const pages = NAV_ITEMS
      .filter((n) => !q || n.label.toLowerCase().includes(q) || n.group.toLowerCase().includes(q))
      .map((n) => ({ kind: "page", key: `p:${n.to}`, item: n }));
    const inc = incidents
      .filter((i) => {
        if (!q) return true;
        return [i.incident_id, i.event_type, i.metric, i.severity, i.root_cause]
          .some((f) => f && String(f).toLowerCase().includes(q));
      })
      .slice(0, 6)
      .map((i) => ({ kind: "incident", key: `i:${i.incident_id}`, item: i }));
    return [...pages, ...inc];
  }, [q, incidents]);

  useEffect(() => { setCursor(0); }, [q]);

  const run = useCallback((row) => {
    if (!row) return;
    if (row.kind === "page") navigate(row.item.to);
    else navigate(`/investigation?id=${encodeURIComponent(row.item.incident_id)}`);
    onClose();
  }, [navigate, onClose]);

  const onKeyDown = (e) => {
    if (e.key === "ArrowDown") { e.preventDefault(); setCursor((c) => Math.min(c + 1, results.length - 1)); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setCursor((c) => Math.max(c - 1, 0)); }
    else if (e.key === "Enter") { e.preventDefault(); run(results[cursor]); }
    else if (e.key === "Escape") onClose();
  };

  useEffect(() => {
    listRef.current?.querySelector('[data-active="true"]')?.scrollIntoView({ block: "nearest" });
  }, [cursor]);

  return (
    <AnimatePresence>
      {open && (
        <motion.div
          initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
          transition={{ duration: 0.12 }}
          onClick={onClose}
          style={{
            position: "fixed", inset: 0, zIndex: 1500, background: "rgba(4,7,12,.55)",
            backdropFilter: "var(--glass-blur)", WebkitBackdropFilter: "var(--glass-blur)",
            display: "flex", justifyContent: "center", alignItems: "flex-start", paddingTop: "14vh",
          }}
        >
          <motion.div
            initial={{ opacity: 0, scale: 0.96, y: -10 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.97, y: -6 }}
            transition={{ type: "spring", stiffness: 480, damping: 34 }}
            onClick={(e) => e.stopPropagation()}
            role="dialog" aria-modal="true" aria-label="Command palette"
            style={{
              width: "min(640px, 92vw)",
              background: "linear-gradient(180deg, var(--surface-2), var(--surface))",
              border: "1px solid var(--border-2)", borderRadius: "var(--r-lg)",
              boxShadow: "var(--e4), var(--edge)", overflow: "hidden",
            }}
          >
            <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "0.9rem 1.1rem", borderBottom: "1px solid var(--border)" }}>
              <Icon name="search" size={15} color="var(--muted)" />
              <input
                ref={inputRef} value={query} onChange={(e) => setQuery(e.target.value)} onKeyDown={onKeyDown}
                placeholder="Jump to a page or incident…" aria-label="Search pages and incidents"
                style={{
                  flex: 1, background: "none", border: "none", outline: "none",
                  color: "var(--text)", fontSize: "var(--fs-md)", fontFamily: "var(--font-body)",
                }}
              />
              <kbd style={{ fontFamily: "var(--font-mono)", fontSize: "var(--fs-2xs)", border: "1px solid var(--border-2)", borderRadius: 4, padding: "1px 6px", color: "var(--faint)" }}>esc</kbd>
            </div>

            <div ref={listRef} style={{ maxHeight: "46vh", overflowY: "auto", padding: "0.5rem" }}>
              {results.length === 0 && (
                <div style={{ padding: "1.6rem", textAlign: "center", color: "var(--muted)", fontSize: "var(--fs-sm)" }}>
                  No matches for “{query}”.
                </div>
              )}
              {results.map((row, idx) => {
                const active = idx === cursor;
                const base = {
                  display: "flex", alignItems: "center", gap: 12, width: "100%",
                  padding: "0.6rem 0.8rem", borderRadius: "var(--r-md)", border: "none",
                  background: active ? "rgba(91,157,255,.12)" : "transparent",
                  color: active ? "var(--text)" : "var(--text-2)",
                  cursor: "pointer", textAlign: "left", fontSize: "var(--fs-sm)", fontFamily: "var(--font-body)",
                };
                if (row.kind === "page") {
                  return (
                    <button key={row.key} data-active={active} style={base}
                      onMouseEnter={() => setCursor(idx)} onClick={() => run(row)}>
                      <Icon name={row.item.icon} size={15} color={active ? "var(--accent)" : "var(--muted)"} />
                      <span style={{ flex: 1 }}>{row.item.label}</span>
                      <span style={{ fontSize: "var(--fs-2xs)", color: "var(--faint)", letterSpacing: ".08em", textTransform: "uppercase" }}>{row.item.group}</span>
                    </button>
                  );
                }
                return (
                  <button key={row.key} data-active={active} style={base}
                    onMouseEnter={() => setCursor(idx)} onClick={() => run(row)}>
                    <Icon name="alert" size={15} color={active ? "var(--warn)" : "var(--muted)"} />
                    <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                      {row.item.event_type || "incident"} · {row.item.metric || "—"}
                      <span style={{ color: "var(--faint)", marginLeft: 8, fontSize: "var(--fs-2xs)" }}>{fmtRelative(row.item.timestamp)}</span>
                    </span>
                    <SeverityBadge severity={row.item.severity} />
                  </button>
                );
              })}
            </div>

            <div style={{
              display: "flex", gap: "1.1rem", padding: "0.55rem 1.1rem", borderTop: "1px solid var(--border)",
              fontSize: "var(--fs-2xs)", color: "var(--faint)", fontFamily: "var(--font-mono)",
            }}>
              <span>↑↓ navigate</span><span>↵ open</span><span>ctrl+K toggle</span>
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
