import { Badge, Icon, getAIEvaluationData } from "./ui";

/* ──────────────────────────────────────────────────────────────────────────
 * Enterprise AI Evaluation view (Phase D2).
 *
 * Renders the Enterprise AI Evaluation & Quality Engine's score of this
 * investigation's own THOROUGHNESS — a SEVENTH, structurally distinct
 * advisory source: it never changes findings, the Execution Plan (C7), or
 * the Explainability object (D1); it only evaluates them. See
 * aeam/intelligence/ai_evaluation.py: every component score is a fully
 * disclosed ratio/count over already-computed evidence — never a new
 * detection, retrieval, or LLM judgement, and never a probability
 * (orthogonal to the system's own root-cause confidence).
 *
 * Honest states:
 * - Never consulted for this investigation — getAIEvaluationData() is null.
 * - Consulted but no execution plan existed to evaluate — overall_score is
 *   null, rendered with the explicit note.
 * - Consulted, real evaluation — component scores (each either a real
 *   number or explicitly "not computable"), strengths, weaknesses, missing
 *   evidence, and improvement opportunities.
 * ────────────────────────────────────────────────────────────────────────── */

const COMPONENT_LABELS = {
  evidence_coverage: "Evidence Coverage",
  retrieval_quality: "Retrieval Quality",
  memory_quality: "Memory Quality",
  policy_coverage: "Policy Coverage",
  cross_dataset_coverage: "Cross-Dataset Coverage",
  adaptive_detection_coverage: "Adaptive Detection Coverage",
  conflict_severity: "Conflict Severity",
  evidence_diversity: "Evidence Diversity",
  recommendation_quality: "Recommendation Quality",
  investigation_completeness: "Investigation Completeness",
};

function scoreColor(score, inverse) {
  if (score == null) return "var(--muted)";
  const v = inverse ? 1 - score : score;
  return v >= 0.7 ? "var(--ok)" : v >= 0.4 ? "var(--warn)" : "var(--err)";
}

function SectionLabel({ icon, children }) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: "0.4rem" }}>
      <Icon name={icon} size={13} color="var(--info)" />
      <span style={{ fontSize: "0.68rem", textTransform: "uppercase", letterSpacing: "0.1em", color: "var(--muted)", fontWeight: 700 }}>
        {children}
      </span>
    </div>
  );
}

function EmptyNote({ children }) {
  return <span style={{ fontSize: "0.74rem", color: "var(--muted)" }}>{children}</span>;
}

function ComponentScoreRow({ id, comp }) {
  const isConflict = id === "conflict_severity";
  const pct = typeof comp.score === "number" ? Math.round(comp.score * 100) : null;
  return (
    <div style={{
      display: "flex", flexDirection: "column", gap: "0.25rem",
      padding: "0.5rem 0.75rem", border: "1px solid var(--border)", borderRadius: 8,
      background: "rgba(255,255,255,0.015)",
    }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: "0.5rem", flexWrap: "wrap" }}>
        <span style={{ fontSize: "0.76rem", color: "var(--text)", fontWeight: 600 }}>{COMPONENT_LABELS[id] || id}</span>
        {pct != null ? (
          <Badge label={`${pct}%`} color={scoreColor(comp.score, isConflict)} />
        ) : (
          <Badge label="not computable" color="var(--muted)" />
        )}
      </div>
      <span style={{ fontSize: "0.7rem", color: "var(--muted)" }}>{comp.reason}</span>
    </div>
  );
}

