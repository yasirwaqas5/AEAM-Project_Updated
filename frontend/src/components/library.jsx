import { Icon } from "./ui";

/* ──────────────────────────────────────────────────────────────────────────
 * components/library.jsx
 * Reusable content primitives that every future AEAM page composes from.
 * Built on top of the existing ui.jsx tokens/primitives — nothing here
 * duplicates Card/Badge/Button/Modal/PageHeader, which live in ui.jsx and
 * remain unchanged. This file adds the pieces the Enterprise Shell needs:
 * page container, section header, metric card, status pieces, empty/loading/
 * error states, data table, panel, split layout, timeline + graph containers,
 * and the coming-soon placeholder — plus the shell stylesheet (ShellStyles).
 * ────────────────────────────────────────────────────────────────────────── */

// ─── Page container ───────────────────────────────────────────────────────
// Every page renders inside this — one workspace layout, no duplication.
export function PageContainer({ children, max = 1200, style = {} }) {
  return (
    <div className="aeam-page aeam-page-container" style={{ maxWidth: max, ...style }}>
      {children}
    </div>
  );
}

// ─── Section header (lighter than ui.jsx PageHeader) ───────────────────────
export function SectionHeader({ title, description, right, icon }) {
  return (
    <div className="aeam-section-head">
      <div style={{ minWidth: 0 }}>
        <div className="aeam-section-title">
          {icon && <Icon name={icon} size={15} />}
          <span>{title}</span>
        </div>
        {description && <p className="aeam-section-desc">{description}</p>}
      </div>
      {right && <div className="aeam-section-actions">{right}</div>}
    </div>
  );
}

// ─── Status dot / badge ────────────────────────────────────────────────────
const STATE_COLOR = {
  ok: "var(--ok)", healthy: "var(--ok)", success: "var(--ok)",
  degraded: "var(--warn)", disabled: "var(--warn)", pending: "var(--warn)",
  error: "var(--err)", down: "var(--err)",
  unknown: "var(--muted)",
};
export function stateToColor(s) { return STATE_COLOR[String(s).toLowerCase()] || "var(--muted)"; }

export function StatusDot({ state = "unknown", label, size = 8, pulse = false }) {
  const color = stateToColor(state);
  return (
    <span className="aeam-status-dot-wrap">
      <span className={`aeam-status-dot${pulse ? " pulse" : ""}`}
        style={{ width: size, height: size, background: color, boxShadow: `0 0 6px ${color}` }} />
      {label && <span className="aeam-status-dot-label">{label}</span>}
    </span>
  );
}

// ─── Metric card ───────────────────────────────────────────────────────────
export function MetricCard({ label, value, sub, icon, accent = "var(--accent)", loading = false }) {
  return (
    <div className="aeam-metric">
      <div className="aeam-metric-top">
        <span className="aeam-metric-label">{label}</span>
        {icon && <Icon name={icon} size={14} color="var(--muted)" />}
      </div>
      <div className="aeam-metric-value" style={{ color: accent }}>
        {loading ? <span className="aeam-skel" style={{ width: 64, height: 28 }} /> : (value ?? "—")}
      </div>
      {sub && <div className="aeam-metric-sub">{sub}</div>}
    </div>
  );
}

// ─── Panel (titled bordered container) ─────────────────────────────────────
export function Panel({ title, icon, right, children, scroll = false, pad = true, style = {} }) {
  return (
    <section className="aeam-panel" style={style}>
      {(title || right) && (
        <header className="aeam-panel-head">
          <div className="aeam-panel-title">
            {icon && <Icon name={icon} size={13} />}
            <span>{title}</span>
          </div>
          {right}
        </header>
      )}
      <div className={`aeam-panel-body${scroll ? " scroll" : ""}${pad ? "" : " nopad"}`}>{children}</div>
    </section>
  );
}

// ─── Empty / loading / error states ────────────────────────────────────────
export function EmptyState({ icon = "layers", title, description, action, tone = "muted" }) {
  return (
    <div className={`aeam-state aeam-state-${tone}`}>
      <span className="aeam-state-icon"><Icon name={icon} size={24} /></span>
      <div className="aeam-state-title">{title}</div>
      {description && <p className="aeam-state-desc">{description}</p>}
      {action && <div className="aeam-state-action">{action}</div>}
    </div>
  );
}

