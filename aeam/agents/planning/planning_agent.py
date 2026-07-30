"""
aeam/agents/planning/planning_agent.py

The Planning Agent (Phase F6 — Agent Mesh Formalization).

A **promotion by composition**, not a rewrite. The C7
:class:`~aeam.intelligence.execution_planning.ExecutionPlanningEngine`
already synthesizes every accumulated finding into one explainable,
priority-ordered execution plan, and it does that correctly. What it lacked
was standing: it was an *engine* reached through a constructor parameter,
not a named member of the roster with its own contract, heartbeat, metrics,
and console presence the way RAG, Report, and Action have.

This class supplies exactly that standing and nothing else.

Byte-identical output, by construction
--------------------------------------
:meth:`plan` forwards its keyword arguments to the engine **unchanged** and
returns **the engine's own object**, unmodified — no wrapping key, no
normalisation, no defaulting, no post-processing. There is no second code
path that could drift, because there is no second code path: this method
contains one delegation and nothing that touches the result.

That is what makes the promotion safe. The Orchestrator's planning stage
calls ``.plan(...)`` on whatever it was given, so passing this agent where
the engine used to go is a drop-in — no orchestrator surgery, and the
``execution_plan`` finding it appends is identical field for field
(COMPAT-1).

What this agent is NOT
---------------------
It is not a coordinator. It plans when asked, exactly like the engine did:
it does not decide *when* planning happens, does not dispatch the plan it
produces, does not call ``ActionAgent``, and holds no per-incident state
(ARCH-1, ARCH-8). The Orchestrator remains the single coordinator and the
sole caller.

Observability (E7/E11, reused)
------------------------------
Three additions, all through existing infrastructure:

* a **heartbeat** on the shared E7 ``heartbeat_tracker`` under ``planning``,
  so the agent's liveness is visible where every other worker's is;
* its **own** ``agent_execution_time`` label (``planning``), so the roster
  entry has a metric of its own in ``/metrics``;
* an **OTel span** (``agent.planning``) nested inside the investigation
  trace the Orchestrator already opens.

The Orchestrator's own F5 stage timing (recorded under ``execution_plan``)
is deliberately left alone. The two measure different things: that one is
"how long the planning STAGE of this investigation took", persisted for
Timeline Replay; this one is "how long the planning AGENT has spent
working", aggregated across incidents in ``/metrics``. Neither replaces the
other, and collapsing them would cost one of the two questions an answer.

A heartbeat here means "this agent ran recently", not "this agent's thread
is alive" — planning is request-scoped, invoked once per finalized incident
rather than looping. ``GET /health`` therefore reports it informationally
and never degrades platform status on its age (see ``aeam/main.py``).
"""

from __future__ import annotations

import logging
from typing import Any

from aeam.monitoring.metrics import (
    agent_execution_time,
    end_timer,
    heartbeat_tracker,
    start_timer,
)
from aeam.monitoring.tracing import investigation_span

logger = logging.getLogger(__name__)

#: The name this agent registers under in the E1 ``agent_roster``, the E7
#: heartbeat tracker, and its ``agent_execution_time`` label. One constant so
#: the roster entry, the heartbeat key, and the metric label can never drift
#: apart.
AGENT_NAME: str = "planning"


class PlanningAgent:
    """
    First-class agent wrapper around the C7 execution-planning engine.

    Args:
        engine: The existing
                :class:`~aeam.intelligence.execution_planning.ExecutionPlanningEngine`.
                Required — this agent has no planning logic of its own to
                fall back on, and inventing one would be the rewrite this
                phase exists to avoid.

    Raises:
        ValueError: If ``engine`` is ``None``.
    """

    def __init__(self, engine: Any) -> None:
        if engine is None:
            raise ValueError(
                "engine must not be None. PlanningAgent is a composition wrapper "
                "over ExecutionPlanningEngine and has no planning logic of its own."
            )
        self._engine = engine

    # ------------------------------------------------------------------
    # Public API — the C7 contract, unchanged
    # ------------------------------------------------------------------

    def plan(self, **kwargs: Any) -> dict[str, Any]:
        """
        Produce the execution plan for one incident.

        The signature is deliberately ``**kwargs``: the C7 engine's
        ``plan()`` is keyword-only, and forwarding opaquely means this
        wrapper cannot silently drop, reorder, rename, or default an
        argument — and it needs no edit when the engine's signature grows.
        A wrapper that restated the parameter list would be a second place
        for the contract to live, and therefore a second place for it to
        drift.

        Args:
            **kwargs: Forwarded verbatim to
                      ``ExecutionPlanningEngine.plan()``. See that method
                      for the authoritative parameter list.

        Returns:
            The engine's own return value, unmodified — the SAME dict
            object, with the same keys and the same values.

        Raises:
            Whatever the engine raises. The engine's documented contract is
            that it never raises; if that ever changes, the exception
            propagates to the Orchestrator's existing handler rather than
            being swallowed here, because an agent that hid a planning
            failure would make the investigation's plan silently absent.
        """
        started = start_timer()
        try:
            with investigation_span(f"agent.{AGENT_NAME}"):
                return self._engine.plan(**kwargs)
        finally:
            # Recorded in `finally` so a raising engine still reports the
            # time it consumed and still proves the agent ran: "the planning
            # agent was invoked and failed" must not read as "the planning
            # agent was never invoked".
            end_timer(agent_execution_time.labels(agent=AGENT_NAME), started)
            heartbeat_tracker.record(AGENT_NAME)

    # ------------------------------------------------------------------
    # Roster identity
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        """The roster/heartbeat/metric name — see :data:`AGENT_NAME`."""
        return AGENT_NAME

    @property
    def engine(self) -> Any:
        """The wrapped C7 engine.

        Exposed read-only so a caller (or a test) can prove the agent and
        the engine are the same planner rather than two implementations.
        """
        return self._engine

    def __repr__(self) -> str:
        return f"PlanningAgent(engine={self._engine!r})"
