import { describe, it, expect } from "vitest";

/* ──────────────────────────────────────────────────────────────────────────
 * Phase E11/E12 frontend lockstep tests.
 *
 * Two vocabularies now exist on both runtimes and MUST stay in step, exactly
 * like deriveStatus/investigation_status.py and lib/rbac.js/rbac.py before
 * them:
 *
 *   - PolicyStatus       (aeam/registry/models.py  <-> lib/governance.js)
 *   - SemanticDocType    (aeam/registry/models.py  <-> lib/governance.js)
 *
 * A drift here is not cosmetic: the console would offer an operator a
 * lifecycle transition the backend rejects, or hide one it accepts.
 * ────────────────────────────────────────────────────────────────────────── */

import {
  POLICY_STATUSES,
  SEMANTIC_DOC_TYPES,
  AUTHORITATIVE_DOC_TYPES,
  isMatchable,
  isAuthoritative,
  formatDurationSeconds,
  summariseMixedHistory,
} from "../../lib/governance";

describe("policy lifecycle vocabulary (lockstep with PolicyStatus)", () => {
  it("defines exactly the three backend statuses", () => {
    expect(POLICY_STATUSES.map((s) => s.value).sort()).toEqual([
      "active", "pending_review", "retired",
    ]);
  });

  it("marks active and pending_review as matchable, retired as not", () => {
    expect(isMatchable("active")).toBe(true);
    expect(isMatchable("pending_review")).toBe(true);
    expect(isMatchable("retired")).toBe(false);
  });

  it("treats an unknown or missing status as active (matching from_row's default)", () => {
    expect(isMatchable(undefined)).toBe(true);
    expect(isMatchable(null)).toBe(true);
  });

  it("gives every status a stated meaning, never a bare label", () => {
    for (const status of POLICY_STATUSES) {
      expect(status.hint, `${status.value} has no hint`).toBeTruthy();
      expect(status.hint.length).toBeGreaterThan(20);
    }
  });
});

describe("semantic doc type vocabulary (lockstep with SemanticDocType)", () => {
  it("includes every declarable type the backend accepts", () => {
    expect(SEMANTIC_DOC_TYPES.sort()).toEqual([
      "api_doc", "incident_report", "policy", "post_mortem",
      "reference", "runbook", "sre_runbook", "wiki",
    ]);
  });

  it("marks exactly the authoritative types that earn the retrieval bonus", () => {
    expect([...AUTHORITATIVE_DOC_TYPES].sort()).toEqual([
      "incident_report", "post_mortem", "runbook", "sre_runbook",
    ]);
  });

  it("every authoritative type is also a declarable type", () => {
    for (const t of AUTHORITATIVE_DOC_TYPES) {
      expect(SEMANTIC_DOC_TYPES).toContain(t);
    }
  });

  it("does not treat a file format as an authoritative semantic type", () => {
    // The exact pre-E12 defect, pinned on the frontend too.
    expect(isAuthoritative("markdown")).toBe(false);
    expect(isAuthoritative("pdf")).toBe(false);
    expect(isAuthoritative("runbook")).toBe(true);
  });
});

describe("duration formatting", () => {
  it("renders sub-minute durations in seconds", () => {
    expect(formatDurationSeconds(4.2)).toBe("4.2s");
  });

  it("renders longer durations in minutes and seconds", () => {
    expect(formatDurationSeconds(125)).toBe("2m 5s");
  });

  it("returns an honest dash for a missing measurement, never a zero", () => {
    expect(formatDurationSeconds(null)).toBe("—");
    expect(formatDurationSeconds(undefined)).toBe("—");
  });

  it("distinguishes a genuine zero from an absent measurement", () => {
    expect(formatDurationSeconds(0)).toBe("0.0s");
  });
});

describe("mixed-history disclosure (EXPL-3)", () => {
  it("says nothing when every incident in the window was measured", () => {
    expect(summariseMixedHistory({ measured: 10, total: 10 })).toBeNull();
  });

  it("discloses how many incidents predate the measurement", () => {
    const text = summariseMixedHistory({ measured: 3, total: 10 });
    expect(text).toContain("7");
    expect(text).toContain("10");
  });

  it("never implies the excluded incidents were zero", () => {
    const text = summariseMixedHistory({ measured: 1, total: 5 });
    expect(text).not.toMatch(/\bzero\b/i);
    expect(text).toMatch(/excluded|not measured/i);
  });
});
