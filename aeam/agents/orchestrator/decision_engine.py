"""
aeam/agents/orchestrator/decision_engine.py

Hybrid rule-first decision engine for the AEAM Orchestrator.

Decision logic follows a strict priority order:
1. Apply deterministic priority rules based on event severity.
2. If rule confidence is >= 0.9, return immediately without LLM involvement.
3. If LLM is enabled AND investigation depth warrants it, delegate to the
   LLM service (placeholder — no real LLM call is made in this module).
4. Fall back to the rule-based decision.

This module:
- Makes no direct agent calls.
- Makes no external API calls.
- Does not import or instantiate agents.
- Is fully typed and deterministic for the rule-based path.
"""

from __future__ import annotations

import logging
from aeam.monitoring.logging_config import get_logger
from typing import Any, Protocol, runtime_checkable

from aeam.config.settings import Settings
from aeam.core.event_models import Event
from aeam.memory.short_term import ShortTermMemory

logger = get_logger(__name__, agent="orchestrator")

# Confidence threshold above which the rule decision is returned immediately
# without consulting the LLM service.
_RULE_CONFIDENCE_THRESHOLD: float = 0.9

# Phase 4 LLM configuration (must match RAGAgent's constants for consistency)
_LLM_TEMPERATURE: float = 0.2
_LLM_MAX_TOKENS: int = 1000


# ---------------------------------------------------------------------------
# LLM service protocol (updated for Phase 4)
# ---------------------------------------------------------------------------


@runtime_checkable
class LLMService(Protocol):
    """
    Structural protocol for an LLM service used by :class:`DecisionEngine`.

    Any object implementing ``query`` is a valid ``llm_service``. No specific
    LLM provider is assumed; the engine treats the service as a black box.

    Phase 4 requires that implementations respect the supplied temperature
    and max_tokens parameters to enforce deterministic output constraints.
    """

    def query(self, prompt: str, *, temperature: float, max_tokens: int) -> str:
        """
        Send ``prompt`` to the LLM with the given generation parameters.

        Args:
            prompt:      The prompt string to send.
            temperature: Sampling temperature (0.0–1.0). Lower values are more
                         deterministic. Phase 4 mandates 0.2.
            max_tokens:  Maximum number of tokens to generate. Phase 4 mandates 1000.

        Returns:
            The LLM's text response as a string.
        """
        ...


# ---------------------------------------------------------------------------
# Decision engine
# ---------------------------------------------------------------------------


