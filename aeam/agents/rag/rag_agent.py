"""
aeam/agents/rag/rag_agent.py

RAG Agent for unstructured investigation in AEAM Phase 4.

Integrates retrieval, LLM reasoning, and response validation into a single
research-only agent. The agent produces structured findings from retrieved
historical context — it never decides STOP/CONTINUE, never writes to a
database, never calls external APIs, and never modifies the Event object.

The Orchestrator remains the sole decision authority. The RAG Agent's only
output is a findings dict returned to the caller; memory is updated through
the return structure only.

Phase 4 constraints:
- LLM temperature: 0.2
- LLM max tokens:  1000
- RAG is research-only.
- No DB writes.
- No external APIs.
- No action execution.
- Orchestrator remains decision authority.
"""

from __future__ import annotations

import json
import logging
from aeam.monitoring.logging_config import get_logger
import re
from typing import Any

from aeam.agents.rag.response_validator import RAGResponseValidator
from aeam.agents.rag.retrieval_pipeline import RetrievalPipeline
from aeam.core.event_models import Event
from aeam.memory.short_term import ShortTermMemory

logger = get_logger(__name__, agent="rag")

# LLM prompt configuration (Phase 4 spec).
_LLM_TEMPERATURE: float = 0.2
_LLM_MAX_TOKENS: int = 1000

# Maximum characters of chunk text included in the LLM prompt.
_MAX_CONTEXT_CHARS: int = 3000

# JSON fence pattern for response parsing.
_JSON_FENCE_RE: re.Pattern[str] = re.compile(
    r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE
)


