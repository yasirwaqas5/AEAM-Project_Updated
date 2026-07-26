"""
aeam/agents/orchestrator/runbooks.py

Deterministic, human-authored safe-action runbooks for the AEAM Action Engine.

Each runbook maps an ``event_type`` to:
- ``recommended_actions``: human-readable suggestions for an operator. These
  are never executed automatically — they are advisory text only.
- ``action_plan``: an ORDERED list of ActionAgent registry keys that ARE safe
  to execute automatically. Every key here must resolve to a reversible,
  non-destructive handler (Slack notification, Jira ticket, local diagnostic
  snapshot, local monitoring flag, email report). No runbook may reference a
  destructive or irreversible action.

Phase E9 (Human-in-the-Loop Enforcement) adds one more classification to the
same table-driven style: which ``action_plan`` steps are **gated** when an
incident's execution plan requires human approval. Every step is safe and
reversible — that is the catalog's entry condition and has not changed — but
"safe" is not the same as "an operator is content for it to happen without
being asked". The split is stated once here, in :data:`NEVER_GATED_STEPS`,
so the Orchestrator and the review API cannot disagree about it:

- **Never gated** — Slack, Jira, marketing Slack, email. These *inform*
  humans; withholding them until a human approves would withhold the very
  notification that tells someone an approval is waiting. Informing is never
  gated.
- **Gated** — everything else in an ``action_plan`` (diagnostics snapshots,
  monitoring flags, webhooks, spreadsheet writes): they change system or
  third-party state, and Phase E9 holds them until the incident's approval
  chain is satisfied.

This module still contains no execution logic — the Orchestrator (and, for
approved-and-released steps, the review service) remain the only callers of
ActionAgent.execute(). This is a pure lookup table plus selection functions,
matching the existing table-driven style already used elsewhere in the
codebase (see rag_agent.py's ``_EVENT_TYPE_NL``).
"""

from __future__ import annotations

from typing import TypedDict


class Runbook(TypedDict):
    recommended_actions: list[str]
    action_plan: list[str]


# Human-readable labels for each ActionAgent registry key, used when
# rendering "Executed Actions" / "Recommended" lists in Slack, Jira, and the
# frontend. Kept here (not duplicated per-runbook) so every caller labels
# the same action_type identically.
ACTION_LABELS: dict[str, str] = {
    "jira":        "Created Jira ticket",
    "slack":       "Posted Slack alert",
    "email":       "Sent email report",
    "diagnostics": "Captured diagnostics snapshot",
    "monitoring":  "Flagged for increased monitoring",
    "marketing_slack": "Notified marketing",
    "webhook":     "Triggered webhook",
    "sheets":      "Logged to spreadsheet",
}

# The runbooks. Only safe, reversible actions are ever listed in an
# ``action_plan`` — never a destructive or irreversible business action.
_RUNBOOKS: dict[str, Runbook] = {
    "DB_LATENCY": {
        "recommended_actions": [
            "Optimize indexes",
            "Review slow query log for repeated offenders",
        ],
        "action_plan": ["jira", "slack", "diagnostics", "monitoring"],
    },
    "SALES_DROP": {
        "recommended_actions": [
            "Review recent marketing campaigns",
            "Verify payment gateway and checkout funnel health",
        ],
        "action_plan": ["marketing_slack", "jira", "diagnostics"],
    },
    "SALES_SPIKE": {
        "recommended_actions": [
            "Confirm surge is legitimate demand, not a tracking/pricing error",
            "Verify infrastructure capacity for sustained load",
        ],
        "action_plan": ["marketing_slack", "jira", "diagnostics"],
    },
    "CPU_HIGH": {
        "recommended_actions": [
            "Identify and terminate runaway processes",
            "Review recent deployments for regressions",
        ],
        "action_plan": ["jira", "slack", "diagnostics", "monitoring"],
    },
    "MEMORY_HIGH": {
        "recommended_actions": [
            "Investigate for memory leaks in recently deployed code",
            "Consider restarting the affected service",
        ],
        "action_plan": ["jira", "slack", "diagnostics", "monitoring"],
    },
    "DISK_IO": {
        "recommended_actions": [
            "Identify high I/O processes and pending disk operations",
        ],
        "action_plan": ["jira", "slack", "diagnostics", "monitoring"],
    },
    "NETWORK_ERROR": {
        "recommended_actions": [
            "Check upstream connectivity and DNS resolution",
        ],
        "action_plan": ["jira", "slack", "diagnostics", "monitoring"],
    },
    "CACHE_MISS": {
        "recommended_actions": [
            "Verify cache warm-up and eviction policy",
        ],
        "action_plan": ["jira", "slack", "diagnostics", "monitoring"],
    },
    "QUEUE_BACKLOG": {
        "recommended_actions": [
            "Scale consumer workers or throttle producers",
        ],
        "action_plan": ["jira", "slack", "diagnostics", "monitoring"],
    },
    "DEPLOYMENT_FAILURE": {
        "recommended_actions": [
            "Review deployment logs and consider rollback",
        ],
        "action_plan": ["jira", "slack", "diagnostics"],
    },
    "AUTH_FAILURE": {
        "recommended_actions": [
            "Check identity provider health and recent policy changes",
        ],
        "action_plan": ["jira", "slack", "diagnostics", "monitoring"],
    },
}

