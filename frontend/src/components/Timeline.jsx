import { Icon, STATE, parseMaybeJSON, getRetrievedCount } from "./ui";

/* ──────────────────────────────────────────────────────────────────────────
 * Incident timeline — the fixed AEAM pipeline
 *   Trigger → Investigation → RAG → Validation → Action → Jira → Slack → Email
 * Each stage's state is derived from fields persisted on the incident. Stages
 * whose outcome the incident record does not capture (Jira / Email per-channel
 * results) are shown as "pending" rather than asserted, so the flow is never
 * misrepresented.
 * ────────────────────────────────────────────────────────────────────────── */

function buildStages(incident) {
  const rag = parseMaybeJSON(incident?.llm_response);
  const ragFindings = rag?.findings || {};
  const retrieved = getRetrievedCount(incident);
  const validationPassed = ragFindings.validation_passed;
  const actionTaken = !!incident?.action_taken;
  const requiresHuman = !!incident?.requires_human;
  const hasDepth = incident?.investigation_depth != null;

  const ragState = retrieved > 0 || rag ? "done" : "pending";
  const validationState =
    validationPassed === true ? "done" :
    validationPassed === false ? "failed" : (rag ? "pending" : "idle");

  return [
    { key: "trigger", icon: "bolt", label: "Trigger", state: "done",
      detail: `${incident?.event_type || "event"} · ${incident?.metric || "—"}` },
    { key: "investigation", icon: "search", label: "Investigation", state: hasDepth ? "done" : "pending",
      detail: `depth ${incident?.investigation_depth ?? "—"}` },
    { key: "rag", icon: "database", label: "RAG Retrieval", state: ragState,
      detail: `${retrieved} chunk${retrieved !== 1 ? "s" : ""} retrieved` },
    { key: "validation", icon: "shield", label: "Validation", state: validationState,
      detail: validationPassed === true ? "grounding passed" : validationPassed === false ? "grounding failed" : "—" },
    { key: "action", icon: "zap", label: "Action", state: actionTaken ? "done" : (requiresHuman ? "skipped" : "pending"),
      detail: requiresHuman ? "escalated to human" : actionTaken ? "actions dispatched" : "no action" },
    { key: "jira", icon: "ticket", label: "Jira", state: "pending", detail: "ticket status not recorded" },
    { key: "slack", icon: "message", label: "Slack", state: actionTaken ? "done" : "pending",
      detail: actionTaken ? "alert sent" : "—" },
    { key: "email", icon: "mail", label: "Email", state: "pending", detail: "requires GCP credentials" },
  ];
}

function StageRow({ stage, last }) {
  const color = STATE[stage.state] || STATE.idle;
  const isDone = stage.state === "done";
  const isFailed = stage.state === "failed";
  return (
    <div style={{ display: "flex", gap: "0.9rem" }}>
      {/* Rail */}
      <div style={{ display: "flex", flexDirection: "column", alignItems: "center" }}>
        <div style={{
          width: 30, height: 30, borderRadius: "50%", flexShrink: 0,
          display: "flex", alignItems: "center", justifyContent: "center",
          background: `${color}18`, border: `1.5px solid ${color}`,
          color, boxShadow: isDone ? `0 0 10px ${color}44` : "none",
        }}>
          <Icon name={isDone ? "check" : isFailed ? "x" : stage.icon} size={14} />
        </div>
        {!last && <div style={{ width: 2, flex: 1, minHeight: 22, background: "var(--border)", marginTop: 2 }} />}
      </div>

      {/* Content */}
      <div style={{ paddingBottom: last ? 0 : "1.1rem", flex: 1, minWidth: 0 }}>
        <div style={{ display: "flex", alignItems: "center", gap: "0.6rem" }}>
          <span style={{ fontSize: "0.85rem", fontWeight: 600, color: "var(--text)" }}>{stage.label}</span>
          <span style={{
            fontSize: "0.6rem", fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.08em",
            color, background: `${color}16`, border: `1px solid ${color}38`,
            borderRadius: 20, padding: "0.1rem 0.5rem",
          }}>{stage.state}</span>
        </div>
        <div style={{ fontSize: "0.73rem", color: "var(--muted)", marginTop: "0.25rem" }}>{stage.detail}</div>
      </div>
    </div>
  );
}

export default function Timeline({ incident }) {
  const stages = buildStages(incident);
  return (
    <div style={{ display: "flex", flexDirection: "column" }}>
      {stages.map((s, i) => (
        <StageRow key={s.key} stage={s} last={i === stages.length - 1} />
      ))}
    </div>
  );
}