export default function AIEvaluationPanel({ incident }) {
  const data = getAIEvaluationData(incident);

  if (data === null) {
    return (
      <div style={{
        textAlign: "center", padding: "2rem 1rem", color: "var(--muted)",
        fontSize: "0.8rem", border: "1px dashed var(--border)", borderRadius: 10,
      }}>
        Enterprise AI Evaluation & Quality Engine was not consulted for this investigation.
      </div>
    );
  }

  const components = data.component_scores || {};
  const strengths = data.strengths || [];
  const weaknesses = data.weaknesses || [];
  const missing = data.missing_evidence || [];
  const opportunities = data.improvement_opportunities || [];
  const overallPct = typeof data.overall_score === "number" ? Math.round(data.overall_score * 100) : null;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "1.1rem" }}>
      {/* Overall Score */}
      <div>
        <SectionLabel icon="activity">Overall Investigation Score</SectionLabel>
        <div style={{ display: "flex", alignItems: "center", gap: "0.6rem", marginTop: "0.4rem", flexWrap: "wrap" }}>
          {overallPct != null ? (
            <Badge label={`${overallPct}%`} color={scoreColor(data.overall_score)} />
          ) : (
            <Badge label="not computable" color="var(--muted)" />
          )}
          <span style={{ fontSize: "0.72rem", color: "var(--muted)" }}>{data.overall_score_formula}</span>
        </div>
        <p style={{ fontSize: "0.78rem", color: "var(--text)", lineHeight: 1.5, marginTop: "0.5rem" }}>
          {data.quality_summary || "Not available."}
        </p>
      </div>

      {/* Component Scores */}
      <div style={{ display: "flex", flexDirection: "column", gap: "0.5rem" }}>
        <SectionLabel icon="branch">Component Scores ({Object.keys(components).length})</SectionLabel>
        {Object.entries(components).map(([id, comp]) => (
          <ComponentScoreRow key={id} id={id} comp={comp} />
        ))}
      </div>

      {/* Strengths */}
      <div style={{ display: "flex", flexDirection: "column", gap: "0.4rem" }}>
        <SectionLabel icon="check">Strengths ({strengths.length})</SectionLabel>
        {strengths.length === 0 ? <EmptyNote>None identified.</EmptyNote> : (
          <ul style={{ margin: 0, paddingLeft: "1.1rem", display: "flex", flexDirection: "column", gap: "0.25rem" }}>
            {strengths.map((s, i) => <li key={i} style={{ fontSize: "0.74rem", color: "var(--text)" }}>{s}</li>)}
          </ul>
        )}
      </div>

      {/* Weaknesses */}
      <div style={{ display: "flex", flexDirection: "column", gap: "0.4rem" }}>
        <SectionLabel icon="alert">Weaknesses ({weaknesses.length})</SectionLabel>
        {weaknesses.length === 0 ? <EmptyNote>None identified.</EmptyNote> : (
          <ul style={{ margin: 0, paddingLeft: "1.1rem", display: "flex", flexDirection: "column", gap: "0.25rem" }}>
            {weaknesses.map((w, i) => <li key={i} style={{ fontSize: "0.74rem", color: "var(--text)" }}>{w}</li>)}
          </ul>
        )}
      </div>

      {/* Missing Evidence */}
      <div style={{ display: "flex", flexDirection: "column", gap: "0.4rem" }}>
        <SectionLabel icon="alert">Missing Evidence ({missing.length})</SectionLabel>
        {missing.length === 0 ? <EmptyNote>None.</EmptyNote> : (
          missing.map((m, i) => (
            <div key={i} style={{ display: "flex", alignItems: "center", gap: "0.5rem", fontSize: "0.74rem", color: "var(--text)" }}>
              <Badge label={m.source} color="var(--muted)" />
              <span>{m.reason}</span>
            </div>
          ))
        )}
      </div>

      {/* Improvement Opportunities */}
      <div style={{ display: "flex", flexDirection: "column", gap: "0.4rem" }}>
        <SectionLabel icon="code">Improvement Opportunities ({opportunities.length})</SectionLabel>
        {opportunities.length === 0 ? <EmptyNote>None identified.</EmptyNote> : (
          <ul style={{ margin: 0, paddingLeft: "1.1rem", display: "flex", flexDirection: "column", gap: "0.25rem" }}>
            {opportunities.map((o, i) => <li key={i} style={{ fontSize: "0.74rem", color: "var(--text)" }}>{o}</li>)}
          </ul>
        )}
      </div>
    </div>
  );
}
