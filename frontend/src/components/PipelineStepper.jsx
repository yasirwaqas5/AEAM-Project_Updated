import { useMemo } from "react";
import {
  getMemoryData, getMemoryMatches, getPolicyMatchData, getPolicyMatches,
  getCrossDatasetData, getAdaptiveDetectionData, getExecutionPlanData,
  getExplainabilityData, getAIEvaluationData, getRetrievedCount, getLatestRagData,
} from "./ui";

/* ──────────────────────────────────────────────────────────────────────────
 * components/PipelineStepper.jsx — the Animated Intelligence Pipeline.
 *
 * Renders the REAL investigation flow for one incident as a connected chain
 * of stages, each derived from the same persisted findings the panels below
 * already read (via ui.jsx's existing extractors — no new data logic).
 *
 * Stage states are honest:
 *   "signal"    — the engine ran AND produced usable evidence
 *   "consulted" — the engine ran but found nothing
 *   "skipped"   — the engine never ran for this incident
 * ────────────────────────────────────────────────────────────────────────── */

const STAGES = [
  { key: "detection", label: "Detection", color: "var(--info)" },
  { key: "memory", label: "Memory", color: "var(--c-memory)", tab: "evidence" },
  { key: "policy", label: "Policy", color: "var(--c-policy)", tab: "evidence" },
  { key: "cross", label: "Cross-Dataset", color: "var(--c-cross)", tab: "signals" },
  { key: "adaptive", label: "Adaptive", color: "var(--c-adaptive)", tab: "signals" },
  { key: "retrieval", label: "Retrieval", color: "var(--c-retrieval)", tab: "evidence" },
  { key: "plan", label: "Execution Plan", color: "var(--c-plan)", tab: "plan" },
  { key: "explain", label: "Explainability", color: "var(--c-forecast)", tab: "plan" },
  { key: "eval", label: "Evaluation", color: "var(--c-eval)", tab: "quality" },
];

function deriveStates(incident) {
  const memory = getMemoryData(incident);
  const policy = getPolicyMatchData(incident);
  const cross = getCrossDatasetData(incident);
  const adaptive = getAdaptiveDetectionData(incident);
  const rag = getLatestRagData(incident);
  const plan = getExecutionPlanData(incident);
  const explain = getExplainabilityData(incident);
  const evaluation = getAIEvaluationData(incident);

  const s = (data, hasSignal) => (data == null ? "skipped" : hasSignal ? "signal" : "consulted");
  return {
    detection: "signal", // an incident row existing IS the detection signal
    memory: s(memory, getMemoryMatches(incident).length > 0),
    policy: s(policy, getPolicyMatches(incident).length > 0),
    cross: s(cross, !cross?.insufficient_data &&
      ((cross?.supporting?.length || 0) + (cross?.strong_correlations?.length || 0)) > 0),
    adaptive: s(adaptive, !!(adaptive && (!adaptive.adaptive_baseline_insufficient || !adaptive.seasonality_insufficient))),
    retrieval: s(rag, getRetrievedCount(incident) > 0),
    plan: s(plan, (plan?.recommended_actions?.length || 0) > 0),
    explain: s(explain, !!explain),
    eval: s(evaluation, evaluation?.overall_score != null),
  };
}

const STATE_TITLES = {
  signal: "produced evidence",
  consulted: "consulted — no signal",
  skipped: "not consulted",
};

export default function PipelineStepper({ incident, onStageClick }) {
  const states = useMemo(() => deriveStates(incident), [incident]);

  return (
    <div style={{ overflowX: "auto", padding: "0.4rem 0.2rem 0.6rem" }}>
      <div style={{ display: "flex", alignItems: "flex-start", minWidth: 720 }}>
        {STAGES.map((stage, i) => {
          const state = states[stage.key];
          const active = state === "signal";
          const consulted = state === "consulted";
          return (
            <div key={stage.key} style={{ display: "flex", alignItems: "flex-start", flex: i < STAGES.length - 1 ? 1 : "0 0 auto" }}>
              <button
                onClick={() => stage.tab && onStageClick?.(stage.tab)}
                title={`${stage.label}: ${STATE_TITLES[state]}`}
                style={{
                  display: "flex", flexDirection: "column", alignItems: "center", gap: 7,
                  background: "none", border: "none", cursor: stage.tab ? "pointer" : "default",
                  minWidth: 74, padding: 0,
                }}
              >
                <span style={{
                  width: 15, height: 15, borderRadius: "50%",
                  background: active ? stage.color : "transparent",
                  border: `2px solid ${state === "skipped" ? "var(--border-2)" : stage.color}`,
                  boxShadow: active ? `0 0 10px color-mix(in srgb, ${stage.color} 55%, transparent)` : "none",
                  opacity: state === "skipped" ? 0.5 : 1,
                  transition: "all var(--t-med) var(--ease-out)",
                  animation: "aeamRise .5s var(--ease-out) backwards",
                  animationDelay: `${i * 45}ms`,
                }} />
                <span style={{
                  fontSize: "var(--fs-2xs)", fontWeight: 600, whiteSpace: "nowrap",
                  color: active ? "var(--text)" : consulted ? "var(--muted)" : "var(--faint)",
                }}>
                  {stage.label}
                </span>
                <span style={{ fontSize: "var(--fs-2xs)", color: "var(--faint)", whiteSpace: "nowrap", transform: "scale(.92)" }}>
                  {state === "signal" ? "evidence" : state === "consulted" ? "no signal" : "skipped"}
                </span>
              </button>
              {i < STAGES.length - 1 && (
                <div style={{ flex: 1, height: 2, marginTop: 7, minWidth: 14, borderRadius: 1, position: "relative", overflow: "hidden", background: "var(--surface-3)" }}>
                  <div style={{
                    position: "absolute", inset: 0,
                    background: `linear-gradient(90deg, ${stage.color}, ${STAGES[i + 1].color})`,
                    opacity: states[STAGES[i + 1].key] === "skipped" ? 0.12 : 0.55,
                    transformOrigin: "left",
                    animation: "aeamGrowX .6s var(--ease-out) backwards",
                    animationDelay: `${i * 45 + 60}ms`,
                  }} />
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
