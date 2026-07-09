"""
aeam/agents/orchestrator/investigation_status.py

Canonical investigation status derivation for the AEAM system.

A single pure function computes one of five states from the same primitive
fields already persisted on every incident (``root_cause``, ``requires_human``,
``had_error``). This is the ONE place the status rule lives on the backend —
Orchestrator, Slack formatting, and Jira formatting all call it, so the value
shown in each channel can never drift out of sync with the others.

The equivalent derivation is intentionally duplicated (not imported) in the
frontend (``frontend/src/components/ui.jsx :: deriveStatus``) since the UI is
a separate JS runtime with no access to this module — the two are written
side by side and must be kept in lockstep; see the docstring there.

No I/O, no LLM calls, no external dependencies.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Canonical states
# ---------------------------------------------------------------------------

INVESTIGATING: str = "INVESTIGATING"
RESOLVED: str = "RESOLVED"
ESCALATED: str = "ESCALATED"
FAILED: str = "FAILED"
COMPLETE: str = "COMPLETE"

ALL_STATES: frozenset[str] = frozenset({
    INVESTIGATING, RESOLVED, ESCALATED, FAILED, COMPLETE,
})


def derive_investigation_status(
    *,
    root_cause: str | None,
    requires_human: bool,
    had_error: bool = False,
    is_finalized: bool = True,
) -> str:
    """
    Derive the canonical investigation status from primitive incident fields.

    Priority order (first match wins):
    1. Investigation still in progress (``is_finalized=False``) → ``INVESTIGATING``.
    2. ``requires_human`` is True → ``ESCALATED`` (escalation always wins,
       even if a root cause was also found — a human still needs to confirm).
    3. A meaningful root cause was identified → ``RESOLVED``.
    4. The investigation hit an unrecoverable error (RAG/LLM/action failure
       with no root cause) → ``FAILED``.
    5. Otherwise (terminated with no root cause, no escalation, no error —
       e.g. STOP fired on confidence/action-taken criteria alone) → ``COMPLETE``.

    Args:
        root_cause:     The incident's root_cause string, or ``None``/empty.
                         Callers should pass only *already quality-filtered*
                         root causes (see ``cause_quality.py``) — this function
                         does not re-validate content, only presence.
        requires_human: Whether the investigation was escalated.
        had_error:      Whether a terminal error occurred (e.g. every RAG
                         query attempt was exhausted with no evidence, or an
                         LLM call failed) and no root cause was ultimately set.
        is_finalized:   Whether the investigation loop has completed. Pass
                         ``False`` for a live, in-progress investigation.

    Returns:
        One of :data:`INVESTIGATING`, :data:`RESOLVED`, :data:`ESCALATED`,
        :data:`FAILED`, :data:`COMPLETE`.

    Example::

        derive_investigation_status(root_cause=None, requires_human=False, is_finalized=False)
        # "INVESTIGATING"

        derive_investigation_status(root_cause="Inefficient queries", requires_human=False)
        # "RESOLVED"

        derive_investigation_status(root_cause=None, requires_human=True)
        # "ESCALATED"

        derive_investigation_status(root_cause=None, requires_human=False, had_error=True)
        # "FAILED"
    """
    if not is_finalized:
        return INVESTIGATING

    if requires_human:
        return ESCALATED

    if root_cause and root_cause.strip():
        return RESOLVED

    if had_error:
        return FAILED

    return COMPLETE