export function LoadingState({ label = "Loading…", rows = 3 }) {
  return (
    <div className="aeam-loading" aria-busy="true" aria-live="polite">
      <div className="aeam-loading-label">
        <span className="aeam-spinner" /> {label}
      </div>
      <div className="aeam-loading-rows">
        {Array.from({ length: rows }).map((_, i) => (
          <span key={i} className="aeam-skel" style={{ height: 40, width: `${100 - i * 8}%` }} />
        ))}
      </div>
    </div>
  );
}

export function ErrorState({ message = "Something went wrong.", onRetry }) {
  return (
    <div className="aeam-state aeam-state-error">
      <span className="aeam-state-icon"><Icon name="alert" size={24} color="var(--err)" /></span>
      <div className="aeam-state-title" style={{ color: "var(--err)" }}>Request failed</div>
      <p className="aeam-state-desc" style={{ fontFamily: "var(--font-mono)" }}>{message}</p>
      {onRetry && (
        <div className="aeam-state-action">
          <button className="aeam-btn aeam-btn-ghost" onClick={onRetry}><Icon name="activity" size={13} /> Retry</button>
        </div>
      )}
    </div>
  );
}

// ─── Data table ────────────────────────────────────────────────────────────
// columns: [{ key, label, align, width, render(row) }]
export function DataTable({ columns = [], rows = [], empty = "No rows.", rowKey }) {
  return (
    <div className="aeam-tbl-wrap">
      <table className="aeam-tbl">
        <thead>
          <tr>{columns.map((c) => (
            <th key={c.key} style={{ textAlign: c.align || "left", width: c.width }}>{c.label}</th>
          ))}</tr>
        </thead>
        <tbody>
          {rows.length === 0 ? (
            <tr><td className="aeam-tbl-empty" colSpan={columns.length}>{empty}</td></tr>
          ) : rows.map((row, i) => (
            <tr key={rowKey ? rowKey(row, i) : i}>
              {columns.map((c) => (
                <td key={c.key} style={{ textAlign: c.align || "left" }}>
                  {c.render ? c.render(row) : row[c.key] ?? "—"}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ─── Split layout (two panes) ──────────────────────────────────────────────
export function SplitLayout({ left, right, ratio = "1fr 1fr", gap = "1.1rem", style = {} }) {
  return (
    <div className="aeam-split" style={{ gridTemplateColumns: ratio, gap, ...style }}>
      <div className="aeam-split-pane" style={{ minWidth: 0 }}>{left}</div>
      <div className="aeam-split-pane" style={{ minWidth: 0 }}>{right}</div>
    </div>
  );
}

// ─── Timeline container (vertical rail) ────────────────────────────────────
export function TimelineContainer({ children }) {
  return <div className="aeam-timeline">{children}</div>;
}
export function TimelineItem({ color = "var(--accent)", title, time, children }) {
  return (
    <div className="aeam-timeline-item">
      <span className="aeam-timeline-node" style={{ background: color, boxShadow: `0 0 0 3px color-mix(in srgb, ${color} 14%, transparent)` }} />
      <div className="aeam-timeline-content">
        <div className="aeam-timeline-head">
          <span className="aeam-timeline-title">{title}</span>
          {time && <span className="aeam-timeline-time">{time}</span>}
        </div>
        {children && <div className="aeam-timeline-body">{children}</div>}
      </div>
    </div>
  );
}

// ─── Graph container placeholder ───────────────────────────────────────────
// A reusable chart frame (faux axes) that future analytics mount real charts
// into. Shows an honest "chart mounts here" state — not fake data.
export function GraphPlaceholder({ title = "Chart", height = 220, note = "Visualization mounts here" }) {
  return (
    <div className="aeam-graph" style={{ height }}>
      <div className="aeam-graph-yaxis" />
      <div className="aeam-graph-xaxis" />
      <div className="aeam-graph-grid" aria-hidden="true">
        {Array.from({ length: 4 }).map((_, i) => <span key={i} />)}
      </div>
      <div className="aeam-graph-center">
        <Icon name="target" size={22} color="var(--muted)" />
        <span className="aeam-graph-title">{title}</span>
        <span className="aeam-graph-note">{note}</span>
      </div>
    </div>
  );
}

// ─── Coming-soon placeholder page body ─────────────────────────────────────
export function ComingSoon({ icon = "layers", title, description, phase, points = [] }) {
  return (
    <div className="aeam-comingsoon">
      <EmptyState
        icon={icon}
        title={title}
        description={description}
        action={phase && <span className="aeam-phase-chip">Ships in Phase {phase}</span>}
      />
      {points.length > 0 && (
        <ul className="aeam-comingsoon-points">
          {points.map((p, i) => <li key={i}>{p}</li>)}
        </ul>
      )}
    </div>
  );
}

// ─── Shell stylesheet (injected once by AppShell) ──────────────────────────
const SHELL_CSS = `
  :root{
    --sidebar-w:248px; --sidebar-min:190px; --sidebar-max:340px;
    --topbar-h:56px; --statusbar-h:30px;
  }

  /* layout shell */
  .aeam-shell{ display:grid; grid-template-columns:var(--sidebar-w) minmax(0,1fr);
    height:100vh; overflow:hidden; }
  .aeam-shell[data-collapsed="true"]{ --sidebar-w:66px; }
  .aeam-maincol{ display:grid; grid-template-rows:var(--topbar-h) minmax(0,1fr) var(--statusbar-h);
    min-width:0; height:100vh; }
  .aeam-workspace{ overflow-y:auto; overflow-x:hidden; background:
    radial-gradient(1200px 500px at 75% -10%, rgba(56,120,220,.05), transparent 60%),
    var(--bg); }
  .aeam-page-container{ padding:2.1rem clamp(1.1rem,3vw,2.6rem) 3rem; margin:0 auto; width:100%; }

  /* skip link (a11y) */
  .aeam-skip{ position:absolute; left:-9999px; top:0; z-index:3000; background:var(--accent);
    color:#0a0e14; font-weight:700; padding:.6rem 1rem; border-radius:0 0 var(--r-md) 0; }
  .aeam-skip:focus{ left:0; }

  /* sidebar */
  .aeam-sidebar{ position:relative; display:flex; flex-direction:column; min-width:0;
    background:linear-gradient(180deg,var(--sidebar) 0%, #0a0d14 100%);
    border-right:1px solid var(--border); height:100vh; overflow:hidden; }
  .aeam-sidebar-head{ display:flex; align-items:center; gap:.6rem; height:var(--topbar-h);
    padding:0 1rem; border-bottom:1px solid var(--border); flex:none; }
  .aeam-logo{ display:inline-flex; align-items:center; gap:.55rem; font-family:var(--font-display);
    font-weight:700; letter-spacing:.12em; color:var(--text); font-size:.95rem; text-decoration:none; white-space:nowrap; }
  .aeam-logo .mark{ color:var(--accent); font-size:1.05rem; filter:drop-shadow(0 0 6px rgba(91,157,255,.55)); }
  .aeam-collapse-btn{ margin-left:auto; background:none; border:1px solid var(--border); color:var(--muted);
    width:26px; height:26px; border-radius:var(--r-sm); cursor:pointer; display:inline-flex; align-items:center; justify-content:center;
    transition:color var(--t-fast),border-color var(--t-fast); }
  .aeam-collapse-btn:hover{ color:var(--accent); border-color:var(--accent-border); }
  .aeam-nav{ flex:1; overflow-y:auto; padding:.7rem .55rem 1.2rem; }
  .aeam-nav-group{ margin-bottom:.35rem; }
  .aeam-nav-group-label{ font-size:var(--fs-2xs); letter-spacing:.14em; text-transform:uppercase; color:var(--faint);
    padding:.9rem .7rem .35rem; font-weight:600; }
  .aeam-nav-link{ display:flex; align-items:center; gap:.7rem; padding:.52rem .7rem; border-radius:var(--r-md);
    color:var(--muted); text-decoration:none; font-size:var(--fs-sm); position:relative;
    transition:background var(--t-fast) var(--ease-out),color var(--t-fast) var(--ease-out); white-space:nowrap; }
  .aeam-nav-link:hover{ background:rgba(255,255,255,.03); color:var(--text); }
  .aeam-nav-link.active{ background:linear-gradient(90deg, rgba(91,157,255,.14), rgba(91,157,255,.05));
    color:#cfe1ff; }
  .aeam-nav-link.active::before{ content:""; position:absolute; left:-0.55rem; top:20%; bottom:20%; width:3px;
    border-radius:2px; background:linear-gradient(180deg,var(--glow),var(--accent));
    box-shadow:0 0 8px rgba(91,157,255,.5); }
  .aeam-nav-link.active svg{ color:var(--accent); }
  .aeam-nav-link .lbl{ flex:1; overflow:hidden; text-overflow:ellipsis; }
  .aeam-nav-soon{ font-size:var(--fs-2xs); letter-spacing:.06em; text-transform:uppercase; color:var(--faint);
    border:1px solid var(--border-2); border-radius:4px; padding:0 5px; transform:scale(.85); }
  .aeam-shell[data-collapsed="true"] .aeam-nav-group-label,
  .aeam-shell[data-collapsed="true"] .aeam-nav-link .lbl,
  .aeam-shell[data-collapsed="true"] .aeam-nav-soon,
  .aeam-shell[data-collapsed="true"] .aeam-logo span,
  .aeam-shell[data-collapsed="true"] .aeam-sidebar-foot{ display:none; }
  .aeam-shell[data-collapsed="true"] .aeam-nav-link{ justify-content:center; padding:.55rem; }
  .aeam-sidebar-foot{ flex:none; padding:.7rem 1rem; border-top:1px solid var(--border);
    font-family:var(--font-mono); font-size:.6rem; color:var(--muted); line-height:1.7; }
  .aeam-resize{ position:absolute; top:0; right:-3px; width:6px; height:100%; cursor:col-resize; z-index:5; }
  .aeam-resize:hover{ background:linear-gradient(90deg,transparent,var(--accent-dim)); }

  /* topbar — glass */
  .aeam-topbar{ display:flex; align-items:center; gap:.9rem; padding:0 clamp(.9rem,2vw,1.5rem);
    background:var(--glass-bg); backdrop-filter:var(--glass-blur); -webkit-backdrop-filter:var(--glass-blur);
    border-bottom:1px solid var(--border); min-width:0; position:relative; z-index:20; }
  .aeam-hamburger{ display:none; background:none; border:1px solid var(--border); color:var(--muted);
    width:32px; height:32px; border-radius:8px; cursor:pointer; align-items:center; justify-content:center; }
  .aeam-crumbs{ display:flex; align-items:center; gap:.45rem; font-size:.78rem; color:var(--muted); min-width:0; }
  .aeam-crumbs .sep{ color:var(--border-2); }
  .aeam-crumbs .cur{ color:var(--text); font-weight:600; }
  .aeam-search{ flex:1; max-width:420px; display:flex; align-items:center; gap:.5rem;
    background:var(--bg); border:1px solid var(--border); border-radius:9px; padding:.42rem .7rem; color:var(--muted); }
  .aeam-search input{ flex:1; background:none; border:none; outline:none; color:var(--text); font-size:.8rem;
    font-family:var(--font-body); min-width:0; }
  .aeam-search input::placeholder{ color:var(--muted); }
  .aeam-search kbd{ font-family:var(--font-mono); font-size:.6rem; border:1px solid var(--border-2);
    border-radius:4px; padding:1px 5px; color:var(--muted); }
  .aeam-topbar-right{ display:flex; align-items:center; gap:.6rem; margin-left:auto; }
  .aeam-env{ font-family:var(--font-mono); font-size:.6rem; letter-spacing:.1em; text-transform:uppercase;
    border-radius:6px; padding:.28rem .55rem; border:1px solid; white-space:nowrap; }
  .aeam-sys-pill{ display:inline-flex; align-items:center; gap:.45rem; font-size:.7rem; color:var(--muted);
    border:1px solid var(--border); border-radius:20px; padding:.3rem .7rem; background:var(--bg); white-space:nowrap; }
  .aeam-icon-btn{ position:relative; background:none; border:1px solid var(--border); color:var(--muted);
    width:34px; height:34px; border-radius:9px; cursor:pointer; display:inline-flex; align-items:center; justify-content:center; }
  .aeam-icon-btn:hover{ color:var(--accent); border-color:var(--accent); }
  .aeam-icon-btn .badge{ position:absolute; top:-4px; right:-4px; min-width:15px; height:15px; padding:0 3px;
    border-radius:8px; background:var(--err); color:var(--bg); font-size:.55rem; font-weight:800;
    display:flex; align-items:center; justify-content:center; border:2px solid var(--sidebar); }
  .aeam-user{ display:inline-flex; align-items:center; gap:.55rem; cursor:default; }
  .aeam-avatar{ width:30px; height:30px; border-radius:8px; background:var(--accent-dim); color:var(--accent);
    display:flex; align-items:center; justify-content:center; font-weight:700; font-size:.72rem; border:1px solid rgba(91,157,255,.3); }
  .aeam-user-meta{ display:flex; flex-direction:column; line-height:1.15; }
  .aeam-user-name{ font-size:.75rem; color:var(--text); font-weight:600; }
  .aeam-user-role{ font-size:.6rem; color:var(--muted); letter-spacing:.05em; }

  /* statusbar */
  .aeam-statusbar{ display:flex; align-items:center; gap:1.1rem; padding:0 1.2rem; background:var(--sidebar);
    border-top:1px solid var(--border); font-family:var(--font-mono); font-size:.62rem; color:var(--muted);
    overflow-x:auto; white-space:nowrap; }
  .aeam-statusbar .grp{ display:inline-flex; align-items:center; gap:.4rem; }
  .aeam-statusbar .sep{ color:var(--border-2); }
  .aeam-statusbar .k{ text-transform:uppercase; letter-spacing:.08em; opacity:.75; }
  .aeam-statusbar .ver{ margin-left:auto; }

  /* status dot — static glow; a single soft ring communicates "live" without
     infinite flashing (glow = focus, not decoration). */
  .aeam-status-dot-wrap{ display:inline-flex; align-items:center; gap:.4rem; }
  .aeam-status-dot{ border-radius:50%; flex:none; display:inline-block; }
  .aeam-status-dot.pulse{ box-shadow:0 0 0 3px color-mix(in srgb, currentColor 14%, transparent); }
  .aeam-status-dot-label{ color:var(--text); }

  /* section header */
  .aeam-section-head{ display:flex; align-items:flex-end; justify-content:space-between; gap:1rem;
    flex-wrap:wrap; margin-bottom:1.3rem; }
  .aeam-section-title{ display:flex; align-items:center; gap:.55rem; font-size:1.05rem; font-weight:700;
    color:var(--text); font-family:var(--font-display); }
  .aeam-section-desc{ margin:.3rem 0 0; color:var(--muted); font-size:.78rem; letter-spacing:.02em; max-width:70ch; }
  .aeam-section-actions{ display:flex; align-items:center; gap:.6rem; }

  /* metric card */
  .aeam-grid-metrics{ display:grid; gap:1rem; grid-template-columns:repeat(auto-fit,minmax(190px,1fr)); }
  .aeam-metric{ background:linear-gradient(180deg,var(--surface-2),var(--surface));
    border:1px solid var(--border); border-radius:var(--r-lg);
    padding:1.1rem 1.25rem; display:flex; flex-direction:column; gap:.5rem;
    box-shadow:var(--e1), var(--edge);
    transition:border-color var(--t-fast) var(--ease-out), box-shadow var(--t-med) var(--ease-out), transform var(--t-med) var(--ease-out); }
  .aeam-metric:hover{ border-color:var(--border-hi); box-shadow:var(--e2), var(--edge); transform:translateY(-2px); }
  .aeam-metric-top{ display:flex; align-items:center; justify-content:space-between; }
  .aeam-metric-label{ font-size:var(--fs-2xs); text-transform:uppercase; letter-spacing:.12em; color:var(--muted); font-weight:600; }
  .aeam-metric-value{ font-family:var(--font-mono); font-size:1.9rem; font-weight:700; line-height:1;
    font-variant-numeric:tabular-nums; letter-spacing:-0.02em; }
  .aeam-metric-sub{ font-size:var(--fs-xs); color:var(--muted); }

  /* panel */
  .aeam-panel{ background:linear-gradient(180deg,var(--surface-2),var(--surface));
    border:1px solid var(--border); border-radius:var(--r-lg); overflow:hidden;
    box-shadow:var(--e1), var(--edge); }
  .aeam-panel-head{ display:flex; align-items:center; justify-content:space-between; gap:.75rem;
    padding:.85rem 1.1rem; border-bottom:1px solid var(--border); background:rgba(255,255,255,.015); }
  .aeam-panel-title{ display:flex; align-items:center; gap:.5rem; font-size:var(--fs-2xs); text-transform:uppercase;
    letter-spacing:.12em; color:var(--muted); font-weight:700; }
  .aeam-panel-body{ padding:1.1rem 1.25rem; }
  .aeam-panel-body.nopad{ padding:0; }
  .aeam-panel-body.scroll{ overflow:auto; max-height:60vh; }

  /* states */
  .aeam-state{ display:flex; flex-direction:column; align-items:center; text-align:center; gap:.7rem;
    padding:3rem 2rem; border:1px dashed var(--border); border-radius:var(--radius); background:rgba(255,255,255,.012); }
  .aeam-state-icon{ display:flex; align-items:center; justify-content:center; width:52px; height:52px;
    border-radius:14px; background:var(--surface); border:1px solid var(--border); color:var(--muted); }
  .aeam-state-title{ font-size:.95rem; font-weight:700; color:var(--text); }
  .aeam-state-desc{ margin:0; color:var(--muted); font-size:.8rem; max-width:52ch; line-height:1.6; }
  .aeam-state-action{ margin-top:.4rem; }
  .aeam-state-error{ border-color:rgba(255,95,87,.3); background:rgba(255,95,87,.04); }

  .aeam-loading{ display:flex; flex-direction:column; gap:1rem; padding:.5rem 0; }
  .aeam-loading-label{ display:flex; align-items:center; gap:.6rem; color:var(--muted); font-size:.8rem; font-family:var(--font-mono); }
  .aeam-loading-rows{ display:flex; flex-direction:column; gap:.7rem; }
  .aeam-spinner{ width:14px; height:14px; border:2px solid var(--border); border-top-color:var(--accent);
    border-radius:50%; animation:aeamSpin .8s linear infinite; display:inline-block; }
  .aeam-skel{ display:block; background:linear-gradient(90deg,var(--border),var(--surface-2),var(--border));
    background-size:200% 100%; border-radius:6px; animation:aeamShimmer 1.3s ease-in-out infinite; }

  /* table */
  .aeam-tbl-wrap{ overflow-x:auto; border:1px solid var(--border); border-radius:var(--radius); background:var(--surface); }
  .aeam-tbl{ width:100%; border-collapse:collapse; font-size:.8rem; min-width:520px; }
  .aeam-tbl thead th{ text-align:left; font-size:.58rem; text-transform:uppercase; letter-spacing:.11em;
    color:var(--muted); font-weight:700; padding:.75rem 1rem; border-bottom:1px solid var(--border-2);
    background:var(--surface-2); position:sticky; top:0; }
  .aeam-tbl td{ padding:.7rem 1rem; border-bottom:1px solid var(--border); color:var(--text); vertical-align:top; }
  .aeam-tbl tbody tr:last-child td{ border-bottom:none; }
  .aeam-tbl tbody tr{ transition:background var(--t-fast); }
  .aeam-tbl tbody tr:hover td{ background:rgba(91,157,255,.045); }
  .aeam-tbl-empty{ text-align:center; color:var(--muted); padding:2.2rem !important; font-style:italic; }

  /* split + timeline + graph */
  .aeam-split{ display:grid; }
  @media (max-width:820px){ .aeam-split{ grid-template-columns:1fr !important; } }
  .aeam-timeline{ display:flex; flex-direction:column; }
  .aeam-timeline-item{ display:grid; grid-template-columns:16px 1fr; gap:.9rem; padding-bottom:1.1rem; position:relative; }
  .aeam-timeline-item:not(:last-child)::before{ content:""; position:absolute; left:7px; top:16px; bottom:0; width:1px; background:var(--border-2); }
  .aeam-timeline-node{ width:14px; height:14px; border-radius:50%; margin-top:2px; z-index:1; }
  .aeam-timeline-head{ display:flex; align-items:baseline; justify-content:space-between; gap:1rem; }
  .aeam-timeline-title{ font-size:.82rem; font-weight:600; color:var(--text); }
  .aeam-timeline-time{ font-family:var(--font-mono); font-size:.66rem; color:var(--muted); }
  .aeam-timeline-body{ margin-top:.35rem; color:var(--muted); font-size:.76rem; }

  .aeam-graph{ position:relative; background:var(--surface); border:1px solid var(--border); border-radius:var(--radius); overflow:hidden; }
  .aeam-graph-yaxis{ position:absolute; left:34px; top:14px; bottom:30px; width:1px; background:var(--border-2); }
  .aeam-graph-xaxis{ position:absolute; left:34px; right:16px; bottom:30px; height:1px; background:var(--border-2); }
  .aeam-graph-grid{ position:absolute; left:34px; right:16px; top:14px; bottom:31px; display:flex; flex-direction:column; justify-content:space-between; }
  .aeam-graph-grid span{ height:1px; background:var(--border); opacity:.5; }
  .aeam-graph-center{ position:absolute; inset:0; display:flex; flex-direction:column; align-items:center; justify-content:center; gap:.4rem; }
  .aeam-graph-title{ font-size:.8rem; color:var(--text); font-weight:600; }
  .aeam-graph-note{ font-size:.68rem; color:var(--muted); }

  /* coming soon */
  .aeam-comingsoon{ display:flex; flex-direction:column; gap:1.4rem; }
  .aeam-phase-chip{ display:inline-flex; align-items:center; font-family:var(--font-mono); font-size:var(--fs-2xs);
    letter-spacing:.06em; color:var(--accent); background:var(--accent-dim); border:1px solid var(--accent-border);
    border-radius:20px; padding:.32rem .8rem; }
  .aeam-comingsoon-points{ margin:0 auto; max-width:640px; width:100%; list-style:none; padding:0;
    display:flex; flex-direction:column; gap:.6rem; }
  .aeam-comingsoon-points li{ position:relative; padding:.7rem .9rem .7rem 2rem; background:var(--surface);
    border:1px solid var(--border); border-radius:var(--r-md); color:var(--muted); font-size:var(--fs-sm); }
  .aeam-comingsoon-points li::before{ content:""; position:absolute; left:.85rem; top:50%; transform:translateY(-50%);
    width:6px; height:6px; border-radius:2px; background:var(--accent); }

  /* toasts */
  .aeam-toast-host{ position:fixed; right:1.1rem; bottom:calc(var(--statusbar-h) + 1rem); z-index:2000;
    display:flex; flex-direction:column; gap:.6rem; max-width:min(360px,90vw); }
  .aeam-toast{ display:flex; align-items:flex-start; gap:.7rem; background:var(--surface-2);
    border:1px solid var(--border-2); border-left:3px solid var(--tc); border-radius:10px; padding:.75rem .85rem;
    box-shadow:0 12px 32px rgba(0,0,0,.5); animation:aeamToastIn .22s ease forwards; }
  .aeam-toast-icon{ flex:none; margin-top:1px; }
  .aeam-toast-body{ flex:1; min-width:0; }
  .aeam-toast-title{ font-size:.78rem; font-weight:600; color:var(--text); }
  .aeam-toast-detail{ font-size:.7rem; color:var(--muted); margin-top:.2rem; line-height:1.5; }
  .aeam-toast-close{ background:none; border:none; color:var(--muted); cursor:pointer; padding:.1rem; flex:none; }
  .aeam-toast-close:hover{ color:var(--text); }

  /* hero grid (Dashboard) */
  @media (max-width:880px){ .aeam-hero-grid{ grid-template-columns:1fr !important; }
    .aeam-hero-grid > div:last-child{ border-left:none !important; border-top:1px solid var(--border); min-height:200px !important; } }

  /* mobile drawer */
  .aeam-backdrop{ display:none; }
  @media (max-width:900px){
    .aeam-shell{ grid-template-columns:1fr; }
    .aeam-sidebar{ position:fixed; top:0; left:0; width:260px; z-index:1200; transform:translateX(-100%);
      transition:transform .2s ease; }
    .aeam-shell[data-mobile-open="true"] .aeam-sidebar{ transform:translateX(0); }
    .aeam-shell[data-collapsed="true"]{ --sidebar-w:1fr; }
    .aeam-shell[data-collapsed="true"] .aeam-nav-link .lbl,
    .aeam-shell[data-collapsed="true"] .aeam-nav-group-label{ display:block; }
    .aeam-resize{ display:none; }
    .aeam-hamburger{ display:inline-flex; }
    .aeam-backdrop{ display:block; position:fixed; inset:0; background:rgba(3,5,10,.6); z-index:1100; }
    .aeam-search{ max-width:none; }
  }
  @media (max-width:640px){
    .aeam-search, .aeam-user-meta, .aeam-sys-pill .txt{ display:none; }
  }

  @keyframes aeamSpin{ to{ transform:rotate(360deg); } }
  @keyframes aeamShimmer{ 0%{ background-position:200% 0; } 100%{ background-position:-200% 0; } }
  @keyframes aeamToastIn{ from{ opacity:0; transform:translateX(12px) scale(.98); } to{ opacity:1; transform:none; } }
  @keyframes aeamDraw{ from{ stroke-dashoffset:var(--dash,300); } to{ stroke-dashoffset:0; } }
`;

export function ShellStyles() {
  return <style>{SHELL_CSS}</style>;
}
