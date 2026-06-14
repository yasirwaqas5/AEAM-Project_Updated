"""
aeam/security/rbac.py

Role-based access control (RBAC) enforcement for the AEAM system.

Provides deterministic, stateless permission checking against a static
permission matrix. No database calls, no external I/O — pure in-memory
logic.

Permission matrix:
    analyst:    kpis:read, kpis:trigger, documents:search,
                incidents:view, logs:view
    operator:   kpis:read, kpis:trigger, documents:search,
                documents:ingest, incidents:view, incidents:resolve,
                actions:execute, logs:view
    admin:      all permissions
    auditor:    logs:view, incidents:view
    readonly:   kpis:read, documents:search, incidents:view, logs:view
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Static permission matrix
# ---------------------------------------------------------------------------
# Structure: role → set of "resource:action" grant strings.
# All comparisons are lower-cased at runtime so the matrix itself is the
# single source of truth and never needs duplicate entries.

_PERMISSION_MATRIX: dict[str, frozenset[str]] = {
    "analyst": frozenset({
        "kpis:read",
        "kpis:trigger",
        "documents:search",
        "incidents:view",
        "logs:view",
    }),
    "operator": frozenset({
        "kpis:read",
        "kpis:trigger",
        "documents:search",
        "documents:ingest",
        "incidents:view",
        "incidents:resolve",
        "actions:execute",
        "logs:view",
    }),
    "admin": frozenset({
        "kpis:read",
        "kpis:trigger",
        "documents:search",
        "documents:ingest",
        "incidents:view",
        "incidents:resolve",
        "actions:execute",
        "actions:approve",
        "logs:view",
    }),
    "auditor": frozenset({
        "incidents:view",
        "logs:view",
    }),
    "readonly": frozenset({
        "kpis:read",
        "documents:search",
        "incidents:view",
        "logs:view",
    }),
}


class RBAC:
    """
    Stateless role-based access control enforcer.

    Checks whether any role in the supplied list grants permission to
    perform ``action`` on ``resource``, using a static in-memory permission
    matrix. Returns ``True`` on the first matching grant; ``False`` if no
    role covers the requested permission.

    Rules:
    - Role names are case-insensitive.
    - Resource and action names are case-insensitive.
    - Any single role granting the permission is sufficient (union semantics).
    - Unrecognised roles are silently treated as having no permissions.
    - No database calls are made.

    Example::

        rbac = RBAC()
        allowed = rbac.check_permission(
            roles=["analyst"],
            resource="kpis",
            action="read",
        )
        # True

        allowed = rbac.check_permission(
            roles=["auditor"],
            resource="actions",
            action="execute",
        )
        # False
    """

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check_permission(
        self,
        roles: list[str],
        resource: str,
        action: str,
    ) -> bool:
        """
        Return ``True`` if any role in ``roles`` permits ``action`` on
        ``resource``.

        Lookup is performed against the static permission matrix. Roles,
        resource, and action are all normalised to lower-case before
        comparison.

        Args:
            roles:    List of role names assigned to the requester
                      (e.g. ``["analyst", "readonly"]``). May be empty.
            resource: The resource being accessed. One of: ``"kpis"``,
                      ``"documents"``, ``"incidents"``, ``"actions"``,
                      ``"logs"``.
            action:   The operation being performed. One of: ``"read"``,
                      ``"trigger"``, ``"search"``, ``"ingest"``,
                      ``"view"``, ``"resolve"``, ``"execute"``,
                      ``"approve"``.

        Returns:
            ``True``  — at least one role grants the permission.
            ``False`` — no role grants the permission, or ``roles`` is
            empty.

        Note:
            Unrecognised roles are silently skipped (treated as no-op).
            Unrecognised resource/action combinations never match any
            grant and therefore always return ``False``.

        Example::

            rbac = RBAC()

            # Operator may execute actions.
            rbac.check_permission(["operator"], "actions", "execute")  # True

            # Auditor may not execute actions.
            rbac.check_permission(["auditor"], "actions", "execute")   # False

            # Multiple roles — granted if any one matches.
            rbac.check_permission(["auditor", "operator"], "actions", "execute")  # True

            # Empty roles → always denied.
            rbac.check_permission([], "kpis", "read")  # False
        """
        if not roles:
            logger.warning(
                "RBAC.check_permission | DENIED | resource=%s | action=%s | "
                "reason=no_roles_provided",
                resource, action,
            )
            return False

        # Normalise to lower-case for case-insensitive comparison.
        resource_lc: str = resource.strip().lower()
        action_lc: str = action.strip().lower()
        grant: str = f"{resource_lc}:{action_lc}"

        for role in roles:
            role_lc: str = role.strip().lower()
            granted_permissions = _PERMISSION_MATRIX.get(role_lc, frozenset())

            if grant in granted_permissions:
                logger.info(
                    "RBAC.check_permission | ALLOWED | role=%s | "
                    "resource=%s | action=%s",
                    role_lc, resource_lc, action_lc,
                )
                return True

        logger.warning(
            "RBAC.check_permission | DENIED | roles=%s | "
            "resource=%s | action=%s",
            [r.strip().lower() for r in roles],
            resource_lc,
            action_lc,
        )
        return False

    @staticmethod
    def available_roles() -> list[str]:
        """
        Return the sorted list of roles defined in the permission matrix.

        Returns:
            Alphabetically sorted list of role name strings.
        """
        return sorted(_PERMISSION_MATRIX.keys())

    @staticmethod
    def permissions_for(role: str) -> frozenset[str]:
        """
        Return the full set of permission grants for ``role``.

        Args:
            role: Role name (case-insensitive).

        Returns:
            Frozenset of ``"resource:action"`` grant strings, or an
            empty frozenset if the role is not recognised.
        """
        return _PERMISSION_MATRIX.get(role.strip().lower(), frozenset())

    def __repr__(self) -> str:
        return f"RBAC(roles={sorted(_PERMISSION_MATRIX.keys())})"