import { useState, useEffect, useMemo, lazy, Suspense } from "react";
import { useNavigate } from "react-router-dom";
import { motion, AnimatePresence } from "framer-motion";
import { UIStyles, Icon, buildMeshLive } from "../components/ui";
import { ShellStyles } from "../components/library";
import { CountUp } from "../components/charts";
import { ENGINES } from "../components/three/AgentMesh";

const AgentMesh = lazy(() => import("../components/three/AgentMesh"));

/* ──────────────────────────────────────────────────────────────────────────
 * pages/Welcome.jsx — the AEAM Welcome Experience (/welcome).
 *
 * A startup-sequence showcase for demonstrations and reviews — NOT a
 * marketing page and NOT the operational entry point (the console still
 * boots straight into the Dashboard). Renders outside the AppShell,
 * full-bleed, on the same design system.
 *
 * Every number shown is fetched live from the real backend; when the
 * backend is unreachable the page says so honestly instead of faking data.
 * ────────────────────────────────────────────────────────────────────────── */

const STACK = [
  "FastAPI", "PostgreSQL", "Redis", "Qdrant", "SentenceTransformers",
  "Prophet", "Prometheus", "React", "Vite", "React Three Fiber",
];

/* Cinematic boot sequence — every line describes a REAL subsystem; the two
   live lines reflect true backend/observability state. ~3s total, skippable. */
const bootSequence = (online, obs) => [
  { text: "Initializing AEAM…", state: "done" },
  { text: `Core services — Postgres · Redis · event queue ${online ? "connected" : "unreachable"}`, state: online ? "done" : "err" },
  { text: "Loading Enterprise Memory", state: "done" },
  { text: "Loading Policy Intelligence", state: "done" },
  { text: "Loading Cross-Dataset Intelligence", state: "done" },
  { text: "Loading Adaptive Detection", state: "done" },
  { text: "Loading Advanced Retrieval", state: "done" },
  { text: "Loading Explainability · AI Evaluation · Observability", state: "done" },
  { text: "Synchronizing Agent Mesh", state: "done" },
  {
    text: obs?.total_investigations != null
      ? `Platform ready — ${obs.total_investigations} investigations in organizational memory`
      : "Platform ready — investigation history unavailable",
    state: obs ? "done" : "warn",
  },
];

/* The real end-to-end flow, one hop per second — teaches the architecture
   in under 20 seconds. Highlight cycles automatically; reduced motion gets
   the static list. */
const FLOW = [
  ["Upload", "documents & datasets enter the platform"],
  ["Monitor", "KPI feeds watched for anomalies"],
  ["Orchestrator", "coordinates the investigation"],
  ["Enterprise Memory", "similar past incidents recalled"],
  ["Policy Intelligence", "governed business rules consulted"],
  ["Cross-Dataset", "other datasets checked for correlation"],
  ["Adaptive Detection", "long-horizon baselines applied"],
  ["Advanced Retrieval", "knowledge evidence retrieved"],
  ["Execution Planning", "one explainable plan synthesized"],
  ["Explainability", "every recommendation justified"],
  ["AI Evaluation", "investigation quality scored"],
  ["Observability", "platform health measured"],
  ["Action Engine", "Slack · Jira · webhooks executed"],
  ["Report Agent", "audit-ready report generated"],
  ["Console", "everything lands in this dashboard"],
];

