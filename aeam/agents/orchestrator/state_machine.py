"""
aeam/agents/orchestrator/state_machine.py

Finite State Machine (FSM) for AEAM incident lifecycle management.

Defines the valid states an incident can occupy and the permitted transitions
between them. All transition attempts that violate the FSM topology raise a
``ValueError``, preventing the orchestrator from placing an incident in an
inconsistent state.

Transition history is maintained as an append-only log so that callers can
audit how an incident progressed from creation to completion.

This module contains no business logic and no orchestrator references — it is
a pure state-management primitive.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Sequence


# ---------------------------------------------------------------------------
# States
# ---------------------------------------------------------------------------


class IncidentState(str, Enum):
    """
    Enumeration of all valid states in the AEAM incident lifecycle.

    States progress (broadly) from left to right; some back-transitions are
    permitted (e.g. ``DECIDING`` → ``INVESTIGATING`` for re-investigation).

    Members:
        IDLE:              No active incident. Starting state for every machine.
        EVENT_RECEIVED:    A qualifying event has been received and is awaiting
                           triage.
        INVESTIGATING:     Active root-cause analysis is in progress.
        DECIDING:          Investigation is complete; an action decision is being
                           evaluated.
        ACTION_PENDING:    An action has been chosen and is awaiting execution
                           (e.g. waiting for approval or a rate-limit window).
        ACTION_EXECUTING:  The chosen action is being executed.
        COMPLETE:          The incident lifecycle has ended (successfully or
                           otherwise).
    """

    IDLE = "IDLE"
    EVENT_RECEIVED = "EVENT_RECEIVED"
    INVESTIGATING = "INVESTIGATING"
    DECIDING = "DECIDING"
    ACTION_PENDING = "ACTION_PENDING"
    ACTION_EXECUTING = "ACTION_EXECUTING"
    COMPLETE = "COMPLETE"


# ---------------------------------------------------------------------------
# Permitted transition graph
# ---------------------------------------------------------------------------

# Maps each state to the set of states it is allowed to transition INTO.
# Any (source → target) pair not present here is illegal.
_ALLOWED_TRANSITIONS: dict[IncidentState, frozenset[IncidentState]] = {
    IncidentState.IDLE: frozenset({
        IncidentState.EVENT_RECEIVED,
    }),
    IncidentState.EVENT_RECEIVED: frozenset({
        IncidentState.INVESTIGATING,
        IncidentState.COMPLETE,          # event discarded / deduplicated
    }),
    IncidentState.INVESTIGATING: frozenset({
        IncidentState.DECIDING,
        IncidentState.INVESTIGATING,     # re-enter for iterative investigation
        IncidentState.COMPLETE,          # investigation inconclusive
    }),
    IncidentState.DECIDING: frozenset({
        IncidentState.ACTION_PENDING,
        IncidentState.INVESTIGATING,     # send back for more evidence
        IncidentState.COMPLETE,          # decision: no action required
    }),
    IncidentState.ACTION_PENDING: frozenset({
        IncidentState.ACTION_EXECUTING,
        IncidentState.DECIDING,          # action rejected; re-decide
        IncidentState.COMPLETE,          # action cancelled
    }),
    IncidentState.ACTION_EXECUTING: frozenset({
        IncidentState.COMPLETE,
        IncidentState.DECIDING,          # action failed; re-decide
    }),
    IncidentState.COMPLETE: frozenset(),  # terminal — no outgoing transitions
}


# ---------------------------------------------------------------------------
# Transition record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TransitionRecord:
    """
    Immutable record of a single state transition.

    Attributes:
        from_state:  The state the machine was in before the transition.
        to_state:    The state the machine moved into.
        occurred_at: UTC datetime when the transition was recorded.
    """

    from_state: IncidentState
    to_state: IncidentState
    occurred_at: datetime = field(
        default_factory=lambda: datetime.now(tz=timezone.utc)
    )

    def __str__(self) -> str:
        ts = self.occurred_at.isoformat()
        return f"{ts} | {self.from_state.value} → {self.to_state.value}"


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------


class IncidentStateMachine:
    """
    Finite State Machine governing the lifecycle of a single AEAM incident.

    Starts in :attr:`IncidentState.IDLE`. Each call to :meth:`transition`
    validates the requested move against the permitted transition graph and
    either applies it or raises :class:`ValueError`.

    All transitions are recorded in an append-only history accessible via
    :attr:`history`.

    Args:
        incident_id: Optional identifier for the incident this machine
                     manages. Used only for human-readable repr / logging;
                     the FSM itself is stateless with respect to it.

    Example::

        sm = IncidentStateMachine(incident_id="INC-42")
        sm.transition(IncidentState.EVENT_RECEIVED)
        sm.transition(IncidentState.INVESTIGATING)
        sm.transition(IncidentState.DECIDING)
        sm.transition(IncidentState.ACTION_PENDING)
        sm.transition(IncidentState.ACTION_EXECUTING)
        sm.transition(IncidentState.COMPLETE)
        print(sm.history)
    """

    def __init__(self, incident_id: str | None = None) -> None:
        """
        Initialise the FSM in the ``IDLE`` state with an empty history.

        Args:
            incident_id: Optional string identifier for the managed incident.
        """
        self._incident_id: str | None = incident_id
        self._state: IncidentState = IncidentState.IDLE
        self._history: list[TransitionRecord] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def transition(self, new_state: IncidentState) -> None:
        """
        Attempt to move the FSM from its current state to ``new_state``.

        The transition is validated against the permitted transition graph.
        If valid, the current state is updated and the transition is appended
        to the history log. If invalid, the state is **not** changed and a
        :class:`ValueError` is raised.

        Self-transitions (``current == new_state``) are permitted only for
        states that explicitly list themselves in the transition graph
        (currently ``INVESTIGATING``). All other self-transitions are rejected.

        Args:
            new_state: The target :class:`IncidentState` to move into.

        Raises:
            ValueError: If the transition from the current state to
                        ``new_state`` is not permitted by the FSM topology.

        Example::

            sm.transition(IncidentState.EVENT_RECEIVED)   # OK
            sm.transition(IncidentState.COMPLETE)          # raises ValueError
                                                           # (EVENT_RECEIVED → COMPLETE is valid,
                                                           # but IDLE → COMPLETE is not)
        """
        allowed = _ALLOWED_TRANSITIONS.get(self._state, frozenset())

        if new_state not in allowed:
            allowed_names = sorted(s.value for s in allowed)
            raise ValueError(
                f"Invalid transition: {self._state.value!r} → {new_state.value!r}. "
                f"Allowed transitions from {self._state.value!r}: "
                f"{allowed_names or ['(none — terminal state)']}"
            )

        record = TransitionRecord(from_state=self._state, to_state=new_state)
        self._state = new_state
        self._history.append(record)

    def get_state(self) -> IncidentState:
        """
        Return the current :class:`IncidentState`.

        Returns:
            The machine's current state.
        """
        return self._state

    @property
    def history(self) -> Sequence[TransitionRecord]:
        """
        Read-only view of the ordered transition history.

        Returns:
            An immutable sequence of :class:`TransitionRecord` objects, in
            chronological order from first transition to most recent.
        """
        return tuple(self._history)

    @property
    def incident_id(self) -> str | None:
        """The incident identifier supplied at construction (may be ``None``)."""
        return self._incident_id

    def is_terminal(self) -> bool:
        """
        Return ``True`` if the FSM is in a terminal state with no valid exits.

        Returns:
            ``True`` if the current state has no outgoing transitions
            (currently only :attr:`IncidentState.COMPLETE`).
        """
        return len(_ALLOWED_TRANSITIONS.get(self._state, frozenset())) == 0

    def allowed_transitions(self) -> frozenset[IncidentState]:
        """
        Return the set of states the FSM may legally move into from its current state.

        Returns:
            Frozenset of :class:`IncidentState` members. Empty if the machine
            is in a terminal state.
        """
        return _ALLOWED_TRANSITIONS.get(self._state, frozenset())

    def reset(self) -> None:
        """
        Reset the FSM to :attr:`IncidentState.IDLE` and clear transition history.

        Useful for recycling a machine instance across incident lifecycles in
        tests. In production, prefer creating a fresh instance per incident.
        """
        self._state = IncidentState.IDLE
        self._history = []

    # ------------------------------------------------------------------
    # Dunder
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return (
            f"IncidentStateMachine("
            f"incident_id={self._incident_id!r}, "
            f"state={self._state.value!r}, "
            f"transitions={len(self._history)})"
        )

    def __str__(self) -> str:
        lines = [
            f"IncidentStateMachine | incident={self._incident_id!r} | "
            f"state={self._state.value}",
        ]
        if self._history:
            lines.append("  Transition history:")
            for record in self._history:
                lines.append(f"    {record}")
        else:
            lines.append("  No transitions recorded.")
        return "\n".join(lines)