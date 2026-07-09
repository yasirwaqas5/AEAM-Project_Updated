import { useEffect } from "react";

/* ──────────────────────────────────────────────────────────────────────────
 * Shared UI primitives for the AEAM operator console.
 * Dark, minimal, enterprise. Consumes the CSS variables defined globally in
 * App.jsx (--bg, --surface, --border, --text, --muted, --accent, fonts).
 * No external dependencies — icons are inline SVG.
 * ────────────────────────────────────────────────────────────────────────── */

// ─── Colour tokens ──────────────────────────────────────────────────────────

export const SEVERITY = {
  CRITICAL: { color: "#ff5f57", label: "Critical", rank: 4 },
  HIGH:     { color: "#ffb800", label: "High",     rank: 3 },
  MEDIUM:   { color: "#00b4ff", label: "Medium",   rank: 2 },
  LOW:      { color: "#00ffa3", label: "Low",      rank: 1 },
};

export const STATE = {
  done:    "#00ffa3",
  success: "#00ffa3",
  passed:  "#00ffa3",
  active:  "#00b4ff",
  running: "#00b4ff",
  pending: "#ffb800",
  skipped: "#ffb800",
  failed:  "#ff5f57",
  error:   "#ff5f57",
  idle:    "#5a5f72",
};

export function severityOf(key) {
  return SEVERITY[(key ?? "").toUpperCase()] ?? { color: "#5a5f72", label: key || "Unknown", rank: 0 };
}

export function stateColor(key) {
  return STATE[(key ?? "").toLowerCase()] ?? "#5a5f72";
}

// ─── Formatters ─────────────────────────────────────────────────────────────

export function fmtTime(ts) {
  if (!ts) return "—";
  try {
    return new Date(ts).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
  } catch {
    return String(ts);
  }
}

export function fmtRelative(ts) {
  if (!ts) return "—";
  const then = new Date(ts).getTime();
  if (isNaN(then)) return String(ts);
  const secs = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (secs < 60) return `${secs}s ago`;
  if (secs < 3600) return `${Math.round(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.round(secs / 3600)}h ago`;
  return `${Math.round(secs / 86400)}d ago`;
}

export function fmtPct(v) {
  if (v == null || isNaN(v)) return "—";
  const n = v <= 1 ? v * 100 : v;
  return `${Math.round(n)}%`;
}

