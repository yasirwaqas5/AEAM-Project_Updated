import {
  Icon, STATE,
  getRetrievedCount, getValidationStatus, getActionOutcome, actionLabel,
} from "./ui";

/* ──────────────────────────────────────────────────────────────────────────
 * Incident timeline — the fixed AEAM pipeline
 *   Trigger → Investigation → RAG → Validation → Action → Jira → Slack → Email
 * Every stage's state is derived from the incident's audit_summary (see
 * aeam/agents/orchestrator/orchestrator.py::finalize_incident) — Jira/Slack/
 * Email each reflect their OWN real executed/skipped outcome and reason
 * instead of being inferred from a single shared boolean.
 * ────────────────────────────────────────────────────────────────────────── */

/**
 * Resolve a timeline stage's outcome, checking every alias that could have
 * produced it (e.g. the Slack stage must reflect either a "slack" or a
 * "marketing_slack" runbook step — both post through the same real Slack
 * integration, just to different channels). Checks executed first across
 * all aliases, then skipped/failed across all aliases, so a FAILED
 * marketing_slack attempt is never mistaken for "not part of the runbook."
 */
function outcomeFor(actionTypes, outcome) {
  const types = Array.isArray(actionTypes) ? actionTypes : [actionTypes];

  for (const t of types) {
    if (outcome.executed.includes(t)) {
      return { state: "done", detail: actionLabel(t) };
    }
  }
  for (const t of types) {
    const skip = outcome.skipped.find((s) => s.action === t);
    if (skip) return { state: "failed", detail: `${actionLabel(t)} — ${skip.reason}` };
  }
  return { state: "pending", detail: "not part of this incident's runbook" };
}

function buildStages(incident) {
  const retrieved = getRetrievedCount(incident);
  const validation = getValidationStatus(incident);
  const outcome = getActionOutcome(incident);
  const requiresHuman = !!incident?.requires_human;
  const hasDepth = incident?.investigation_depth != null;

  const ragState = retrieved > 0 ? "done" : (validation.status === "SKIPPED" ? "pending" : "failed");

  const validationState =
    validation.status === "PASSED" ? "done" :
    validation.status === "FAILED" ? "failed" :
    validation.status === "SKIPPED" ? "skipped" : "pending";

  const anyActionExecuted = outcome.executed.length > 0;

  const jira = outcomeFor("jira", outcome);
  const slack = outcomeFor(["slack", "marketing_slack"], outcome);
  const email = outcomeFor("email", outcome);

  return [
    { key: "trigger", icon: "bolt", label: "Trigger", state: "done",
      detail: `${incident?.event_type || "event"} · ${incident?.metric || "—"}` },
    { key: "investigation", icon: "search", label: "Investigation", state: hasDepth ? "done" : "pending",
      detail: `depth ${incident?.investigation_depth ?? "—"}` },
    { key: "rag", icon: "database", label: "RAG Retrieval", state: ragState,
      detail: `${retrieved} chunk${retrieved !== 1 ? "s" : ""} retrieved` },
    { key: "validation", icon: "shield", label: "Validation", state: validationState,
      detail: `${validation.status} — ${validation.reason}` },
    { key: "action", icon: "zap", label: "Action", state: anyActionExecuted ? "done" : (requiresHuman ? "skipped" : "pending"),
      detail: anyActionExecuted
        ? `${outcome.executed.length} action${outcome.executed.length !== 1 ? "s" : ""} executed`
        : (requiresHuman ? "escalated to human" : "no action") },
    { key: "jira", icon: "ticket", label: "Jira", state: jira.state, detail: jira.detail },
    { key: "slack", icon: "message", label: "Slack", state: slack.state, detail: slack.detail },
    { key: "email", icon: "mail", label: "Email", state: email.state, detail: email.detail },
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
        <div style={{ fontSize: "0.73rem", color: "var(--muted)", marginTop: "0.25rem", wordBreak: "break-word" }}>{stage.detail}</div>
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
