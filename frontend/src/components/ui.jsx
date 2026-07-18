import { useEffect, useRef } from "react";

/* ──────────────────────────────────────────────────────────────────────────
 * Shared UI primitives for the AEAM operator console.
 * Dark, minimal, enterprise. Consumes the CSS variables defined globally in
 * App.jsx (--bg, --surface, --border, --text, --muted, --accent, fonts).
 * No external dependencies — icons are inline SVG.
 * ────────────────────────────────────────────────────────────────────────── */

// ─── Colour tokens ──────────────────────────────────────────────────────────

export const SEVERITY = {
  CRITICAL: { color: "var(--err)", label: "Critical", rank: 4 },
  HIGH:     { color: "var(--warn)", label: "High",     rank: 3 },
  MEDIUM:   { color: "var(--info)", label: "Medium",   rank: 2 },
  LOW:      { color: "var(--ok)", label: "Low",      rank: 1 },
};

export const STATE = {
  done:    "var(--ok)",
  success: "var(--ok)",
  passed:  "var(--ok)",
  active:  "var(--info)",
  running: "var(--info)",
  pending: "var(--warn)",
  skipped: "var(--warn)",
  failed:  "var(--err)",
  error:   "var(--err)",
  idle:    "var(--faint)",
};

export function severityOf(key) {
  return SEVERITY[(key ?? "").toUpperCase()] ?? { color: "var(--faint)", label: key || "Unknown", rank: 0 };
}

export function stateColor(key) {
  return STATE[(key ?? "").toLowerCase()] ?? "var(--faint)";
}

// ─── Formatters ─────────────────────────────────────────────────────────────

export function fmtTime(ts) {
  if (!ts) return "—";
  try {
    return new Date(ts).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
  } catch {
    return String(ts);
  }
}

export function fmtRelative(ts) {
  if (!ts) return "—";
  const then = new Date(ts).getTime();
  if (isNaN(then)) return String(ts);
  const secs = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (secs < 60) return `${secs}s ago`;
  if (secs < 3600) return `${Math.round(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.round(secs / 3600)}h ago`;
  return `${Math.round(secs / 86400)}d ago`;
}

export function fmtPct(v) {
  if (v == null || isNaN(v)) return "—";
  const n = v <= 1 ? v * 100 : v;
  return `${Math.round(n)}%`;
}

