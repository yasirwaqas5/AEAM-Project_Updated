import { useEffect, useRef, useState, useMemo } from "react";

/* ──────────────────────────────────────────────────────────────────────────
 * components/charts.jsx — AEAM data-visualization kit.
 * Pure SVG + rAF. No chart library. Every mark draws from real values the
 * caller already fetched — these components never invent data.
 * All motion respects prefers-reduced-motion.
 * ────────────────────────────────────────────────────────────────────────── */

const prefersReduced = () =>
  typeof window !== "undefined" &&
  window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;

/* ─── CountUp — animated numeric roll-up ─────────────────────────────────── */
export function CountUp({ value, format, duration = 700 }) {
  const [shown, setShown] = useState(prefersReduced() ? value : 0);
  const fromRef = useRef(0);

  useEffect(() => {
    if (value == null || Number.isNaN(value)) return;
    // Skip straight to the final value when motion is reduced OR the tab is
    // hidden/throttled (rAF may never fire there — the number must still land).
    if (prefersReduced() || document.hidden) { setShown(value); fromRef.current = value; return; }
    const from = fromRef.current;
    const start = performance.now();
    let raf;
    const tick = (now) => {
      const t = Math.min(1, (now - start) / duration);
      const eased = 1 - Math.pow(1 - t, 3);
      setShown(from + (value - from) * eased);
      if (t < 1) raf = requestAnimationFrame(tick);
      else fromRef.current = value;
    };
    raf = requestAnimationFrame(tick);
    // Guarantee the final value even if rAF stalls mid-flight.
    const settle = setTimeout(() => { setShown(value); fromRef.current = value; }, duration + 150);
    return () => { cancelAnimationFrame(raf); clearTimeout(settle); };
  }, [value, duration]);

  if (value == null || Number.isNaN(value)) return "—";
  const v = Math.abs(value % 1) > 0 ? shown : Math.round(shown);
  return format ? format(v) : String(Math.round(v));
}

/* ─── Sparkline — mini trend line with gradient fill + draw-in ───────────── */
export function Sparkline({ values = [], color = "var(--accent)", width = 120, height = 34, strokeWidth = 1.75 }) {
  const id = useRef(`sp${Math.random().toString(36).slice(2, 8)}`).current;
  const pts = useMemo(() => {
    const nums = values.filter((v) => typeof v === "number" && !Number.isNaN(v));
    if (nums.length < 2) return null;
    const min = Math.min(...nums), max = Math.max(...nums);
    const range = max - min || 1;
    const px = 3;
    return nums.map((v, i) => [
      px + (i * (width - px * 2)) / (nums.length - 1),
      height - px - ((v - min) / range) * (height - px * 2),
    ]);
  }, [values, width, height]);

  if (!pts) return <span style={{ color: "var(--faint)", fontSize: "var(--fs-2xs)" }}>insufficient data</span>;

  const line = pts.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  const area = `${line} L${pts[pts.length - 1][0].toFixed(1)},${height} L${pts[0][0].toFixed(1)},${height} Z`;

  return (
    <svg width={width} height={height} aria-hidden="true" style={{ display: "block", overflow: "visible" }}>
      <defs>
        <linearGradient id={id} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={color} stopOpacity="0.25" />
          <stop offset="100%" stopColor={color} stopOpacity="0" />
        </linearGradient>
      </defs>
      <path d={area} fill={`url(#${id})`} />
      <path
        d={line} fill="none" stroke={color} strokeWidth={strokeWidth}
        strokeLinecap="round" strokeLinejoin="round"
        style={prefersReduced() ? undefined : {
          strokeDasharray: 400, "--dash": 400,
          animation: "aeamDraw 1s var(--ease-out) forwards",
        }}
      />
      <circle cx={pts[pts.length - 1][0]} cy={pts[pts.length - 1][1]} r="2.4" fill={color}
        style={{ filter: `drop-shadow(0 0 3px ${color})` }} />
    </svg>
  );
}

