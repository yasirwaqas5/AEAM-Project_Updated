/* ──────────────────────────────────────────────────────────────────────────
 * lib/rbac.js
 *
 * Phase E10 — frontend half of the RBAC lockstep pair with
 * aeam/security/rbac.py's `_PERMISSION_MATRIX` (ENG-6: one permission
 * VOCABULARY, two necessary runtimes -- Python enforces it server-side,
 * this module drives what the console shows). If the backend matrix
 * changes, this object must change with it in the same commit; nothing
 * here re-derives it dynamically, exactly like the deriveStatus /
 * investigation_status.py lockstep in components/ui.jsx.
 *
 * This is UI-affordance only. It never grants access on its own -- every
 * mutating request still goes through SecurityMiddleware server-side,
 * which is the only enforcement that matters. Hiding a button a role
 * cannot use is a courtesy, not a security boundary.
 * ────────────────────────────────────────────────────────────────────────── */

const ANALYST = ["kpis:read", "kpis:trigger", "documents:search", "incidents:view", "logs:view"];
const OPERATOR = [...ANALYST, "documents:ingest", "incidents:resolve", "actions:execute"];
const ADMIN = [...OPERATOR, "actions:approve", "admin:config"];
const AUDITOR = ["incidents:view", "logs:view"];
const READONLY = ["kpis:read", "documents:search", "incidents:view", "logs:view"];

export const PERMISSION_MATRIX = {
  analyst: ANALYST,
  operator: OPERATOR,
  admin: ADMIN,
  auditor: AUDITOR,
  readonly: READONLY,
};

/** Every role name known to the matrix, for display/testing. */
export const ROLES = Object.keys(PERMISSION_MATRIX);

/**
 * True if any of `roles` grants `resource:action`. Case-insensitive.
 * Empty/undefined roles never match anything (mirrors RBAC.check_permission).
 */
export function hasPermission(roles, resource, action) {
  if (!roles || roles.length === 0) return false;
  const grant = `${String(resource).toLowerCase()}:${String(action).toLowerCase()}`;
  return roles.some((r) => (PERMISSION_MATRIX[String(r).toLowerCase()] || []).includes(grant));
}

/** True if `roles` includes any permission at all (used for "show a nav group?" checks). */
export function hasAnyPermission(roles, permissions) {
  return (permissions || []).some((p) => {
    const [resource, action] = p.split(":");
    return hasPermission(roles, resource, action);
  });
}
