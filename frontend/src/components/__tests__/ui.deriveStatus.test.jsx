import { describe, it, expect } from "vitest";
import { deriveStatus } from "../ui";

/* ──────────────────────────────────────────────────────────────────────────
 * Phase E10 (ENG-7, TEST-2): regression coverage for the deriveStatus
 * lockstep with the backend's aeam/agents/orchestrator/investigation_status.py
 * vocabulary. Both runtimes must agree on the 5-state vocabulary and the
 * priority order used when audit_summary is absent (pre-phase incidents).
 * ────────────────────────────────────────────────────────────────────────── */

function incidentWithAudit(investigation_status) {
  return {
    findings: [{ type: "audit_summary", investigation_status }],
  };
}

describe("deriveStatus", () => {
  it("returns UNKNOWN for a missing incident", () => {
    expect(deriveStatus(null).key).toBe("UNKNOWN");
    expect(deriveStatus(undefined).key).toBe("UNKNOWN");
  });

  it.each([
    ["INVESTIGATING", "Investigating"],
    ["RESOLVED", "Resolved"],
    ["ESCALATED", "Escalated"],
    ["FAILED", "Failed"],
    ["COMPLETE", "Complete"],
  ])("reads %s directly from audit_summary.investigation_status", (key, label) => {
    const status = deriveStatus(incidentWithAudit(key));
    expect(status.key).toBe(key);
    expect(status.label).toBe(label);
  });

  it("falls back to ESCALATED when requires_human is set and no audit_summary exists", () => {
    expect(deriveStatus({ requires_human: true }).key).toBe("ESCALATED");
  });

  it("falls back to RESOLVED when root_cause is set and no audit_summary exists", () => {
    expect(deriveStatus({ root_cause: "disk full" }).key).toBe("RESOLVED");
  });

  it("falls back to COMPLETE when neither requires_human nor root_cause is set", () => {
    expect(deriveStatus({}).key).toBe("COMPLETE");
  });

  it("prioritises requires_human over root_cause in the fallback path", () => {
    expect(deriveStatus({ requires_human: true, root_cause: "x" }).key).toBe("ESCALATED");
  });

  it("passes an unrecognised investigation_status through verbatim", () => {
    const status = deriveStatus(incidentWithAudit("SOMETHING_NEW"));
    expect(status.key).toBe("SOMETHING_NEW");
    expect(status.label).toBe("SOMETHING_NEW");
  });
});