export function fmtMs(ms) {
  if (ms == null) return "—";
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(2)}s`;
}

// ─── Incident-shape helpers (derive fields the API does not store directly) ──

export function parseMaybeJSON(value) {
  if (value == null) return null;
  if (typeof value === "object") return value;
  if (typeof value === "string" && value.trim()) {
    try { return JSON.parse(value); } catch { return null; }
  }
  return null;
}

/** Derive an investigation status from persisted incident fields. */
export function deriveStatus(incident) {
  if (!incident) return { key: "unknown", label: "Unknown", color: STATE.idle };
  if (incident.requires_human) return { key: "escalated", label: "Escalated", color: STATE.failed };
  if (incident.root_cause) return { key: "resolved", label: "Resolved", color: STATE.done };
  return { key: "investigating", label: "Investigating", color: STATE.active };
}

/** Extract retrieved-evidence chunks from an incident's RAG payload. */
export function getEvidence(incident) {
  const rag = parseMaybeJSON(incident?.llm_response);
  let causes = rag?.findings?.possible_causes;
  if (!Array.isArray(causes)) {
    const findings = parseMaybeJSON(incident?.findings);
    if (Array.isArray(findings)) {
      const ragEntry = findings.find((f) => f?.type === "rag");
      causes = ragEntry?.data?.possible_causes;
    }
  }
  return Array.isArray(causes) ? causes : [];
}

/** Count of retrieved evidence chunks recorded for the incident. */
export function getRetrievedCount(incident) {
  const rag = parseMaybeJSON(incident?.llm_response);
  const direct = rag?.findings?.retrieved_count;
  if (typeof direct === "number") return direct;
  const findings = parseMaybeJSON(incident?.findings);
  if (Array.isArray(findings)) {
    const ragEntry = findings.find((f) => f?.type === "rag");
    if (typeof ragEntry?.data?.retrieved_count === "number") return ragEntry.data.retrieved_count;
  }
  const evidence = getEvidence(incident);
  return evidence.length;
}

/** Derive a recommended action (UI hint) from persisted fields. */
export function getRecommendedAction(incident) {
  const rag = parseMaybeJSON(incident?.llm_response);
  if (rag?.recommended_action) return rag.recommended_action;
  if (rag?.findings?.recommended_action) return rag.findings.recommended_action;
  if (incident?.requires_human) return "Escalate to human review";
  if (incident?.action_taken) return "Automated Slack alert dispatched";
  return "Continue monitoring";
}

// ─── Icons (inline SVG, no dependency) ──────────────────────────────────────

const ICON_PATHS = {
  activity:  "M22 12h-4l-3 9L9 3l-3 9H2",
  alert:     "M12 9v4M12 17h.01M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z",
  check:     "M20 6 9 17l-5-5",
  x:         "M18 6 6 18M6 6l12 12",
  clock:     "M12 6v6l4 2M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20z",
  layers:    "M12 2 2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5",
  target:    "M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20zM12 18a6 6 0 1 0 0-12 6 6 0 0 0 0 12zM12 14a2 2 0 1 0 0-4 2 2 0 0 0 0 4z",
  database:  "M12 8c4.42 0 8-1.34 8-3s-3.58-3-8-3-8 1.34-8 3 3.58 3 8 3zM4 5v6c0 1.66 3.58 3 8 3s8-1.34 8-3V5M4 11v6c0 1.66 3.58 3 8 3s8-1.34 8-3v-6",
  zap:       "M13 2 3 14h9l-1 8 10-12h-9l1-8z",
  search:    "M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16zM21 21l-4.35-4.35",
  code:      "m16 18 6-6-6-6M8 6l-6 6 6 6",
  branch:    "M6 3v12M18 9a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM6 21a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM15 6a9 9 0 0 1-9 9",
  mail:      "M4 4h16a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2zM22 6l-10 7L2 6",
  message:   "M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z",
  ticket:    "M3 7v3a2 2 0 0 1 0 4v3a1 1 0 0 0 1 1h16a1 1 0 0 0 1-1v-3a2 2 0 0 1 0-4V7a1 1 0 0 0-1-1H4a1 1 0 0 0-1 1z",
  chevron:   "m6 9 6 6 6-6",
  shield:    "M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z",
  bolt:      "M13 2 3 14h9l-1 8 10-12h-9l1-8z",
  play:      "m5 3 14 9-14 9V3z",
};

export function Icon({ name, size = 16, color = "currentColor", style = {} }) {
  const d = ICON_PATHS[name] || ICON_PATHS.activity;
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none"
      stroke={color} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
      style={{ flexShrink: 0, ...style }} aria-hidden="true">
      <path d={d} />
    </svg>
  );
}

// ─── Card ───────────────────────────────────────────────────────────────────

export function Card({ children, accent, className = "", style = {}, ...rest }) {
  return (
    <div className={`aeam-card ${className}`} style={style} {...rest}>
      {accent && <span className="aeam-card-accent" style={{ background: accent }} />}
      {children}
    </div>
  );
}

export function CardTitle({ icon, children, right }) {
  return (
    <div className="aeam-card-title-row">
      <div className="aeam-card-title">
        {icon && <Icon name={icon} size={13} />}
        <span>{children}</span>
      </div>
      {right}
    </div>
  );
}

// ─── Badge ──────────────────────────────────────────────────────────────────

export function Badge({ label, color = "#5a5f72", dot = false, subtle = true, style = {} }) {
  return (
    <span
      style={{
        display: "inline-flex", alignItems: "center", gap: "0.4rem",
        fontSize: "0.68rem", fontWeight: 700, letterSpacing: "0.08em",
        textTransform: "uppercase",
        color, background: subtle ? `${color}16` : color,
        border: `1px solid ${color}40`, borderRadius: "20px",
        padding: "0.22rem 0.65rem", whiteSpace: "nowrap", ...style,
      }}
    >
      {dot && <span style={{
        width: 6, height: 6, borderRadius: "50%", background: color,
        boxShadow: `0 0 6px ${color}`,
      }} />}
      {label}
    </span>
  );
}

export function SeverityBadge({ severity }) {
  const s = severityOf(severity);
  return <Badge label={severity || "—"} color={s.color} subtle />;
}

export function StatusBadge({ status }) {
  const color = stateColor(status);
  return <Badge label={status || "—"} color={color} dot />;
}

// ─── Field (label / value pair) ──────────────────────────────────────────────

export function Field({ label, value, mono = false, color, title }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "0.3rem", minWidth: 0 }}>
      <span style={{
        fontSize: "0.62rem", textTransform: "uppercase", letterSpacing: "0.12em",
        color: "var(--muted)",
      }}>{label}</span>
      <span title={title || (typeof value === "string" ? value : undefined)} style={{
        fontSize: "0.85rem", fontWeight: 600,
        color: color || "var(--text)",
        fontFamily: mono ? "var(--font-mono)" : "inherit",
        lineHeight: 1.4, wordBreak: "break-word",
      }}>{value ?? "—"}</span>
    </div>
  );
}

// ─── Confidence bar ───────────────────────────────────────────────────────────

export function ConfidenceBar({ value, width = "100%" }) {
  const pct = value == null ? 0 : Math.round((value <= 1 ? value * 100 : value));
  const color = pct >= 80 ? "#00ffa3" : pct >= 50 ? "#ffb800" : "#ff5f57";
  return (
    <div style={{ display: "flex", alignItems: "center", gap: "0.6rem", width }}>
      <div style={{ flex: 1, height: 6, background: "var(--border)", borderRadius: 3, overflow: "hidden" }}>
        <div style={{ height: "100%", width: `${pct}%`, background: color, borderRadius: 3, transition: "width 0.4s ease" }} />
      </div>
      <span style={{ fontFamily: "var(--font-mono)", fontSize: "0.72rem", color, fontWeight: 700 }}>
        {value == null ? "—" : `${pct}%`}
      </span>
    </div>
  );
}

// ─── Button ───────────────────────────────────────────────────────────────────

export function Button({ children, icon, onClick, variant = "ghost", disabled, style = {} }) {
  return (
    <button className={`aeam-btn aeam-btn-${variant}`} onClick={onClick} disabled={disabled} style={style}>
      {icon && <Icon name={icon} size={13} />}
      {children}
    </button>
  );
}

// ─── Modal ────────────────────────────────────────────────────────────────────

export function Modal({ title, icon, onClose, children, maxWidth = 760 }) {
  useEffect(() => {
    const onKey = (e) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("keydown", onKey);
    document.body.style.overflow = "hidden";
    return () => { document.removeEventListener("keydown", onKey); document.body.style.overflow = ""; };
  }, [onClose]);

  return (
    <div className="aeam-modal-overlay" onClick={onClose}>
      <div className="aeam-modal" style={{ maxWidth }} onClick={(e) => e.stopPropagation()}>
        <div className="aeam-modal-head">
          <div className="aeam-card-title">
            {icon && <Icon name={icon} size={15} />}
            <span style={{ fontSize: "0.9rem", color: "var(--text)", letterSpacing: "0.04em" }}>{title}</span>
          </div>
          <button className="aeam-modal-close" onClick={onClose} aria-label="Close"><Icon name="x" size={16} /></button>
        </div>
        <div className="aeam-modal-body">{children}</div>
      </div>
    </div>
  );
}

// ─── Collapsible ────────────────────────────────────────────────────────────────

export function Collapsible({ summary, children, defaultOpen = false }) {
  return (
    <details className="aeam-collapsible" open={defaultOpen}>
      <summary className="aeam-collapsible-summary">
        <Icon name="chevron" size={14} style={{ transition: "transform 0.2s" }} />
        {summary}
      </summary>
      <div className="aeam-collapsible-body">{children}</div>
    </details>
  );
}

// ─── Skeleton ───────────────────────────────────────────────────────────────────

export function Skeleton({ width = "100%", height = 16, style = {} }) {
  return <div style={{ width, height, background: "var(--border)", borderRadius: 4, animation: "aeamPulse 1.2s ease-in-out infinite", ...style }} />;
}

// ─── Global UI stylesheet (injected once) ─────────────────────────────────────

const UI_CSS = `
  @keyframes aeamPulse { 0%,100%{opacity:1} 50%{opacity:0.4} }
  @keyframes aeamFade { from{opacity:0;transform:translateY(8px)} to{opacity:1;transform:translateY(0)} }

  .aeam-page { animation: aeamFade 0.3s ease forwards; }

  .aeam-grid-auto { display:grid; gap:1.1rem; grid-template-columns:repeat(auto-fit,minmax(215px,1fr)); }
  .aeam-grid-2 { display:grid; gap:1.1rem; grid-template-columns:repeat(auto-fit,minmax(330px,1fr)); }

  .aeam-card {
    position:relative; overflow:hidden;
    background:var(--surface); border:1px solid var(--border);
    border-radius:12px; padding:1.35rem 1.5rem;
    transition:border-color 0.15s, box-shadow 0.15s, transform 0.15s;
  }
  .aeam-card-hover:hover { border-color:#2c3142; box-shadow:0 4px 18px rgba(0,0,0,0.35); }
  .aeam-card-accent { position:absolute; top:0; left:0; width:100%; height:2px; }

  .aeam-card-title-row { display:flex; align-items:center; justify-content:space-between; margin-bottom:1rem; }
  .aeam-card-title {
    display:flex; align-items:center; gap:0.5rem;
    font-size:0.65rem; text-transform:uppercase; letter-spacing:0.14em;
    color:var(--muted); font-weight:600;
  }

  .aeam-btn {
    display:inline-flex; align-items:center; gap:0.4rem;
    font-size:0.72rem; letter-spacing:0.06em; font-family:var(--font-body);
    border-radius:7px; padding:0.4rem 0.75rem; cursor:pointer;
    transition:all 0.15s; background:none;
  }
  .aeam-btn:disabled { opacity:0.5; cursor:default; }
  .aeam-btn-ghost { color:var(--muted); border:1px solid var(--border); }
  .aeam-btn-ghost:hover:not(:disabled) { color:var(--accent); border-color:var(--accent); }
  .aeam-btn-primary { color:var(--accent); border:1px solid rgba(0,255,163,0.4); background:var(--accent-dim); }
  .aeam-btn-primary:hover:not(:disabled) { background:rgba(0,255,163,0.16); }

  .aeam-modal-overlay {
    position:fixed; inset:0; z-index:1000;
    background:rgba(3,5,10,0.72); backdrop-filter:blur(3px);
    display:flex; align-items:center; justify-content:center; padding:1.5rem;
    animation:aeamFade 0.18s ease forwards;
  }
  .aeam-modal {
    width:100%; max-height:86vh; display:flex; flex-direction:column;
    background:var(--surface); border:1px solid var(--border);
    border-radius:14px; overflow:hidden; box-shadow:0 24px 60px rgba(0,0,0,0.55);
  }
  .aeam-modal-head {
    display:flex; align-items:center; justify-content:space-between;
    padding:1.1rem 1.4rem; border-bottom:1px solid var(--border); flex-shrink:0;
  }
  .aeam-modal-close { background:none; border:none; color:var(--muted); cursor:pointer; padding:0.25rem; border-radius:6px; transition:color 0.15s; }
  .aeam-modal-close:hover { color:var(--text); }
  .aeam-modal-body { padding:1.4rem; overflow-y:auto; }

  .aeam-collapsible { border:1px solid var(--border); border-radius:9px; background:rgba(255,255,255,0.015); }
  .aeam-collapsible-summary {
    list-style:none; cursor:pointer; display:flex; align-items:center; gap:0.5rem;
    padding:0.75rem 0.9rem; font-size:0.78rem; color:var(--text); user-select:none;
  }
  .aeam-collapsible-summary::-webkit-details-marker { display:none; }
  .aeam-collapsible[open] > .aeam-collapsible-summary svg { transform:rotate(180deg); }
  .aeam-collapsible-body { padding:0 0.9rem 0.9rem; }

  .aeam-json {
    font-family:var(--font-mono); font-size:0.72rem; line-height:1.55;
    color:#9fe8c8; background:#0a0c11; border:1px solid var(--border);
    border-radius:9px; padding:1rem 1.1rem; overflow:auto; max-height:60vh;
    white-space:pre; margin:0;
  }

  @media (max-width:760px) {
    main { padding:1.4rem !important; }
    .aeam-hide-sm { display:none !important; }
  }
`;

export function UIStyles() {
  return <style>{UI_CSS}</style>;
}

// ─── Page header ────────────────────────────────────────────────────────────────

export function PageHeader({ title, subtitle, right }) {
  return (
    <div style={{
      display: "flex", alignItems: "flex-end", justifyContent: "space-between",
      marginBottom: "2rem", gap: "1rem", flexWrap: "wrap",
    }}>
      <div>
        <h1 style={{
          fontSize: "1.7rem", fontWeight: 700, fontFamily: "var(--font-display)",
          color: "var(--text)", margin: 0, lineHeight: 1.2,
        }}>{title}</h1>
        {subtitle && <p style={{ margin: "0.4rem 0 0", color: "var(--muted)", fontSize: "0.8rem", letterSpacing: "0.04em" }}>{subtitle}</p>}
      </div>
      {right && <div style={{ display: "flex", alignItems: "center", gap: "0.75rem" }}>{right}</div>}
    </div>
  );
}