export function fmtMs(ms) {
  if (ms == null) return "—";
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(2)}s`;
}

// ─── Incident-shape helpers (derive fields the API does not store directly) ──
//
// The backend packs everything new (canonical status, query attempts,
// validation outcome, recommended/executed actions, evidence ranking) into
// ONE consolidated `audit_summary` entry inside the existing `findings` JSON
// column (see aeam/agents/orchestrator/orchestrator.py::finalize_incident).
// Every helper below reads that entry FIRST, and falls back to the older
// heuristics (root_cause/requires_human/llm_response) only for incidents
// persisted before this change — so old rows never break the UI.
//
// The 5-state status vocabulary mirrors
// aeam/agents/orchestrator/investigation_status.py EXACTLY (same priority
// order: ESCALATED > RESOLVED > FAILED > COMPLETE, plus INVESTIGATING for
// a live/unfinished record). Keep these two files in sync if either changes.

export function parseMaybeJSON(value) {
  if (value == null) return null;
  if (typeof value === "object") return value;
  if (typeof value === "string" && value.trim()) {
    try { return JSON.parse(value); } catch { return null; }
  }
  return null;
}

/** Parsed findings array (empty array if absent/unparseable). */
export function getFindings(incident) {
  const findings = parseMaybeJSON(incident?.findings);
  return Array.isArray(findings) ? findings : [];
}

/** The single consolidated audit_summary findings entry, or null. */
export function getAuditSummary(incident) {
  const findings = getFindings(incident);
  // Last one wins in case finalize_incident somehow ran twice (defensive).
  for (let i = findings.length - 1; i >= 0; i--) {
    if (findings[i]?.type === "audit_summary") return findings[i];
  }
  return null;
}

const STATUS_LABELS = {
  INVESTIGATING: "Investigating",
  RESOLVED: "Resolved",
  ESCALATED: "Escalated",
  FAILED: "Failed",
  COMPLETE: "Complete",
};
const STATUS_COLORS = {
  INVESTIGATING: STATE.active,
  RESOLVED: STATE.done,
  ESCALATED: STATE.failed,
  FAILED: STATE.failed,
  COMPLETE: STATE.idle,
};

/** Derive the canonical 5-state investigation status for an incident. */
export function deriveStatus(incident) {
  if (!incident) return { key: "UNKNOWN", label: "Unknown", color: STATE.idle };

  const audit = getAuditSummary(incident);
  if (audit?.investigation_status) {
    const key = audit.investigation_status;
    return { key, label: STATUS_LABELS[key] || key, color: STATUS_COLORS[key] || STATE.idle };
  }

  // Fallback for incidents predating audit_summary — same priority order
  // as derive_investigation_status() on the backend, minus the FAILED case
  // (older rows carry no explicit error signal to detect that from).
  if (incident.requires_human) return { key: "ESCALATED", label: "Escalated", color: STATE.failed };
  if (incident.root_cause) return { key: "RESOLVED", label: "Resolved", color: STATE.done };
  return { key: "COMPLETE", label: "Complete", color: STATE.idle };
}

/** The `data` dict of the most recent RAG pass recorded in findings. */
export function getLatestRagData(incident) {
  const findings = getFindings(incident);
  let latest = null;
  for (const entry of findings) {
    if (entry?.type === "rag" && entry.data) latest = entry.data;
  }
  return latest;
}

/** Extract retrieved-evidence chunks (LLM-cited causes) from an incident. */
export function getEvidence(incident) {
  const latest = getLatestRagData(incident);
  if (Array.isArray(latest?.possible_causes)) return latest.possible_causes;
  // Legacy fallback: llm_response held the raw RAGAgent result directly.
  const rag = parseMaybeJSON(incident?.llm_response);
  const causes = rag?.findings?.possible_causes;
  return Array.isArray(causes) ? causes : [];
}

/**
 * The `data` dict of the most recent Enterprise Memory recall pass recorded
 * in findings (type "memory") — kept structurally separate from RAG's
 * `type: "rag"` findings entries; never merged into the same list.
 */
export function getMemoryData(incident) {
  const findings = getFindings(incident);
  let latest = null;
  for (const entry of findings) {
    if (entry?.type === "memory" && entry.data) latest = entry.data;
  }
  return latest;
}

/**
 * Similar resolved incidents surfaced by the Enterprise Memory Engine for
 * this investigation — a DISTINCT evidence source from RAG's knowledge-
 * document chunks (see getEvidence). Each entry: {incident_id, similarity,
 * category, severity, triggered_metric, root_cause, resolution_status,
 * confidence, timestamp, incident_summary}. Empty array (not an error) both
 * when memory was never consulted and when it found nothing similar —
 * distinguish the two with getMemoryData(incident) === null vs.
 * getMemoryData(incident)?.matches?.length === 0.
 */
export function getMemoryMatches(incident) {
  const data = getMemoryData(incident);
  return Array.isArray(data?.matches) ? data.matches : [];
}

/**
 * The `data` dict of the most recent Enterprise Policy Registry match pass
 * recorded in findings (type "policy", Phase C3) — a THIRD, structurally
 * distinct evidence source: never merged with RAG's `type: "rag"` document
 * chunks or Enterprise Memory's `type: "memory"` past incidents.
 */
export function getPolicyMatchData(incident) {
  const findings = getFindings(incident);
  let latest = null;
  for (const entry of findings) {
    if (entry?.type === "policy" && entry.data) latest = entry.data;
  }
  return latest;
}

/**
 * Enterprise policies matched to this investigation — advisory evidence
 * only (see aeam/intelligence/policy_registry.py: policies never override
 * a deterministic RuleEngine decision). Each entry: {policy_id,
 * source_document, source_chunk, business_rule, condition, threshold,
 * actions, escalation_rule, approval_required, department, role,
 * time_constraint, priority, related_metrics, match_reason, similarity?}.
 * Empty array both when the registry was never consulted and when it
 * found nothing — distinguish via getPolicyMatchData(incident) === null.
 */
export function getPolicyMatches(incident) {
  const data = getPolicyMatchData(incident);
  return Array.isArray(data?.matches) ? data.matches : [];
}

/**
 * The Cross-Dataset Intelligence finding (type "cross_dataset", Phase C4)
 * — a FOURTH, structurally distinct evidence source: correlated signals
 * across OTHER activated datasets, never merged with RAG documents,
 * Enterprise Memory, or Enterprise Policies. Shape:
 * {insufficient_data, reason, origin_dataset_id, origin_dataset_name,
 *  candidates_checked, supporting, contradicting, strong_correlations,
 *  missing_signals}. Returns null if Cross-Dataset Intelligence was never
 * consulted for this investigation (distinct from having run and found
 * insufficient_data=true, or having run and found nothing).
 */
export function getCrossDatasetData(incident) {
  const findings = getFindings(incident);
  let latest = null;
  for (const entry of findings) {
    if (entry?.type === "cross_dataset" && entry.data) latest = entry.data;
  }
  return latest;
}

/**
 * The Adaptive Detection Engine finding (type "adaptive", Phase C5) — a
 * FIFTH, structurally distinct evidence source: a longer-horizon rolling
 * baseline plus day-of-week seasonality judgement for the incident's own
 * metric, combined with the event's already-computed statistical/forecast
 * signals. Never merged with RAG documents, Enterprise Memory, Enterprise
 * Policies, or Cross-Dataset Intelligence. Shape: {history_points_used,
 * adaptive_baseline, adaptive_baseline_insufficient, seasonality,
 * seasonality_insufficient, existing_statistical, existing_forecast,
 * combined_signal, corroborating_signals}. Returns null if the Adaptive
 * Detection Engine was never consulted for this investigation (distinct
 * from having run and found insufficient history for either sub-analysis).
 */
export function getAdaptiveDetectionData(incident) {
  const findings = getFindings(incident);
  let latest = null;
  for (const entry of findings) {
    if (entry?.type === "adaptive" && entry.data) latest = entry.data;
  }
  return latest;
}

/**
 * The Enterprise Action Planning Engine's execution plan (type
 * "execution_plan", Phase C7) — the FINAL reasoning stage, synthesizing
 * every prior evidence source (memory/policy/cross_dataset/adaptive/
 * retrieval) into one explainable plan. Never merged with any individual
 * evidence panel's data; it only REFERENCES them. Shape: {executive_summary,
 * recommended_actions, order_rationale, supporting_evidence,
 * business_risk_assessment, expected_impact, confidence, evidence_quality,
 * evidence_conflicts, human_approval_required, explanation,
 * insufficient_evidence, sources_consulted, sources_with_signal}. Returns
 * null if the planning engine was never consulted for this investigation
 * (distinct from having run and found insufficient evidence).
 */
export function getExecutionPlanData(incident) {
  const findings = getFindings(incident);
  let latest = null;
  for (const entry of findings) {
    if (entry?.type === "execution_plan" && entry.data) latest = entry.data;
  }
  return latest;
}

/**
 * The Enterprise Explainability Engine's output (type "explainability",
 * Phase D1) — explains WHY the execution plan reached its recommendations;
 * never changes them. Shape: {decision_graph, evidence_graph,
 * recommendation_trace, confidence_breakdown, evidence_contribution,
 * contradictions, missing_evidence, assumptions, evidence_quality,
 * lower_priority_justification, insufficient_evidence}. Returns null if the
 * engine was never consulted for this investigation.
 */
export function getExplainabilityData(incident) {
  const findings = getFindings(incident);
  let latest = null;
  for (const entry of findings) {
    if (entry?.type === "explainability" && entry.data) latest = entry.data;
  }
  return latest;
}

/**
 * The Enterprise AI Evaluation & Quality Engine's output (type
 * "ai_evaluation", Phase D2) — scores the QUALITY of the investigation;
 * never changes findings, the execution plan, or explainability. Shape:
 * {overall_score, overall_score_formula, component_scores, strengths,
 * weaknesses, missing_evidence, improvement_opportunities, quality_summary}.
 * Returns null if the engine was never consulted for this investigation.
 */
export function getAIEvaluationData(incident) {
  const findings = getFindings(incident);
  let latest = null;
  for (const entry of findings) {
    if (entry?.type === "ai_evaluation" && entry.data) latest = entry.data;
  }
  return latest;
}

/** Count of retrieved evidence chunks recorded for the incident. */
export function getRetrievedCount(incident) {
  const audit = getAuditSummary(incident);
  if (typeof audit?.evidence_count === "number") return audit.evidence_count;
  const latest = getLatestRagData(incident);
  if (typeof latest?.retrieved_count === "number") return latest.retrieved_count;
  const rag = parseMaybeJSON(incident?.llm_response);
  if (typeof rag?.findings?.retrieved_count === "number") return rag.findings.retrieved_count;
  return getEvidence(incident).length;
}

/**
 * Every RAG query attempt made this incident, in order — used to render
 * the Retrieval Summary when no evidence was found.
 * Each entry: {attempt, strategy, query, retrieved_count, threshold}.
 */
export function getQueryAttempts(incident) {
  const audit = getAuditSummary(incident);
  if (Array.isArray(audit?.query_attempts) && audit.query_attempts.length) {
    return audit.query_attempts;
  }
  // Fallback: reconstruct from raw findings (works for incidents recorded
  // after the adaptive-query change but before audit_summary existed).
  return getFindings(incident)
    .filter((f) => f?.type === "rag" && f.data && f.data.query_strategy !== "exhausted")
    .map((f) => ({
      attempt: f.data.query_attempt,
      strategy: f.data.query_strategy,
      query: f.data.query,
      retrieved_count: f.data.retrieved_count ?? 0,
      threshold: f.data.threshold,
    }));
}

/** {status: "PASSED"|"FAILED"|"SKIPPED"|"PENDING", reason} */
export function getValidationStatus(incident) {
  const audit = getAuditSummary(incident);
  if (audit?.validation_status) {
    return { status: audit.validation_status, reason: audit.validation_reason || "" };
  }
  // Fallback for pre-audit_summary incidents.
  const latest = getLatestRagData(incident);
  if (!latest) return { status: "SKIPPED", reason: "RAG was not invoked for this investigation." };
  if (latest.validation_passed === true) {
    return { status: "PASSED", reason: "Grounded response validated successfully." };
  }
  return { status: "FAILED", reason: latest.error || "Validation failed." };
}

/** Recommended actions (advisory text, plural — the runbook may list several). */
export function getRecommendedActions(incident) {
  const audit = getAuditSummary(incident);
  if (Array.isArray(audit?.recommended_actions) && audit.recommended_actions.length) {
    return audit.recommended_actions;
  }
  if (incident?.requires_human) return ["Escalate to human review"];
  if (incident?.root_cause) return ["Continue monitoring"];
  return ["Investigate the affected metric and recent related changes"];
}

/** Singular convenience wrapper — joined recommended actions as one string. */
export function getRecommendedAction(incident) {
  return getRecommendedActions(incident).join("; ");
}

/**
 * {executed: string[], skipped: {action, reason}[]} — ONLY actions
 * ActionAgent confirmed with status SUCCESS are ever listed as executed.
 */
export function getActionOutcome(incident) {
  const audit = getAuditSummary(incident);
  if (audit) {
    return {
      executed: Array.isArray(audit.executed_actions) ? audit.executed_actions : [],
      skipped: Array.isArray(audit.skipped_actions) ? audit.skipped_actions : [],
    };
  }
  // Fallback: the old schema only had a single boolean.
  return incident?.action_taken
    ? { executed: ["slack"], skipped: [] }
    : { executed: [], skipped: [] };
}

const ACTION_LABELS = {
  jira: "Created Jira ticket",
  slack: "Posted Slack alert",
  marketing_slack: "Notified marketing",
  email: "Sent email report",
  diagnostics: "Captured diagnostics snapshot",
  monitoring: "Flagged for increased monitoring",
  webhook: "Triggered webhook",
  sheets: "Logged to spreadsheet",
};

export function actionLabel(actionType) {
  return ACTION_LABELS[actionType] || actionType;
}

/**
 * Top-ranked retrieved evidence with similarity/confidence/source/reason,
 * for the Evidence panel's ranking view. Cross-references the raw retrieved
 * chunk metadata (similarity, source) against which chunk_ids the LLM
 * actually cited as a cause (confidence, "reason selected").
 */
export function getTopEvidence(incident) {
  const latest = getLatestRagData(incident);
  const retrievedChunks = Array.isArray(latest?.retrieved_chunks) ? latest.retrieved_chunks : [];
  const causesByChunk = new Map(
    (Array.isArray(latest?.possible_causes) ? latest.possible_causes : [])
      .filter((c) => c?.chunk_id)
      .map((c) => [c.chunk_id, c]),
  );

  if (retrievedChunks.length) {
    return retrievedChunks
      .map((c) => {
        const cause = causesByChunk.get(c.chunk_id);
        return {
          chunk_id: c.chunk_id,
          similarity: c.similarity,
          confidence: cause?.confidence ?? null,
          source: c.source,
          preview: c.text_preview,
          cited: !!c.cited,
          reasonSelected: c.cited
            ? `Cited as contributing cause (confidence ${fmtPct(cause?.confidence)})`
            : "Retrieved but not cited by the LLM",
          // Phase C6 — Advanced Retrieval Engine. Present (possibly null)
          // even when the engine is disabled, so callers never need an
          // `in` check.
          businessRelevanceScore: c.business_relevance_score ?? null,
          rankingReasons: Array.isArray(c.ranking_reasons) ? c.ranking_reasons : [],
          retrievalConfidence: c.retrieval_confidence ?? null,
          metadataFilterRelaxed: !!c.metadata_filter_relaxed,
        };
      })
      .sort((a, b) => (b.similarity ?? 0) - (a.similarity ?? 0));
  }

  // Legacy fallback: only cause/chunk_id/confidence are available (no
  // similarity/source since retrieved_chunks didn't exist yet).
  return getEvidence(incident).map((c) => ({
    chunk_id: c.chunk_id,
    similarity: null,
    confidence: c.confidence,
    source: null,
    preview: c.cause,
    cited: true,
    reasonSelected: `Cited as contributing cause (confidence ${fmtPct(c.confidence)})`,
    businessRelevanceScore: null,
    rankingReasons: [],
    retrievalConfidence: null,
    metadataFilterRelaxed: false,
  }));
}

/**
 * Entities extracted from event.metadata by the Adaptive Retrieval Engine's
 * IncidentEntityExtractor (Phase C6, type "rag" finding's nested
 * `extracted_entities`) — e.g. [{key, label, value}, ...]. Empty array both
 * when no entity extractor was wired and when the event carried no
 * recognised metadata keys — this helper does not distinguish the two
 * (unlike getMemoryData/getCrossDatasetData's null-vs-empty convention)
 * because entity extraction is a sub-step of RAG, not its own findings type.
 */
export function getExtractedEntities(incident) {
  const latest = getLatestRagData(incident);
  return Array.isArray(latest?.extracted_entities) ? latest.extracted_entities : [];
}

/** Whether a metadata-aware retrieval filter was actually applied (Phase C6). */
export function getMetadataFilterApplied(incident) {
  const latest = getLatestRagData(incident);
  return !!latest?.metadata_filter_applied;
}

/**
 * Real per-agent activity for the Agent Mesh, derived ONLY from persisted
 * incidents (newest-first) + the Observability engine's summary. An agent is
 * "active" when it contributed to the LATEST investigation; health strings
 * come from real observability rates; anything unrecorded stays undefined so
 * the mesh renders "no recorded activity" instead of inventing one.
 */
export function buildMeshLive(incidents = [], observability = null) {
  const types = {
    memory: "memory", policy: "policy", cross: "cross_dataset", adaptive: "adaptive",
    retrieval: "rag", plan: "execution_plan", explain: "explainability",
    eval: "ai_evaluation", report: "audit_summary",
  };
  const latest = incidents[0] || null;
  const latestTypes = new Set(latest ? getFindings(latest).map((f) => f?.type) : []);
  const lastSeen = {};
  for (const inc of incidents) {
    const present = new Set(getFindings(inc).map((f) => f?.type));
    for (const [key, t] of Object.entries(types)) {
      if (lastSeen[key] == null && present.has(t)) lastSeen[key] = inc.timestamp;
    }
    if (lastSeen.action == null && (getAuditSummary(inc)?.executed_actions || []).length) lastSeen.action = inc.timestamp;
    if (lastSeen.monitor == null) lastSeen.monitor = inc.timestamp;
  }
  const rate = (m, label = "hit rate") =>
    observability?.[m]?.available ? `${Math.round(observability[m].rate * 100)}% ${label}` : undefined;
  const trendAvg = (m, label) =>
    observability?.[m]?.available ? `${Math.round(observability[m].average * 100)}% avg ${label}` : undefined;
  const health = {
    memory: rate("memory_hit_rate"), policy: rate("policy_hit_rate"),
    retrieval: rate("retrieval_success_rate", "success"),
    cross: rate("cross_dataset_usage_rate", "usage"), adaptive: rate("adaptive_detection_usage_rate", "usage"),
    plan: trendAvg("execution_plan_confidence_trend", "confidence"),
    eval: trendAvg("ai_evaluation_trend", "quality"),
    observe: observability?.overall_ai_health?.available
      ? `${Math.round(observability.overall_ai_health.score * 100)}% AI health` : undefined,
  };
  const out = {};
  for (const key of ["monitor", "memory", "policy", "cross", "adaptive", "retrieval", "plan", "explain", "eval", "observe", "report", "action"]) {
    const activeNow =
      key === "monitor" ? !!latest
      : key === "action" ? !!(latest && (getAuditSummary(latest)?.executed_actions || []).length)
      : key === "observe" ? !!observability
      : latestTypes.has(types[key]);
    out[key] = {
      state: activeNow ? "active" : "idle",
      health: health[key],
      lastActivity: key === "observe"
        ? (observability ? "live — recomputed on read" : undefined)
        : lastSeen[key] ? fmtRelative(lastSeen[key]) : undefined,
    };
  }
  return out;
}

// ─── Icons (inline SVG, no dependency) ──────────────────────────────────────

const ICON_PATHS = {
  activity:  "M22 12h-4l-3 9L9 3l-3 9H2",
  alert:     "M12 9v4M12 17h.01M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z",
  check:     "M20 6 9 17l-5-5",
  x:         "M18 6 6 18M6 6l12 12",
  clock:     "M12 6v6l4 2M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20z",
  layers:    "M12 2 2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5",
  target:    "M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20zM12 18a6 6 0 1 0 0-12 6 6 0 0 0 0 12zM12 14a2 2 0 1 0 0-4 2 2 0 0 0 0 4z",
  database:  "M12 8c4.42 0 8-1.34 8-3s-3.58-3-8-3-8 1.34-8 3 3.58 3 8 3zM4 5v6c0 1.66 3.58 3 8 3s8-1.34 8-3V5M4 11v6c0 1.66 3.58 3 8 3s8-1.34 8-3v-6",
  zap:       "M13 2 3 14h9l-1 8 10-12h-9l1-8z",
  search:    "M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16zM21 21l-4.35-4.35",
  code:      "m16 18 6-6-6-6M8 6l-6 6 6 6",
  branch:    "M6 3v12M18 9a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM6 21a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM15 6a9 9 0 0 1-9 9",
  mail:      "M4 4h16a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2zM22 6l-10 7L2 6",
  message:   "M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z",
  ticket:    "M3 7v3a2 2 0 0 1 0 4v3a1 1 0 0 0 1 1h16a1 1 0 0 0 1-1v-3a2 2 0 0 1 0-4V7a1 1 0 0 0-1-1H4a1 1 0 0 0-1 1z",
  chevron:   "m6 9 6 6 6-6",
  shield:    "M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z",
  bolt:      "M13 2 3 14h9l-1 8 10-12h-9l1-8z",
  play:      "m5 3 14 9-14 9V3z",
  sliders:   "M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6",
  filter:    "M22 3H2l8 9.46V19l4 2v-8.54L22 3z",
  arrowr:    "M5 12h14M12 5l7 7-7 7",
  external:  "M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6M15 3h6v6M10 14 21 3",
  sparkle:   "M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M5.6 18.4l2.1-2.1M16.3 7.7l2.1-2.1M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8z",
  home:      "M3 9.5 12 3l9 6.5V21a1 1 0 0 1-1 1h-5v-7h-6v7H4a1 1 0 0 1-1-1V9.5z",
  grid:      "M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h7v7h-7z",
  book:      "M4 19.5A2.5 2.5 0 0 1 6.5 17H20M4 19.5A2.5 2.5 0 0 0 6.5 22H20V2H6.5A2.5 2.5 0 0 0 4 4.5v15z",
  command:   "M9 9V6a3 3 0 1 0-3 3h3zm0 0v6m0-6h6m-6 6v3a3 3 0 1 1-3-3h3zm6 0h3a3 3 0 1 1-3 3v-3zm0-6V6a3 3 0 1 1 3 3h-3z",
};

export function Icon({ name, size = 16, color = "currentColor", style = {} }) {
  const d = ICON_PATHS[name] || ICON_PATHS.activity;
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none"
      stroke={color} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
      style={{ flexShrink: 0, ...style }} aria-hidden="true">
      <path d={d} />
    </svg>
  );
}

// ─── Card ───────────────────────────────────────────────────────────────────

export function Card({ children, accent, className = "", style = {}, ...rest }) {
  return (
    <div className={`aeam-card ${className}`} style={style} {...rest}>
      {accent && <span className="aeam-card-accent" style={{ background: accent }} />}
      {children}
    </div>
  );
}

export function CardTitle({ icon, children, right }) {
  return (
    <div className="aeam-card-title-row">
      <div className="aeam-card-title">
        {icon && <Icon name={icon} size={13} />}
        <span>{children}</span>
      </div>
      {right}
    </div>
  );
}

// ─── Badge ──────────────────────────────────────────────────────────────────

export function Badge({ label, color = "var(--faint)", dot = false, subtle = true, style = {} }) {
  return (
    <span
      style={{
        display: "inline-flex", alignItems: "center", gap: "0.4rem",
        fontSize: "0.68rem", fontWeight: 700, letterSpacing: "0.08em",
        textTransform: "uppercase",
        color, background: subtle ? `color-mix(in srgb, ${color} 10%, transparent)` : color,
        border: `1px solid color-mix(in srgb, ${color} 28%, transparent)`, borderRadius: "20px",
        padding: "0.22rem 0.65rem", whiteSpace: "nowrap", ...style,
      }}
    >
      {dot && <span style={{
        width: 6, height: 6, borderRadius: "50%", background: color,
        boxShadow: `0 0 6px ${color}`,
      }} />}
      {label}
    </span>
  );
}

export function SeverityBadge({ severity }) {
  const s = severityOf(severity);
  return <Badge label={severity || "—"} color={s.color} subtle />;
}

export function StatusBadge({ status }) {
  const color = stateColor(status);
  return <Badge label={status || "—"} color={color} dot />;
}

// ─── Field (label / value pair) ──────────────────────────────────────────────

export function Field({ label, value, mono = false, color, title }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "0.3rem", minWidth: 0 }}>
      <span style={{
        fontSize: "0.62rem", textTransform: "uppercase", letterSpacing: "0.12em",
        color: "var(--muted)",
      }}>{label}</span>
      <span title={title || (typeof value === "string" ? value : undefined)} style={{
        fontSize: "0.85rem", fontWeight: 600,
        color: color || "var(--text)",
        fontFamily: mono ? "var(--font-mono)" : "inherit",
        lineHeight: 1.4, wordBreak: "break-word",
      }}>{value ?? "—"}</span>
    </div>
  );
}

// ─── Confidence bar ───────────────────────────────────────────────────────────

export function ConfidenceBar({ value, width = "100%" }) {
  const pct = value == null ? 0 : Math.round((value <= 1 ? value * 100 : value));
  const color = pct >= 80 ? "var(--ok)" : pct >= 50 ? "var(--warn)" : "var(--err)";
  return (
    <div style={{ display: "flex", alignItems: "center", gap: "0.6rem", width }}>
      <div style={{ flex: 1, height: 6, background: "var(--surface-3)", borderRadius: 3, overflow: "hidden" }}>
        <div style={{
          height: "100%", width: `${pct}%`, borderRadius: 3,
          background: `linear-gradient(90deg, color-mix(in srgb, ${color} 70%, transparent), ${color})`,
          boxShadow: `0 0 8px color-mix(in srgb, ${color} 45%, transparent)`,
          transition: "width 0.6s var(--ease-out)",
        }} />
      </div>
      <span style={{ fontFamily: "var(--font-mono)", fontSize: "var(--fs-xs)", color, fontWeight: 700 }}>
        {value == null ? "—" : `${pct}%`}
      </span>
    </div>
  );
}

// ─── Button ───────────────────────────────────────────────────────────────────

export function Button({ children, icon, onClick, variant = "ghost", size, disabled, title, style = {} }) {
  return (
    <button
      className={`aeam-btn aeam-btn-${variant}${size === "sm" ? " aeam-btn-sm" : ""}`}
      onClick={onClick} disabled={disabled} title={title} style={style}
    >
      {icon && <Icon name={icon} size={size === "sm" ? 12 : 13} />}
      {children}
    </button>
  );
}

// ─── Modal ────────────────────────────────────────────────────────────────────

export function Modal({ title, icon, onClose, children, maxWidth = 760 }) {
  const panelRef = useRef(null);
  const restoreRef = useRef(null);

  useEffect(() => {
    restoreRef.current = document.activeElement;
    const panel = panelRef.current;
    const focusables = () => panel.querySelectorAll(
      'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
    );
    (focusables()[0] || panel).focus();

    const onKey = (e) => {
      if (e.key === "Escape") { onClose(); return; }
      // Focus trap: Tab cycles within the dialog.
      if (e.key === "Tab") {
        const els = Array.from(focusables());
        if (!els.length) return;
        const first = els[0], last = els[els.length - 1];
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
      }
    };
    document.addEventListener("keydown", onKey);
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = "";
      restoreRef.current?.focus?.();
    };
  }, [onClose]);

  return (
    <div className="aeam-modal-overlay" onClick={onClose}>
      <div
        ref={panelRef} className="aeam-modal" style={{ maxWidth }}
        onClick={(e) => e.stopPropagation()}
        role="dialog" aria-modal="true" aria-label={typeof title === "string" ? title : undefined}
        tabIndex={-1}
      >
        <div className="aeam-modal-head">
          <div className="aeam-card-title">
            {icon && <Icon name={icon} size={15} />}
            <span style={{ fontSize: "var(--fs-md)", color: "var(--text)", letterSpacing: "0.03em" }}>{title}</span>
          </div>
          <button className="aeam-modal-close" onClick={onClose} aria-label="Close"><Icon name="x" size={16} /></button>
        </div>
        <div className="aeam-modal-body">{children}</div>
      </div>
    </div>
  );
}

// ─── Collapsible ────────────────────────────────────────────────────────────────

export function Collapsible({ summary, children, defaultOpen = false }) {
  return (
    <details className="aeam-collapsible" open={defaultOpen}>
      <summary className="aeam-collapsible-summary">
        <Icon name="chevron" size={14} style={{ transition: "transform 0.2s" }} />
        {summary}
      </summary>
      <div className="aeam-collapsible-body">{children}</div>
    </details>
  );
}

// ─── Tabs ───────────────────────────────────────────────────────────────────────

export function Tabs({ tabs, active, onChange, ariaLabel }) {
  return (
    <div role="tablist" aria-label={ariaLabel} className="aeam-tabs">
      {tabs.map((t) => (
        <button
          key={t.key} role="tab" aria-selected={active === t.key}
          className={`aeam-tab${active === t.key ? " active" : ""}`}
          onClick={() => onChange(t.key)}
        >
          {t.icon && <Icon name={t.icon} size={13} />}
          {t.label}
          {t.badge != null && <span className="aeam-tab-badge">{t.badge}</span>}
        </button>
      ))}
    </div>
  );
}

// ─── Skeleton ───────────────────────────────────────────────────────────────────

export function Skeleton({ width = "100%", height = 16, style = {} }) {
  return <div style={{ width, height, background: "var(--border)", borderRadius: 4, animation: "aeamPulse 1.2s ease-in-out infinite", ...style }} />;
}

// ─── Global UI stylesheet (injected once) ─────────────────────────────────────

const UI_CSS = `
  @keyframes aeamPulse { 0%,100%{opacity:1} 50%{opacity:0.4} }
  @keyframes aeamFade { from{opacity:0;transform:translateY(10px)} to{opacity:1;transform:translateY(0)} }
  @keyframes aeamScaleIn { from{opacity:0;transform:scale(.96) translateY(8px)} to{opacity:1;transform:scale(1) translateY(0)} }
  @keyframes aeamRise { from{opacity:0;transform:translateY(14px)} to{opacity:1;transform:translateY(0)} }
  @keyframes aeamGrowX { from{transform:scaleX(0)} to{transform:scaleX(1)} }

  .aeam-page { animation: aeamFade var(--t-slow) var(--ease-out) forwards; }

  /* Staggered card entrance — applied to direct children of stagger groups. */
  .aeam-stagger > * { animation: aeamRise .5s var(--ease-out) backwards; }
  .aeam-stagger > *:nth-child(1){ animation-delay:.02s } .aeam-stagger > *:nth-child(2){ animation-delay:.07s }
  .aeam-stagger > *:nth-child(3){ animation-delay:.12s } .aeam-stagger > *:nth-child(4){ animation-delay:.17s }
  .aeam-stagger > *:nth-child(5){ animation-delay:.22s } .aeam-stagger > *:nth-child(6){ animation-delay:.27s }
  .aeam-stagger > *:nth-child(n+7){ animation-delay:.32s }

  .aeam-grid-auto { display:grid; gap:var(--sp-4); grid-template-columns:repeat(auto-fit,minmax(215px,1fr)); }
  .aeam-grid-2 { display:grid; gap:var(--sp-4); grid-template-columns:repeat(auto-fit,minmax(330px,1fr)); }

  .aeam-card {
    position:relative; overflow:hidden;
    background:linear-gradient(180deg, var(--surface-2), var(--surface));
    border:1px solid var(--border);
    border-radius:var(--r-lg); padding:1.25rem 1.4rem;
    box-shadow:var(--e1), var(--edge);
    transition:border-color var(--t-fast) var(--ease-out), box-shadow var(--t-med) var(--ease-out), transform var(--t-med) var(--ease-out);
  }
  .aeam-card-hover:hover {
    border-color:var(--border-hi);
    box-shadow:var(--e3), var(--edge);
    transform:translateY(-2px);
  }
  .aeam-card-accent { position:absolute; top:0; left:0; width:100%; height:2px; opacity:.9; }

  .aeam-card-title-row { display:flex; align-items:center; justify-content:space-between; margin-bottom:1rem; }
  .aeam-card-title {
    display:flex; align-items:center; gap:0.5rem;
    font-size:var(--fs-2xs); text-transform:uppercase; letter-spacing:0.13em;
    color:var(--muted); font-weight:600;
  }

  /* ─ Buttons ─ */
  .aeam-btn {
    display:inline-flex; align-items:center; justify-content:center; gap:0.45rem;
    font-size:var(--fs-xs); font-weight:600; letter-spacing:0.02em; font-family:var(--font-body);
    border-radius:var(--r-sm); padding:0.48rem 0.9rem; cursor:pointer;
    border:1px solid transparent; background:none; color:var(--text-2);
    transition:background var(--t-fast) var(--ease-out), border-color var(--t-fast) var(--ease-out),
      color var(--t-fast) var(--ease-out), box-shadow var(--t-fast) var(--ease-out), transform var(--t-fast) var(--ease-out);
    white-space:nowrap; user-select:none;
  }
  .aeam-btn:active:not(:disabled) { transform:translateY(1px) scale(.99); }
  .aeam-btn:disabled { opacity:0.45; cursor:default; }

  .aeam-btn-ghost { color:var(--text-2); border-color:var(--border-2); background:rgba(255,255,255,.015); }
  .aeam-btn-ghost:hover:not(:disabled) { color:var(--text); border-color:var(--border-hi); background:var(--surface-3); }

  .aeam-btn-primary {
    color:#f4f8ff; border-color:rgba(122,174,255,.55);
    background:linear-gradient(180deg,#6aa6ff,#4a8bf0);
    box-shadow:0 1px 2px rgba(2,6,12,.5), inset 0 1px 0 rgba(255,255,255,.22);
  }
  .aeam-btn-primary:hover:not(:disabled) {
    background:linear-gradient(180deg,#79b0ff,#5b9dff);
    box-shadow:0 2px 10px rgba(91,157,255,.35), inset 0 1px 0 rgba(255,255,255,.24);
  }

  .aeam-btn-secondary { color:var(--accent); border-color:var(--accent-border); background:var(--accent-dim); }
  .aeam-btn-secondary:hover:not(:disabled) { background:rgba(91,157,255,.18); }

  .aeam-btn-danger { color:var(--err); border-color:rgba(248,113,113,.4); background:var(--err-dim); }
  .aeam-btn-danger:hover:not(:disabled) { background:rgba(248,113,113,.18); }

  .aeam-btn-sm { padding:0.3rem 0.6rem; font-size:var(--fs-2xs); }

  /* ─ Modal ─ */
  .aeam-modal-overlay {
    position:fixed; inset:0; z-index:1000;
    background:rgba(4,7,12,0.6); backdrop-filter:var(--glass-blur);
    display:flex; align-items:center; justify-content:center; padding:1.5rem;
    animation:aeamFade var(--t-fast) var(--ease-out) forwards;
  }
  .aeam-modal {
    width:100%; max-height:86vh; display:flex; flex-direction:column;
    background:linear-gradient(180deg,var(--surface-2),var(--surface));
    border:1px solid var(--border-2);
    border-radius:var(--r-xl); overflow:hidden;
    box-shadow:var(--e4), var(--edge);
    animation:aeamScaleIn var(--t-med) var(--ease-spring) forwards;
  }
  .aeam-modal-head {
    display:flex; align-items:center; justify-content:space-between;
    padding:1.1rem 1.4rem; border-bottom:1px solid var(--border); flex-shrink:0;
  }
  .aeam-modal-close { background:none; border:none; color:var(--muted); cursor:pointer; padding:0.25rem; border-radius:var(--r-sm); transition:color var(--t-fast); }
  .aeam-modal-close:hover { color:var(--text); }
  .aeam-modal-body { padding:1.4rem; overflow-y:auto; }

  .aeam-collapsible { border:1px solid var(--border); border-radius:var(--r-md); background:rgba(255,255,255,0.015); }
  .aeam-collapsible-summary {
    list-style:none; cursor:pointer; display:flex; align-items:center; gap:0.5rem;
    padding:0.75rem 0.9rem; font-size:var(--fs-sm); color:var(--text); user-select:none;
    border-radius:var(--r-md); transition:background var(--t-fast);
  }
  .aeam-collapsible-summary:hover { background:rgba(255,255,255,.02); }
  .aeam-collapsible-summary::-webkit-details-marker { display:none; }
  .aeam-collapsible[open] > .aeam-collapsible-summary svg { transform:rotate(180deg); }
  .aeam-collapsible-body { padding:0 0.9rem 0.9rem; }

  .aeam-json {
    font-family:var(--font-mono); font-size:var(--fs-xs); line-height:1.6;
    color:var(--text-2); background:var(--bg);
    border:1px solid var(--border);
    border-radius:var(--r-md); padding:1rem 1.1rem; overflow:auto; max-height:60vh;
    white-space:pre; margin:0;
  }

  /* ─ Tabs ─ */
  .aeam-tabs { display:flex; gap:.25rem; border-bottom:1px solid var(--border); overflow-x:auto; }
  .aeam-tab {
    display:inline-flex; align-items:center; gap:.45rem; white-space:nowrap;
    background:none; border:none; border-bottom:2px solid transparent; cursor:pointer;
    padding:.6rem .9rem; font-size:var(--fs-sm); font-weight:600; font-family:var(--font-body);
    color:var(--muted); transition:color var(--t-fast), border-color var(--t-fast);
    margin-bottom:-1px;
  }
  .aeam-tab:hover { color:var(--text-2); }
  .aeam-tab.active { color:var(--accent); border-bottom-color:var(--accent); }
  .aeam-tab-badge {
    font-family:var(--font-mono); font-size:var(--fs-2xs); color:var(--text-2);
    background:var(--surface-3); border-radius:9px; padding:0 6px; min-width:17px; text-align:center;
  }
  .aeam-tabpanel { animation: aeamFade var(--t-med) var(--ease-out) forwards; }

  @media (max-width:760px) {
    main { padding:1.4rem !important; }
    .aeam-hide-sm { display:none !important; }
  }
`;

export function UIStyles() {
  return <style>{UI_CSS}</style>;
}

// ─── Page header ────────────────────────────────────────────────────────────────

export function PageHeader({ title, subtitle, right }) {
  return (
    <div style={{
      display: "flex", alignItems: "flex-end", justifyContent: "space-between",
      marginBottom: "2rem", gap: "1rem", flexWrap: "wrap",
    }}>
      <div>
        <h1 style={{
          fontSize: "var(--fs-2xl)", fontWeight: 650, fontFamily: "var(--font-display)",
          color: "var(--text)", margin: 0, lineHeight: 1.15, letterSpacing: "-0.02em",
        }}>{title}</h1>
        {subtitle && <p style={{ margin: "0.45rem 0 0", color: "var(--muted)", fontSize: "var(--fs-sm)", letterSpacing: "0.01em" }}>{subtitle}</p>}
      </div>
      {right && <div style={{ display: "flex", alignItems: "center", gap: "0.75rem" }}>{right}</div>}
    </div>
  );
}
