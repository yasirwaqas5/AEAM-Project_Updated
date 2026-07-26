/* ──────────────────────────────────────────────────────────────────────────
 * lib/governance.js
 *
 * Phase E11/E12 — frontend half of two more lockstep pairs, following the
 * exact precedent set by lib/rbac.js (against aeam/security/rbac.py) and
 * deriveStatus (against investigation_status.py):
 *
 *   POLICY_STATUSES      <-> aeam/registry/models.py :: PolicyStatus
 *   SEMANTIC_DOC_TYPES   <-> aeam/registry/models.py :: SemanticDocType
 *   AUTHORITATIVE_DOC_TYPES
 *                        <-> aeam/agents/rag/advanced_retrieval.py ::
 *                            DEFAULT_ACTIONABLE_DOC_TYPES
 *
 * **These must be changed in the same commit as their backend counterpart.**
 * Drift here is not cosmetic: the console would either offer an operator a
 * lifecycle transition the backend rejects, or hide one it accepts. The
 * frontend tests in pages/__tests__/observabilityPanels.test.jsx pin the
 * vocabularies so a one-sided change fails the build.
 *
 * Also holds the small formatting/disclosure helpers the E11 observability
 * panels share, so "how do we render an unmeasured value" is answered once
 * rather than per page.
 * ────────────────────────────────────────────────────────────────────────── */

/** Policy lifecycle — lockstep with PolicyStatus. */
export const POLICY_STATUSES = [
  {
    value: "active",
    label: "Active",
    color: "var(--ok)",
    matchable: true,
    hint: "In force — can be cited as advisory evidence by new investigations.",
  },
  {
    value: "pending_review",
    label: "Pending Review",
    color: "var(--warn)",
    matchable: true,
    hint: "Queued for a governance decision. Still matches, so a review backlog never silently degrades investigation quality.",
  },
  {
    value: "retired",
    label: "Retired",
    color: "var(--muted)",
    matchable: false,
    hint: "Withdrawn from force. Never matches a new investigation — but is retained, not deleted, so incidents that already cited it stay explainable.",
  },
];

/** Declarable semantic document types — lockstep with SemanticDocType. */
export const SEMANTIC_DOC_TYPES = [
  "runbook", "sre_runbook", "incident_report", "post_mortem",
  "policy", "wiki", "api_doc", "reference",
];

/**
 * Types that earn the retrieval authoritative-source bonus — lockstep with
 * BusinessRelevanceScorer's DEFAULT_ACTIONABLE_DOC_TYPES. A declarable type
 * missing from that allowlist would be a declaration that silently does
 * nothing, which is why the test asserts both directions.
 */
export const AUTHORITATIVE_DOC_TYPES = new Set([
  "runbook", "sre_runbook", "incident_report", "post_mortem",
]);

/**
 * True if a policy in `status` can still be cited by a new investigation.
 * An absent status means "active" — matching Policy.from_row's default, so
 * a pre-E12 row is never ambiguous on either runtime.
 */
export function isMatchable(status) {
  const resolved = status || "active";
  const entry = POLICY_STATUSES.find((s) => s.value === resolved);
  return entry ? entry.matchable : true;
}

/** True if `docType` earns the retrieval authoritative-source bonus. */
export function isAuthoritative(docType) {
  return AUTHORITATIVE_DOC_TYPES.has(String(docType || "").toLowerCase());
}

/** Look up a lifecycle status's display metadata, defaulting to active. */
export function policyStatusMeta(status) {
  return POLICY_STATUSES.find((s) => s.value === (status || "active")) || POLICY_STATUSES[0];
}

/**
 * Format a measured duration in seconds.
 *
 * Returns an em dash for a MISSING measurement — never "0s", which would be
 * indistinguishable from an investigation that genuinely took no time. A real
 * zero still renders as "0.0s".
 */
export function formatDurationSeconds(seconds) {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return "—";
  const value = Number(seconds);
  if (value < 60) return `${value.toFixed(1)}s`;
  const minutes = Math.floor(value / 60);
  const remainder = Math.round(value % 60);
  return `${minutes}m ${remainder}s`;
}

/**
 * One-line mixed-history disclosure (EXPL-3), or null when every incident in
 * the window carried the measurement.
 *
 * Deliberately never says the excluded incidents were "zero" — they were not
 * measured, which is a different fact, and conflating the two is precisely
 * the dishonesty this phase's disclosure exists to prevent.
 */
export function summariseMixedHistory({ measured, total }) {
  const missing = Math.max(0, (total ?? 0) - (measured ?? 0));
  if (missing === 0) return null;
  return (
    `${missing} of ${total} incidents in this window predate this measurement and are ` +
    `excluded from the figures above — they were never measured, so no value is shown for them.`
  );
}
