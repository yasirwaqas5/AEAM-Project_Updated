"""
aeam/agents/orchestrator/evaluation_engine.py

Evaluation engine for determining whether an AEAM investigation should
continue, stop, or escalate.

The engine scores the current investigation state from ShortTermMemory using
four additive criteria, then maps the score to a three-way decision:
- STOP      (score >= 0.8)
- ESCALATE  (investigation depth >= MAX_INVESTIGATION_DEPTH)
- CONTINUE  (otherwise)

No LLM calls are made. All logic is deterministic.
"""

from __future__ import annotations

import logging
from aeam.monitoring.logging_config import get_logger
from typing import Any, TypedDict

from aeam.config.settings import Settings
from aeam.memory.short_term import ShortTermMemory

logger = get_logger(__name__, agent="orchestrator")

# Score threshold above which the investigation is considered sufficiently resolved.
_STOP_THRESHOLD: float = 0.8

# ---------------------------------------------------------------------------
# Type definitions for better static safety
# ---------------------------------------------------------------------------


class EvaluationResult(TypedDict):
    """
    Typed dictionary for evaluation results.
    
    Attributes:
        decision: One of "STOP", "CONTINUE", or "ESCALATE".
        score:    Accumulated score (0.0–1.0).
        reasons:  Human-readable list of scoring outcomes.
    """
    decision: str
    score: float
    reasons: list[str]


# ---------------------------------------------------------------------------
# Scoring criteria
# ---------------------------------------------------------------------------

# Each criterion is a (label, score_contribution) pair.
# Reason strings are hardcoded in the evaluation logic for clarity and
# to avoid brittle indexing.
_CRITERIA: list[tuple[str, float]] = [
    ("root_cause",   0.4),
    ("evidence",     0.3),
    ("confidence",   0.2),
    ("action_taken", 0.1),
]


