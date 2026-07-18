import { Badge, Icon, getExecutionPlanData } from "./ui";

/* ──────────────────────────────────────────────────────────────────────────
 * Enterprise Execution Plan view (Phase C7).
 *
 * Renders the Enterprise Action Planning Engine's single explainable
 * execution plan — the FINAL reasoning stage before Human Review and
 * ActionAgent, synthesizing every prior evidence source (Enterprise Memory,
 * Enterprise Policies, Cross-Dataset Intelligence, Adaptive Detection,
 * Advanced Retrieval) into one plan. See
 * aeam/intelligence/execution_planning.py: this performs NO retrieval, NO
 * detection, and no LLM call — it only reads findings the Orchestrator
 * already accumulated, and it never alters ActionAgent's own execution
 * (which runs from the deterministic runbook, unchanged).
 *
 * Honest states:
 * - Never consulted for this investigation (older incident, or the engine
 *   was unavailable at the time) — getExecutionPlanData() is null.
 * - Consulted but evidence was genuinely insufficient — rendered with the
 *   explicit "insufficient evidence" note, never a fabricated plan.
 * - Consulted, real plan — recommended actions (ordered, each attributing
 *   its source evidence), supporting evidence, conflicts (if any), risk/
 *   impact assessment, confidence, and the human-approval classification.
 * ────────────────────────────────────────────────────────────────────────── */

function classificationColor(classification) {
  if (classification === "execute_immediately") return "var(--ok)";
  if (classification === "requires_human_approval") return "var(--warn)";
  return "var(--info)"; // informational_only
}

function classificationLabel(classification) {
  if (classification === "execute_immediately") return "execute immediately";
  if (classification === "requires_human_approval") return "requires human approval";
  if (classification === "informational_only") return "informational only";
  return classification || "unclassified";
}

function InsufficientNote({ children }) {
  return (
    <div style={{
      display: "flex", alignItems: "flex-start", gap: "0.5rem",
      padding: "0.55rem 0.8rem", border: "1px dashed var(--border)", borderRadius: 8,
      color: "var(--muted)", fontSize: "0.72rem",
    }}>
      <Icon name="alert" size={12} color="var(--muted)" style={{ marginTop: "0.1rem", flexShrink: 0 }} />
      <span>{children}</span>
    </div>
  );
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

function RecommendedActionRow({ action }) {
  return (
    <div style={{
      display: "flex", flexDirection: "column", gap: "0.3rem",
      padding: "0.55rem 0.8rem", border: "1px solid var(--border)", borderRadius: 8,
      background: "rgba(255,255,255,0.015)",
    }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: "0.6rem", flexWrap: "wrap" }}>
        <span style={{ fontSize: "0.78rem", color: "var(--text)", fontWeight: 600 }}>
          {action.order}. {action.action}
        </span>
        <div style={{ display: "flex", gap: "0.35rem", alignItems: "center" }}>
          <Badge label={action.source} color="var(--muted)" />
          <Badge label={classificationLabel(action.classification)} color={classificationColor(action.classification)} />
        </div>
      </div>
      <span style={{ fontSize: "0.71rem", color: "var(--muted)" }}>{action.rationale}</span>
    </div>
  );
}