class DecisionEngine:
    """
    Hybrid rule-first decision engine for the AEAM Orchestrator.

    Applies deterministic priority rules to an incoming event and optionally
    delegates to an LLM service when rule confidence is low and investigation
    depth warrants deeper analysis.

    The rule-based path is always deterministic: given the same event severity
    the same decision dict is returned. The LLM path is guarded by both a
    feature flag (``settings.LLM_ENABLED``) and an investigation-depth check,
    ensuring LLM calls are a last resort rather than the default.

    Args:
        settings:    Application configuration. Provides ``LLM_ENABLED``.
        llm_service: Optional object conforming to :class:`LLMService`. May be
                     ``None`` — if ``LLM_ENABLED`` is ``True`` but no service
                     is injected, the engine falls back to the rule decision.

    Example::

        engine = DecisionEngine(settings=settings, llm_service=None)
        result = engine.decide(event=event, memory=stm)
        # → {"decision": "INVESTIGATE", "agents": ["KPI"],
        #    "confidence": 0.9, "source": "rule"}
    """

    def __init__(
        self,
        settings: Settings,
        llm_service: LLMService | None = None,
    ) -> None:
        """
        Initialise the engine with settings and an optional LLM service.

        Args:
            settings:    Validated :class:`~aeam.config.settings.Settings`.
            llm_service: Optional LLM service conforming to :class:`LLMService`.
                         Pass ``None`` to disable LLM augmentation entirely,
                         even when ``settings.LLM_ENABLED`` is ``True``.
        """
        self._settings = settings
        self._llm: LLMService | None = llm_service

        if llm_service is not None and not isinstance(llm_service, LLMService):
            raise TypeError(
                f"llm_service must implement the LLMService protocol. "
                f"Got: {type(llm_service).__name__!r}."
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def apply_priority_rules(self, event: Event) -> dict[str, Any]:
        """
        Return a deterministic rule-based decision for ``event``.

        Decision is derived solely from ``event.severity``:

        +------------+-------------+-----------+------------+
        | Severity   | Decision    | Agents    | Confidence |
        +============+=============+===========+============+
        | CRITICAL   | INVESTIGATE | [``KPI``] | 0.95       |
        +------------+-------------+-----------+------------+
        | HIGH       | INVESTIGATE | [``KPI``] | 0.90       |
        +------------+-------------+-----------+------------+
        | MEDIUM     | INVESTIGATE | [``KPI``] | 0.70       |
        +------------+-------------+-----------+------------+
        | LOW        | INVESTIGATE | [``KPI``] | 0.70       |
        +------------+-------------+-----------+------------+

        Args:
            event: The :class:`~aeam.core.event_models.Event` to decide on.

        Returns:
            A :class:`dict` with the following keys::

                {
                    "decision":   str,        # action to take
                    "agents":     list[str],  # agents to involve
                    "confidence": float,      # rule confidence (0–1)
                    "source":     "rule",     # always "rule" for this method
                    "severity":   str,        # echoed from the event
                }

        Example::

            engine.apply_priority_rules(critical_event)
            # → {"decision": "INVESTIGATE", "agents": ["KPI"],
            #    "confidence": 0.95, "source": "rule", "severity": "CRITICAL"}
        """
        severity_map: dict[str, dict[str, Any]] = {
            "CRITICAL": {
                "decision": "INVESTIGATE",
                "agents": ["KPI", "RAG"],
                "confidence": 0.95,
            },
            "HIGH": {
                "decision": "INVESTIGATE",
                "agents": ["KPI", "RAG"],
                "confidence": 0.90,   # <-- restored to original value
            },
        }

        # Normalize severity to uppercase to prevent case-sensitive bugs
        severity = str(event.severity).upper()
        
        base = severity_map.get(
            severity,
            {
                "decision": "INVESTIGATE",
                "agents": ["KPI"],
                "confidence": 0.70,
            },
        )

        return {
            **base,
            "source": "rule",
            "severity": event.severity,  # Keep original case for metadata
        }

    def should_use_llm(self, memory: ShortTermMemory) -> bool:
        """
        Determine whether the LLM service should be consulted.

        Returns ``True`` only when **all** of the following hold:
        1. ``settings.LLM_ENABLED`` is ``True``.
        2. An ``llm_service`` was provided at construction.
        3. ``memory.get("investigation_depth", 0) > 2`` — the investigation
           has already made more than two analysis passes, implying the
           deterministic path has not yet resolved the incident.

        Args:
            memory: The active :class:`~aeam.memory.short_term.ShortTermMemory`
                    for this investigation.

        Returns:
            ``True`` if LLM augmentation should be attempted; ``False`` otherwise.
        """
        if not self._settings.LLM_ENABLED:
            return False
        if self._llm is None:
            logger.debug(
                "should_use_llm: LLM_ENABLED=True but no llm_service injected."
            )
            return False
        depth: int = memory.get("investigation_depth", 0) or 0
        return int(depth) > 2

    def decide(
        self,
        event: Event,
        memory: ShortTermMemory,
    ) -> dict[str, Any]:
        """
        Produce a final decision for ``event`` using the hybrid rule-first strategy.

        Decision flow:
        1. Apply deterministic priority rules via :meth:`apply_priority_rules`.
        2. If rule ``confidence >= 0.9`` → return the rule decision immediately.
        3. Else, check :meth:`should_use_llm`:
           a. If ``True`` → call :meth:`_query_llm` (placeholder; returns a
              structured dict wrapping the LLM response).
           b. If ``False`` → fall back to the rule decision.

        The returned dict always contains a ``"source"`` key indicating which
        path produced the decision (``"rule"`` or ``"llm"``).

        Args:
            event:  The :class:`~aeam.core.event_models.Event` to decide on.
            memory: The active :class:`~aeam.memory.short_term.ShortTermMemory`
                    for this incident, used to check investigation depth.

        Returns:
            A :class:`dict` with the following keys::

                {
                    "decision":   str,        # e.g. "INVESTIGATE"
                    "agents":     list[str],  # e.g. ["KPI"]
                    "confidence": float,
                    "source":     "rule" | "llm",
                    "severity":   str,
                }

        Raises:
            Exception: Any exception raised by the LLM service propagates to
                       the caller. The rule-based fallback is NOT applied on
                       LLM failure — callers should wrap ``decide`` in
                       try/except if they want graceful degradation.

        Example::

            result = engine.decide(event=event, memory=stm)
            if result["decision"] == "INVESTIGATE":
                orchestrator.dispatch(result["agents"], event)
        """
        rule_decision = self.apply_priority_rules(event)
        confidence: float = rule_decision["confidence"]

        logger.debug(
            "decide | event_id=%s | severity=%s | rule_confidence=%.2f",
            event.event_id, event.severity, confidence,
        )

        # Fast path: high-confidence rule decision — no LLM needed.
        if confidence >= _RULE_CONFIDENCE_THRESHOLD:
            logger.info(
                "Decision via rule (confidence=%.2f) | event_id=%s | decision=%s",
                confidence, event.event_id, rule_decision["decision"],
            )
            return rule_decision

        # Low-confidence path: attempt LLM augmentation if conditions are met.
        if self.should_use_llm(memory):
            logger.info(
                "Decision delegating to LLM | event_id=%s | rule_confidence=%.2f",
                event.event_id, confidence,
            )
            return self._query_llm(event=event, memory=memory, rule_decision=rule_decision)

        # Fallback: return rule decision despite low confidence.
        logger.info(
            "Decision via rule fallback (LLM unavailable, confidence=%.2f) | event_id=%s",
            confidence, event.event_id,
        )
        return rule_decision

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _query_llm(
        self,
        event: Event,
        memory: ShortTermMemory,
        rule_decision: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Query the injected LLM service with the appropriate generation parameters
        and return a structured decision.

        The prompt is constructed from the event's serialisable fields and the
        current STM snapshot. The LLM response is wrapped in a dict with
        ``source: "llm"`` and the rule decision's ``agents`` list preserved,
        since the LLM is not expected to select agents — only to refine the
        action and confidence.

        This method performs no parsing or validation of the LLM response
        beyond wrapping it. Downstream orchestrator logic is responsible for
        interpreting the raw response text.

        Args:
            event:         The event being decided on.
            memory:        Current short-term memory snapshot.
            rule_decision: The low-confidence rule decision used as context.

        Returns:
            A :class:`dict` with the following keys::

                {
                    "decision":      str,    # from rule_decision (preserved)
                    "agents":        list,   # from rule_decision (preserved)
                    "confidence":    float,  # from rule_decision (preserved)
                    "source":        "llm",
                    "severity":      str,
                    "llm_response":  str,    # raw LLM text
                }
        """
        assert self._llm is not None  # guarded by should_use_llm()

        prompt = self._build_prompt(event=event, memory=memory, rule_decision=rule_decision)

        logger.debug("Sending prompt to LLM | event_id=%s", event.event_id)
        # Use the Phase 4 temperature and max_tokens constants.
        raw_response: str = self._llm.query(
            prompt,
            temperature=_LLM_TEMPERATURE,
            max_tokens=_LLM_MAX_TOKENS,
        )
        logger.debug("LLM response received | event_id=%s", event.event_id)

        return {
            "decision": rule_decision["decision"],
            "agents": rule_decision["agents"],
            "confidence": rule_decision["confidence"],
            "source": "llm",
            "severity": event.severity,
            "llm_response": raw_response,
        }

    @staticmethod
    def _build_prompt(
        event: Event,
        memory: ShortTermMemory,
        rule_decision: dict[str, Any],
    ) -> str:
        """
        Construct an LLM prompt from event fields, STM snapshot, and rule context.

        Args:
            event:         The event under investigation.
            memory:        Current STM snapshot (serialised to JSON).
            rule_decision: The low-confidence rule decision for context.

        Returns:
            A formatted prompt string ready for submission to the LLM service.
        """
        stm_snapshot = memory.serialize_for_llm()

        return (
            f"You are an expert site-reliability engineer.\n\n"
            f"An anomaly has been detected with the following details:\n"
            f"  Metric:        {event.metric}\n"
            f"  Severity:      {event.severity}\n"
            f"  Current value: {event.current_value}\n"
            f"  Expected value: {event.expected_value}\n"
            f"  Detected by:   {', '.join(event.detection_methods)}\n\n"
            f"Investigation memory snapshot:\n{stm_snapshot}\n\n"
            f"A rule-based engine suggested: {rule_decision['decision']} "
            f"(confidence={rule_decision['confidence']:.2f}).\n\n"
            f"Please confirm or refine this decision and explain your reasoning."
        )

    def __repr__(self) -> str:
        return (
            f"DecisionEngine("
            f"llm_enabled={self._settings.LLM_ENABLED}, "
            f"llm_service={self._llm!r})"
        )