class EvaluationEngine:
    """
    Scores the current investigation state and emits a STOP / CONTINUE / ESCALATE decision.

    The score is assembled from four additive criteria checked against the
    active :class:`~aeam.memory.short_term.ShortTermMemory`. The decision is
    then determined by a priority-ordered set of rules:

    1. If ``investigation_depth >= settings.MAX_INVESTIGATION_DEPTH`` → **ESCALATE**
       (depth limit overrides score — prevents infinite loops regardless of progress).
    2. If ``score >= 0.8`` → **STOP** (sufficient resolution).
    3. Otherwise → **CONTINUE**.

    No LLM calls are made. All logic is deterministic.

    Args:
        settings: Application configuration. Provides ``MAX_INVESTIGATION_DEPTH``.

    Example::

        engine = EvaluationEngine(settings=settings)
        result = engine.evaluate(memory=stm)
        # → {"decision": "STOP", "score": 0.9, "reasons": [...]}
    """

    def __init__(self, settings: Settings) -> None:
        """
        Initialise with injected application settings.

        Args:
            settings: Validated :class:`~aeam.config.settings.Settings` instance.
        """
        self._settings = settings

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def evaluate(self, memory: ShortTermMemory) -> EvaluationResult:
        """
        Evaluate investigation progress and return a STOP / CONTINUE / ESCALATE decision.

        Scoring criteria (additive):

        +-------------------+-------+--------------------------------------------------+
        | Criterion         | Score | Condition                                        |
        +===================+=======+==================================================+
        | ``root_cause``    | +0.4  | ``memory.get("root_cause")`` is not None/falsy   |
        +-------------------+-------+--------------------------------------------------+
        | ``evidence``      | +0.3  | ``len(memory.get("evidence", [])) >= 3``          |
        +-------------------+-------+--------------------------------------------------+
        | ``confidence``    | +0.2  | ``memory.get("confidence", 0) > 0.8``             |
        +-------------------+-------+--------------------------------------------------+
        | ``action_taken``  | +0.1  | ``memory.get("action_taken") == True``            |
        +-------------------+-------+--------------------------------------------------+

        Decision rules (evaluated in priority order):

        1. ``investigation_depth >= MAX_INVESTIGATION_DEPTH`` → ``"ESCALATE"``
        2. ``score >= 0.8``                                  → ``"STOP"``
        3. Otherwise                                         → ``"CONTINUE"``

        Args:
            memory: The active :class:`~aeam.memory.short_term.ShortTermMemory`
                    for the current investigation. Must have been initialised
                    via :meth:`~aeam.memory.short_term.ShortTermMemory.initialize`.

        Returns:
            A :class:`dict` with the following structure::

                {
                    "decision": "STOP" | "CONTINUE" | "ESCALATE",
                    "score":    float,   # 0.0–1.0 accumulated from criteria
                    "reasons":  list[str],  # human-readable explanation of score
                }

        Example::

            stm.set("root_cause", "Memory leak in service A")
            stm.append("evidence", {"check": "heap_dump", "result": "confirmed"})
            stm.append("evidence", {"check": "gc_log",   "result": "confirmed"})
            stm.append("evidence", {"check": "metrics",  "result": "elevated"})
            stm.set("confidence", 0.85)

            result = engine.evaluate(memory=stm)
            # → {"decision": "STOP", "score": 0.9, "reasons": [
            #       "Root cause identified (+0.4)",
            #       "Sufficient evidence collected — >= 3 items (+0.3)",
            #       "High confidence threshold met — > 0.8 (+0.2)",
            #    ]}
        """
        score: float = 0.0
        reasons: list[str] = []

        # --- Criterion 1: root cause present ---
        if memory.get("root_cause"):
            score += 0.4
            reasons.append("Root cause identified (+0.4)")

        # --- Criterion 2: sufficient evidence ---
        evidence = memory.get("evidence", []) or []
        if isinstance(evidence, list) and len(evidence) >= 3:
            score += 0.3
            reasons.append("Sufficient evidence collected — >= 3 items (+0.3)")

        # --- Criterion 3: high confidence ---
        confidence_val = memory.get("confidence", 0) or 0
        try:
            if float(confidence_val) > 0.8:
                score += 0.2
                reasons.append("High confidence threshold met — > 0.8 (+0.2)")
        except (TypeError, ValueError):
            logger.debug(
                "evaluate: 'confidence' value %r is not numeric; skipping criterion.",
                confidence_val,
            )

        # --- Criterion 4: action taken ---
        if memory.get("action_taken") is True:
            score += 0.1
            reasons.append("Remediation action recorded (+0.1)")

        score = round(score, 10)   # floating-point hygiene

        # --- Investigation depth check ---
        depth: int = 0
        raw_depth = memory.get("investigation_depth", 0)
        try:
            depth = int(raw_depth or 0)
        except (TypeError, ValueError):
            logger.debug(
                "evaluate: 'investigation_depth' value %r is not int; treating as 0.",
                raw_depth,
            )

        max_depth: int = self._settings.MAX_INVESTIGATION_DEPTH

        logger.debug(
            "evaluate | score=%.2f | depth=%d | max_depth=%d | reasons=%s",
            score, depth, max_depth, reasons,
        )

        # Priority 1: depth limit — overrides score.
        if depth >= max_depth:
            reasons.append(
                f"Investigation depth limit reached ({depth}/{max_depth}) → ESCALATE"
            )
            return self._result("ESCALATE", score, reasons)

        # Priority 2: sufficient score.
        if score >= _STOP_THRESHOLD:
            return self._result("STOP", score, reasons)

        # Default: keep going.
        if not reasons:
            reasons.append(
                f"Insufficient evidence to resolve (score={score:.2f} < {_STOP_THRESHOLD})"
            )
        return self._result("CONTINUE", score, reasons)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _result(
        decision: str,
        score: float,
        reasons: list[str],
    ) -> EvaluationResult:
        """
        Assemble the standardised return dict.

        Args:
            decision: One of ``"STOP"``, ``"CONTINUE"``, ``"ESCALATE"``.
            score:    Accumulated score (0.0–1.0).
            reasons:  Human-readable list of scoring outcomes.

        Returns:
            Formatted result dict conforming to EvaluationResult TypedDict.
        """
        return {
            "decision": decision,
            "score": score,
            "reasons": reasons,
        }

    def __repr__(self) -> str:
        return (
            f"EvaluationEngine("
            f"max_depth={self._settings.MAX_INVESTIGATION_DEPTH}, "
            f"stop_threshold={_STOP_THRESHOLD})"
        )