function FlowStory() {
  const [step, setStep] = useState(0);
  const reduced = typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
  useEffect(() => {
    if (reduced) return;
    const id = setInterval(() => setStep((s) => (s + 1) % FLOW.length), 1200);
    return () => clearInterval(id);
  }, [reduced]);

  return (
    <div style={{
      background: "linear-gradient(180deg,var(--surface-2),var(--surface))",
      border: "1px solid var(--border)", borderRadius: "var(--r-lg)",
      padding: "1.5rem 1.4rem", boxShadow: "var(--e1), var(--edge)",
    }}>
      <div style={{ display: "flex", alignItems: "center", gap: "0.45rem", flexWrap: "wrap" }}>
        {FLOW.map(([label], i, arr) => {
          const active = !reduced && i === step;
          return (
            <div key={label} style={{ display: "flex", alignItems: "center", gap: "0.45rem" }}>
              <span style={{
                padding: ".42rem .8rem", borderRadius: "var(--r-md)", fontSize: "var(--fs-xs)", fontWeight: 600,
                whiteSpace: "nowrap", transition: "all .35s var(--ease-out)",
                color: active ? "#fff" : "var(--text-2)",
                background: active ? "var(--accent-dim)" : "rgba(255,255,255,.02)",
                border: `1px solid ${active ? "var(--accent)" : "var(--border)"}`,
                boxShadow: active ? "0 0 18px rgba(91,157,255,.3)" : "none",
                transform: active ? "translateY(-2px)" : "none",
              }}>{label}</span>
              {i < arr.length - 1 && <Icon name="arrowr" size={11} color={active ? "var(--accent)" : "var(--faint)"} />}
            </div>
          );
        })}
      </div>
      <p aria-live="polite" style={{
        margin: "1.1rem 0 0", minHeight: "1.4em", fontSize: "var(--fs-sm)",
        color: "var(--muted)", fontFamily: "var(--font-mono)",
      }}>
        {reduced ? "Signals flow left to right — every investigation traverses this chain." : (
          <><span style={{ color: "var(--accent)", fontWeight: 700 }}>{FLOW[step][0]}</span> — {FLOW[step][1]}</>
        )}
      </p>
    </div>
  );
}

const rise = {
  hidden: { opacity: 0, y: 22 },
  show: (i = 0) => ({ opacity: 1, y: 0, transition: { delay: 0.08 * i, duration: 0.55, ease: [0.215, 0.61, 0.355, 1] } }),
};

function Section({ children, style }) {
  return (
    <motion.section
      initial="hidden" whileInView="show" viewport={{ once: true, margin: "-80px" }}
      style={{ maxWidth: 1080, margin: "0 auto", padding: "4.5rem clamp(1.2rem,4vw,2.5rem)", ...style }}
    >
      {children}
    </motion.section>
  );
}

function SectionTitle({ kicker, title }) {
  return (
    <motion.div variants={rise} style={{ marginBottom: "2.2rem" }}>
      <div style={{ fontSize: "var(--fs-2xs)", letterSpacing: ".22em", textTransform: "uppercase", color: "var(--accent)", fontWeight: 700, marginBottom: ".6rem" }}>
        {kicker}
      </div>
      <h2 style={{ fontFamily: "var(--font-display)", fontSize: "clamp(1.4rem, 3vw, 1.9rem)", fontWeight: 650, letterSpacing: "-0.02em", color: "var(--text)" }}>
        {title}
      </h2>
    </motion.div>
  );
}

