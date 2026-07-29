"""
aeam/agents/orchestrator/orchestrator.py

Central coordination logic for the AEAM modular monolith.

The Orchestrator drives the full incident lifecycle:
    EVENT_RECEIVED → INVESTIGATING → DECIDING → … → COMPLETE

It wires together the DecisionEngine, EvaluationEngine, per-incident
state machine, per-incident short-term memory, and long-term persistence
without containing any detection logic, LLM calls, Action Agent calls,
RAG, forecasting, or direct database writes.

**Concurrency (Phase E2, ARCH-8).** ``handle_event`` is fully reentrant:
every call allocates a fresh :class:`IncidentContext` holding this
incident's own :class:`ShortTermMemory` and :class:`IncidentStateMachine`,
and threads it through every internal helper. Nothing per-incident lives
on the Orchestrator instance. Two threads driving ``handle_event``
concurrently — Monitor + HTTP trigger, or N parallel triggers — cannot
cross-contaminate each other's STM, FSM, or active event.

All dependencies are injected; the Orchestrator itself is stateless
between incidents. Its collaborators (event bus, decision/evaluation
engines, long-term memory, RAG/Action/Report agents, C1-D2 engines) are
shared across incidents on purpose — they are either read-only or
individually thread-safe. Only per-incident state was ever the concurrency
hazard; that is what E2 relocates.
"""

from __future__ import annotations

import json
import logging
import uuid
import warnings
from typing import TYPE_CHECKING, Any

from aeam.agents.kpi.kpi_agent import _DEFAULT_HISTORY_LIMIT as _KPI_DEFAULT_HISTORY_LIMIT
from aeam.agents.kpi.kpi_agent import KPIAgent
from aeam.agents.learning.learning_agent import LearningAgent, calibrate_confidence
from aeam.intelligence.calibration import CalibrationEngine
from aeam.agents.orchestrator.decision_engine import DecisionEngine
from aeam.agents.orchestrator.evaluation_engine import EvaluationEngine
from aeam.agents.orchestrator.incident_context import IncidentContext
from aeam.agents.orchestrator.investigation_status import derive_investigation_status
from aeam.agents.orchestrator.notifications import format_jira_description, format_slack_message
from aeam.agents.orchestrator.runbooks import get_runbook, is_gated_step, resolve_action_step
from aeam.agents.orchestrator.state_machine import IncidentState, IncidentStateMachine
from aeam.agents.rag.cause_quality import best_meaningful_cause
from aeam.agents.rag.rag_agent import RAGAgent, parse_llm_json
from aeam.config.settings import Settings
from aeam.core.event_bus import EventBus
from aeam.core.event_models import Event
from aeam.memory.long_term import LongTermMemory
from aeam.memory.short_term import ShortTermMemory
from aeam.monitoring.metrics import (
    action_failure_total,
    action_success_total,
    active_incidents,
    agent_execution_time,
    end_timer,
    incident_cost_scope,
    incidents_total,
    investigation_duration,
    start_timer,
)
from aeam.monitoring.tracing import current_trace_id, investigation_span
from aeam.services.llm_service import LLMService

if TYPE_CHECKING:
    from aeam.agents.report.report_agent import ReportAgent

logger = logging.getLogger(__name__)


def _kpi_history_limit(settings: Any) -> int:
    """Read ``KPI_AGENT_HISTORY_LIMIT``, falling back to the engine default.

    Guarded rather than read directly because construction sites across the
    suite pass Settings-shaped doubles whose attributes are auto-created;
    a non-integer reaching ``KPIAgent`` would raise at construction and take
    the whole Orchestrator down with it. The engine-owned default lives in
    :mod:`aeam.agents.kpi.kpi_agent` (ENG-6), so ``None`` here means "use
    whatever that module says", not a second copy of the number.
    """
    value = getattr(settings, "KPI_AGENT_HISTORY_LIMIT", None)
    if isinstance(value, int) and not isinstance(value, bool) and value >= 2:
        return value
    return _KPI_DEFAULT_HISTORY_LIMIT