export default function ExecutionPlanPanel({ incident }) {
  const data = getExecutionPlanData(incident);

  if (data === null) {
    return (
      <div style={{
        textAlign: "center", padding: "2rem 1rem", color: "var(--muted)",
        fontSize: "0.8rem", border: "1px dashed var(--border)", borderRadius: 10,
      }}>
        Enterprise Action Planning Engine was not consulted for this investigation.
      </div>
    );
  }

  const actions = data.recommended_actions || [];
  const evidence = data.supporting_evidence || [];
  const conflicts = data.evidence_conflicts || [];
  const confidencePct = typeof data.confidence === "number" ? Math.round(data.confidence * 100) : null;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "1.1rem" }}>
      {/* Executive Summary */}
      <div>
        <SectionLabel icon="activity">Executive Summary</SectionLabel>
        <p style={{ fontSize: "0.8rem", color: "var(--text)", lineHeight: 1.5, marginTop: "0.4rem" }}>
          {data.executive_summary || "Not available."}
        </p>
        {data.insufficient_evidence && (
          <InsufficientNote>
            Insufficient evidence to synthesize a confident plan — recommendations below (if any) are standard runbook guidance only, never fabricated.
          </InsufficientNote>
        )}
      </div>

      {/* Confidence / Evidence Quality / Human Approval */}
      <div style={{ display: "flex", gap: "0.5rem", flexWrap: "wrap" }}>
        {confidencePct != null && <Badge label={`confidence ${confidencePct}%`} color={confidencePct >= 70 ? "var(--ok)" : confidencePct >= 40 ? "var(--warn)" : "var(--err)"} />}
        {data.evidence_quality && <Badge label={`evidence: ${data.evidence_quality}`} color={data.evidence_quality === "high" ? "var(--ok)" : data.evidence_quality === "insufficient" ? "var(--err)" : "var(--warn)"} />}
        <Badge label={data.human_approval_required ? "human approval required" : "no approval required"} color={data.human_approval_required ? "var(--warn)" : "var(--ok)"} />
      </div>

      {/* Recommended Actions (order) */}
      <div style={{ display: "flex", flexDirection: "column", gap: "0.5rem" }}>
        <SectionLabel icon="zap">Recommended Actions ({actions.length})</SectionLabel>
        {data.order_rationale && (
          <span style={{ fontSize: "0.7rem", color: "var(--muted)", fontStyle: "italic" }}>{data.order_rationale}</span>
        )}
        {actions.length === 0 ? (
          <div style={{ textAlign: "center", padding: "0.9rem 0.8rem", color: "var(--muted)", fontSize: "0.75rem", border: "1px dashed var(--border)", borderRadius: 8 }}>
            No recommendations synthesized.
          </div>
        ) : (
          actions.map((a, i) => <RecommendedActionRow key={i} action={a} />)
        )}
      </div>

      {/* Supporting Evidence */}
      {evidence.length > 0 && (
        <div style={{ display: "flex", flexDirection: "column", gap: "0.4rem" }}>
          <SectionLabel icon="database">Supporting Evidence ({evidence.length})</SectionLabel>
          {evidence.map((e, i) => (
            <div key={i} style={{ display: "flex", alignItems: "center", gap: "0.5rem", fontSize: "0.74rem", color: "var(--text)" }}>
              <Badge label={e.source} color="var(--muted)" />
              <span>{e.summary}</span>
            </div>
          ))}
        </div>
      )}

      {/* Evidence Conflicts */}
      <div style={{ display: "flex", flexDirection: "column", gap: "0.4rem" }}>
        <SectionLabel icon="alert">Evidence Conflicts ({conflicts.length})</SectionLabel>
        {conflicts.length === 0 ? (
          <span style={{ fontSize: "0.74rem", color: "var(--muted)" }}>None detected — evidence sources agree.</span>
        ) : (
          conflicts.map((c, i) => (
            <div key={i} style={{
              display: "flex", alignItems: "flex-start", gap: "0.5rem",
              padding: "0.5rem 0.75rem", border: "1px solid var(--warn)", borderRadius: 8,
              background: "rgba(255,184,0,0.06)", fontSize: "0.74rem", color: "var(--text)",
            }}>
              <Icon name="alert" size={12} color="var(--warn)" style={{ marginTop: "0.1rem", flexShrink: 0 }} />
              <span>{c.description}</span>
            </div>
          ))
        )}
      </div>

      {/* Business Risk / Expected Impact */}
      {(data.business_risk_assessment || data.expected_impact) && (
        <div style={{ display: "flex", flexDirection: "column", gap: "0.5rem" }}>
          {data.business_risk_assessment && (
            <div>
              <SectionLabel icon="shield">Business Risk Assessment</SectionLabel>
              <p style={{ fontSize: "0.76rem", color: "var(--text)", marginTop: "0.3rem" }}>{data.business_risk_assessment}</p>
            </div>
          )}
          {data.expected_impact && (
            <div>
              <SectionLabel icon="branch">Expected Impact</SectionLabel>
              <p style={{ fontSize: "0.76rem", color: "var(--text)", marginTop: "0.3rem" }}>{data.expected_impact}</p>
            </div>
          )}
        </div>
      )}

      {/* Explanation */}
      {data.explanation && (
        <div>
          <SectionLabel icon="code">Explanation</SectionLabel>
          <p style={{ fontSize: "0.75rem", color: "var(--muted)", marginTop: "0.3rem", lineHeight: 1.5 }}>{data.explanation}</p>
        </div>
      )}
    </div>
  );
}
