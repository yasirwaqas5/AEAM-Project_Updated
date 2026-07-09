"""
aeam/agents/orchestrator/orchestrator.py

Central coordination logic for the AEAM modular monolith.

The Orchestrator drives the full incident lifecycle:
    EVENT_RECEIVED → INVESTIGATING → DECIDING → … → COMPLETE

It wires together the DecisionEngine, EvaluationEngine, state machine,
short-term memory, and long-term persistence without containing any
detection logic, LLM calls, Action Agent calls, RAG, forecasting, or
direct database writes.

All dependencies are injected; the Orchestrator itself is stateless between
incidents — per-incident state lives entirely in the injected
ShortTermMemory and IncidentStateMachine.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import TYPE_CHECKING, Any

from aeam.agents.orchestrator.decision_engine import DecisionEngine
from aeam.agents.orchestrator.evaluation_engine import EvaluationEngine
from aeam.agents.orchestrator.investigation_status import derive_investigation_status
from aeam.agents.orchestrator.notifications import format_jira_description, format_slack_message
from aeam.agents.orchestrator.runbooks import get_runbook, resolve_action_step
from aeam.agents.orchestrator.state_machine import IncidentState, IncidentStateMachine
from aeam.agents.rag.cause_quality import best_meaningful_cause
from aeam.agents.rag.rag_agent import parse_llm_json
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
    incidents_total,
    investigation_duration,
    start_timer,
)
from aeam.services.llm_service import LLMService

if TYPE_CHECKING:
    from aeam.agents.report.report_agent import ReportAgent

logger = logging.getLogger(__name__)


class Orchestrator:
    """
    Central coordinator for AEAM incident lifecycle management.

    Receives confirmed events from the EventBus, drives them through the
    investigation loop, and persists outcomes to long-term memory.

    The Orchestrator:
    - Manages per-incident state via :class:`~aeam.agents.orchestrator.state_machine.IncidentStateMachine`.
    - Stores transient investigation context in :class:`~aeam.memory.short_term.ShortTermMemory`.
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
        short_term_memory: Ephemeral per-incident working memory.
        long_term_memory:  Persistent incident and decision store.
        state_machine:     Finite state machine governing incident lifecycle.
        settings:          Application configuration.
        rag_agent:         Optional RAG agent for Phase 4 (default None).
        action_agent:      Optional Action agent for Phase 6 (default None).
        report_agent:      Optional Report agent for Phase 7 (default None).

    Example::

        orchestrator = Orchestrator(
            event_bus=bus,
            decision_engine=decision_engine,
            evaluation_engine=evaluation_engine,
            short_term_memory=stm,
            long_term_memory=ltm,
            state_machine=IncidentStateMachine(),
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
        short_term_memory: ShortTermMemory,
        long_term_memory: LongTermMemory,
        state_machine: IncidentStateMachine,
        settings: Settings,
        rag_agent: Any | None = None,
        action_agent: Any | None = None,  # Phase 6
        report_agent: Any | None = None,  # Phase 7
    ) -> None:
        self._bus = event_bus
        self._decision = decision_engine
        self._evaluation = evaluation_engine
        self._stm = short_term_memory
        self._ltm = long_term_memory
        self._sm = state_machine
        self._settings = settings
        self._rag = rag_agent
        self._action = action_agent  # Phase 6
        self._report = report_agent   # Phase 7

        # Track the active event for the duration of a handle_event() call.
        self._active_event: Event | None = None
        # Wall-clock start of the current incident lifecycle, for the
        # investigation_duration histogram (set in handle_event(), consumed
        # and cleared in finalize_incident()).
        self._investigation_started_at: float | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def handle_event(self, event: Event) -> None:
        """
        Entry point for a confirmed anomaly event.

        Steps:
        1. Generate a new ``incident_id`` for this lifecycle.
        2. Reset and re-initialise the state machine.
        3. Initialise :class:`~aeam.memory.short_term.ShortTermMemory`.
        4. Transition FSM to ``EVENT_RECEIVED``.
        5. Store the event in STM.
        6. Delegate to :meth:`investigate`.

        Args:
            event: The confirmed :class:`~aeam.core.event_models.Event`
                   to handle.

        Note:
            This method is designed to be registered directly as an
            :class:`~aeam.core.event_bus.EventBus` handler::

                bus.register_handler("KPI_ANOMALY", orchestrator.handle_event)
        """
        incident_id = str(uuid.uuid4())
        self._sm.reset()
        self._active_event = event

        # Metrics: one incident counted, one active investigation started.
        incidents_total.labels(event_type=event.event_type, severity=event.severity).inc()
        active_incidents.inc()
        self._investigation_started_at = start_timer()

        logger.info(
            "Orchestrator.handle_event | incident_id=%s | event_id=%s | "
            "metric=%s | severity=%s",
            incident_id, event.event_id, event.metric, event.severity,
        )

        # Initialise STM for this incident.
        self._stm.initialize(
            task_type="anomaly_investigation",
            incident_id=incident_id,
        )
        self._stm.set("investigation_depth", 0)
        self._stm.set("findings", [])
        self._stm.set("hypotheses", [])
        self._stm.set("evidence", [])
        self._stm.set("confidence", None)
        self._stm.set("root_cause", None)
        self._stm.set("action_taken", False)
        self._stm.set("requires_human", False)

        # Transition FSM and store event.
        self._sm.transition(IncidentState.EVENT_RECEIVED)
        self._stm.set("event", event.model_dump(mode="json"))
        self._stm.set("incident_id", incident_id)   # <-- added: store incident_id in STM

        logger.debug(
            "handle_event | STM initialised | incident_id=%s", incident_id
        )

        self.investigate()

    def investigate(self) -> None:
        """
        Perform one investigation pass.

        Steps:
        1. Transition FSM to ``INVESTIGATING``.
        2. Increment ``investigation_depth`` in STM.
        3. Call :meth:`~aeam.agents.orchestrator.decision_engine.DecisionEngine.decide`.
        4. Transition FSM to ``DECIDING``.
        5. Act on the decision:
           - ``"INVESTIGATE"`` → (optionally invoke RAG if configured), then
             run KPI placeholder and call :meth:`evaluate`.
           - ``"STOP"`` → call :meth:`finalize_incident`.
           - Unknown decision → log a warning and call :meth:`finalize_incident`.

        Raises:
            RuntimeError: If called without an active event (i.e. before
                          :meth:`handle_event`).
        """
        if self._active_event is None:
            raise RuntimeError(
                "investigate() called with no active event. "
                "Call handle_event() first."
            )

        # --- Step 1 & 2: transition + depth increment ---
        self._sm.transition(IncidentState.INVESTIGATING)

        depth: int = (self._stm.get("investigation_depth") or 0)
        depth += 1
        self._stm.set("investigation_depth", depth)

        logger.info(
            "investigate | depth=%d | event_id=%s",
            depth, self._active_event.event_id,
        )

        # --- Step 3: decide ---
        decision_result = self._decision.decide(
            event=self._active_event,
            memory=self._stm,
        )

        # ✅ NEW: store LLM response if present (from the decision engine)
        if decision_result.get("source") == "llm":
            self._stm.set("llm_response", decision_result.get("llm_response", ""))

        action: str = decision_result.get("decision", "INVESTIGATE")
        confidence: float = float(decision_result.get("confidence", 0.0))

        logger.info(
            "investigate | decision=%s | confidence=%.2f | source=%s",
            action, confidence, decision_result.get("source", "unknown"),
        )

        # Log the decision to STM for evaluation and audit.
        self._stm.append("findings", {
            "depth": depth,
            "decision": action,
            "confidence": confidence,
            "source": decision_result.get("source"),
        })

        # --- Step 4: transition to DECIDING ---
        if self._sm.get_state() != IncidentState.DECIDING:
            self._sm.transition(IncidentState.DECIDING)

        # --- Step 5: act on decision ---
        if action == "STOP":
            self.finalize_incident()
            return

        if action != "INVESTIGATE":
            logger.warning(
                "investigate | unknown decision %r | defaulting to finalize",
                action,
            )
            self.finalize_incident()
            return

        # ---------- INVESTIGATE path ----------
        agents = decision_result.get("agents", []) or []

        # --- RAG integration (Phase 4) ---
        if "RAG" in agents and self._rag is not None:
            logger.info("investigate | invoking RAG agent")

            t = start_timer()
            rag_result = self._rag.investigate(
                event=self._active_event,
                memory=self._stm,
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
            self._stm.append("findings", {
                "type": "rag",
                "depth": depth,
                "confidence": rag_confidence,
                "root_cause": rag_root_cause,
                "data": rag_findings,
            })
            # Persist the full RAG result for this pass unconditionally
            # (previously only set on success, which is why a failed pass
            # left the dashboard with nothing to show).
            self._stm.set("llm_response", json.dumps(rag_result, default=str))

            # Promote the grounded root cause into STM so finalize_incident()
            # persists it — and so the KPI placeholder does not overwrite it.
            if rag_root_cause and not no_knowledge:
                self._stm.set("root_cause", rag_root_cause)
                existing_conf = float(self._stm.get("confidence") or 0.0)
                self._stm.set(
                    "confidence",
                    round(max(existing_conf, rag_confidence), 2),
                )

            # Map possible_causes into STM evidence with full grounding metadata.
            if isinstance(rag_findings, dict):
                for h in memory_updates.get("hypotheses", []):
                    self._stm.append("hypotheses", h)
                for cause in rag_findings.get("possible_causes", []):
                    self._stm.append("evidence", {
                        "source": "rag",
                        "chunk_id": cause.get("chunk_id"),
                        "cause": cause.get("cause"),
                        "confidence": cause.get("confidence"),
                    })

            # Escalation signal from the actual contract.
            requires_human = rag_findings.get("requires_human_review") if isinstance(rag_findings, dict) else None
            if requires_human is True:
                self._stm.set("requires_human", True)

        # Always run the KPI placeholder (can be replaced later with actual KPI Agent)
        self._run_kpi_investigation_placeholder()

        # ---------- Force LLM reasoning at depth >= 3 ----------
        if depth >= 3 and self._settings.LLM_ENABLED:
            logger.info("investigate | triggering LLM reasoning at depth %d", depth)
            try:
                llm = LLMService(settings=self._settings)

                # Build a structured prompt that insists on pure JSON
                prompt = (
                    f"You are an expert business analyst. Based on the following incident details, "
                    f"provide a concise root cause analysis and recommended actions.\n\n"
                    f"Incident:\n"
                    f"- Metric: {self._active_event.metric}\n"
                    f"- Current value: {self._active_event.current_value}\n"
                    f"- Expected value: {self._active_event.expected_value}\n"
                    f"- Severity: {self._active_event.severity}\n"
                    f"- Detection methods: {', '.join(self._active_event.detection_methods)}\n\n"
                    f"Short‑Term Memory findings:\n{self._stm.serialize_for_llm()}\n\n"
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
                    # root cause from unparseable output. The placeholder
                    # root cause already set by
                    # _run_kpi_investigation_placeholder() is left intact.
                    self._stm.append("findings", {
                        "type": "llm_reasoning_error",
                        "depth": depth,
                        "reason": "LLM response could not be parsed as JSON.",
                        "raw_response": raw,
                    })
                else:
                    # Update STM with the LLM output
                    self._stm.set("root_cause", insight.get("root_cause", "Unknown"))
                    self._stm.set("confidence", insight.get("confidence", 0.0))
                    self._stm.set("llm_response", raw)

                    logger.info("investigate | LLM reasoning stored successfully")
            except Exception as exc:
                logger.warning("investigate | LLM reasoning failed: %s", exc)
                # Keep the existing placeholder root cause (already set in _run_kpi_investigation_placeholder)

        self.evaluate()

    def evaluate(self) -> None:
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
        eval_result = self._evaluation.evaluate(memory=self._stm)
        eval_decision: str = eval_result.get("decision", "CONTINUE")
        score: float = float(eval_result.get("score", 0.0))
        reasons: list[str] = eval_result.get("reasons", [])

        logger.info(
            "evaluate | decision=%s | score=%.2f | reasons=%s",
            eval_decision, score, reasons,
        )

        self._stm.append("findings", {
            "type": "evaluation",
            "decision": eval_decision,
            "score": score,
            "reasons": reasons,
        })

        if eval_decision == "CONTINUE":
            self.investigate()

        elif eval_decision == "STOP":
            # All external notifications/actions are dispatched exactly once,
            # from finalize_incident() (see notifications.py for the
            # structured Slack/Jira formatters). Sending an alert here too
            # would both duplicate that notification and dump raw serialized
            # findings as message text instead of a structured summary.
            self.finalize_incident()

        elif eval_decision == "ESCALATE":
            logger.warning(
                "evaluate | ESCALATE triggered | marking requires_human"
            )
            self._stm.set("requires_human", True)
            self._stm.append("findings", {
                "type": "escalation",
                "reason": "Max investigation depth reached without resolution.",
            })
            self.finalize_incident()

        else:
            logger.warning(
                "evaluate | unknown eval decision %r | defaulting to finalize",
                eval_decision,
            )
            self.finalize_incident()

    def finalize_incident(self) -> None:
        """
        Close the incident, execute its safe runbook, notify, persist, and clean up.

        Steps:
        1. Transition FSM to ``COMPLETE``.
        2. Derive the canonical investigation status (see
           :mod:`~aeam.agents.orchestrator.investigation_status`), the
           validation outcome of the latest RAG pass, and the event's safe
           runbook (see :mod:`~aeam.agents.orchestrator.runbooks`).
        3. Execute the runbook's action plan via ActionAgent. Only actions
           that actually return ``SUCCESS`` are recorded as executed; every
           skipped or failed action is recorded with its reason — this
           method never claims an action ran unless ActionAgent confirmed it.
        4. Send Slack/Jira notifications built from explicit named fields
           (see :mod:`~aeam.agents.orchestrator.notifications`) — never a
           raw JSON dump of findings or the LLM response.
        5. Always attempt an email report last (independent of the runbook;
           a missing-credentials failure is expected and already reported
           with a structured reason by EmailActions).
        6. Append one consolidated ``audit_summary`` findings entry — the
           single source of truth the frontend reads for status, evidence,
           and executed actions.
        7. Persist via :meth:`~aeam.memory.long_term.LongTermMemory.record_incident`
           (schema unchanged — everything new lives inside the existing
           ``findings`` JSON column).
        8. Clear STM and reset ``_active_event``.
        """
        if self._sm.get_state() == IncidentState.COMPLETE:
            logger.info("finalize_incident | already COMPLETE; skipping duplicate finalize")
            return

        logger.info("finalize_incident | transitioning to COMPLETE")
        self._sm.transition(IncidentState.COMPLETE)

        # Metrics: one active investigation ended. The COMPLETE-state guard
        # above already prevents finalize_incident() from running twice for
        # the same incident, so this dec()/observe() pair can never double-count.
        active_incidents.dec()
        if self._investigation_started_at is not None:
            end_timer(investigation_duration, self._investigation_started_at)
            self._investigation_started_at = None

        event_data: dict[str, Any] = self._stm.get("event") or {}
        incident_id: str = self._stm.get("incident_id", "unknown")
        root_cause = self._stm.get("root_cause")
        requires_human = bool(self._stm.get("requires_human"))
        confidence = self._stm.get("confidence")

        # --- Derive validation status + latest RAG snapshot for reporting. ---
        latest_rag = self._latest_rag_finding()
        query_attempts = self._collect_query_attempts()

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
                    "finalize_incident | action %s raised: %s", step, exc,
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
                report = self._report.generate_report(memory=self._stm)
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

        # --- Consolidated audit summary: the single source of truth the
        # frontend reads instead of reconstructing state from scattered
        # findings entries. Stored inside the existing "findings" JSON
        # column — no schema change.
        self._stm.append("findings", {
            "type": "audit_summary",
            "investigation_status": investigation_status,
            "root_cause": root_cause,
            "validation_status": validation_status,
            "validation_reason": validation_reason,
            "reranking": "not_applicable",
            "escalation_reason": self._escalation_reason(investigation_status),
            "query_attempts": query_attempts,
            "evidence_count": evidence_count,
            "top_confidence": top_confidence,
            "chunk_ids": chunk_ids,
            "recommended_actions": runbook["recommended_actions"],
            "executed_actions": executed_actions,
            "skipped_actions": skipped_actions,
        })

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
            "investigation_depth": self._stm.get("investigation_depth"),
            "root_cause":          root_cause,
            "confidence":          confidence,
            # Reflects reality now: True only if >=1 action actually
            # succeeded (previously hardcoded False regardless of outcome).
            "action_taken":        bool(executed_actions),
            "requires_human":      requires_human,
            "findings":            self._stm.get("findings", []),
            "llm_response":        self._stm.get("llm_response", ""),
        }

        try:
            db_incident_id = self._ltm.record_incident(payload)
            logger.info("finalize_incident | persisted | incident_id=%s", db_incident_id)
        except Exception as exc:  # noqa: BLE001
            logger.error("finalize_incident | LTM persist failed: %s", exc)

        logger.info(
            "finalize_incident | status=%s | root_cause=%r | executed=%s | skipped=%s",
            investigation_status, root_cause, executed_actions,
            [s["action"] for s in skipped_actions],
        )

        # Clean up.
        self._stm.clear()
        self._active_event = None
        logger.info("finalize_incident | STM cleared | lifecycle complete")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _escalation_reason(self, investigation_status: str) -> str | None:
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

        findings = self._stm.get("findings", []) or []
        if any(isinstance(f, dict) and f.get("type") == "escalation" for f in findings):
            return "Max investigation depth reached without resolution."

        return (
            "Flagged for human review by the RAG investigation "
            "(borderline confidence or ambiguous grounded finding)."
        )

    def _latest_rag_finding(self) -> dict[str, Any] | None:
        """
        Return the ``data`` dict of the most recent RAG pass recorded in
        STM findings for the current incident.

        Returns:
            The last ``type == "rag"`` finding's ``data`` dict, or ``None``
            if RAG has not been invoked at all this incident.
        """
        findings = self._stm.get("findings", []) or []
        latest: dict[str, Any] | None = None
        for entry in findings:
            if isinstance(entry, dict) and entry.get("type") == "rag":
                data = entry.get("data")
                if isinstance(data, dict):
                    latest = data
        return latest

    def _collect_query_attempts(self) -> list[dict[str, Any]]:
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
        findings = self._stm.get("findings", []) or []
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

    def _run_kpi_investigation_placeholder(self) -> None:
        """
        Placeholder for a KPI investigation pass.

        In production this would dispatch to a KPI Agent (not yet implemented)
        and await structured findings. For now it writes synthetic evidence to
        STM so the EvaluationEngine has non-empty data to score against.

        This method contains NO real analysis, NO LLM calls, NO external I/O.
        It exists solely to keep the investigation loop functional until the
        KPI Agent is wired in.
        """
        if self._active_event is None:
            return

        depth: int = self._stm.get("investigation_depth") or 1

        logger.debug(
            "_run_kpi_investigation_placeholder | depth=%d | metric=%s",
            depth, self._active_event.metric,
        )

        # Simulate accumulating evidence over successive passes.
        self._stm.append("evidence", {
            "depth": depth,
            "metric": self._active_event.metric,
            "current_value": self._active_event.current_value,
            "expected_value": self._active_event.expected_value,
            "note": "placeholder — awaiting KPI Agent integration",
        })

        # On the first pass, propose a generic hypothesis.
        if depth == 1:
            self._stm.append("hypotheses", f"Anomaly in {self._active_event.metric} ({self._active_event.current_value} vs expected {self._active_event.expected_value})")

        # Simulate progressively increasing confidence with each pass.
        current_confidence: float = float(self._stm.get("confidence") or 0.0)
        new_confidence = min(current_confidence + 0.3, 1.0)
        self._stm.set("confidence", round(new_confidence, 2))

        # On the second pass, simulate root cause identification (placeholder).
        if depth >= 2 and not self._stm.get("root_cause"):
            self._stm.set(
                "root_cause",
                f"Simulated root cause for metric '{self._active_event.metric}' "
                f"(placeholder — replace with real KPI Agent output)",
            )

    def __repr__(self) -> str:
        return (
            f"Orchestrator("
            f"state={self._sm.get_state().value!r}, "
            f"llm_enabled={self._settings.LLM_ENABLED})"
        )
