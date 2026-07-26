import { describe, it, expect } from "vitest";
import { hasPermission, hasAnyPermission, PERMISSION_MATRIX, ROLES } from "../rbac";

/* ──────────────────────────────────────────────────────────────────────────
 * Phase E10 (ENG-6): the frontend half of the RBAC lockstep pair with
 * aeam/security/rbac.py's _PERMISSION_MATRIX. These assertions mirror the
 * backend's own RBAC test expectations (analyst can read kpis, cannot
 * execute actions; auditor is read-only; admin holds everything).
 * ────────────────────────────────────────────────────────────────────────── */

describe("PERMISSION_MATRIX", () => {
  it("defines exactly the five backend roles", () => {
    expect(ROLES.sort()).toEqual(["admin", "analyst", "auditor", "operator", "readonly"]);
  });

  it("admin holds every grant used across the matrix", () => {
    const allGrants = new Set(Object.values(PERMISSION_MATRIX).flat());
    for (const grant of allGrants) {
      expect(PERMISSION_MATRIX.admin).toContain(grant);
    }
  });
});

describe("hasPermission", () => {
  it("grants analyst read access to kpis but not action execution", () => {
    expect(hasPermission(["analyst"], "kpis", "read")).toBe(true);
    expect(hasPermission(["analyst"], "actions", "execute")).toBe(false);
  });

  it("grants operator action execution but not approval", () => {
    expect(hasPermission(["operator"], "actions", "execute")).toBe(true);
    expect(hasPermission(["operator"], "actions", "approve")).toBe(false);
  });

  it("grants admin everything, including admin:config", () => {
    expect(hasPermission(["admin"], "admin", "config")).toBe(true);
    expect(hasPermission(["admin"], "actions", "approve")).toBe(true);
  });

  it("restricts auditor to view-only grants", () => {
    expect(hasPermission(["auditor"], "incidents", "view")).toBe(true);
    expect(hasPermission(["auditor"], "incidents", "resolve")).toBe(false);
    expect(hasPermission(["auditor"], "admin", "config")).toBe(false);
  });

  it("is case-insensitive on roles, resource, and action", () => {
    expect(hasPermission(["ADMIN"], "Admin", "CONFIG")).toBe(true);
  });

  it("denies when roles is empty or missing", () => {
    expect(hasPermission([], "kpis", "read")).toBe(false);
    expect(hasPermission(undefined, "kpis", "read")).toBe(false);
  });

  it("grants on union semantics across multiple roles", () => {
    expect(hasPermission(["auditor", "operator"], "actions", "execute")).toBe(true);
  });
});

describe("hasAnyPermission", () => {
  it("is true if any listed permission is granted", () => {
    expect(hasAnyPermission(["auditor"], ["admin:config", "incidents:view"])).toBe(true);
  });

  it("is false if none are granted", () => {
    expect(hasAnyPermission(["readonly"], ["admin:config", "actions:approve"])).toBe(false);
  });
});
