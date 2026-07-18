import { Badge, Icon, getExplainabilityData } from "./ui";

/* ──────────────────────────────────────────────────────────────────────────
 * Enterprise Explainability view (Phase D1).
 *
 * Renders the Enterprise Explainability Engine's explanation of WHY the
 * Execution Plan (Phase C7) reached every recommendation it did — a
 * SIXTH, structurally distinct advisory source: it never changes a
 * recommendation, root_cause, or confidence value, it only explains one
 * that already exists. See aeam/intelligence/explainability.py: this
 * performs NO retrieval/detection/LLM call and reuses ExecutionPlanningEngine's
 * own fixed construction order to recover concrete evidence IDs — never a
 * new inference about the evidence.
 *
 * Honest states:
 * - Never consulted for this investigation — getExplainabilityData() is
 *   null.
 * - Consulted but evidence was genuinely insufficient — rendered with the
 *   explicit note, mirroring ExecutionPlanPanel's own wording.
 * - Consulted, real explanation — recommendation trace, decision graph
 *   (recommendation → evidence → confidence contribution), evidence graph
 *   (every collected evidence node, whether or not it was used),
 *   confidence breakdown (raw vs plan-adjusted, per-source real signals —
 *   never fabricated per-source weights), contradictions, missing
 *   evidence, and assumptions.
 * ────────────────────────────────────────────────────────────────────────── */

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

function DecisionGraphRow({ node }) {
  return (
    <div style={{
      display: "flex", flexDirection: "column", gap: "0.35rem",
      padding: "0.55rem 0.8rem", border: "1px solid var(--border)", borderRadius: 8,
      background: "rgba(255,255,255,0.015)",
    }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: "0.6rem", flexWrap: "wrap" }}>
        <span style={{ fontSize: "0.78rem", color: "var(--text)", fontWeight: 600 }}>
          {node.order}. {node.recommendation}
        </span>
        <Badge label={node.source} color="var(--muted)" />
      </div>
      <div style={{ display: "flex", alignItems: "center", gap: "0.5rem", flexWrap: "wrap", fontSize: "0.71rem", color: "var(--muted)" }}>
        <span>↓ evidence: {node.evidence_id != null ? <code style={{ fontFamily: "var(--font-mono)" }}>{node.evidence_id}</code> : "none (not evidence-derived)"}</span>
        {node.confidence_contribution != null && <Badge label={`signal ${node.confidence_contribution}`} color="var(--info)" />}
      </div>
      <span style={{ fontSize: "0.71rem", color: "var(--muted)" }}>{node.evidence_summary}</span>
      <span style={{ fontSize: "0.66rem", color: "var(--muted)", fontStyle: "italic" }}>Report section: {node.report_section}</span>
    </div>
  );
}

function EvidenceGraphSection({ source, nodes }) {
  if (!nodes || nodes.length === 0) return null;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "0.35rem" }}>
      <span style={{ fontSize: "0.68rem", textTransform: "uppercase", letterSpacing: "0.08em", color: "var(--muted)", fontWeight: 700 }}>
        {source} ({nodes.length})
      </span>
      {nodes.map((n, i) => (
        <div key={i} style={{ display: "flex", alignItems: "center", gap: "0.5rem", fontSize: "0.72rem", color: "var(--text)", flexWrap: "wrap" }}>
          <code style={{ fontFamily: "var(--font-mono)", color: "var(--muted)" }}>{String(n.id)}</code>
          <span style={{ color: "var(--muted)" }}>
            {n.business_rule || n.root_cause || n.cause || n.relation || (n.z_score != null ? `z=${n.z_score}` : "")}
          </span>
        </div>
      ))}
    </div>
  );
}

