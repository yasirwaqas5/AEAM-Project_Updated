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
from aeam.agents.orchestrator.state_machine import IncidentState, IncidentStateMachine
from aeam.config.settings import Settings
from aeam.core.event_bus import EventBus
from aeam.core.event_models import Event
from aeam.memory.long_term import LongTermMemory
from aeam.memory.short_term import ShortTermMemory
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

            rag_result = self._rag.investigate(
                event=self._active_event,
                memory=self._stm,
            )

            rag_findings = rag_result.get("findings", {})
            rag_confidence = float(rag_result.get("confidence", 0.0))
            memory_updates = rag_result.get("memory_updates", {})

            # Append RAG findings to STM safely
            self._stm.append("findings", {
                "type": "rag",
                "depth": depth,
                "confidence": rag_confidence,
                "data": rag_findings,
            })

            # Apply memory updates (research-only)
            if "hypotheses" in memory_updates:
                for h in memory_updates["hypotheses"]:
                    self._stm.append("hypotheses", h)

            if "confidence" in memory_updates:
                existing = float(self._stm.get("confidence") or 0.0)
                self._stm.set(
                    "confidence",
                    round(max(existing, float(memory_updates["confidence"])), 2),
                )

            if rag_findings.get("requires_human_review") is True:
                self._stm.set("requires_human", True)

        # Always run the KPI placeholder (can be replaced later with actual KPI Agent)
        self._run_kpi_investigation_placeholder()

        # ---------- Force LLM reasoning at depth >= 3 ----------
        if depth >= 3 and self._settings.LLM_ENABLED:
            logger.info("investigate | triggering LLM reasoning at depth %d", depth)
            try:
                llm = LLMService(settings=self._settings)

                # Build a structured prompt
                prompt = f"""
You are an expert business analyst. Based on the following incident details,
provide a concise root cause analysis and recommended actions.

Incident:
- Metric: {self._active_event.metric}
- Current value: {self._active_event.current_value}
- Expected value: {self._active_event.expected_value}
- Severity: {self._active_event.severity}
- Detection methods: {', '.join(self._active_event.detection_methods)}

Short‑Term Memory findings:
{self._stm.serialize_for_llm()}

Return a JSON object with:
- root_cause (string)
- confidence (float 0-1)
- recommended_action (string)
"""
                raw = llm.query(prompt, temperature=0.2, max_tokens=500)
                insight = json.loads(raw)

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
            # Phase 6: Execute external actions if ActionAgent is available.
            if self._action is not None:
                incident_id = self._stm.get("incident_id")
                severity = self._active_event.severity.upper() if self._active_event else "UNKNOWN"
                if severity in ("HIGH", "CRITICAL"):
                    # Slack alert
                    params = {
                        "channel": "#aeam-alerts",
                        "message": (
                            f"🚨 AEAM Incident Resolved\n"
                            f"Incident: {incident_id}\n"
                            f"Metric: {self._active_event.metric}\n"
                            f"Severity: {self._active_event.severity}\n"
                            f"Current value: {self._active_event.current_value}\n"
                            f"Expected value: {self._active_event.expected_value}\n"
                            f"Detection methods: {', '.join(self._active_event.detection_methods)}\n"
                            f"Root cause: {self._stm.get('root_cause', 'Unknown')}\n"
                            f"LLM Insight: {self._stm.get('llm_response', 'Not available')[:200]}..."
                        ),
                        "severity": self._active_event.severity,
                    }
                    try:
                        action_result = self._action.execute(
                            action_type="slack",
                            parameters=params,
                            incident_id=incident_id,
                        )
                        logger.info(
                            "Action executed | incident_id=%s | result=%s",
                            incident_id, action_result,
                        )
                    except Exception as exc:
                        logger.error(
                            "Action failed | incident_id=%s | error=%s",
                            incident_id, exc,
                        )

                    # DEBUG: print the condition status
                    print(f">>> JIRA CONDITION CHECK: URL={'OK' if self._settings.JIRA_URL else 'MISSING'}  TOKEN={'OK' if self._settings.JIRA_API_TOKEN else 'MISSING'}")

                    # Jira ticket creation (if Jira is configured)
                    if self._settings.JIRA_URL and self._settings.JIRA_API_TOKEN:
                        priority_map = {
                            "HIGH": "High",
                            "CRITICAL": "Highest",
                            "MEDIUM": "Medium",
                            "LOW": "Low"
                        }
                        jira_priority = priority_map.get(self._active_event.severity.upper(), "Medium")

                        jira_params = {
                            "summary": f"Incident {incident_id}: {self._active_event.metric} anomaly",
                            "description": (
                                f"Automated investigation for incident {incident_id}\n\n"
                                f"Metric: {self._active_event.metric}\n"
                                f"Severity: {self._active_event.severity}\n"
                                f"Current value: {self._active_event.current_value}\n"
                                f"Expected value: {self._active_event.expected_value}\n"
                                f"Detection methods: {', '.join(self._active_event.detection_methods)}\n"
                                f"Root cause: {self._stm.get('root_cause', 'Unknown')}\n\n"
                                f"LLM Reasoning: {self._stm.get('llm_response', 'Not available')}"
                            ),
                            "priority": jira_priority,
                        }
                        try:
                            jira_result = self._action.execute(
                                action_type="jira",
                                parameters=jira_params,
                                incident_id=incident_id,
                            )
                            logger.info(
                                "Jira ticket created | incident_id=%s | result=%s",
                                incident_id, jira_result,
                            )
                        except Exception as exc:
                            logger.error(
                                "Jira action failed | incident_id=%s | error=%s",
                                incident_id, exc,
                            )
                else:
                    logger.debug("No action triggered for severity=%s", severity)

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
        Close the incident, persist it, generate reports and alerts, and clean up.

        Steps:
        1. Transition FSM to ``COMPLETE``.
        2. Assemble a persistence payload from STM and the active event.
        3. Persist via :meth:`~aeam.memory.long_term.LongTermMemory.record_incident`.
        4. If a ReportAgent is available, generate a human-readable report and
           alert from the current memory state.
        5. If an ActionAgent is available, send the alert to Slack and the
           detailed report via email.
        6. Log the resulting ``incident_id``.
        7. Clear STM.
        8. Reset ``_active_event`` to ``None``.
        """
        logger.info("finalize_incident | transitioning to COMPLETE")

        self._sm.transition(IncidentState.COMPLETE)

        # Assemble persistence payload — safe even if keys are missing.
        event_data: dict[str, Any] = self._stm.get("event") or {}
        payload: dict[str, Any] = {
            "event_id":           event_data.get("event_id"),
            "event_type":         event_data.get("event_type"),
            "metric":             event_data.get("metric"),
            "severity":           event_data.get("severity"),
            "current_value":      event_data.get("current_value"),
            "expected_value":     event_data.get("expected_value"),
            "detection_methods":  event_data.get("detection_methods", []),
            "timestamp":          event_data.get("timestamp"),
            "investigation_depth": self._stm.get("investigation_depth"),
            "root_cause":         self._stm.get("root_cause"),
            "confidence":         self._stm.get("confidence"),
            "action_taken":       self._stm.get("action_taken"),
            "requires_human":     self._stm.get("requires_human"),
            "findings":           self._stm.get("findings", []),
            # ✅ NEW: persist LLM reasoning
            "llm_response":       self._stm.get("llm_response", ""),
        }

        try:
            incident_id = self._ltm.record_incident(payload)
            logger.info("finalize_incident | persisted | incident_id=%s", incident_id)
        except Exception as exc:  # noqa: BLE001
            logger.error("finalize_incident | LTM persist failed: %s", exc)
            incident_id = self._stm.get("incident_id", "unknown")

        # Phase 7: Generate human‑readable reports and alerts.
        if self._report is not None:
            try:
                report = self._report.generate_report(memory=self._stm)
                alert = self._report.generate_alert(memory=self._stm)

                logger.info(
                    "finalize_incident | report and alert generated | incident_id=%s",
                    incident_id,
                )

                # Phase 6: Send via ActionAgent.
                if self._action is not None:
                    # Send Slack alert.
                    slack_result = self._action.execute(
                        action_type="slack",
                        parameters={
                            "channel": "#alerts",
                            "message": alert["message"],
                            "severity": alert["severity"],
                        },
                        incident_id=incident_id,
                    )
                    logger.debug(
                        "finalize_incident | slack alert sent | result=%s",
                        slack_result,
                    )

                    # Send email with detailed report.
                    email_result = self._action.execute(
                        action_type="email",
                        parameters={
                            "to": ["ops@company.com"],
                            "subject": f"AEAM Incident - {alert['event_type']}",
                            "body": report["detailed_report"],
                        },
                        incident_id=incident_id,
                    )
                    logger.debug(
                        "finalize_incident | email sent | result=%s",
                        email_result,
                    )
                else:
                    logger.debug(
                        "finalize_incident | ActionAgent not available — skipping notifications."
                    )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "finalize_incident | report/notification generation failed: %s",
                    exc,
                )

        # Clean up.
        self._stm.clear()
        self._active_event = None
        logger.info("finalize_incident | STM cleared | lifecycle complete")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

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
            self._stm.append("hypotheses", f"Anomaly in {self._active_event.metric}")

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