# Fallback runbook applied to any event_type not explicitly listed above.
_DEFAULT_RUNBOOK: Runbook = {
    "recommended_actions": [
        "Investigate the affected metric and recent related changes",
    ],
    "action_plan": ["jira", "slack", "diagnostics"],
}


# Some runbook steps are logical aliases for an existing ActionAgent registry
# handler invoked with different parameters (e.g. "marketing_slack" is really
# the "slack" handler posting to a different channel) rather than a distinct
# handler. This map resolves a runbook step name to the
# (registry_action_type, extra_params) pair actually passed to
# ActionAgent.execute(). Steps not listed here resolve to themselves with no
# extra params.
_ACTION_STEP_ALIASES: dict[str, tuple[str, dict]] = {
    "marketing_slack": ("slack", {"channel": "#marketing-alerts"}),
}


# Phase E9: runbook steps that are NEVER withheld pending human approval.
# These notify humans (Slack/Jira/marketing Slack/email report) — gating a
# notification would suppress the message that tells a reviewer an approval
# is waiting, which is the opposite of human-in-the-loop. Every other step
# in an action_plan is gated when the incident requires approval.
NEVER_GATED_STEPS: frozenset[str] = frozenset({
    "jira", "slack", "marketing_slack", "email",
})


def is_gated_step(step: str) -> bool:
    """
    Return ``True`` if ``step`` must wait for human approval when the
    incident's execution plan requires it (Phase E9).

    Unknown/new steps default to gated — the conservative direction: a step
    nobody has classified is held for a human rather than executed on the
    assumption it is harmless.

    Args:
        step: A runbook ``action_plan`` entry (e.g. ``"diagnostics"``).

    Returns:
        ``True`` if the step is subject to approval gating, ``False`` for
        the notification steps in :data:`NEVER_GATED_STEPS`.

    Example::

        is_gated_step("slack")        # False — informing is never gated
        is_gated_step("diagnostics")  # True
    """
    return step not in NEVER_GATED_STEPS


def resolve_action_step(step: str) -> tuple[str, dict]:
    """
    Resolve a runbook ``action_plan`` step to the actual ActionAgent call.

    Most steps map directly to an ActionAgent registry key with no extra
    parameters. A few (currently just ``"marketing_slack"``) are aliases for
    an existing handler invoked with different parameters, so the audit
    trail and UI can label them distinctly (e.g. "Notified marketing")
    without requiring a second registered handler class.

    Args:
        step: A runbook ``action_plan`` entry (e.g. ``"jira"``,
              ``"marketing_slack"``).

    Returns:
        Tuple of ``(registry_action_type, extra_params)``. ``extra_params``
        should be merged into (and take precedence over) the base parameters
        before calling ``ActionAgent.execute()``.

    Example::

        resolve_action_step("jira")
        # ("jira", {})

        resolve_action_step("marketing_slack")
        # ("slack", {"channel": "#marketing-alerts"})
    """
    return _ACTION_STEP_ALIASES.get(step, (step, {}))


def get_runbook(event_type: str) -> Runbook:
    """
    Look up the safe-action runbook for ``event_type``.

    Args:
        event_type: The event's ``event_type`` string (e.g. ``"DB_LATENCY"``).
                    Matched case-sensitively against the canonical enum values
                    used elsewhere in the system (e.g. ``_EVENT_TYPE_NL`` in
                    ``rag_agent.py``); unrecognised values fall back to the
                    default runbook rather than raising.

    Returns:
        A :class:`Runbook` dict with ``recommended_actions`` and
        ``action_plan`` keys. Always returns a valid runbook — never ``None``.

    Example::

        get_runbook("DB_LATENCY")
        # {"recommended_actions": ["Optimize indexes", ...],
        #  "action_plan": ["jira", "slack", "diagnostics", "monitoring"]}

        get_runbook("SOME_UNKNOWN_TYPE")
        # falls back to _DEFAULT_RUNBOOK
    """
    return _RUNBOOKS.get(event_type, _DEFAULT_RUNBOOK)