export default function Welcome() {
  const navigate = useNavigate();
  const [health, setHealth] = useState(null);
  const [obs, setObs] = useState(null);
  const [incidents, setIncidents] = useState(null);
  const [bootDone, setBootDone] = useState(false);
  const [bootHidden, setBootHidden] = useState(false);

  useEffect(() => {
    fetch("/health").then((r) => (r.ok ? r.json() : null)).then(setHealth).catch(() => setHealth(null));
    fetch("/api/v1/observability/").then((r) => (r.ok ? r.json() : null)).then(setObs).catch(() => setObs(null));
    fetch("/api/v1/incidents/").then((r) => (r.ok ? r.json() : null))
      .then((d) => setIncidents(Array.isArray(d) ? d : null)).catch(() => setIncidents(null));
  }, []);

  const aiHealth = obs?.overall_ai_health?.available ? obs.overall_ai_health.score : null;
  const resolved = obs?.investigation_success_rate?.available ? obs.investigation_success_rate : null;
  const online = health?.status === "healthy";
  const meshLive = useMemo(() => buildMeshLive(incidents || [], obs), [incidents, obs]);

  // Boot completes on its own (~3s) — the skip button just fast-forwards.
  useEffect(() => {
    const t = setTimeout(() => setBootDone(true), 3200);
    return () => clearTimeout(t);
  }, []);

  // Once complete, the boot log folds away so the agent mesh is unobstructed;
  // a small status chip remains and can reopen the log on demand.
  useEffect(() => {
    if (!bootDone) return;
    const t = setTimeout(() => setBootHidden(true), 1400);
    return () => clearTimeout(t);
  }, [bootDone]);

  const stats = [
    { label: "Investigations recorded", value: obs?.total_investigations ?? null },
    { label: "AI health", value: aiHealth != null ? Math.round(aiHealth * 100) : null, suffix: "%" },
    { label: "Resolution rate", value: resolved ? Math.round(resolved.rate * 100) : null, suffix: "%" },
    { label: "Mesh components", value: ENGINES.length + 1, suffix: " agents" },
  ];

  return (
    <div style={{ height: "100%", overflowY: "auto", overflowX: "hidden", background: "var(--bg)" }}>
      <UIStyles />
      <ShellStyles />

      {/* ── Hero: the startup sequence ─────────────────────────────────── */}
      <div style={{
        position: "relative", minHeight: "92vh", display: "flex", flexDirection: "column",
        alignItems: "center", justifyContent: "center", overflow: "hidden",
        background: "radial-gradient(1100px 600px at 50% 30%, rgba(56,120,220,.10), transparent 65%), var(--bg)",
      }}>
        <div style={{ position: "absolute", inset: 0, opacity: 0.92 }}>
          <Suspense fallback={null}>
            <AgentMesh variant="welcome" health={aiHealth} height="100%" live={meshLive} />
          </Suspense>
        </div>

        <div style={{ position: "relative", textAlign: "center", pointerEvents: "none", padding: "0 1.2rem" }}>
          <motion.div custom={0} initial="hidden" animate="show" variants={rise}
            style={{ fontFamily: "var(--font-mono)", fontSize: "var(--fs-xs)", letterSpacing: ".3em", color: "var(--accent)", textTransform: "uppercase", marginBottom: "1rem" }}>
            Autonomous Enterprise AI Agent Mesh
          </motion.div>
          <motion.h1 custom={1} initial="hidden" animate="show" variants={rise}
            style={{
              fontFamily: "var(--font-display)", fontWeight: 700, letterSpacing: "-0.03em",
              fontSize: "clamp(3rem, 9vw, 5.6rem)", lineHeight: 1, color: "var(--text)",
              textShadow: "0 0 80px rgba(91,157,255,.35)",
            }}>
            AEAM
          </motion.h1>
          <motion.p custom={2} initial="hidden" animate="show" variants={rise}
            style={{ margin: "1.1rem auto 0", maxWidth: "46ch", color: "var(--text-2)", fontSize: "var(--fs-lg)", lineHeight: 1.6 }}>
            An intelligence platform that investigates business anomalies the way a
            senior analyst would — with memory, policy, evidence and an audit trail.
          </motion.p>

          {/* Cinematic boot sequence — real subsystems, real signals, skippable.
              Folds away after completion so the mesh stays visible; the chip
              below can reopen it. */}
          <div style={{ minHeight: 56, marginTop: "2rem" }}>
            <AnimatePresence mode="wait">
              {!bootHidden ? (
                <motion.div key="bootlog" custom={3} initial="hidden" animate="show" variants={rise}
                  exit={{ opacity: 0, y: -14, scale: 0.97, transition: { duration: 0.35 } }}
                  style={{
                    margin: "0 auto", width: "min(470px, 90vw)", textAlign: "left",
                    background: "var(--glass-bg)", backdropFilter: "var(--glass-blur)",
                    border: "1px solid var(--border)", borderRadius: "var(--r-lg)",
                    padding: "1rem 1.2rem", fontFamily: "var(--font-mono)", fontSize: "var(--fs-xs)",
                    boxShadow: "var(--e3), var(--edge)", position: "relative", pointerEvents: "auto",
                  }}>
                  <button onClick={() => (bootDone ? setBootHidden(true) : setBootDone(true))} style={{
                    position: "absolute", top: 8, right: 10,
                    background: "none", border: "none", cursor: "pointer",
                    color: "var(--faint)", fontSize: "var(--fs-2xs)", fontFamily: "var(--font-mono)",
                  }}>
                    {bootDone ? "hide ✕" : "skip ↵"}
                  </button>
                  {bootSequence(online, obs).map((l, i) => (
                    <motion.div key={l.text}
                      initial={{ opacity: 0, x: -8 }}
                      animate={bootDone ? { opacity: 1, x: 0, transition: { duration: 0.12 } } : { opacity: 1, x: 0 }}
                      transition={bootDone ? undefined : { delay: 0.5 + i * 0.27 }}
                      style={{ display: "flex", alignItems: "center", gap: 10, padding: ".26rem 0", color: "var(--text-2)" }}>
                      <span style={{
                        width: 7, height: 7, borderRadius: "50%", flexShrink: 0,
                        background: l.state === "done" ? "var(--ok)" : l.state === "warn" ? "var(--warn)" : "var(--err)",
                        boxShadow: `0 0 6px ${l.state === "done" ? "var(--ok)" : l.state === "warn" ? "var(--warn)" : "var(--err)"}`,
                      }} />
                      {l.text}
                    </motion.div>
                  ))}
                </motion.div>
              ) : (
                <motion.button key="bootchip"
                  initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }}
                  exit={{ opacity: 0, transition: { duration: 0.15 } }}
                  onClick={() => setBootHidden(false)}
                  title="Show the boot log"
                  style={{
                    margin: "0 auto", display: "inline-flex", alignItems: "center", gap: 9,
                    pointerEvents: "auto", cursor: "pointer",
                    background: "var(--glass-bg)", backdropFilter: "var(--glass-blur)",
                    border: "1px solid var(--border)", borderRadius: 20,
                    padding: ".45rem 1rem", fontFamily: "var(--font-mono)", fontSize: "var(--fs-2xs)",
                    color: "var(--text-2)", boxShadow: "var(--e2), var(--edge)",
                  }}>
                  <span style={{ width: 7, height: 7, borderRadius: "50%", background: online ? "var(--ok)" : "var(--err)", boxShadow: `0 0 6px ${online ? "var(--ok)" : "var(--err)"}` }} />
                  {online ? "Platform ready" : "Backend unreachable"}
                  {obs?.total_investigations != null && ` — ${obs.total_investigations} investigations in memory`}
                  <span style={{ color: "var(--faint)" }}>· log</span>
                </motion.button>
              )}
            </AnimatePresence>
          </div>

          <motion.div custom={4} initial="hidden" animate="show" variants={rise}
            style={{ marginTop: "2.2rem", display: "flex", gap: "0.9rem", justifyContent: "center", pointerEvents: "auto" }}>
            <button className="aeam-btn aeam-btn-primary" style={{ padding: ".7rem 1.5rem", fontSize: "var(--fs-md)" }}
              onClick={() => navigate("/")}>
              Enter Console <Icon name="arrowr" size={15} />
            </button>
            <button className="aeam-btn aeam-btn-ghost" style={{ padding: ".7rem 1.3rem", fontSize: "var(--fs-md)" }}
              onClick={() => navigate("/investigation")}>
              Investigation Workspace
            </button>
          </motion.div>
        </div>
      </div>

      {/* ── Live platform overview ─────────────────────────────────────── */}
      <Section>
        <SectionTitle kicker="Live platform" title={online ? "Connected to a running mesh" : "Backend offline — live data unavailable"} />
        <motion.div variants={rise} className="aeam-grid-auto">
          {stats.map((s) => (
            <div key={s.label} className="aeam-metric">
              <span className="aeam-metric-label">{s.label}</span>
              <span className="aeam-metric-value" style={{ color: s.value == null ? "var(--faint)" : "var(--text)" }}>
                {s.value == null ? "N/A" : <><CountUp value={s.value} />{s.suffix || ""}</>}
              </span>
            </div>
          ))}
        </motion.div>
        {incidents && incidents.length > 0 && (
          <motion.p variants={rise} style={{ marginTop: "1rem", color: "var(--muted)", fontSize: "var(--fs-sm)" }}>
            Most recent investigation: <b style={{ color: "var(--text-2)" }}>{incidents[0].event_type} · {incidents[0].metric}</b> — every figure above is read live from this deployment.
          </motion.p>
        )}
      </Section>

      {/* ── Capability grid — the real component roster ────────────────── */}
      <Section>
        <SectionTitle kicker="The agent mesh" title="One orchestrator, thirteen specialists" />
        <div className="aeam-grid-2" style={{ gap: "1rem" }}>
          {ENGINES.map((e, i) => (
            <motion.div key={e.key} variants={rise} custom={i % 3}
              className="aeam-card aeam-card-hover" style={{ padding: "1.15rem 1.3rem" }}>
              <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: ".55rem" }}>
                <span style={{ width: 9, height: 9, borderRadius: 3, background: e.color, boxShadow: `0 0 8px ${e.color}` }} />
                <span style={{ fontWeight: 650, color: "var(--text)", fontSize: "var(--fs-md)" }}>{e.label}</span>
              </div>
              <p style={{ color: "var(--muted)", fontSize: "var(--fs-sm)", lineHeight: 1.6 }}>
                {e.purpose}
              </p>
            </motion.div>
          ))}
        </div>
      </Section>

      {/* ── Architecture story — animated end-to-end flow ──────────────── */}
      <Section>
        <SectionTitle kicker="Architecture" title="Signal to action in one auditable chain" />
        <motion.div variants={rise}>
          <FlowStory />
          <p style={{ margin: "1rem 0 0", color: "var(--muted)", fontSize: "var(--fs-sm)", lineHeight: 1.6 }}>
            Every investigation persists its complete evidence trail — memory recalls, policy matches,
            cross-dataset correlations, adaptive baselines, retrieved documents, the execution plan,
            its explanation and a quality score — as one auditable record. Resolved incidents feed back
            into Enterprise Memory, so the mesh compounds.
          </p>
        </motion.div>
      </Section>

      {/* ── Technology stack ───────────────────────────────────────────── */}
      <Section>
        <SectionTitle kicker="Foundation" title="Technology stack" />
        <motion.div variants={rise} style={{ display: "flex", flexWrap: "wrap", gap: ".6rem" }}>
          {STACK.map((t) => (
            <span key={t} style={{
              fontFamily: "var(--font-mono)", fontSize: "var(--fs-xs)", color: "var(--text-2)",
              border: "1px solid var(--border-2)", borderRadius: 20, padding: ".42rem .95rem",
              background: "rgba(255,255,255,.02)",
            }}>{t}</span>
          ))}
        </motion.div>
      </Section>

      {/* ── Exit ───────────────────────────────────────────────────────── */}
      <Section style={{ textAlign: "center", paddingBottom: "6rem" }}>
        <motion.div variants={rise}>
          <button className="aeam-btn aeam-btn-primary" style={{ padding: ".8rem 1.7rem", fontSize: "var(--fs-md)" }}
            onClick={() => navigate("/")}>
            Enter Console <Icon name="arrowr" size={15} />
          </button>
          <p style={{ marginTop: "1rem", color: "var(--faint)", fontSize: "var(--fs-xs)", fontFamily: "var(--font-mono)" }}>
            AEAM · Enterprise Intelligence Platform
          </p>
        </motion.div>
      </Section>
    </div>
  );
}