def _build_learning_agent(settings: Any, long_term_memory: Any) -> Any | None:
    """Construct a LearningAgent from the composition root's own database.

    Reaches the database client through LongTermMemory's injected client
    rather than taking a second one: the Orchestrator already holds exactly
    one path to the database and opening a second would be a second
    persistence seam (ARCH-1).

    Returns ``None`` when no client can be reached — calibration then
    reports "no active calibration" and raw confidence stands, which is the
    honest outcome for a deployment that enabled the flag without a
    reachable store.
    """
    client = getattr(long_term_memory, "_db", None)
    if client is None:
        logger.warning(
            "LEARNING_CALIBRATION_ENABLED is true but no database client is "
            "reachable through LongTermMemory; confidence will be reported raw."
        )
        return None
    try:
        return LearningAgent(
            database_client=client,
            engine=CalibrationEngine(
                min_training_samples=getattr(settings, "LEARNING_MIN_TRAINING_SAMPLES", 60),
                holdout_fraction=getattr(settings, "LEARNING_HOLDOUT_FRACTION", 0.3),
                min_improvement=getattr(settings, "LEARNING_MIN_ECE_IMPROVEMENT", 0.01),
            ),
            history_limit=getattr(settings, "LEARNING_HISTORY_LIMIT", 5000),
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("Learning Agent could not be constructed: %s", exc)
        return None


class Orchestrator:
    """
    Central coordinator for AEAM incident lifecycle management.

    Receives confirmed events from the EventBus, drives them through the
    investigation loop, and persists outcomes to long-term memory.

    The Orchestrator:
    - Allocates a fresh per-incident
      :class:`~aeam.agents.orchestrator.state_machine.IncidentStateMachine`
      and :class:`~aeam.memory.short_term.ShortTermMemory` inside
      :meth:`handle_event`, bundled into an
      :class:`~aeam.agents.orchestrator.incident_context.IncidentContext`
      that is threaded through every internal helper (Phase E2, ARCH-8).
    - Delegates action decisions to :class:`~aeam.agents.orchestrator.decision_engine.DecisionEngine`.
    - Determines loop termination via :class:`~aeam.agents.orchestrator.evaluation_engine.EvaluationEngine`.
    - Persists resolved incidents via :class:`~aeam.memory.long_term.LongTermMemory`.
    - Executes external actions via injected ActionAgent (Phase 6).
    - Generates human‑readable reports and alerts via injected ReportAgent (Phase 7).

    Constraints:
    - No RAG calls.
    - No Forecast calls.
    - No external API calls (delegated to ActionAgent).
    - No direct database writes (delegated to LongTermMemory).

    Args:
        event_bus:         Internal event dispatcher (used to register handler).
        decision_engine:   Hybrid rule-first decision engine.
        evaluation_engine: Investigation progress evaluator.
        long_term_memory:  Persistent incident and decision store.
        settings:          Application configuration.
        rag_agent:         Optional RAG agent for Phase 4 (default None).
        action_agent:      Optional Action agent for Phase 6 (default None).
        report_agent:      Optional Report agent for Phase 7 (default None).
        short_term_memory: DEPRECATED (Phase E2). A per-incident STM is
                           allocated internally; anything passed here is
                           ignored and triggers a DeprecationWarning.
                           Accepted only so pre-E2 constructor call
                           sites keep compiling (COMPAT-1).
        state_machine:     DEPRECATED (Phase E2). Same treatment.

    **Concurrency contract (Phase E2, ARCH-8).** ``handle_event`` is
    fully reentrant. N threads may drive it concurrently — the Monitor
    thread and any number of Starlette threadpool workers running
    ``POST /trigger`` — without cross-contamination. Every incident is
    scoped by its own IncidentContext; the Orchestrator holds no
    per-incident instance attributes.

    Example::

        orchestrator = Orchestrator(
            event_bus=bus,
            decision_engine=decision_engine,
            evaluation_engine=evaluation_engine,
            long_term_memory=ltm,
            settings=settings,
            rag_agent=rag_agent,      # optional
            action_agent=action_agent, # optional
            report_agent=report_agent, # optional
        )
        # Register as an EventBus handler:
        bus.register_handler("KPI_ANOMALY", orchestrator.handle_event)
    """

    def __init__(
        self,
        event_bus: EventBus,
        decision_engine: DecisionEngine,
        evaluation_engine: EvaluationEngine,
        long_term_memory: LongTermMemory,
        settings: Settings,
        rag_agent: Any | None = None,
        action_agent: Any | None = None,  # Phase 6
        report_agent: Any | None = None,  # Phase 7
        memory_engine: Any | None = None,  # Phase C1 — Enterprise Memory Engine
        policy_registry: Any | None = None,  # Phase C3 — Enterprise Policy Registry
        cross_dataset_analyzer: Any | None = None,  # Phase C4 — Cross-Dataset Intelligence
        adaptive_detection_engine: Any | None = None,  # Phase C5 — Adaptive Detection Engine
        execution_planning_engine: Any | None = None,  # Phase C7 — Enterprise Action Planning Engine
        explainability_engine: Any | None = None,  # Phase D1 — Enterprise Explainability Engine
        ai_evaluation_engine: Any | None = None,  # Phase D2 — Enterprise AI Evaluation & Quality Engine
        human_review_service: Any | None = None,  # Phase E9 — Human-in-the-Loop Enforcement
        business_graph_engine: Any | None = None,  # Phase F4 — Business Graph (advisory)
        kpi_agent: Any | None = None,  # Phase F1 — real KPI investigation pass
        learning_agent: Any | None = None,  # Phase F2 — confidence recalibration
        short_term_memory: ShortTermMemory | None = None,   # DEPRECATED (Phase E2) — see class docstring
        state_machine: IncidentStateMachine | None = None,  # DEPRECATED (Phase E2) — see class docstring
    ) -> None:
        self._bus = event_bus
        self._decision = decision_engine
        self._evaluation = evaluation_engine
        self._ltm = long_term_memory
        self._settings = settings
        self._rag = rag_agent
        self._action = action_agent  # Phase 6
        self._report = report_agent   # Phase 7
        self._memory = memory_engine  # Phase C1
        self._policy_registry = policy_registry  # Phase C3
        self._cross_dataset = cross_dataset_analyzer  # Phase C4
        # Phase F4: None unless BUSINESS_GRAPH_ENABLED and a graph store
        # was wired. None means the graph stage is skipped entirely and the
        # investigation is byte-identical to F3's.
        self._business_graph = business_graph_engine
        self._adaptive_detection = adaptive_detection_engine  # Phase C5
        self._execution_planner = execution_planning_engine  # Phase C7
        self._explainability_engine = explainability_engine  # Phase D1
        self._ai_evaluator = ai_evaluation_engine  # Phase D2
        # Phase E9: when wired AND enforcing, gated runbook steps are
        # recorded as pending instead of executed. None (or a service with
        # enforcement off) leaves finalization byte-identical to pre-E9.
        self._human_review = human_review_service  # Phase E9

        # Phase F1: the real KPI Agent. An explicitly injected agent wins
        # (tests, and any caller assembling the graph itself); otherwise
        # one is constructed unless KPI_AGENT_ENABLED is explicitly false.
        # Defaulting to constructed is deliberate: F1 DELETED the
        # placeholder this pass replaces, so an absent agent means the
        # investigation has no KPI pass at all — a posture that exists only
        # for the documented rollback, never by accident.
        if kpi_agent is not None:
            self._kpi_agent = kpi_agent
        elif getattr(settings, "KPI_AGENT_ENABLED", True) is False:
            self._kpi_agent = None
        else:
            self._kpi_agent = KPIAgent(
                long_term_memory=long_term_memory,
                history_limit=_kpi_history_limit(settings),
            )

        # Phase F2: the Learning Agent supplies the active calibration at
        # finalize. It is wired ONLY when calibration is explicitly enabled
        # -- with the flag off, `self._learning_agent` is None and the
        # calibration block in _finalize_incident does not execute at all,
        # so raw confidence is byte-identical to F1 (the stated rollback).
        # An explicitly injected agent wins, for tests and for callers that
        # assemble the graph themselves.
        self._learning_agent = learning_agent
        if self._learning_agent is None and getattr(
            settings, "LEARNING_CALIBRATION_ENABLED", False
        ) is True:
            self._learning_agent = _build_learning_agent(settings, long_term_memory)

        # Phase E2, ARCH-8: no per-incident instance attributes. STM,
        # FSM, active event, and investigation_started_at all live on the
        # per-invocation IncidentContext instead. The kwargs above are
        # retained purely so pre-E2 construction sites (tests, older
        # bootstrap code) keep compiling; the concurrency bug they used
        # to cause is now impossible because the injected objects are
        # ignored and per-incident instances are allocated inside
        # handle_event(). One-shot DeprecationWarning surfaces the change.
        if short_term_memory is not None or state_machine is not None:
            warnings.warn(
                "Orchestrator no longer accepts a shared ShortTermMemory or "
                "IncidentStateMachine (Phase E2, ARCH-8). A fresh per-incident "
                "instance is allocated inside handle_event(). The passed-in "
                "objects are ignored.",
                DeprecationWarning,
                stacklevel=2,
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def handle_event(self, event: Event) -> None:
        """
        Entry point for a confirmed anomaly event.

        Allocates a fresh :class:`IncidentContext` (per-incident STM,
        per-incident FSM, per-incident started_at), seeds it, and drives
        the investigation loop through :meth:`_investigate`. This is the
        ONLY public method that mutates incident state — every internal
        helper reads from and writes to the ctx passed in, never to
        instance attributes.

        Concurrency (Phase E2, ARCH-8): reentrant. Two threads calling
        ``handle_event`` at the same time — Monitor + trigger, or two
        triggers, or any mixture — receive their own IncidentContext and
        cannot cross-contaminate. The Orchestrator itself is stateless
        with respect to the incident.

        Args:
            event: The confirmed :class:`~aeam.core.event_models.Event`
                   to handle.

        Note:
            This method is designed to be registered directly as an
            :class:`~aeam.core.event_bus.EventBus` handler::

                bus.register_handler("KPI_ANOMALY", orchestrator.handle_event)
        """
        incident_id = str(uuid.uuid4())
        ctx = IncidentContext(
            incident_id=incident_id,
            event=event,
            stm=ShortTermMemory(),
            state_machine=IncidentStateMachine(incident_id=incident_id),
            started_at=start_timer(),
        )

        # Metrics: one incident counted, one active investigation started.
        incidents_total.labels(event_type=event.event_type, severity=event.severity).inc()
        active_incidents.inc()

        # Phase E11 (OBS-2): open this thread's per-incident cost scope so
        # every LLM call, retrieval, and action the investigation causes is
        # attributable to it. Thread-local, so concurrent investigations
        # (Phase E2) accumulate separately. Closed in _finalize_incident.
        incident_cost_scope.start(incident_id)

        logger.info(
            "Orchestrator.handle_event | incident_id=%s | event_id=%s | "
            "metric=%s | severity=%s",
            incident_id, event.event_id, event.metric, event.severity,
        )

        # Initialise this incident's private STM.
        ctx.stm.initialize(
            task_type="anomaly_investigation",
            incident_id=incident_id,
        )
        ctx.stm.set("investigation_depth", 0)
        ctx.stm.set("findings", [])
        ctx.stm.set("hypotheses", [])
        ctx.stm.set("evidence", [])
        ctx.stm.set("confidence", None)
        ctx.stm.set("root_cause", None)
        # Provenance of the root cause (Phase E1, ENG-5): "rag" |
        # "llm_reasoning" | "kpi_analysis" | None. Every writer of
        # "root_cause" also writes this, so _finalize_incident can
        # quarantine untrustworthy conclusions from Enterprise Memory and
        # the UI can label them. Phase F1 retired the "placeholder" value's
        # only producer; the vocabulary keeps the member because persisted
        # incidents predating F1 still carry it (COMPAT-1/COMPAT-6).
        ctx.stm.set("root_cause_source", None)
        ctx.stm.set("action_taken", False)
        ctx.stm.set("requires_human", False)

        # Transition FSM and store event.
        ctx.state_machine.transition(IncidentState.EVENT_RECEIVED)
        ctx.stm.set("event", event.model_dump(mode="json"))
        ctx.stm.set("incident_id", incident_id)

        logger.debug(
            "handle_event | STM initialised | incident_id=%s", incident_id
        )

        # Phase E11 (OBS-6): one ROOT span per investigation. Every stage
        # span opened below nests inside it automatically, so an
        # investigation emits a single connected trace joinable to its logs
        # by incident id. A no-op when tracing is disabled (the default).
        with investigation_span(
            "investigation",
            incident_id=incident_id,
            event_type=event.event_type,
            metric=event.metric,
            severity=event.severity,
        ):
            self._investigate(ctx)

    def _investigate(self, ctx: IncidentContext) -> None:
        """
        Perform one investigation pass.

        Steps:
        1. Transition FSM to ``INVESTIGATING``.
        2. Increment ``investigation_depth`` in STM.
        3. Call :meth:`~aeam.agents.orchestrator.decision_engine.DecisionEngine.decide`.
        4. Transition FSM to ``DECIDING``.
        5. Act on the decision:
           - ``"INVESTIGATE"`` → (optionally invoke RAG if configured), then
             run the KPI Agent pass and call :meth:`evaluate`.
           - ``"STOP"`` → call :meth:`finalize_incident`.
           - Unknown decision → log a warning and call :meth:`finalize_incident`.

        Raises:
            RuntimeError: If called without an active event (i.e. before
                          :meth:`handle_event`).
        """
        # Phase E2, ARCH-8: ctx.event is always the event we were
        # invoked for — there is no more "no active event" failure
        # mode, because the ctx cannot exist without one.

        # --- Step 1 & 2: transition + depth increment ---
        ctx.state_machine.transition(IncidentState.INVESTIGATING)

        depth: int = (ctx.stm.get("investigation_depth") or 0)
        depth += 1
        ctx.stm.set("investigation_depth", depth)

        logger.info(
            "investigate | depth=%d | event_id=%s | incident_id=%s",
            depth, ctx.event.event_id, ctx.incident_id,
        )

        # --- Step 3: decide ---
        # Phase E11 (OBS-6): the first stage span of the investigation trace.
        with investigation_span("decision", incident_id=ctx.incident_id, depth=depth):
            decision_result = self._decision.decide(
                event=ctx.event,
                memory=ctx.stm,
            )

        # ✅ NEW: store LLM response if present (from the decision engine)
        if decision_result.get("source") == "llm":
            ctx.stm.set("llm_response", decision_result.get("llm_response", ""))

        action: str = decision_result.get("decision", "INVESTIGATE")
        confidence: float = float(decision_result.get("confidence", 0.0))

        # Phase E11 (OBS-5): every investigation-path log line carries the
        # incident id, so logs, metrics, and the OTel trace all correlate on
        # one key without a second correlation scheme.
        logger.info(
            "investigate | decision=%s | confidence=%.2f | source=%s | incident_id=%s",
            action, confidence, decision_result.get("source", "unknown"), ctx.incident_id,
        )

        # Log the decision to STM for evaluation and audit.
        ctx.stm.append("findings", {
            "depth": depth,
            "decision": action,
            "confidence": confidence,
            "source": decision_result.get("source"),
        })

        # --- Step 4: transition to DECIDING ---
        if ctx.state_machine.get_state() != IncidentState.DECIDING:
            ctx.state_machine.transition(IncidentState.DECIDING)

        # --- Step 5: act on decision ---
        if action == "STOP":
            self._finalize_incident(ctx)
            return

        if action != "INVESTIGATE":
            logger.warning(
                "investigate | unknown decision %r | defaulting to finalize",
                action,
            )
            self._finalize_incident(ctx)
            return

        # ---------- INVESTIGATE path ----------
        agents = decision_result.get("agents", []) or []

        # --- Enterprise Memory recall (Phase C1) ---
        # Runs once per incident lifecycle (idempotency guarded by
        # _has_memory_finding(), same pattern RAGAgent uses for its own
        # adaptive-query exhaustion check) rather than being gated behind
        # the decision engine's "RAG" agent routing -- memory recall is a
        # distinct retrieval stage from document RAG, not a sub-step of it.
        # Reuses RAGAgent._formulate_query() (the SAME query-formulation
        # helper document RAG itself calls) so memory and document
        # retrieval search on identical vocabulary -- no duplicate query
        # logic. A missing/failed memory engine never blocks investigation.
        if self._memory is not None and not self._has_memory_finding(ctx):
            memory_incident_id = ctx.stm.get("incident_id", "unknown")
            with investigation_span("evidence.memory", incident_id=ctx.incident_id):
                try:
                    memory_query = RAGAgent._formulate_query(ctx.event)
                    similar_incidents = self._memory.recall_similar_incidents(
                        query=memory_query,
                        exclude_incident_id=memory_incident_id,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.error(
                        "investigate | memory recall failed | incident_id=%s | error=%s",
                        ctx.incident_id, exc,
                    )
                    memory_query = None
                    similar_incidents = []

            ctx.stm.append("findings", {
                "type": "memory",
                "data": {
                    "query": memory_query,
                    "matches": similar_incidents,
                },
            })
            logger.info(
                "investigate | memory recall | matches=%d | incident_id=%s",
                len(similar_incidents), memory_incident_id,
            )

        # --- Enterprise Policy Registry match (Phase C3) ---
        # Same idempotency pattern as Enterprise Memory above -- runs once
        # per incident lifecycle, independent of decision-engine agent
        # routing. Reuses RAGAgent._formulate_query() (the SAME query
        # RAG/Memory already compute) so all three evidence sources search
        # on identical vocabulary -- no duplicate query logic. Advisory
        # only: matched policies are appended as their own findings entry
        # and NEVER passed to RuleEngine.evaluate(), DecisionEngine, or
        # ActionAgent -- they cannot trigger, suppress, or override a
        # deterministic rule.
        if self._policy_registry is not None and not self._has_policy_finding(ctx):
            policy_incident_id = ctx.stm.get("incident_id", "unknown")
            with investigation_span("evidence.policy", incident_id=ctx.incident_id):
                try:
                    policy_query = RAGAgent._formulate_query(ctx.event)
                    policy_matches = self._policy_registry.match_for_incident(
                        metric=ctx.event.metric,
                        query=policy_query,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.error(
                        "investigate | policy match failed | incident_id=%s | error=%s",
                        ctx.incident_id, exc,
                    )
                    policy_query = None
                    policy_matches = []

            ctx.stm.append("findings", {
                "type": "policy",
                "data": {
                    "query": policy_query,
                    "matches": policy_matches,
                },
            })
            logger.info(
                "investigate | policy match | matches=%d | incident_id=%s",
                len(policy_matches), policy_incident_id,
            )

        # --- Cross-Dataset Intelligence (Phase C4) ---
        # Same idempotency pattern as Enterprise Memory / Policy Registry
        # above -- runs once per incident lifecycle. Correlates the
        # incident's metric against every OTHER currently-activated
        # dataset via CrossDatasetAnalyzer, which reuses
        # DatasetIntelligenceService/DatasetKPISource/StatisticalDetector
        # unmodified -- no second MonitorAgent, no second RuleEngine, no
        # new detection logic. Advisory only: appended as its own findings
        # entry, never fed back into RuleEngine/DecisionEngine/ActionAgent.
        if self._cross_dataset is not None and not self._has_cross_dataset_finding(ctx):
            cross_incident_id = ctx.stm.get("incident_id", "unknown")
            with investigation_span("evidence.cross_dataset", incident_id=ctx.incident_id):
                try:
                    cross_dataset_result = self._cross_dataset.analyze(metric=ctx.event.metric)
                except Exception as exc:  # noqa: BLE001
                    logger.error(
                        "investigate | cross-dataset analysis failed | incident_id=%s | error=%s",
                        ctx.incident_id, exc,
                    )
                    cross_dataset_result = {
                        "insufficient_data": True,
                        "reason": f"Cross-dataset analysis failed: {exc}",
                        "origin_dataset_id": None, "origin_dataset_name": None,
                        "candidates_checked": 0, "supporting": [], "contradicting": [],
                        "strong_correlations": [], "missing_signals": [],
                    }

            ctx.stm.append("findings", {
                "type": "cross_dataset",
                "data": cross_dataset_result,
            })
            logger.info(
                "investigate | cross-dataset analysis | supporting=%d | contradicting=%d | "
                "strong_correlations=%d | incident_id=%s",
                len(cross_dataset_result.get("supporting", [])),
                len(cross_dataset_result.get("contradicting", [])),
                len(cross_dataset_result.get("strong_correlations", [])),
                cross_incident_id,
            )

        # --- Business Graph (Phase F4) ---
        # Same idempotency pattern as every advisory source above -- runs
        # once per incident lifecycle. Traverses the PERSISTED business
        # graph outward from this incident's metric, within a hard budget
        # (depth/nodes/edges), and reports what the platform already knows
        # relates to it: metrics correlated in past investigations,
        # policies that govern it, its datasets and their source systems,
        # and prior incidents that cited it.
        #
        # Three properties this stage deliberately has:
        #   - It READS ONLY. An investigation never writes to the graph;
        #     edges reach it solely through an explicit, privileged build
        #     that re-reads findings already persisted. So the graph can
        #     never grow from its own output.
        #   - It never re-derives a measurement. Every correlation it
        #     reports was measured by C4 at some earlier investigation and
        #     is cited back with the incidents that produced it.
        #   - It is advisory. Like memory/policy/cross_dataset/adaptive, it
        #     is appended as its own findings entry and never fed into
        #     RuleEngine/DecisionEngine/ActionAgent (AGENT-5).
        if self._business_graph is not None and not self._has_graph_finding(ctx):
            with investigation_span("evidence.business_graph", incident_id=ctx.incident_id):
                try:
                    graph_result = self._business_graph.analyze(metric=ctx.event.metric)
                except Exception as exc:  # noqa: BLE001
                    logger.error(
                        "investigate | business graph traversal failed | incident_id=%s | error=%s",
                        ctx.incident_id, exc,
                    )
                    graph_result = {
                        "available": False,
                        "reason": f"Business graph traversal failed: {exc}",
                        "origin_key": None, "origin_label": None, "budget": None,
                        "truncated": False, "truncation_reason": None,
                        "depth_reached": 0, "nodes_visited": 0, "edges_traversed": 0,
                        "correlated_metrics": [], "governing_policies": [],
                        "related_datasets": [], "related_services": [],
                        "prior_incidents": [], "related_total": 0,
                    }

            ctx.stm.append("findings", {
                "type": "graph",
                "data": graph_result,
            })
            logger.info(
                "investigate | business graph | available=%s | related=%d | "
                "depth_reached=%d | truncated=%s | incident_id=%s",
                graph_result.get("available"), graph_result.get("related_total", 0),
                graph_result.get("depth_reached", 0), graph_result.get("truncated"),
                ctx.stm.get("incident_id", "unknown"),
            )

        # --- Adaptive Detection Engine (Phase C5) ---
        # Same idempotency pattern as Enterprise Memory / Policy Registry /
        # Cross-Dataset Intelligence above -- runs once per incident
        # lifecycle. Computes a longer-horizon, seasonality-aware adaptive
        # baseline via AdaptiveDetectionEngine, which reuses
        # StatisticalDetector/LongTermMemory unmodified and only READS the
        # event's own already-computed metadata["statistical"]/["forecast"]
        # -- it never re-invokes ForecastAgent or MonitorAgent's own
        # detector, never calls RuleEngine.evaluate(), and never feeds back
        # into DecisionEngine/ActionAgent. Advisory only.
        if self._adaptive_detection is not None and not self._has_adaptive_finding(ctx):
            adaptive_incident_id = ctx.stm.get("incident_id", "unknown")
            with investigation_span("evidence.adaptive_detection", incident_id=ctx.incident_id):
                try:
                    adaptive_result = self._adaptive_detection.analyze(
                        metric=ctx.event.metric,
                        current_value=ctx.event.current_value,
                        event_metadata=ctx.event.metadata,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.error(
                        "investigate | adaptive detection failed | incident_id=%s | error=%s",
                        ctx.incident_id, exc,
                    )
                    adaptive_result = {
                        "history_points_used": 0,
                        "adaptive_baseline": None,
                        "adaptive_baseline_insufficient": f"Adaptive analysis failed: {exc}",
                        "seasonality": None,
                        "seasonality_insufficient": f"Adaptive analysis failed: {exc}",
                        "existing_statistical": None,
                        "existing_forecast": None,
                        "combined_signal": False,
                        "corroborating_signals": [],
                    }

            ctx.stm.append("findings", {
                "type": "adaptive",
                "data": adaptive_result,
            })
            logger.info(
                "investigate | adaptive detection | history_points=%d | combined_signal=%s | "
                "corroborating=%s | incident_id=%s",
                adaptive_result.get("history_points_used", 0),
                adaptive_result.get("combined_signal"),
                adaptive_result.get("corroborating_signals"),
                adaptive_incident_id,
            )

        # --- RAG integration (Phase 4) ---
        if "RAG" in agents and self._rag is not None:
            logger.info("investigate | invoking RAG agent | incident_id=%s", ctx.incident_id)

            t = start_timer()
            with investigation_span("evidence.rag", incident_id=ctx.incident_id):
                rag_result = self._rag.investigate(
                    event=ctx.event,
                    memory=ctx.stm,
                )
            end_timer(agent_execution_time.labels(agent="rag"), t)

            # RAG Agent actual contract (rag_agent.py:278-282):
            #   findings: dict with possible_causes/overall_confidence/etc.
            #   confidence: float
            #   memory_updates: dict with rag_findings/hypotheses/confidence
            rag_findings = rag_result.get("findings", "")
            rag_confidence = float(rag_result.get("confidence", 0.0) or 0.0)
            memory_updates = rag_result.get("memory_updates", {}) or {}
            no_knowledge = bool(rag_result.get("no_knowledge_retrieved", False))

            # Derive root_cause from the actual contract structure.
            # RAGAgent returns findings as a dict with possible_causes.
            #
            # Root-cause quality gate: the highest-CONFIDENCE cause is not
            # always the most descriptive one (e.g. a chunk-boundary artifact
            # can produce a high-confidence but content-free single word).
            # Walk causes in confidence order and take the first one that
            # actually passes is_meaningful_root_cause() — defense-in-depth
            # alongside the corpus-level chunking fix.
            rag_root_cause = ""
            if isinstance(rag_findings, dict):
                possible_causes = rag_findings.get("possible_causes", []) or []
                if possible_causes:
                    sorted_causes = sorted(
                        possible_causes,
                        key=lambda c: float(c.get("confidence", 0.0) or 0.0),
                        reverse=True,
                    )
                    chosen = best_meaningful_cause(sorted_causes)
                    if chosen is not None:
                        rag_root_cause = str(chosen.get("cause", "")).strip()

            # Record the RAG pass in the findings log (audit trail) —
            # unconditionally, on every pass whether it succeeded or failed,
            # so query attempts, validation outcome, and evidence are always
            # available to the dashboard/evidence/timeline instead of
            # silently disappearing whenever RAG fails to find a root cause.
            ctx.stm.append("findings", {
                "type": "rag",
                "depth": depth,
                "confidence": rag_confidence,
                "root_cause": rag_root_cause,
                "data": rag_findings,
            })
            # Persist the full RAG result for this pass unconditionally
            # (previously only set on success, which is why a failed pass
            # left the dashboard with nothing to show).
            ctx.stm.set("llm_response", json.dumps(rag_result, default=str))

            # Promote the grounded root cause into STM so finalize_incident()
            # persists it. The KPI Agent pass below writes a root cause only
            # when none is set, so this grounded, chunk-cited cause wins.
            if rag_root_cause and not no_knowledge:
                ctx.stm.set("root_cause", rag_root_cause)
                ctx.stm.set("root_cause_source", "rag")
                existing_conf = float(ctx.stm.get("confidence") or 0.0)
                ctx.stm.set(
                    "confidence",
                    round(max(existing_conf, rag_confidence), 2),
                )

            # Map possible_causes into STM evidence with full grounding metadata.
            if isinstance(rag_findings, dict):
                for h in memory_updates.get("hypotheses", []):
                    ctx.stm.append("hypotheses", h)
                for cause in rag_findings.get("possible_causes", []):
                    ctx.stm.append("evidence", {
                        "source": "rag",
                        "chunk_id": cause.get("chunk_id"),
                        "cause": cause.get("cause"),
                        "confidence": cause.get("confidence"),
                    })

            # Escalation signal from the actual contract.
            requires_human = rag_findings.get("requires_human_review") if isinstance(rag_findings, dict) else None
            if requires_human is True:
                ctx.stm.set("requires_human", True)

            # Phase E11 (OBS-2): attribute this pass's retrieval VOLUME to the
            # incident's cost scope — the third cost axis alongside LLM spend
            # and action executions. Counts what retrieval actually returned;
            # never an estimate.
            retrieved_this_pass = 0
            if isinstance(rag_findings, dict):
                raw_count = rag_findings.get("retrieved_count")
                if isinstance(raw_count, int):
                    retrieved_this_pass = raw_count
                else:
                    retrieved_this_pass = len(rag_findings.get("possible_causes", []) or [])
            incident_cost_scope.record_retrieval(retrieved_this_pass)

        # Phase F1: the real KPI Agent pass. Runs on every depth, exactly
        # where the placeholder ran, so the EvaluationEngine keeps scoring
        # against a populated STM — the difference is that what it scores
        # is now measured rather than simulated.
        self._run_kpi_investigation(ctx)

        # ---------- Force LLM reasoning at depth >= 3 ----------
        if depth >= 3 and self._settings.LLM_ENABLED:
            logger.info(
                "investigate | triggering LLM reasoning at depth %d | incident_id=%s",
                depth, ctx.incident_id,
            )
            try:
                llm = LLMService(settings=self._settings)

                # Build a structured prompt that insists on pure JSON
                prompt = (
                    f"You are an expert business analyst. Based on the following incident details, "
                    f"provide a concise root cause analysis and recommended actions.\n\n"
                    f"Incident:\n"
                    f"- Metric: {ctx.event.metric}\n"
                    f"- Current value: {ctx.event.current_value}\n"
                    f"- Expected value: {ctx.event.expected_value}\n"
                    f"- Severity: {ctx.event.severity}\n"
                    f"- Detection methods: {', '.join(ctx.event.detection_methods)}\n\n"
                    f"Short‑Term Memory findings:\n{ctx.stm.serialize_for_llm()}\n\n"
                    f"Return ONLY a valid JSON object, no other text, with these keys:\n"
                    f'{{"root_cause": "...", "confidence": 0.0, "recommended_action": "..."}}'
                )
                raw = llm.query(prompt, temperature=0.2, max_tokens=500)

                # Resilient parse (markdown fences, leading/trailing prose,
                # minor formatting issues) — same parser RAG uses, so a
                # recoverable formatting slip succeeds on attempt 1 instead
                # of silently relying on a later investigation depth to
                # paper over it.
                insight = parse_llm_json(raw)

                if insight is None:
                    logger.warning(
                        "investigate | LLM reasoning response could not be "
                        "parsed as JSON | depth=%d", depth,
                    )
                    # Structured, visible failure record — never fabricate a
                    # root cause from unparseable output. The grounded
                    # characterisation already set by
                    # _run_kpi_investigation() is left intact.
                    ctx.stm.append("findings", {
                        "type": "llm_reasoning_error",
                        "depth": depth,
                        "reason": "LLM response could not be parsed as JSON.",
                        "raw_response": raw,
                    })
                else:
                    # Update STM with the LLM output
                    ctx.stm.set("root_cause", insight.get("root_cause", "Unknown"))
                    ctx.stm.set("root_cause_source", "llm_reasoning")
                    ctx.stm.set("confidence", insight.get("confidence", 0.0))
                    ctx.stm.set("llm_response", raw)

                    logger.info("investigate | LLM reasoning stored successfully")
            except Exception as exc:
                logger.warning("investigate | LLM reasoning failed: %s", exc)
                # Keep the KPI Agent's grounded characterisation (already set in _run_kpi_investigation)

        self._evaluate(ctx)

    def _evaluate(self, ctx: IncidentContext) -> None:
        """
        Evaluate investigation progress and route to the next lifecycle step.

        Calls :class:`~aeam.agents.orchestrator.evaluation_engine.EvaluationEngine`
        with the current STM snapshot and routes based on the result:

        - ``"CONTINUE"``  → call :meth:`investigate` (recurse).
        - ``"STOP"``      → (optionally execute actions) then call
          :meth:`finalize_incident`.
        - ``"ESCALATE"``  → mark ``requires_human`` in STM, call
          :meth:`finalize_incident`.
        """
        eval_result = self._evaluation.evaluate(memory=ctx.stm)
        eval_decision: str = eval_result.get("decision", "CONTINUE")
        score: float = float(eval_result.get("score", 0.0))
        reasons: list[str] = eval_result.get("reasons", [])

        logger.info(
            "evaluate | decision=%s | score=%.2f | reasons=%s",
            eval_decision, score, reasons,
        )

        ctx.stm.append("findings", {
            "type": "evaluation",
            "decision": eval_decision,
            "score": score,
            "reasons": reasons,
        })

        if eval_decision == "CONTINUE":
            self._investigate(ctx)

        elif eval_decision == "STOP":
            # All external notifications/actions are dispatched exactly once,
            # from finalize_incident() (see notifications.py for the
            # structured Slack/Jira formatters). Sending an alert here too
            # would both duplicate that notification and dump raw serialized
            # findings as message text instead of a structured summary.
            self._finalize_incident(ctx)

        elif eval_decision == "ESCALATE":
            logger.warning(
                "evaluate | ESCALATE triggered | marking requires_human"
            )
            ctx.stm.set("requires_human", True)
            ctx.stm.append("findings", {
                "type": "escalation",
                "reason": "Max investigation depth reached without resolution.",
            })
            self._finalize_incident(ctx)

        else:
            logger.warning(
                "evaluate | unknown eval decision %r | defaulting to finalize",
                eval_decision,
            )
            self._finalize_incident(ctx)

    def _finalize_incident(self, ctx: IncidentContext) -> None:
        """
        Close the incident, execute its safe runbook, notify, persist, and clean up.

        Steps:
        1. Transition FSM to ``COMPLETE``.
        2. Derive the canonical investigation status (see
           :mod:`~aeam.agents.orchestrator.investigation_status`), the
           validation outcome of the latest RAG pass, and the event's safe
           runbook (see :mod:`~aeam.agents.orchestrator.runbooks`).
        3. Synthesize the Enterprise Action Planning Engine's execution plan
           (Phase C7) from every already-computed finding -- appended as its
           own advisory findings entry, strictly before the action plan runs.
        4. Explain the Enterprise Action Planning Engine's execution plan
           (Phase D1) -- decision graph, evidence graph, confidence
           breakdown, contradictions, missing evidence, and assumptions --
           appended as its own advisory findings entry. Never changes a
           recommendation; only explains it.
        5. Score the investigation's own QUALITY via the Enterprise AI
           Evaluation & Quality Engine (Phase D2) -- evidence coverage,
           retrieval/memory/policy/cross-dataset/adaptive coverage,
           conflict severity, diversity, recommendation quality, and
           completeness -- appended as its own advisory findings entry.
           Never changes findings, the execution plan, or explainability;
           only evaluates them.
        6. Execute the runbook's action plan via ActionAgent — SUBJECT TO
           the human-approval gate (Phase E9). When the execution plan set
           ``human_approval_required`` and enforcement is on, gated steps
           are recorded as pending instead of executed and released later
           by an authorized approval; notification steps are never gated.
           Only actions that actually return ``SUCCESS`` are recorded as
           executed; every withheld, skipped, or failed action is recorded
           with its reason — this method never claims an action ran unless
           ActionAgent confirmed it.
        7. Send Slack/Jira notifications built from explicit named fields
           (see :mod:`~aeam.agents.orchestrator.notifications`) — never a
           raw JSON dump of findings or the LLM response.
        8. Always attempt an email report last (independent of the runbook;
           a missing-credentials failure is expected and already reported
           with a structured reason by EmailActions).
        9. Append one consolidated ``audit_summary`` findings entry — the
           single source of truth the frontend reads for status, evidence,
           and executed actions.
        10. Persist via :meth:`~aeam.memory.long_term.LongTermMemory.record_incident`
           (schema unchanged — everything new lives inside the existing
           ``findings`` JSON column).
        11. Clear STM and reset ``_active_event``.
        """
        if ctx.state_machine.get_state() == IncidentState.COMPLETE:
            logger.info("finalize_incident | already COMPLETE; skipping duplicate finalize")
            return

        logger.info("finalize_incident | transitioning to COMPLETE")
        ctx.state_machine.transition(IncidentState.COMPLETE)

        # Metrics: one active investigation ended. The COMPLETE-state guard
        # above already prevents finalize_incident() from running twice for
        # the same incident, so this dec()/observe() pair can never double-count.
        active_incidents.dec()
        # Phase E11 (OBS-2 / COMPAT-1): end_timer already returns the measured
        # elapsed seconds — capture it so the SAME measurement that feeds the
        # Prometheus histogram is also persisted per-incident into
        # audit_summary below. This closes D3's honestly-disclosed duration
        # gap the honest way (measured at finalize), rather than merging in a
        # differently-shaped, process-lifetime Prometheus aggregate.
        investigation_duration_seconds: float | None = None
        if ctx.started_at is not None:
            investigation_duration_seconds = round(
                end_timer(investigation_duration, ctx.started_at), 4
            )
            ctx.started_at = None

        event_data: dict[str, Any] = ctx.stm.get("event") or {}
        incident_id: str = ctx.stm.get("incident_id", "unknown")
        root_cause = ctx.stm.get("root_cause")
        # Provenance written by whichever component set root_cause (Phase E1,
        # ENG-5): "rag" | "llm_reasoning" | "kpi_analysis" | None.
        # "placeholder" survives only on incidents persisted before F1.
        root_cause_source = ctx.stm.get("root_cause_source")
        requires_human = bool(ctx.stm.get("requires_human"))
        confidence = ctx.stm.get("confidence")

        # --- Phase F2: confidence recalibration (EXPL-4, PHIL-1) ---
        # A disclosed, reversible post-processing step. The raw value is
        # never discarded -- it is carried in the disclosure below and
        # surfaced by the console alongside the calibrated one, because a
        # number that silently moved is worse than an uncalibrated one:
        # nobody can tell it moved.
        #
        # `calibration_disclosure` always exists and always states whether
        # the adjustment was applied and, if not, why (EXPL-3). With
        # LEARNING_CALIBRATION_ENABLED off -- the default -- `confidence`
        # below is byte-identical to what F1 produced.
        confidence_raw = confidence
        calibration_disclosure: dict[str, Any] = {
            "applied": False,
            "reason": "Confidence recalibration is disabled for this deployment.",
        }
        if self._learning_agent is not None:
            try:
                confidence, calibration_disclosure = calibrate_confidence(
                    confidence, self._learning_agent.active_calibration()
                )
            except Exception as exc:  # noqa: BLE001
                # Declared never-raise boundary: a calibration failure must
                # never cost the platform an incident record. Raw
                # confidence stands and the failure is stated.
                logger.error(
                    "finalize_incident | calibration failed, reporting raw confidence | "
                    "incident_id=%s | error=%s",
                    ctx.incident_id, exc,
                )
                confidence = confidence_raw
                calibration_disclosure = {
                    "applied": False,
                    "reason": f"Calibration failed; raw confidence reported unchanged: {exc}",
                    "confidence_raw": confidence_raw,
                }

        # --- Derive validation status + latest RAG snapshot for reporting. ---
        latest_rag = self._latest_rag_finding(ctx)
        query_attempts = self._collect_query_attempts(ctx)

        if latest_rag is None:
            validation_status = "SKIPPED"
            validation_reason = "RAG was not invoked for this investigation."
        elif latest_rag.get("validation_passed") is True:
            validation_status = "PASSED"
            validation_reason = "Grounded response validated successfully."
        else:
            validation_status = "FAILED"
            validation_reason = latest_rag.get("error") or "Validation failed."

        had_error = bool(latest_rag and latest_rag.get("error") and not root_cause)
        investigation_status = derive_investigation_status(
            root_cause=root_cause,
            requires_human=requires_human,
            had_error=had_error,
        )

        possible_causes = (latest_rag or {}).get("possible_causes", []) or []
        evidence_count = (latest_rag or {}).get("retrieved_count", 0) or 0
        top_confidence = max(
            (float(c.get("confidence", 0.0) or 0.0) for c in possible_causes),
            default=None,
        )
        chunk_ids = sorted({
            c.get("chunk_id") for c in possible_causes if c.get("chunk_id")
        })

        runbook = get_runbook(event_data.get("event_type", ""))

        # --- Enterprise Action Planning Engine (Phase C7) ---
        # Synthesizes every already-computed finding (memory/policy/
        # cross_dataset/adaptive/rag) into ONE explainable execution plan.
        # Runs exactly once (guarded both by finalize_incident()'s own
        # COMPLETE-state check above and this defensive idempotency guard),
        # strictly AFTER every evidence-producing stage in investigate() has
        # already run, and BEFORE the runbook action-execution loop below --
        # so it is genuinely "the final reasoning stage before Human Review
        # and ActionAgent" the mission describes. Performs NO retrieval, NO
        # detection, and calls ActionAgent/RuleEngine/DecisionEngine
        # nothing -- it only reads ctx.stm.get("findings") and the
        # already-resolved runbook, and appends its own advisory findings
        # entry. ActionAgent.execute() and the action_plan loop below are
        # completely unchanged by this stage.
        if self._execution_planner is not None and not self._has_execution_plan_finding(ctx):
            with investigation_span("planning", incident_id=ctx.incident_id):
                try:
                    execution_plan = self._execution_planner.plan(
                        event_type=event_data.get("event_type", ""),
                        metric=event_data.get("metric", ""),
                        severity=event_data.get("severity", ""),
                        current_value=event_data.get("current_value"),
                        expected_value=event_data.get("expected_value"),
                        findings=ctx.stm.get("findings", []) or [],
                        root_cause=root_cause,
                        confidence=confidence,
                        requires_human=requires_human,
                        runbook_recommended_actions=runbook["recommended_actions"],
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.error(
                        "finalize_incident | execution planning failed | incident_id=%s | error=%s",
                        incident_id, exc,
                    )
                    execution_plan = {
                        "executive_summary": f"Execution planning failed: {exc}",
                        "recommended_actions": [], "order_rationale": None,
                        "supporting_evidence": [], "business_risk_assessment": None,
                        "expected_impact": None, "confidence": 0.0,
                        "evidence_quality": "insufficient", "evidence_conflicts": [],
                        "human_approval_required": True, "explanation": f"Execution planning failed: {exc}",
                        "insufficient_evidence": True,
                        "sources_consulted": {}, "sources_with_signal": {},
                    }

            ctx.stm.append("findings", {
                "type": "execution_plan",
                "data": execution_plan,
            })
            logger.info(
                "finalize_incident | execution plan | actions=%d | confidence=%s | "
                "evidence_quality=%s | conflicts=%d | incident_id=%s",
                len(execution_plan.get("recommended_actions", [])),
                execution_plan.get("confidence"), execution_plan.get("evidence_quality"),
                len(execution_plan.get("evidence_conflicts", [])), incident_id,
            )

        # --- Enterprise Explainability Engine (Phase D1) ---
        # Explains WHY the execution plan above reached its recommendations
        # -- never recomputes them. Runs exactly once (guarded both by
        # finalize_incident()'s own COMPLETE-state check and this defensive
        # idempotency guard), strictly AFTER the execution_plan finding above
        # was appended (it explains THAT finding -- read back from findings,
        # never a possibly-stale local variable), and BEFORE the runbook
        # action-execution loop below. Performs NO retrieval, NO detection,
        # and calls no other engine -- it only reads ctx.stm.get("findings")
        # (which now includes the execution_plan entry) and appends its own
        # advisory findings entry. Never alters recommended_actions/root_cause/
        # confidence -- purely explanatory.
        if self._explainability_engine is not None and not self._has_explainability_finding(ctx):
            execution_plan_data: dict[str, Any] = {}
            for entry in ctx.stm.get("findings", []) or []:
                if isinstance(entry, dict) and entry.get("type") == "execution_plan":
                    execution_plan_data = entry.get("data") or {}

            try:
                explainability_result = self._explainability_engine.explain(
                    findings=ctx.stm.get("findings", []) or [],
                    execution_plan=execution_plan_data,
                    raw_confidence=confidence,
                )
            except Exception as exc:  # noqa: BLE001
                logger.error("finalize_incident | explainability failed: %s", exc)
                explainability_result = {
                    "decision_graph": [], "evidence_graph": {}, "recommendation_trace": [],
                    "confidence_breakdown": {}, "evidence_contribution": [], "contradictions": [],
                    "missing_evidence": [], "assumptions": [], "evidence_quality": "insufficient",
                    "lower_priority_justification": {}, "insufficient_evidence": True,
                }

            ctx.stm.append("findings", {
                "type": "explainability",
                "data": explainability_result,
            })
            logger.info(
                "finalize_incident | explainability | decision_graph=%d | contradictions=%d | "
                "missing_evidence=%d | incident_id=%s",
                len(explainability_result.get("decision_graph", [])),
                len(explainability_result.get("contradictions", [])),
                len(explainability_result.get("missing_evidence", [])), incident_id,
            )

        # --- Enterprise AI Evaluation & Quality Engine (Phase D2) ---
        # Scores the QUALITY of this investigation -- never changes
        # findings, the execution plan, or the explainability object; it
        # only reads them (execution_plan and explainability read back from
        # findings, never a possibly-stale local variable). Runs exactly
        # once (guarded both by finalize_incident()'s own COMPLETE-state
        # check and this defensive idempotency guard), strictly AFTER
        # explainability above and BEFORE the runbook action-execution loop.
        # Performs NO retrieval, NO detection, and calls no other engine.
        if self._ai_evaluator is not None and not self._has_ai_evaluation_finding(ctx):
            execution_plan_data_for_eval: dict[str, Any] = {}
            explainability_data_for_eval: dict[str, Any] | None = None
            for entry in ctx.stm.get("findings", []) or []:
                if isinstance(entry, dict) and entry.get("type") == "execution_plan":
                    execution_plan_data_for_eval = entry.get("data") or {}
                elif isinstance(entry, dict) and entry.get("type") == "explainability":
                    explainability_data_for_eval = entry.get("data") or {}

            try:
                ai_evaluation_result = self._ai_evaluator.assess(
                    findings=ctx.stm.get("findings", []) or [],
                    execution_plan=execution_plan_data_for_eval,
                    explainability=explainability_data_for_eval,
                    root_cause=root_cause,
                    confidence=confidence,
                )
            except Exception as exc:  # noqa: BLE001
                logger.error("finalize_incident | AI evaluation failed: %s", exc)
                ai_evaluation_result = {
                    "overall_score": None, "overall_score_formula": f"AI evaluation failed: {exc}",
                    "component_scores": {}, "strengths": [], "weaknesses": [],
                    "missing_evidence": [], "improvement_opportunities": [],
                    "quality_summary": f"AI evaluation failed: {exc}",
                }

            ctx.stm.append("findings", {
                "type": "ai_evaluation",
                "data": ai_evaluation_result,
            })
            logger.info(
                "finalize_incident | AI evaluation | overall_score=%s | strengths=%d | "
                "weaknesses=%d | incident_id=%s",
                ai_evaluation_result.get("overall_score"),
                len(ai_evaluation_result.get("strengths", [])),
                len(ai_evaluation_result.get("weaknesses", [])), incident_id,
            )

        # --- Human-in-the-Loop gate (Phase E9, AGENT-5) ---
        # C7's human_approval_required has been computed since Phase C7 and
        # enforced by nothing. Here it becomes a real gate: when the plan
        # requires approval AND a HumanReviewService is wired AND enforcement
        # is on, gated runbook steps are RECORDED as pending instead of
        # executed, and only an authorized approval through the review API
        # releases them. All three conditions must hold — an unwired service
        # or HUMAN_APPROVAL_ENFORCED=false leaves this method behaving
        # exactly as it did before E9 (COMPAT-1), which is the documented
        # rollback posture.
        gate = self._resolve_approval_gate(ctx, event_data)
        pending_actions: list[dict[str, Any]] = []

        # --- Execute the safe runbook action plan. ---
        executed_actions: list[str] = []
        skipped_actions: list[dict[str, str]] = []

        def _run_step(step: str, params: dict[str, Any]) -> None:
            if self._action is None:
                skipped_actions.append({
                    "action": step, "reason": "ActionAgent not available.",
                })
                return
            registry_type, extra_params = resolve_action_step(step)
            merged_params = {**params, **extra_params}
            t = start_timer()
            try:
                with investigation_span(
                    "action", incident_id=ctx.incident_id, action_type=registry_type, step=step,
                ):
                    result = self._action.execute(
                        action_type=registry_type,
                        parameters=merged_params,
                        incident_id=incident_id,
                    )
            except Exception as exc:  # noqa: BLE001
                end_timer(agent_execution_time.labels(agent=f"action:{registry_type}"), t)
                action_failure_total.labels(action_type=registry_type).inc()
                skipped_actions.append({"action": step, "reason": str(exc)})
                logger.error(
                    "finalize_incident | action %s raised | incident_id=%s | error=%s",
                    step, incident_id, exc,
                )
                return

            end_timer(agent_execution_time.labels(agent=f"action:{registry_type}"), t)

            if result.get("status") == "SUCCESS":
                action_success_total.labels(action_type=registry_type).inc()
                executed_actions.append(step)
                logger.debug(
                    "finalize_incident | action %s SUCCESS | incident_id=%s",
                    step, incident_id,
                )
            else:
                action_failure_total.labels(action_type=registry_type).inc()
                result_detail: dict[str, Any] = result.get("result") or {}
                reason = (
                    result.get("failure_reason")
                    or result_detail.get("reason")
                    or result_detail.get("error")
                    or result.get("status", "unknown")
                )
                skipped_actions.append({"action": step, "reason": str(reason)})
                logger.debug(
                    "finalize_incident | action %s SKIPPED | reason=%s | incident_id=%s",
                    step, reason, incident_id,
                )

        priority_map = {
            "HIGH": "High", "CRITICAL": "Highest", "MEDIUM": "Medium", "LOW": "Low",
        }
        event_severity = str(event_data.get("severity") or "").upper()
        is_business_event = event_data.get("event_type") in ("SALES_DROP", "SALES_SPIKE")

        # Non-notification steps first, so Slack/Jira can honestly report
        # what already ran before they themselves send.
        for step in runbook["action_plan"]:
            if step in ("jira", "slack", "marketing_slack"):
                continue
            params: dict[str, Any] = {"incident_id": incident_id}
            if step == "diagnostics":
                params.update({
                    "kind": "analytics_snapshot" if is_business_event else "diagnostics",
                    "metric": event_data.get("metric"),
                    "current_value": event_data.get("current_value"),
                    "expected_value": event_data.get("expected_value"),
                    "root_cause": root_cause,
                })
            elif step == "monitoring":
                params.update({
                    "metric": event_data.get("metric"),
                    "window_minutes": 120,
                    "reason": f"Elevated monitoring after {event_severity} incident.",
                })

            # Phase E9: withhold, don't execute. The fully-built params are
            # recorded verbatim so an approval later runs exactly this call —
            # never a re-derived or re-planned one.
            if gate["active"] and is_gated_step(step):
                pending_actions.append({"step": step, "params": params})
                skipped_actions.append({
                    "action": step,
                    "reason": (
                        f"Withheld pending human approval "
                        f"({' → '.join(gate['required_tiers'])})."
                    ),
                })
                logger.info(
                    "finalize_incident | action %s WITHHELD pending approval | "
                    "incident_id=%s", step, incident_id,
                )
                continue

            _run_step(step, params)

        # Notification steps — built from explicit named fields only.
        notify_payload: dict[str, Any] = {
            "incident_id": incident_id,
            "metric": event_data.get("metric", "unknown"),
            "severity": event_data.get("severity", "unknown"),
            "current_value": event_data.get("current_value"),
            "expected_value": event_data.get("expected_value"),
            "investigation_status": investigation_status,
            "root_cause": root_cause,
            "confidence": confidence,
            "evidence_count": evidence_count,
            "recommended_actions": runbook["recommended_actions"],
            "requires_human": requires_human,
        }

        for step in runbook["action_plan"]:
            if step not in ("jira", "slack", "marketing_slack"):
                continue
            notify_payload["executed_actions"] = list(executed_actions)
            if step == "jira":
                _run_step("jira", {
                    "summary": (
                        f"Incident {incident_id}: "
                        f"{event_data.get('metric', 'unknown')} anomaly"
                    ),
                    "description": format_jira_description({
                        **notify_payload,
                        "retrieval_completed": latest_rag is not None,
                        "validation_status": validation_status,
                        "top_confidence": top_confidence,
                        "chunk_ids": chunk_ids,
                        "llm_reasoning": (latest_rag or {}).get("raw_llm_response"),
                    }),
                    "priority": priority_map.get(event_severity, "Medium"),
                })
            else:  # "slack" or "marketing_slack" alias
                _run_step(step, {
                    "message": format_slack_message(notify_payload),
                    "severity": event_data.get("severity", "MEDIUM"),
                })

        # Always attempt the email report last, independent of the runbook —
        # a missing-credentials failure is expected (see CLAUDE.md) and
        # already reported with a structured reason by EmailActions itself.
        if self._report is not None:
            t = start_timer()
            try:
                report = self._report.generate_report(memory=ctx.stm)
                end_timer(agent_execution_time.labels(agent="report"), t)
            except Exception as exc:  # noqa: BLE001
                end_timer(agent_execution_time.labels(agent="report"), t)
                report = {"detailed_report": f"Report generation failed: {exc}"}
        else:
            report = {"detailed_report": "ReportAgent not available."}

        _run_step("email", {
            "to": ["ops@company.com"],
            "subject": f"AEAM Incident - {event_data.get('event_type', 'unknown')}",
            "body": report.get("detailed_report", ""),
        })

        # --- Human-approval findings entry (Phase E9). Appended only when
        # the gate actually held something back, so an incident with no
        # approval requirement carries no such entry and renders exactly as
        # before (COMPAT-1). Stored inside the existing "findings" JSON
        # column, like every other engine's entry — no schema change here;
        # the durable approval record itself lands in incident_approvals
        # once the incident row exists (below).
        if gate["active"]:
            ctx.stm.append("findings", {
                "type": "human_approval",
                "data": {
                    "approval_id": gate["approval_id"],
                    "required": True,
                    "enforced": True,
                    "status": "pending",
                    "required_tiers": list(gate["required_tiers"]),
                    "current_tier": 0,
                    "chain_source": gate["chain_source"],
                    "pending_actions": [entry["step"] for entry in pending_actions],
                    "gate_reason": gate["reason"],
                },
            })
            logger.info(
                "finalize_incident | human approval gate ACTIVE | tiers=%s | "
                "withheld=%d | incident_id=%s",
                gate["required_tiers"], len(pending_actions), incident_id,
            )

        # --- Consolidated audit summary: the single source of truth the
        # frontend reads instead of reconstructing state from scattered
        # findings entries. Stored inside the existing "findings" JSON
        # column — no schema change.
        # Phase E11 (OBS-2 / EXPL-5): close the per-incident cost scope and
        # attribute this incident's action outcomes before snapshotting it.
        # An incident whose scope was never opened (a direct
        # _finalize_incident call in a unit test) simply yields no cost block
        # — absence stays honestly distinguishable from a measured zero.
        for _ in executed_actions:
            incident_cost_scope.record_action("executed")
        for _ in skipped_actions:
            incident_cost_scope.record_action("skipped")
        for _ in pending_actions:
            incident_cost_scope.record_action("withheld")
        cost_snapshot = incident_cost_scope.snapshot()
        incident_cost_scope.clear()

        audit_summary: dict[str, Any] = {
            "type": "audit_summary",
            "investigation_status": investigation_status,
            "root_cause": root_cause,
            # Additive (Phase E1, ENG-5/COMPAT-1): absent on incidents that
            # predate provenance tracking — readers must tolerate absence.
            "root_cause_source": root_cause_source,
            "validation_status": validation_status,
            "validation_reason": validation_reason,
            "reranking": "not_applicable",
            "escalation_reason": self._escalation_reason(ctx, investigation_status),
            "query_attempts": query_attempts,
            "evidence_count": evidence_count,
            "top_confidence": top_confidence,
            "chunk_ids": chunk_ids,
            "recommended_actions": runbook["recommended_actions"],
            "executed_actions": executed_actions,
            "skipped_actions": skipped_actions,
            # Phase F2 (EXPL-4/EXPL-5): the adjustment, its magnitude, its
            # reason, and the raw value it was applied to. Additive, so
            # every pre-F2 incident simply lacks the key and every reader
            # that ignores unknown keys is unaffected (COMPAT-1/COMPAT-4).
            "calibration": calibration_disclosure,
        }

        # --- Phase E11 additive fields (COMPAT-1: every existing reader
        # ignores an unknown key, and every incident recorded BEFORE this
        # phase simply lacks them — which is exactly what lets D3 report
        # "measured" for new incidents and an honest reason for old ones,
        # instead of silently backfilling a number it never measured).
        if investigation_duration_seconds is not None:
            audit_summary["investigation_duration_seconds"] = investigation_duration_seconds
        if cost_snapshot is not None:
            audit_summary["cost"] = {
                "llm_calls": cost_snapshot["llm_calls"],
                "llm_prompt_tokens": cost_snapshot["llm_prompt_tokens"],
                "llm_completion_tokens": cost_snapshot["llm_completion_tokens"],
                "llm_total_tokens": cost_snapshot["llm_total_tokens"],
                "llm_cost_usd": cost_snapshot["llm_cost_usd"],
                "retrieval_chunks": cost_snapshot["retrieval_chunks"],
                "actions_executed": cost_snapshot["actions_executed"],
                "actions_skipped": cost_snapshot["actions_skipped"],
                "actions_withheld": cost_snapshot["actions_withheld"],
                # The cost figure is operator-priced from real token counts
                # (Settings.LLM_COST_PER_1K_*), never an invoiced total —
                # stated inline so no reader has to infer the semantics.
                "cost_basis": (
                    "LLM spend estimated from provider-reported token counts at the "
                    "operator-configured LLM_COST_PER_1K_* rates; 0.0 when no rate is "
                    "configured. Action and retrieval counts are measured, not estimated."
                ),
            }
        trace_id = current_trace_id()
        if trace_id:
            audit_summary["trace_id"] = trace_id

        ctx.stm.append("findings", audit_summary)

        # --- Assemble persistence payload (schema unchanged). ---
        payload: dict[str, Any] = {
            "event_id":            event_data.get("event_id"),
            "event_type":          event_data.get("event_type"),
            "metric":              event_data.get("metric"),
            "severity":            event_data.get("severity"),
            "current_value":       event_data.get("current_value"),
            "expected_value":      event_data.get("expected_value"),
            "detection_methods":   event_data.get("detection_methods", []),
            "timestamp":           event_data.get("timestamp"),
            "investigation_depth": ctx.stm.get("investigation_depth"),
            "root_cause":          root_cause,
            "confidence":          confidence,
            # Reflects reality now: True only if >=1 action actually
            # succeeded (previously hardcoded False regardless of outcome).
            "action_taken":        bool(executed_actions),
            "requires_human":      requires_human,
            "findings":            ctx.stm.get("findings", []),
            "llm_response":        ctx.stm.get("llm_response", ""),
        }

        db_incident_id: str | None = None
        try:
            db_incident_id = self._ltm.record_incident(payload)
            logger.info("finalize_incident | persisted | incident_id=%s", db_incident_id)
        except Exception as exc:  # noqa: BLE001
            logger.error("finalize_incident | LTM persist failed: %s", exc)

        # --- Persist the approval requirement (Phase E9). Written AFTER the
        # incident row so it is keyed by the incident_id the review queue and
        # the console actually address (the incidents table generates its own
        # primary key on insert — it is not the Orchestrator's per-
        # investigation id, which is recorded alongside for traceability).
        # A failure here must never block finalization, exactly like the LTM
        # and Enterprise Memory writes around it — but it IS loud: the gate
        # already withheld the actions, so an unrecorded approval means those
        # actions are unreleasable until an operator intervenes.
        if gate["active"]:
            try:
                self._human_review.record_pending_approval(
                    approval_id=gate["approval_id"],
                    incident_id=db_incident_id or incident_id,
                    investigation_id=incident_id,
                    event_type=event_data.get("event_type"),
                    metric=event_data.get("metric"),
                    severity=event_data.get("severity"),
                    required_tiers=list(gate["required_tiers"]),
                    pending_actions=pending_actions,
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "finalize_incident | approval record FAILED — %d gated action(s) "
                    "are withheld with no approval record to release them | "
                    "incident_id=%s | error=%s",
                    len(pending_actions), db_incident_id or incident_id, exc,
                )

        # --- Enterprise Memory (Phase C1): turn this resolved incident into
        # reusable organizational knowledge for future investigations. Every
        # incident is remembered regardless of outcome -- a FAILED/ESCALATED
        # investigation is still genuinely useful memory (what didn't
        # resolve it), not just successes. Reuses the exact fields already
        # computed above; nothing here is invented. A memory-write failure
        # must never block incident finalization (same resilience contract
        # as the LTM persist above).
        #
        # ENG-5 quarantine (Phase E1): the ONE exception is a
        # placeholder-derived root cause — synthetic KPI-stub output is not
        # organizational knowledge, and remembering it would poison future
        # recalls with fabricated precedent. Skipped loudly, never silently.
        #
        # Phase F1 deleted the only code that ever produced this marker, so
        # no NEW incident can reach this branch. It is deliberately kept:
        # a re-investigation or replay of an incident persisted before F1
        # still carries "placeholder", and the quarantine must hold for it
        # exactly as it did then (COMPAT-1 — persisted incidents render,
        # and are governed, forever).
        if self._memory is not None and root_cause_source == "placeholder":
            logger.warning(
                "finalize_incident | Enterprise Memory write SKIPPED — root cause is "
                "placeholder-derived, not real analysis (ENG-5 quarantine) | incident_id=%s",
                incident_id,
            )
        elif self._memory is not None:
            try:
                self._memory.remember_incident({
                    "incident_id": incident_id,
                    "event_type": event_data.get("event_type"),
                    "metric": event_data.get("metric"),
                    "severity": event_data.get("severity"),
                    "timestamp": event_data.get("timestamp"),
                    "root_cause": root_cause,
                    "confidence": confidence,
                    "investigation_status": investigation_status,
                    "recommended_actions": runbook["recommended_actions"],
                    "executed_actions": executed_actions,
                    "chunk_ids": chunk_ids,
                })
            except Exception as exc:  # noqa: BLE001
                logger.error("finalize_incident | memory persist failed: %s", exc)

        logger.info(
            "finalize_incident | status=%s | root_cause=%r | executed=%s | skipped=%s",
            investigation_status, root_cause, executed_actions,
            [s["action"] for s in skipped_actions],
        )

        # Clean up: ctx itself is a stack-local and will be garbage-
        # collected as _finalize_incident returns. STM.clear() is a
        # cheap defense-in-depth: even if a bug held a reference to
        # ctx.stm past return, the sensitive incident state is gone.
        ctx.stm.clear()
        logger.info("finalize_incident | STM cleared | lifecycle complete")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_approval_gate(
        self, ctx: IncidentContext, event_data: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Decide whether this incident's gated runbook steps must wait for a
        human (Phase E9, AGENT-5).

        The gate is active only when ALL of the following hold:

        1. A :class:`~aeam.governance.human_review.HumanReviewService` is
           wired (a platform built without one has no gate to enforce).
        2. That service reports ``enforced`` — i.e.
           ``HUMAN_APPROVAL_ENFORCED`` is on. Off is the documented rollback
           posture and reproduces pre-E9 behaviour exactly.
        3. The Enterprise Action Planning Engine's own execution plan set
           ``human_approval_required=True``. This method never re-derives
           that judgement — it reads C7's already-computed flag from the
           findings entry, exactly as the explainability and evaluation
           stages read the plan they explain and score.

        Args:
            ctx:        The live incident context (findings are read from
                        its STM; nothing is written).
            event_data: The incident's event fields (severity drives the
                        configured chain override, if any).

        Returns:
            ``{"active", "required_tiers", "approval_id", "chain_source",
            "reason"}``. ``active`` is ``False`` for every incident that is
            not gated, in which case the other fields carry the honest
            reason why.
        """
        findings = ctx.stm.get("findings", []) or []

        execution_plan: dict[str, Any] = {}
        for entry in findings:
            if isinstance(entry, dict) and entry.get("type") == "execution_plan":
                execution_plan = entry.get("data") or {}

        inactive = {
            "active": False,
            "required_tiers": [],
            "approval_id": None,
            "chain_source": None,
        }

        if self._human_review is None:
            return {**inactive, "reason": "No human review service is wired."}
        if not self._human_review.enforced:
            return {
                **inactive,
                "reason": "Approval enforcement is disabled (HUMAN_APPROVAL_ENFORCED=false).",
            }
        if execution_plan.get("human_approval_required") is not True:
            return {
                **inactive,
                "reason": "The execution plan did not require human approval.",
            }

        try:
            required_tiers = self._human_review.resolve_chain(
                event_data.get("severity"), findings,
            )
        except Exception as exc:  # noqa: BLE001
            # A chain-resolution failure must never quietly release the
            # gate — fall back to the strictest coherent posture (one
            # approval still required) rather than executing ungated.
            logger.error(
                "finalize_incident | approval chain resolution failed, "
                "falling back to a single-tier chain: %s", exc,
            )
            required_tiers = ["reviewer"]

        return {
            "active": True,
            "required_tiers": required_tiers,
            "approval_id": str(uuid.uuid4()),
            "chain_source": (
                "policy" if any(
                    isinstance(e, dict) and e.get("type") == "policy"
                    and any(
                        isinstance(m, dict) and m.get("approval_required") and m.get("role")
                        for m in ((e.get("data") or {}).get("matches") or [])
                    )
                    for e in findings
                ) else "configuration"
            ),
            "reason": (
                "The execution plan requires human approval; gated runbook "
                "steps are withheld until the chain is satisfied."
            ),
        }

    def _escalation_reason(self, ctx: IncidentContext, investigation_status: str) -> str | None:
        """
        Return the ACTUAL reason this incident was escalated, or None if it
        was not escalated at all.

        requires_human (and therefore ESCALATED) can be set via two
        independent paths that must not be conflated:
        1. The EvaluationEngine hit MAX_INVESTIGATION_DEPTH without a
           sufficient score -- an explicit "type": "escalation" marker is
           appended to findings for this path (see evaluate()).
        2. The RAG agent's own LLM output set requires_human_review=True
           on a pass that otherwise found a root cause (e.g. borderline
           confidence) -- no depth-limit marker exists for this path.

        Returns:
            A specific, accurate reason string, or None if not escalated.
        """
        if investigation_status != "ESCALATED":
            return None

        findings = ctx.stm.get("findings", []) or []
        if any(isinstance(f, dict) and f.get("type") == "escalation" for f in findings):
            return "Max investigation depth reached without resolution."

        return (
            "Flagged for human review by the RAG investigation "
            "(borderline confidence or ambiguous grounded finding)."
        )

    def _latest_rag_finding(self, ctx: IncidentContext) -> dict[str, Any] | None:
        """
        Return the ``data`` dict of the most recent RAG pass recorded in
        STM findings for the current incident.

        Returns:
            The last ``type == "rag"`` finding's ``data`` dict, or ``None``
            if RAG has not been invoked at all this incident.
        """
        findings = ctx.stm.get("findings", []) or []
        latest: dict[str, Any] | None = None
        for entry in findings:
            if isinstance(entry, dict) and entry.get("type") == "rag":
                data = entry.get("data")
                if isinstance(data, dict):
                    latest = data
        return latest

    def _has_memory_finding(self, ctx: IncidentContext) -> bool:
        """
        True if Enterprise Memory recall has already run this incident
        lifecycle -- guards investigate() against repeating the same memory
        search on every investigation depth (mirrors RAGAgent's own
        adaptive-query exhaustion guard for the same reason: investigate()
        can be called multiple times per incident, and a memory search's
        result would not change between depths for an unchanged event).
        """
        findings = ctx.stm.get("findings", []) or []
        return any(
            isinstance(entry, dict) and entry.get("type") == "memory"
            for entry in findings
        )

    def _has_policy_finding(self, ctx: IncidentContext) -> bool:
        """
        True if the Enterprise Policy Registry match has already run this
        incident lifecycle -- same idempotency guard as
        _has_memory_finding(), for the same reason.
        """
        findings = ctx.stm.get("findings", []) or []
        return any(
            isinstance(entry, dict) and entry.get("type") == "policy"
            for entry in findings
        )

    def _has_cross_dataset_finding(self, ctx: IncidentContext) -> bool:
        """
        True if Cross-Dataset Intelligence has already run this incident
        lifecycle -- same idempotency guard as _has_memory_finding()/
        _has_policy_finding(), for the same reason.
        """
        findings = ctx.stm.get("findings", []) or []
        return any(
            isinstance(entry, dict) and entry.get("type") == "cross_dataset"
            for entry in findings
        )

    def _has_graph_finding(self, ctx: IncidentContext) -> bool:
        """
        True if the Business Graph has already run this incident lifecycle
        -- same idempotency guard as _has_memory_finding()/
        _has_policy_finding()/_has_cross_dataset_finding(), for the same
        reason.
        """
        findings = ctx.stm.get("findings", []) or []
        return any(
            isinstance(entry, dict) and entry.get("type") == "graph"
            for entry in findings
        )

    def _has_adaptive_finding(self, ctx: IncidentContext) -> bool:
        """
        True if the Adaptive Detection Engine has already run this incident
        lifecycle -- same idempotency guard as _has_memory_finding()/
        _has_policy_finding()/_has_cross_dataset_finding(), for the same
        reason.
        """
        findings = ctx.stm.get("findings", []) or []
        return any(
            isinstance(entry, dict) and entry.get("type") == "adaptive"
            for entry in findings
        )

    def _has_execution_plan_finding(self, ctx: IncidentContext) -> bool:
        """
        True if the Enterprise Action Planning Engine has already run this
        incident lifecycle -- defensive idempotency guard mirroring
        _has_adaptive_finding()/etc, even though finalize_incident() itself
        already guards against running more than once (COMPLETE-state check).
        """
        findings = ctx.stm.get("findings", []) or []
        return any(
            isinstance(entry, dict) and entry.get("type") == "execution_plan"
            for entry in findings
        )

    def _has_explainability_finding(self, ctx: IncidentContext) -> bool:
        """
        True if the Enterprise Explainability Engine has already run this
        incident lifecycle -- defensive idempotency guard mirroring
        _has_execution_plan_finding(), for the same reason.
        """
        findings = ctx.stm.get("findings", []) or []
        return any(
            isinstance(entry, dict) and entry.get("type") == "explainability"
            for entry in findings
        )

    def _has_ai_evaluation_finding(self, ctx: IncidentContext) -> bool:
        """
        True if the Enterprise AI Evaluation & Quality Engine has already run
        this incident lifecycle -- defensive idempotency guard mirroring
        _has_explainability_finding(), for the same reason.
        """
        findings = ctx.stm.get("findings", []) or []
        return any(
            isinstance(entry, dict) and entry.get("type") == "ai_evaluation"
            for entry in findings
        )

    def _collect_query_attempts(self, ctx: IncidentContext) -> list[dict[str, Any]]:
        """
        Reconstruct every distinct RAG query attempt made this incident, in
        order, from STM findings — used for the audit trail / Retrieval
        Summary shown in the Evidence panel.

        Returns:
            Ordered list of ``{"attempt", "strategy", "query",
            "retrieved_count", "threshold"}`` dicts, oldest first. The
            exhaustion marker (``query_strategy == "exhausted"``) is
            excluded since it carries no new query of its own.
        """
        findings = ctx.stm.get("findings", []) or []
        attempts: list[dict[str, Any]] = []
        for entry in findings:
            if not isinstance(entry, dict) or entry.get("type") != "rag":
                continue
            data = entry.get("data") or {}
            if not isinstance(data, dict) or data.get("query_strategy") == "exhausted":
                continue
            attempts.append({
                "attempt":         data.get("query_attempt"),
                "strategy":        data.get("query_strategy"),
                "query":           data.get("query"),
                "retrieved_count": data.get("retrieved_count", 0),
                "threshold":       data.get("threshold"),
            })
        return attempts

    def _run_kpi_investigation(self, ctx: IncidentContext) -> None:
        """
        Run the KPI Agent's investigation pass (Phase F1).

        Replaces ``_run_kpi_investigation_placeholder``, which stood here
        from Phase 3 until F1 emitting a synthetic ``"Simulated root
        cause"``. That method is **deleted**, not bypassed: PHIL-1 is
        satisfied by removal, and a placeholder left behind a flag would
        still be a placeholder that could reach an operator.

        What changes for readers of the record:

        * ``root_cause_source`` becomes ``"kpi_analysis"`` where it was
          ``"placeholder"``. The E1 quarantine on ``"placeholder"`` is left
          untouched in :meth:`finalize_incident` — it still guards every
          historical incident that carries the old marker (COMPAT-1), it
          simply has no new producer.
        * Evidence entries carry real measurements instead of a
          ``placeholder: True`` flag.
        * Confidence is derived from the measurement, not incremented by a
          fixed 0.3 per pass.

        The agent is advisory (AGENT-5): a grounded root cause is written
        to STM only when nothing better is already there, so RAG's
        chunk-cited cause and LLM reasoning both continue to win — a
        characterisation of *what* changed must never displace an
        explanation of *why*.
        """
        depth: int = ctx.stm.get("investigation_depth") or 1

        if self._kpi_agent is None:
            # KPI_AGENT_ENABLED=false. Recorded, not silent: an operator
            # reading the findings must be able to see that the KPI pass
            # did not run, rather than inferring it from an absence
            # (EXPL-3 — "not consulted" is its own state).
            ctx.stm.append("findings", {
                "type": "kpi_analysis",
                "depth": depth,
                "data": {
                    "not_consulted": (
                        "KPI Agent is disabled for this deployment "
                        "(KPI_AGENT_ENABLED=false)."
                    ),
                },
            })
            return

        with investigation_span("evidence.kpi_analysis", incident_id=ctx.incident_id):
            result = self._kpi_agent.analyze(
                metric=ctx.event.metric,
                current_value=ctx.event.current_value,
                expected_value=ctx.event.expected_value,
                event_metadata=ctx.event.metadata,
                depth=depth,
            )

        ctx.stm.append("findings", {
            "type": "kpi_analysis",
            "depth": depth,
            "confidence": result.get("confidence", 0.0),
            "root_cause": result.get("root_cause"),
            "data": result,
        })

        # Evidence carries the measured characterisation. Nothing synthetic
        # is written: every field below was computed from the event's own
        # detector output or from real history.
        ctx.stm.append("evidence", {
            "source": "kpi_analysis",
            "depth": depth,
            "metric": result.get("metric"),
            "current_value": result.get("current_value"),
            "expected_value": result.get("expected_value"),
            "baseline_source": result.get("baseline_source"),
            "history_points_used": result.get("history_points_used"),
            "deviation": result.get("deviation"),
            "persistence": result.get("persistence"),
            "trend": result.get("trend"),
            "detectors_fired": result.get("detectors_fired"),
            "insufficient_data": result.get("insufficient_data"),
        })

        if depth == 1 and result.get("root_cause"):
            ctx.stm.append("hypotheses", result["root_cause"])

        # Confidence is the maximum of what any pass has established, not a
        # running increment — a later, weaker pass must not talk a stronger
        # earlier finding down.
        existing_confidence = float(ctx.stm.get("confidence") or 0.0)
        agent_confidence = float(result.get("confidence") or 0.0)
        if agent_confidence > existing_confidence:
            ctx.stm.set("confidence", round(agent_confidence, 2))

        if result.get("root_cause") and not ctx.stm.get("root_cause"):
            ctx.stm.set("root_cause", result["root_cause"])
            ctx.stm.set("root_cause_source", "kpi_analysis")

        logger.info(
            "kpi_analysis | depth=%d | metric=%s | detectors=%s | history_points=%d | "
            "confidence=%.2f | grounded=%s | incident_id=%s",
            depth,
            ctx.event.metric,
            result.get("detectors_fired"),
            result.get("history_points_used", 0),
            agent_confidence,
            bool(result.get("root_cause")),
            ctx.incident_id,
        )

    def __repr__(self) -> str:
        # Phase E2, ARCH-8: no per-incident FSM state — incidents
        # each own their own state machine on the IncidentContext.
        return (
            f"Orchestrator("
            f"stateless=True, "
            f"llm_enabled={self._settings.LLM_ENABLED})"
        )