/* ─── ProgressRing — score/health donut with center label ────────────────── */
export function ProgressRing({ value, size = 96, stroke = 7, color, label, sublabel }) {
  const pct = value == null ? null : Math.max(0, Math.min(100, Math.round(value <= 1 ? value * 100 : value)));
  const c = color || (pct == null ? "var(--faint)" : pct >= 70 ? "var(--ok)" : pct >= 40 ? "var(--warn)" : "var(--err)");
  const r = (size - stroke) / 2;
  const circ = 2 * Math.PI * r;
  const [drawn, setDrawn] = useState(prefersReduced());
  useEffect(() => { const t = setTimeout(() => setDrawn(true), 40); return () => clearTimeout(t); }, []);

  return (
    <div style={{ position: "relative", width: size, height: size, flexShrink: 0 }}
      role="img" aria-label={`${label || sublabel || "progress"}: ${pct == null ? "unavailable" : `${pct}%`}`}>
      <svg width={size} height={size} style={{ transform: "rotate(-90deg)" }}>
        <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="var(--surface-3)" strokeWidth={stroke} />
        {pct != null && (
          <circle
            cx={size / 2} cy={size / 2} r={r} fill="none" stroke={c} strokeWidth={stroke}
            strokeLinecap="round" strokeDasharray={circ}
            strokeDashoffset={drawn ? circ * (1 - pct / 100) : circ}
            style={{ transition: "stroke-dashoffset 1s var(--ease-out)", filter: `drop-shadow(0 0 5px color-mix(in srgb, ${c} 50%, transparent))` }}
          />
        )}
      </svg>
      <div style={{
        position: "absolute", inset: 0, display: "flex", flexDirection: "column",
        alignItems: "center", justifyContent: "center", gap: 1,
      }}>
        <span style={{ fontFamily: "var(--font-mono)", fontWeight: 700, color: c, fontSize: size / 4.6, lineHeight: 1, fontVariantNumeric: "tabular-nums" }}>
          {pct == null ? "N/A" : <><CountUp value={pct} />%</>}
        </span>
        {sublabel && <span style={{ fontSize: "var(--fs-2xs)", color: "var(--muted)", letterSpacing: ".08em", textTransform: "uppercase" }}>{sublabel}</span>}
      </div>
    </div>
  );
}

/* ─── BarTrend — daily/bucketed bar chart with rise-in + hover tooltip ───── */
export function BarTrend({ buckets = [], color = "var(--accent)", height = 120, valueLabel = "events" }) {
  const max = Math.max(...buckets.map((b) => b.count), 1);
  if (!buckets.length) return <span style={{ color: "var(--faint)", fontSize: "var(--fs-xs)" }}>No data in range.</span>;
  return (
    <div style={{ display: "flex", alignItems: "flex-end", gap: 4, height, padding: "4px 0" }}>
      {buckets.map((b, i) => (
        <div key={b.label ?? i} title={`${b.label}: ${b.count} ${valueLabel}`}
          style={{ flex: 1, display: "flex", flexDirection: "column", alignItems: "center", gap: 5, minWidth: 0, height: "100%", justifyContent: "flex-end" }}>
          <div style={{
            width: "100%", maxWidth: 26, borderRadius: "4px 4px 2px 2px",
            height: `${Math.max(3, (b.count / max) * 82)}%`,
            background: b.count === 0 ? "var(--surface-3)"
              : `linear-gradient(180deg, ${color}, color-mix(in srgb, ${color} 45%, transparent))`,
            boxShadow: b.count > 0 ? `0 0 8px color-mix(in srgb, ${color} 25%, transparent)` : "none",
            animation: prefersReduced() ? "none" : "aeamRise .55s var(--ease-out) backwards",
            animationDelay: `${i * 28}ms`,
            transition: "filter var(--t-fast)",
          }}
            onMouseEnter={(e) => (e.currentTarget.style.filter = "brightness(1.25)")}
            onMouseLeave={(e) => (e.currentTarget.style.filter = "")} />
          {b.label != null && (
            <span style={{ fontSize: "var(--fs-2xs)", color: "var(--faint)", fontFamily: "var(--font-mono)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", maxWidth: "100%" }}>
              {b.label}
            </span>
          )}
        </div>
      ))}
    </div>
  );
}

/* ─── SegmentBar — horizontal composition bar (active/resolved/failed…) ──── */
export function SegmentBar({ segments = [], height = 10 }) {
  const total = segments.reduce((s, x) => s + x.value, 0);
  if (!total) return <span style={{ color: "var(--faint)", fontSize: "var(--fs-xs)" }}>No data.</span>;
  return (
    <div>
      <div style={{ display: "flex", height, borderRadius: height / 2, overflow: "hidden", background: "var(--surface-3)" }}>
        {segments.filter((s) => s.value > 0).map((s) => (
          <div key={s.label} title={`${s.label}: ${s.value}`}
            style={{ width: `${(s.value / total) * 100}%`, background: s.color, transition: "width .6s var(--ease-out)" }} />
        ))}
      </div>
      <div style={{ display: "flex", gap: "1.1rem", marginTop: 8, flexWrap: "wrap" }}>
        {segments.map((s) => (
          <span key={s.label} style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: "var(--fs-2xs)", color: "var(--muted)" }}>
            <span style={{ width: 8, height: 8, borderRadius: 2, background: s.color }} />
            {s.label} <b style={{ color: "var(--text-2)", fontFamily: "var(--font-mono)" }}>{s.value}</b>
          </span>
        ))}
      </div>
    </div>
  );
}