export default function ExplainabilityPanel({ incident }) {
  const data = getExplainabilityData(incident);

  if (data === null) {
    return (
      <div style={{
        textAlign: "center", padding: "2rem 1rem", color: "var(--muted)",
        fontSize: "0.8rem", border: "1px dashed var(--border)", borderRadius: 10,
      }}>
        Enterprise Explainability Engine was not consulted for this investigation.
      </div>
    );
  }

  const trace = data.recommendation_trace || [];
  const decisionGraph = data.decision_graph || [];
  const evidenceGraph = data.evidence_graph || {};
  const cb = data.confidence_breakdown || {};
  const contradictions = data.contradictions || [];
  const missing = data.missing_evidence || [];
  const assumptions = data.assumptions || [];
  const lpj = data.lower_priority_justification || {};

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "1.1rem" }}>
      {data.insufficient_evidence && (
        <div style={{
          display: "flex", alignItems: "flex-start", gap: "0.5rem",
          padding: "0.55rem 0.8rem", border: "1px dashed var(--border)", borderRadius: 8,
          color: "var(--muted)", fontSize: "0.72rem",
        }}>
          <Icon name="alert" size={12} color="var(--muted)" style={{ marginTop: "0.1rem", flexShrink: 0 }} />
          <span>Evidence was insufficient for a confident plan — this explanation covers runbook-only guidance, never a fabricated rationale.</span>
        </div>
      )}

      <div style={{ display: "flex", gap: "0.5rem", flexWrap: "wrap" }}>
        {data.evidence_quality && <Badge label={`evidence: ${data.evidence_quality}`} color={data.evidence_quality === "high" ? "var(--ok)" : data.evidence_quality === "insufficient" ? "var(--err)" : "var(--warn)"} />}
      </div>

      {/* Recommendation Trace */}
      <div style={{ display: "flex", flexDirection: "column", gap: "0.5rem" }}>
        <SectionLabel icon="code">Recommendation Trace ({trace.length})</SectionLabel>
        {trace.length === 0 ? <EmptyNote>No recommendations to trace.</EmptyNote> : (
          <ul style={{ margin: 0, paddingLeft: "1.1rem", display: "flex", flexDirection: "column", gap: "0.3rem" }}>
            {trace.map((t, i) => (
              <li key={i} style={{ fontSize: "0.75rem", color: "var(--text)" }}>{t}</li>
            ))}
          </ul>
        )}
      </div>

      {/* Decision Graph */}
      <div style={{ display: "flex", flexDirection: "column", gap: "0.5rem" }}>
        <SectionLabel icon="branch">Decision Graph ({decisionGraph.length})</SectionLabel>
        {decisionGraph.length === 0 ? <EmptyNote>No decisions to graph.</EmptyNote> : (
          decisionGraph.map((n, i) => <DecisionGraphRow key={i} node={n} />)
        )}
      </div>

      {/* Confidence Breakdown */}
      <div style={{ display: "flex", flexDirection: "column", gap: "0.4rem" }}>
        <SectionLabel icon="activity">Confidence Breakdown</SectionLabel>
        <div style={{
          display: "flex", alignItems: "center", justifyContent: "space-between", gap: "0.75rem",
          padding: "0.55rem 0.8rem", border: "1px solid var(--border)", borderRadius: 8,
          background: "rgba(255,255,255,0.015)", flexWrap: "wrap",
        }}>
          <span style={{ fontSize: "0.76rem", color: "var(--text)" }}>
            raw=<strong>{cb.raw_confidence ?? "—"}</strong>{"  →  "}plan=<strong>{cb.plan_confidence ?? "—"}</strong>
          </span>
          {cb.adjustment != null && (
            <Badge label={`${cb.adjustment >= 0 ? "+" : ""}${cb.adjustment}`} color={cb.adjustment < 0 ? "var(--err)" : "var(--ok)"} />
          )}
        </div>
        <span style={{ fontSize: "0.71rem", color: "var(--muted)" }}>{cb.adjustment_reason}</span>
        {(cb.per_source || []).map((s, i) => (
          <div key={i} style={{ display: "flex", alignItems: "center", gap: "0.5rem", fontSize: "0.72rem", color: "var(--text)", flexWrap: "wrap" }}>
            <Badge label={s.source} color={s.has_signal ? "var(--info)" : "var(--muted)"} />
            <span style={{ color: "var(--muted)" }}>
              {s.raw_value != null ? `${s.raw_value} — ${s.raw_value_label}` : s.raw_value_label}
            </span>
          </div>
        ))}
      </div>

      {/* Evidence Graph */}
      <div style={{ display: "flex", flexDirection: "column", gap: "0.6rem" }}>
        <SectionLabel icon="database">Evidence Graph</SectionLabel>
        {Object.values(evidenceGraph).every((nodes) => !nodes || nodes.length === 0) ? (
          <EmptyNote>No evidence collected this investigation.</EmptyNote>
        ) : (
          Object.entries(evidenceGraph).map(([source, nodes]) => (
            <EvidenceGraphSection key={source} source={source} nodes={nodes} />
          ))
        )}
      </div>

      {/* Contradictions */}
      <div style={{ display: "flex", flexDirection: "column", gap: "0.4rem" }}>
        <SectionLabel icon="alert">Contradictions ({contradictions.length})</SectionLabel>
        {contradictions.length === 0 ? (
          <EmptyNote>None detected — evidence sources agree.</EmptyNote>
        ) : (
          contradictions.map((c, i) => (
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

      {/* Missing Evidence */}
      <div style={{ display: "flex", flexDirection: "column", gap: "0.4rem" }}>
        <SectionLabel icon="alert">Missing Evidence ({missing.length})</SectionLabel>
        {missing.length === 0 ? (
          <EmptyNote>None — every evidence source consulted produced a usable signal.</EmptyNote>
        ) : (
          missing.map((m, i) => (
            <div key={i} style={{ display: "flex", alignItems: "center", gap: "0.5rem", fontSize: "0.74rem", color: "var(--text)" }}>
              <Badge label={m.source} color="var(--muted)" />
              <span>{m.reason}</span>
            </div>
          ))
        )}
      </div>

      {/* Assumptions */}
      <div style={{ display: "flex", flexDirection: "column", gap: "0.4rem" }}>
        <SectionLabel icon="code">Assumptions ({assumptions.length})</SectionLabel>
        {assumptions.length === 0 ? (
          <EmptyNote>None identified.</EmptyNote>
        ) : (
          assumptions.map((a, i) => (
            <div key={i} style={{ fontSize: "0.74rem", color: "var(--text)" }}>
              {a.assumption} <span style={{ color: "var(--muted)", fontStyle: "italic" }}>(based on: {a.based_on})</span>
            </div>
          ))
        )}
      </div>

      {/* Lower-priority justification */}
      {lpj.lower_priority_used && (
        <div>
          <SectionLabel icon="shield">Why Lower-Priority Evidence Was Used</SectionLabel>
          <p style={{ fontSize: "0.75rem", color: "var(--text)", marginTop: "0.3rem" }}>{lpj.reason}</p>
        </div>
      )}
    </div>
  );
}
