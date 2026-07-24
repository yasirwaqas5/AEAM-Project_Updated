"""
aeam/agents/orchestrator/incident_context.py

Per-incident working state for the Orchestrator (Phase E2, ARCH-8).

Before E2, ``Orchestrator`` held per-incident state (``_active_event``,
``_investigation_started_at``, a shared :class:`ShortTermMemory`, a shared
:class:`IncidentStateMachine`) as instance attributes. That made
``handle_event`` non-reentrant: two threads calling it concurrently —
which happens whenever a Monitor-thread event and an HTTP trigger arrive
together, since Starlette runs sync endpoints in a threadpool — would
interleave their reads and writes into the same STM/FSM and produce
cross-contaminated incidents.

``IncidentContext`` is the fix. Every call to
:meth:`~aeam.agents.orchestrator.orchestrator.Orchestrator.handle_event`
allocates one fresh context and threads it through every internal helper.
Nothing per-incident lives on the Orchestrator instance any more. The
Orchestrator itself becomes stateless with respect to incidents; only
its injected collaborators (bus, engines, LTM, agents) are shared, and
each of those is either read-only or independently thread-safe.

This matches the guidance :class:`IncidentStateMachine`'s own docstring
already gave: "in production, prefer creating a fresh instance per
incident." E2 makes that the only way the Orchestrator uses it.

The dataclass is intentionally tiny — it is a *carrier*, not a service.
It owns nothing (no locks, no I/O, no persistence). Constructing one is
a few dict/attribute assignments; there is no meaningful hot-path cost.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from aeam.agents.orchestrator.state_machine import IncidentStateMachine
from aeam.core.event_models import Event
from aeam.memory.short_term import ShortTermMemory


@dataclass
class IncidentContext:
    """
    Isolated per-incident state for one ``handle_event`` invocation.

    Attributes:
        incident_id:   The UUID minted for this incident lifecycle. The
                       same value is also written into ``stm`` under
                       ``"incident_id"`` so LLM-facing serialization
                       carries it, and used as the LTM primary key.
        event:         The confirmed :class:`Event` this context is
                       investigating. Immutable through the incident's
                       lifetime; internal helpers read it, they never
                       replace it.
        stm:           This incident's private
                       :class:`ShortTermMemory`. Never shared across
                       incidents; guaranteed to be reachable only from
                       this ``IncidentContext`` and the stack frames
                       that hold it.
        state_machine: This incident's private
                       :class:`IncidentStateMachine`. Same isolation
                       guarantee as ``stm``.
        started_at:    Monotonic timestamp captured at ``handle_event``
                       entry. Consumed by ``finalize_incident`` to
                       observe the ``investigation_duration`` histogram
                       — a strictly per-incident measurement, so it
                       lives here rather than on the Orchestrator.
    """

    incident_id: str
    event: Event
    stm: ShortTermMemory
    state_machine: IncidentStateMachine
    started_at: float = field(default=0.0)
