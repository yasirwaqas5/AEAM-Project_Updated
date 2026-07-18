import {
  Icon, STATE,
  getRetrievedCount, getValidationStatus, getActionOutcome, actionLabel,
  getAuditSummary, fmtPct, parseMaybeJSON,
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

/**
 * Detection sub-stages (Rule Evaluation / Statistical Analysis / Forecast
 * Analysis) are not persisted as structured objects anywhere — only the
 * flattened `detection_methods` signal-name list is (see
 * MonitorAgent._collect_signals / create_event). Rather than persist a new
 * field on Orchestrator, this parses the EXACT string formats that producer
 * already emits ("rule:<name>", "statistical:z_score(<n>)" |
 * "statistical:below_p5" | "statistical:above_p95", "FORECAST") — the same
 * "derive rich display from a raw persisted field" idiom this file's own
 * `outcomeFor` and every ui.jsx getter already use.
 *
 * `incident.detection_methods` arrives as a raw JSON-encoded STRING, not an
 * array — aeam/api/incidents.py's `SELECT *` returns the JSON column
 * verbatim, exactly like `findings` (see ui.jsx's own `getFindings`, which
 * already runs `parseMaybeJSON` for the same reason). Never assume it is
 * already an array.
 */
function parseDetectionMethods(rawMethods) {
  const parsed = parseMaybeJSON(rawMethods);
  const list = Array.isArray(parsed) ? parsed : (Array.isArray(rawMethods) ? rawMethods : []);
  const ruleEntry = list.find((m) => m.startsWith("rule:"));
  const statEntries = list.filter((m) => m.startsWith("statistical:"));
  return {
    any: list.length > 0,
    count: list.length,
    rule: ruleEntry
      ? { fired: true, detail: ruleEntry.slice("rule:".length) }
      : { fired: false, detail: null },
    statistical: {
      fired: statEntries.length > 0,
      detail: statEntries.map((s) => s.slice("statistical:".length)).join(", "),
    },
    forecast: { fired: list.includes("FORECAST") },
  };
}

// Exported (additive — no existing behaviour changed) so pages/Replay.jsx can
// reuse the SAME stage-derivation logic for its step-through playback, rather
// than duplicating this component's stage-building rules.
export function buildStages(incident) {
  const retrieved = getRetrievedCount(incident);
  const validation = getValidationStatus(incident);
  const outcome = getActionOutcome(incident);
  const requiresHuman = !!incident?.requires_human;
  const hasDepth = incident?.investigation_depth != null;
  const audit = getAuditSummary(incident);
  const detection = parseDetectionMethods(incident?.detection_methods);
  const confidenceValue = incident?.confidence ?? audit?.top_confidence ?? null;
  const recommended = audit?.recommended_actions || [];

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
    { key: "detection", icon: "target", label: "Detection", state: detection.any ? "done" : "pending",
      detail: detection.any
        ? `${detection.count} signal${detection.count !== 1 ? "s" : ""} fired`
        : "no anomaly signals recorded" },
    { key: "rule_evaluation", icon: "shield", label: "Rule Evaluation",
      state: detection.rule.fired ? "done" : "idle",
      detail: detection.rule.fired ? `breached: ${detection.rule.detail}` : "no governed rule triggered" },
    { key: "statistical_analysis", icon: "activity", label: "Statistical Analysis",
      state: detection.statistical.fired ? "done" : "idle",
      detail: detection.statistical.fired ? detection.statistical.detail : "within normal statistical range" },
    { key: "forecast_analysis", icon: "target", label: "Forecast Analysis",
      state: detection.forecast.fired ? "done" : "idle",
      detail: detection.forecast.fired ? "deviation from forecast detected" : "no forecast deviation / not applicable" },
    { key: "investigation", icon: "search", label: "Investigation", state: hasDepth ? "done" : "pending",
      detail: `depth ${incident?.investigation_depth ?? "—"}` },
    { key: "rag", icon: "database", label: "RAG Decision", state: ragState,
      detail: retrieved > 0 ? "retrieval invoked" : (validation.status === "SKIPPED" ? "RAG not invoked" : "retrieval found nothing") },
    { key: "retrieved_evidence", icon: "database", label: "Retrieved Evidence",
      state: retrieved > 0 ? "done" : "idle",
      detail: retrieved > 0
        ? `${retrieved} chunk${retrieved !== 1 ? "s" : ""} · top confidence ${audit?.top_confidence != null ? fmtPct(audit.top_confidence) : "—"}`
        : "no evidence retrieved" },
    { key: "validation", icon: "shield", label: "Validation", state: validationState,
      detail: `${validation.status} — ${validation.reason}` },
    { key: "llm_reasoning", icon: "code", label: "LLM Reasoning",
      state: incident?.root_cause ? "done" : (validation.status === "SKIPPED" ? "pending" : "failed"),
      detail: incident?.root_cause ? "root cause reasoning produced" : "no reasoning produced" },
    { key: "confidence", icon: "check", label: "Confidence",
      state: confidenceValue != null ? "done" : "pending",
      detail: confidenceValue != null ? `${fmtPct(confidenceValue)} confidence assigned` : "no confidence score assigned" },
    { key: "recommended_action", icon: "zap", label: "Recommended Action",
      state: recommended.length > 0 ? "done" : "pending",
      detail: recommended.length > 0 ? recommended.join("; ") : "no recommendation generated" },
    { key: "human_review", icon: "shield", label: "Human Review",
      state: requiresHuman ? "pending" : "idle",
      detail: requiresHuman ? (audit?.escalation_reason || "escalated for manual review") : "not required — auto-resolved" },
    { key: "action", icon: "zap", label: "Execution Status", state: anyActionExecuted ? "done" : (requiresHuman ? "skipped" : "pending"),
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
            color, background: `color-mix(in srgb, ${color} 10%, transparent)`, border: `1px solid color-mix(in srgb, ${color} 24%, transparent)`,
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