class RAGAgent:
    """
    RAG Agent — retrieval-augmented investigation for unstructured knowledge.

    Formulates a query from the event, retrieves semantically similar chunks
    from the vector store, assembles a strictly templated prompt, calls the
    LLM, validates the structured response, and returns findings.

    This agent:
    - Makes no autonomous decisions (STOP / CONTINUE / ESCALATE).
    - Writes nothing to any database.
    - Calls no external APIs.
    - Does not modify the Event object.
    - Updates ShortTermMemory through its return structure only.

    Args:
        retrieval_pipeline: Online retrieval pipeline (Qdrant + embedding).
        validator:          LLM output validator for grounding compliance.
        llm_service:        Local LLM HTTP client (must implement the updated
                            LLMService protocol with temperature and max_tokens
                            parameters, e.g. ``query(prompt, *, temperature, max_tokens)``).
        top_k:              Max chunks to retrieve per query. Defaults to 5.

    Example::

        agent = RAGAgent(
            retrieval_pipeline=retrieval_pipeline,
            validator=RAGResponseValidator(),
            llm_service=llm_service,
        )
        result = agent.investigate(event=event, memory=stm)
        # result["findings"], result["confidence"], result["memory_updates"]
    """

    def __init__(
        self,
        retrieval_pipeline: RetrievalPipeline,
        validator: RAGResponseValidator,
        llm_service: Any,
        top_k: int = 5,
    ) -> None:
        """
        Initialise the RAG Agent with injected dependencies.

        Args:
            retrieval_pipeline: Configured retrieval pipeline.
            validator:          Response validator instance.
            llm_service:        LLM service satisfying the LLMService protocol.
                                Must expose ``query(prompt: str, *, temperature: float, max_tokens: int) -> str``.
            top_k:              Max retrieved chunks. Must be >= 1.

        Raises:
            ValueError: If any required dependency is None or top_k < 1.
        """
        if retrieval_pipeline is None:
            raise ValueError("retrieval_pipeline must not be None.")
        if validator is None:
            raise ValueError("validator must not be None.")
        if llm_service is None:
            raise ValueError("llm_service must not be None.")
        if top_k < 1:
            raise ValueError(f"top_k must be >= 1. Got: {top_k}.")

        self._retrieval = retrieval_pipeline
        self._validator = validator
        self._llm = llm_service
        self._top_k = top_k

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def investigate(
        self,
        event: Event,
        memory: ShortTermMemory,
    ) -> dict[str, Any]:
        """
        Run the full RAG investigation pipeline for ``event``.

        Steps:
        1. Formulate a search query from ``event.event_type``,
           ``event.metric``, and ``event.metadata``.
        2. Retrieve similar chunks via RetrievalPipeline.
        3. Assemble a strict prompt from retrieved chunks only.
        4. Call the LLM with temperature=0.2 and max_tokens=1000.
        5. Parse the JSON response.
        6. Validate with RAGResponseValidator.
        7. Return structured findings.

        The Event object is never modified. Memory is not written here —
        ``memory_updates`` in the return dict tells the caller what to write.

        Args:
            event:  The anomaly event under investigation. Read-only.
            memory: The active ShortTermMemory for context (read-only here).

        Returns:
            A dict with the following structure::

                {
                    "findings": {
                        "possible_causes":       list[dict],
                        "overall_confidence":    float,
                        "requires_human_review": bool,
                        "retrieved_count":       int,
                        "validation_passed":     bool,
                        "raw_llm_response":      str | None,
                    },
                    "confidence":     float,
                    "memory_updates": {
                        "rag_findings": dict,
                        "hypotheses":   list[str],
                        "confidence":   float,
                    },
                }

            On retrieval failure, LLM failure, or validation failure, the
            dict still contains all keys with safe default values and an
            ``"error"`` key describing the failure.

        Note:
            This method catches all internal errors and surfaces them in the
            return dict rather than raising, ensuring the Orchestrator loop
            is never interrupted by a RAG failure.
        """
        logger.info(
            "RAGAgent.investigate | event_id=%s | metric=%s | severity=%s",
            event.event_id, event.metric, event.severity,
        )

        # Step 1: formulate query.
        query = self._formulate_query(event)
        logger.debug("RAGAgent | query=%r", query)

        # Step 2: retrieve chunks.
        try:
            chunks = self._retrieval.search(query=query, top_k=self._top_k)
        except Exception as exc:  # noqa: BLE001
            logger.error("RAGAgent | retrieval failed: %s", exc)
            return self._error_result(f"Retrieval failed: {exc}")

        # 🔐 Defensive clamp – ensure we never exceed top_k.
        chunks = chunks[:self._top_k]

        logger.info(
            "RAGAgent | retrieved %d chunks for event_id=%s",
            len(chunks), event.event_id,
        )

        if not chunks:
            logger.info("RAGAgent | no relevant chunks found; skipping LLM.")
            return self._no_context_result()

        # Step 3: assemble prompt.
        prompt = self._assemble_prompt(event=event, chunks=chunks, memory=memory)

        # Step 4: call LLM with explicit temperature and max_tokens.
        raw_response: str | None = None
        try:
            raw_response = self._llm.query(
                prompt,
                temperature=_LLM_TEMPERATURE,
                max_tokens=_LLM_MAX_TOKENS,
            )
            logger.debug("RAGAgent | LLM response length=%d", len(raw_response))
        except Exception as exc:  # noqa: BLE001
            logger.error("RAGAgent | LLM call failed: %s", exc)
            return self._error_result(f"LLM call failed: {exc}")

        # Step 5: parse JSON response.
        parsed: dict[str, Any] | None = self._parse_json(raw_response)
        if parsed is None:
            logger.warning("RAGAgent | could not parse LLM response as JSON.")
            return self._error_result(
                "LLM response could not be parsed as JSON.",
                raw_response=raw_response,
            )

        # Step 6: validate.
        valid, reason = self._validator.validate(
            output=parsed,
            retrieved_chunks=chunks,
        )
        if not valid:
            logger.warning(
                "RAGAgent | validation failed: %s | event_id=%s",
                reason, event.event_id,
            )
            return self._error_result(
                f"Validation failed: {reason}",
                raw_response=raw_response,
            )

        # Step 7: assemble return structure.
        overall_confidence: float = float(parsed.get("overall_confidence", 0.0))
        possible_causes: list[dict[str, Any]] = parsed.get("possible_causes", [])
        requires_human: bool = bool(parsed.get("requires_human_review", False))

        findings: dict[str, Any] = {
            "possible_causes":       possible_causes,
            "overall_confidence":    overall_confidence,
            "requires_human_review": requires_human,
            "retrieved_count":       len(chunks),
            "validation_passed":     True,
            "raw_llm_response":      raw_response,
        }

        hypotheses: list[str] = [
            c.get("cause", "") for c in possible_causes if c.get("cause")
        ]

        memory_updates: dict[str, Any] = {
            "rag_findings": findings,
            "hypotheses":   hypotheses,
            "confidence":   overall_confidence,
        }

        logger.info(
            "RAGAgent.investigate complete | confidence=%.2f | causes=%d | "
            "requires_human=%s | event_id=%s",
            overall_confidence, len(possible_causes),
            requires_human, event.event_id,
        )

        return {
            "findings":       findings,
            "confidence":     overall_confidence,
            "memory_updates": memory_updates,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _formulate_query(event: Event) -> str:
        """
        Build a natural-language search query from event fields.

        Uses ``event.event_type``, ``event.metric``, ``event.severity``, and
        selected keys from ``event.metadata`` (region, metric, host).
        The Event object is not modified.

        Args:
            event: The event under investigation.

        Returns:
            A descriptive query string for the retrieval pipeline.
        """
        parts: list[str] = [
            f"event type: {event.event_type}",
            f"metric: {event.metric}",
            f"severity: {event.severity}",
        ]

        meta = event.metadata or {}
        for field in ("region", "metric", "host", "service"):
            if field in meta:
                parts.append(f"{field}: {meta[field]}")

        if event.current_value is not None:
            parts.append(f"current value: {event.current_value}")
        if event.expected_value is not None:
            parts.append(f"expected value: {event.expected_value}")

        return " | ".join(parts)

    @staticmethod
    def _assemble_prompt(
        event: Event,
        chunks: list[dict[str, Any]],
        memory: ShortTermMemory,
    ) -> str:
        """
        Assemble a strictly templated LLM prompt from retrieved chunks only.

        The prompt:
        - Instructs the LLM to use ONLY the provided context.
        - Requires every cause to reference a chunk_id from the context.
        - Requires JSON-only output matching the exact response schema.
        - Forbids external knowledge and hallucinated sources.

        Args:
            event:  The event under investigation (read-only).
            chunks: Retrieved context chunks.
            memory: ShortTermMemory for depth and prior hypotheses (read-only).

        Returns:
            Fully formatted prompt string.
        """
        context_lines: list[str] = []
        total_chars = 0

        for i, chunk in enumerate(chunks, start=1):
            chunk_id = chunk.get("chunk_id", f"unknown_{i}")
            text = chunk.get("text", "").strip()
            similarity = chunk.get("similarity", 0.0)
            source = chunk.get("metadata", {}).get("source", "unknown")

            line = (
                f"[{i}] chunk_id={chunk_id!r} | source={source!r} | "
                f"similarity={similarity:.3f}\n    {text[:500]}"
            )

            if total_chars + len(line) > _MAX_CONTEXT_CHARS:
                break

            context_lines.append(line)
            total_chars += len(line)

        context_block = "\n\n".join(context_lines)

        depth = memory.get("investigation_depth", 0) or 0
        existing_hypotheses: list[str] = memory.get("hypotheses") or []
        hypotheses_block = (
            "\n".join(f"  - {h}" for h in existing_hypotheses[:5])
            if existing_hypotheses
            else "  (none yet)"
        )

        return (
            "You are an expert site-reliability engineer performing root cause analysis.\n"
            "You must respond ONLY in valid JSON. Do not include any prose outside the JSON block.\n"
            "You must ONLY use the provided context chunks below. "
            "Do NOT draw on external knowledge or hallucinate sources.\n\n"
            "=== INCIDENT DETAILS ===\n"
            f"Event type:          {event.event_type}\n"
            f"Metric:              {event.metric}\n"
            f"Severity:            {event.severity}\n"
            f"Current value:       {event.current_value}\n"
            f"Expected value:      {event.expected_value}\n"
            f"Detection methods:   {', '.join(event.detection_methods)}\n"
            f"Investigation depth: {depth}\n\n"
            "=== EXISTING HYPOTHESES ===\n"
            f"{hypotheses_block}\n\n"
            "=== RETRIEVED CONTEXT (use ONLY these) ===\n"
            f"{context_block}\n\n"
            "=== REQUIRED JSON RESPONSE SCHEMA ===\n"
            "{\n"
            '  "possible_causes": [\n'
            "    {\n"
            '      "cause": "<concise description of the contributing factor>",\n'
            '      "chunk_id": "<must exactly match one chunk_id listed above>",\n'
            '      "confidence": <float between 0.0 and 1.0>\n'
            "    }\n"
            "  ],\n"
            '  "overall_confidence": <float between 0.0 and 1.0>,\n'
            '  "requires_human_review": <true or false>\n'
            "}\n\n"
            "=== RULES ===\n"
            "1. Every cause MUST use a chunk_id exactly as shown above.\n"
            "2. Do NOT invent chunk_ids or reference external URLs or papers.\n"
            "3. Do NOT include any text outside the JSON object.\n"
            "4. All confidence values must be between 0.0 and 1.0.\n"
            "5. Set requires_human_review to true if confidence < 0.6.\n"
        )

    @staticmethod
    def _parse_json(raw: str) -> dict[str, Any] | None:
        """
        Parse ``raw`` LLM output as JSON using three fallback strategies.

        Strategies (attempted in order):
        1. Direct ``json.loads`` on the stripped string.
        2. Extract from a markdown ``` or ```json fence.
        3. Substring between the first ``{`` and last ``}``.

        Args:
            raw: Raw string returned by the LLM.

        Returns:
            Parsed dict, or ``None`` if all strategies fail.
        """
        if not raw or not raw.strip():
            return None

        # Strategy 1: direct parse.
        try:
            result = json.loads(raw.strip())
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass

        # Strategy 2: markdown fence.
        fence_match = _JSON_FENCE_RE.search(raw)
        if fence_match:
            try:
                result = json.loads(fence_match.group(1).strip())
                if isinstance(result, dict):
                    return result
            except json.JSONDecodeError:
                pass

        # Strategy 3: brace substring.
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end > start:
            try:
                result = json.loads(raw[start: end + 1])
                if isinstance(result, dict):
                    return result
            except json.JSONDecodeError:
                pass

        return None

    @staticmethod
    def _error_result(
        reason: str,
        raw_response: str | None = None,
    ) -> dict[str, Any]:
        """
        Build a safe error result that matches the full ``investigate()`` schema.

        Args:
            reason:       Human-readable failure description.
            raw_response: Raw LLM response string if available.

        Returns:
            Full return dict with safe defaults.
        """
        findings: dict[str, Any] = {
            "possible_causes":       [],
            "overall_confidence":    0.0,
            "requires_human_review": True,
            "retrieved_count":       0,
            "validation_passed":     False,
            "raw_llm_response":      raw_response,
            "error":                 reason,
        }
        return {
            "findings":       findings,
            "confidence":     0.0,
            "memory_updates": {
                "rag_findings": findings,
                "hypotheses":   [],
                "confidence":   0.0,
            },
        }

    @staticmethod
    def _no_context_result() -> dict[str, Any]:
        """
        Build a result dict for the case where no chunks were retrieved.

        Returns:
            Full return dict with requires_human_review=True and zero confidence.
        """
        findings: dict[str, Any] = {
            "possible_causes":       [],
            "overall_confidence":    0.0,
            "requires_human_review": True,
            "retrieved_count":       0,
            "validation_passed":     False,
            "raw_llm_response":      None,
            "error":                 "No relevant context retrieved from vector store.",
        }
        return {
            "findings":       findings,
            "confidence":     0.0,
            "memory_updates": {
                "rag_findings": findings,
                "hypotheses":   [],
                "confidence":   0.0,
            },
        }

    def __repr__(self) -> str:
        return (
            f"RAGAgent("
            f"top_k={self._top_k}, "
            f"retrieval={self._retrieval!r})"
        )