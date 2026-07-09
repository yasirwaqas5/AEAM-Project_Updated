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

# Maximum number of DISTINCT query variants to try before giving up rather
# than repeating an identical search. Query rewriting is fully deterministic
# (no LLM, no hallucination) — see _formulate_query_variant().
_MAX_QUERY_ATTEMPTS: int = 3

_QUERY_STRATEGY_NAMES: dict[int, str] = {
    1: "original",
    2: "rewritten",
    3: "broadened",
}

# JSON fence pattern for response parsing.
_JSON_FENCE_RE: re.Pattern[str] = re.compile(
    r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE
)

# Natural-language descriptions for each internal event-type enum code.
# Used by _formulate_query() to produce semantically rich SRE investigation
# queries that align with the vocabulary used in the knowledge base.
# Unknown event types fall back to a cleaned version of the enum string.
_EVENT_TYPE_NL: dict[str, str] = {
    "DB_LATENCY":         "database latency slow query performance lock contention replication lag",
    "SALES_DROP":         "sales drop revenue decline checkout failure payment gateway anomaly",
    "SALES_SPIKE":        "sales spike unexpected revenue surge anomaly",
    "KPI_ANOMALY":        "KPI anomaly metric deviation performance regression threshold breach",
    "CPU_HIGH":           "CPU saturation high utilization runaway process resource exhaustion",
    "MEMORY_HIGH":        "memory exhaustion high usage OOM kill application crash",
    "DISK_IO":            "disk IO saturation high await latency read write",
    "NETWORK_ERROR":      "network failure latency packet loss connectivity",
    "ERROR_RATE":         "error rate spike service failure",
    "LATENCY_HIGH":       "high latency response time degradation API slowdown backend",
    "CACHE_MISS":         "cache failure miss rate elevated Redis eviction",
    "QUEUE_BACKLOG":      "queue backlog consumer lag processing delay",
    "DEPLOYMENT_FAILURE": "deployment failure rollback health check crash",
    "AUTH_FAILURE":       "authentication failure login error token rejection",
}

# Deterministic, hand-curated BROAD phrases used only for query attempt 3
# (the widest-net search). Intentionally short (2-3 words) — short queries
# score measurably higher on cosine similarity against this corpus than the
# long, keyword-stuffed descriptions in _EVENT_TYPE_NL (see retrieval
# threshold benchmark). This is a static lookup table, not an LLM call — the
# rewrite is fully deterministic and never hallucinates new vocabulary.
_EVENT_TYPE_BROAD: dict[str, str] = {
    "DB_LATENCY":         "database performance",
    "SALES_DROP":         "sales revenue",
    "SALES_SPIKE":        "sales revenue",
    "KPI_ANOMALY":        "performance anomaly",
    "CPU_HIGH":           "resource exhaustion",
    "MEMORY_HIGH":        "resource exhaustion",
    "DISK_IO":            "resource exhaustion",
    "NETWORK_ERROR":      "connectivity failure",
    "ERROR_RATE":         "service failure",
    "LATENCY_HIGH":       "performance degradation",
    "CACHE_MISS":         "cache failure",
    "QUEUE_BACKLOG":      "processing delay",
    "DEPLOYMENT_FAILURE": "deployment failure",
    "AUTH_FAILURE":       "authentication failure",
}

_METADATA_QUERY_LABELS: dict[str, str] = {
    "service": "service",
    "service_name": "service",
    "application": "application",
    "app": "application",
    "component": "component",
    "host": "host",
    "hostname": "host",
    "instance": "instance",
    "pod": "pod",
    "namespace": "namespace",
    "cluster": "cluster",
    "region": "region",
    "zone": "zone",
    "environment": "environment",
    "team": "team",
    "database": "database",
    "db": "database",
    "queue": "queue",
    "topic": "topic",
    "endpoint": "endpoint",
    "path": "path",
}

_METADATA_QUERY_ORDER: tuple[str, ...] = (
    "service",
    "service_name",
    "application",
    "app",
    "component",
    "host",
    "hostname",
    "instance",
    "pod",
    "namespace",
    "cluster",
    "region",
    "zone",
    "environment",
    "team",
    "database",
    "db",
    "queue",
    "topic",
    "endpoint",
    "path",
)

_METADATA_QUERY_IGNORED: frozenset[str] = frozenset({
    "event_id",
    "request_id",
    "trace_id",
    "span_id",
    "timestamp",
})

_QUERY_TOKEN_RE: re.Pattern[str] = re.compile(r"[_\-/]+")
_QUERY_CAMEL_RE: re.Pattern[str] = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

# ---------------------------------------------------------------------------
# Resilient LLM JSON parsing
# ---------------------------------------------------------------------------
#
# Shared by RAGAgent.investigate() and Orchestrator.investigate() (depth>=3
# LLM reasoning) — one parser, reused, instead of two divergent ad-hoc ones.

_SMART_QUOTES_TABLE: dict[int, str] = str.maketrans({
    "“": '"', "”": '"',  # “ ”
    "‘": "'", "’": "'",  # ‘ ’
})
_TRAILING_COMMA_RE: re.Pattern[str] = re.compile(r",(\s*[}\]])")
_PY_TRUE_RE: re.Pattern[str] = re.compile(r"\bTrue\b")
_PY_FALSE_RE: re.Pattern[str] = re.compile(r"\bFalse\b")
_PY_NONE_RE: re.Pattern[str] = re.compile(r"\bNone\b")


def _sanitize_json_candidate(text: str) -> str:
    """
    Best-effort fix-up for minor, recoverable LLM JSON formatting mistakes.

    Does NOT attempt to fix structural problems (missing braces, truncated
    output) — only cosmetic deviations from strict JSON that a real model
    commonly produces:
    - Smart/curly quotes copied from a markdown render.
    - Trailing commas before a closing ``}``/``]``.
    - Python literals (``True``/``False``/``None``) instead of JSON's
      (``true``/``false``/``null``).
    """
    text = text.translate(_SMART_QUOTES_TABLE)
    text = _TRAILING_COMMA_RE.sub(r"\1", text)
    text = _PY_TRUE_RE.sub("true", text)
    text = _PY_FALSE_RE.sub("false", text)
    text = _PY_NONE_RE.sub("null", text)
    return text


def _try_parse_dict(candidate: str) -> dict[str, Any] | None:
    """Try strict ``json.loads`` on ``candidate``, then again after sanitizing."""
    for text in (candidate, _sanitize_json_candidate(candidate)):
        try:
            result = json.loads(text.strip())
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            continue
    return None


def parse_llm_json(raw: str) -> dict[str, Any] | None:
    """
    Parse ``raw`` LLM output as a JSON object, tolerating common formatting
    slips instead of only strict JSON.

    Strategies (attempted in order, each tried strict-then-sanitized):
    1. Direct parse of the full stripped string.
    2. Extract from a markdown ``` or ```json fence (leading explanations
       and trailing text outside the fence are discarded).
    3. Substring between the first ``{`` and last ``}`` (handles leading
       explanations / trailing text with no fence at all).

    Never raises and never fabricates a result — returns ``None`` when every
    strategy genuinely fails, so callers can surface a structured error
    instead of silently hallucinating field values.

    Args:
        raw: Raw string returned by the LLM.

    Returns:
        Parsed dict, or ``None`` if all strategies fail.
    """
    if not raw or not raw.strip():
        return None

    # Strategy 1: direct parse.
    result = _try_parse_dict(raw)
    if result is not None:
        return result

    # Strategy 2: markdown fence.
    fence_match = _JSON_FENCE_RE.search(raw)
    if fence_match:
        result = _try_parse_dict(fence_match.group(1))
        if result is not None:
            return result

    # Strategy 3: brace substring.
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end > start:
        result = _try_parse_dict(raw[start: end + 1])
        if result is not None:
            return result

    return None


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

        # Step 0: check whether all deterministic query variants are already
        # exhausted (every prior attempt this incident returned 0 chunks).
        # This is the adaptive-loop fix: rather than repeating an identical
        # search at every remaining investigation depth, RAG becomes a no-op
        # once it has genuinely tried every variant, and the orchestrator's
        # existing depth/evaluation loop proceeds to escalate on schedule.
        prior_attempts = self._extract_prior_rag_attempts(memory)
        if (
            len(prior_attempts) >= _MAX_QUERY_ATTEMPTS
            and all(a.get("retrieved_count", 0) == 0 for a in prior_attempts)
        ):
            logger.info(
                "RAGAgent | query variants exhausted (%d attempts, 0 chunks each) — "
                "skipping repeat search | event_id=%s",
                len(prior_attempts), event.event_id,
            )
            return self._exhausted_result(prior_attempts)

        # Step 1: formulate this attempt's query (deterministic rewrite/broaden).
        attempt: int = min(len(prior_attempts) + 1, _MAX_QUERY_ATTEMPTS)
        query, strategy = self._formulate_query_variant(event, attempt)
        threshold: float = getattr(self._retrieval, "similarity_threshold", None)
        logger.debug(
            "RAGAgent | attempt=%d | strategy=%s | query=%r", attempt, strategy, query,
        )

        # Step 2: retrieve chunks.
        try:
            chunks = self._retrieval.search(query=query, top_k=self._top_k)
        except Exception as exc:  # noqa: BLE001
            logger.error("RAGAgent | retrieval failed: %s", exc)
            return self._error_result(
                f"Retrieval failed: {exc}",
                query=query, attempt=attempt, strategy=strategy, threshold=threshold,
            )

        # 🔐 Defensive clamp – ensure we never exceed top_k.
        chunks = chunks[:self._top_k]

        logger.info(
            "RAGAgent | retrieved %d chunks for event_id=%s | attempt=%d | strategy=%s",
            len(chunks), event.event_id, attempt, strategy,
        )

        if not chunks:
            logger.info("RAGAgent | no relevant chunks found; skipping LLM.")
            return self._no_context_result(
                query=query, attempt=attempt, strategy=strategy, threshold=threshold,
            )

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
            return self._error_result(
                f"LLM call failed: {exc}",
                query=query, attempt=attempt, strategy=strategy, threshold=threshold,
            )

        # Step 5: parse JSON response.
        parsed: dict[str, Any] | None = parse_llm_json(raw_response)
        if parsed is None:
            logger.warning("RAGAgent | could not parse LLM response as JSON.")
            return self._error_result(
                "LLM response could not be parsed as JSON.",
                raw_response=raw_response,
                query=query, attempt=attempt, strategy=strategy, threshold=threshold,
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
                query=query, attempt=attempt, strategy=strategy, threshold=threshold,
            )

        # Step 7: assemble return structure.
        overall_confidence: float = float(parsed.get("overall_confidence", 0.0))
        possible_causes: list[dict[str, Any]] = parsed.get("possible_causes", [])
        requires_human: bool = bool(parsed.get("requires_human_review", False))

        cited_chunk_ids = {c.get("chunk_id") for c in possible_causes if c.get("cause")}
        retrieved_chunks_meta: list[dict[str, Any]] = [
            {
                "chunk_id":     c.get("chunk_id"),
                "similarity":   c.get("similarity"),
                "source":       c.get("metadata", {}).get("source", "unknown"),
                "text_preview": (c.get("text", "") or "")[:160],
                "cited":        c.get("chunk_id") in cited_chunk_ids,
            }
            for c in chunks
        ]

        findings: dict[str, Any] = {
            "possible_causes":       possible_causes,
            "overall_confidence":    overall_confidence,
            "requires_human_review": requires_human,
            "retrieved_count":       len(chunks),
            "validation_passed":     True,
            "raw_llm_response":      raw_response,
            "query":                 query,
            "query_attempt":         attempt,
            "query_strategy":        strategy,
            "threshold":             threshold,
            "retrieved_chunks":      retrieved_chunks_meta,
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
        Build a natural-language SRE investigation query from event fields.

        Converts the internal event-type enum code to a domain-appropriate
        description using ``_EVENT_TYPE_NL``, then appends the metric name
        (underscores replaced with spaces) and any service/host context
        present in ``event.metadata``.

        This produces queries whose vocabulary is semantically aligned with
        the SRE runbook content indexed in the vector store, significantly
        improving cosine similarity scores compared to the previous
        pipe-delimited key=value format.

        Unknown event types fall back to a cleaned version of the enum
        string (underscores → spaces, lower-cased) so the method never
        returns an empty query.

        Args:
            event: The event under investigation.

        Returns:
            A concise natural-language query string for the retrieval pipeline.
        """
        event_desc: str = _EVENT_TYPE_NL.get(
            event.event_type,
            event.event_type.replace("_", " ").lower(),
        )

        metric_nl: str = RAGAgent._normalise_query_fragment(event.metric)
        parts: list[str] = [event_desc]

        if metric_nl:
            parts.append(metric_nl)

        parts.extend(RAGAgent._metadata_query_fragments(event.metadata or {}))

        return " ".join(parts)

    @staticmethod
    def _normalise_query_fragment(value: Any) -> str:
        """Convert internal identifiers into compact natural-language text."""
        text = str(value).strip()
        if not text:
            return ""

        text = _QUERY_CAMEL_RE.sub(" ", text)
        text = _QUERY_TOKEN_RE.sub(" ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    @staticmethod
    def _metadata_query_fragments(metadata: dict[str, Any]) -> list[str]:
        """Extract runbook-aligned context fragments from event metadata."""
        fragments: list[str] = []
        used_keys: set[str] = set()

        for key in _METADATA_QUERY_ORDER:
            if key not in metadata:
                continue

            value = metadata.get(key)
            if value in (None, ""):
                continue

            label = _METADATA_QUERY_LABELS.get(key, key)
            value_nl = RAGAgent._normalise_query_fragment(value)
            if not value_nl:
                continue

            fragments.append(f"{label} {value_nl}")
            used_keys.add(key)

        for key in sorted(metadata):
            if key in used_keys or key in _METADATA_QUERY_IGNORED:
                continue

            value = metadata.get(key)
            if value in (None, ""):
                continue

            key_nl = RAGAgent._normalise_query_fragment(key)
            value_nl = RAGAgent._normalise_query_fragment(value)
            if not key_nl or not value_nl:
                continue

            fragments.append(f"{key_nl} {value_nl}")

        return fragments

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
    def _error_result(
        reason: str,
        raw_response: str | None = None,
        query: str | None = None,
        attempt: int | None = None,
        strategy: str | None = None,
        threshold: float | None = None,
    ) -> dict[str, Any]:
        """
        Build a safe error result that matches the full ``investigate()`` schema.

        Args:
            reason:       Human-readable failure description.
            raw_response: Raw LLM response string if available.
            query:        The query attempted on this pass, if formulated.
            attempt:      Which query attempt (1-3) this was.
            strategy:     Query strategy name ("original"/"rewritten"/"broadened").
            threshold:    Similarity threshold in effect for this pass.

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
            "query":                 query,
            "query_attempt":         attempt,
            "query_strategy":        strategy,
            "threshold":             threshold,
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
    def _no_context_result(
        query: str | None = None,
        attempt: int | None = None,
        strategy: str | None = None,
        threshold: float | None = None,
    ) -> dict[str, Any]:
        """
        Build a result dict for the case where no chunks were retrieved.

        Args:
            query:     The query attempted on this pass.
            attempt:   Which query attempt (1-3) this was.
            strategy:  Query strategy name ("original"/"rewritten"/"broadened").
            threshold: Similarity threshold in effect for this pass.

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
            "query":                 query,
            "query_attempt":         attempt,
            "query_strategy":        strategy,
            "threshold":             threshold,
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
    def _exhausted_result(prior_attempts: list[dict[str, Any]]) -> dict[str, Any]:
        """
        Build a result dict for the case where all query variants have
        already been tried (each returning 0 chunks) — no new search is run.

        Args:
            prior_attempts: The previously attempted
                            ``{"query", "query_strategy", "retrieved_count"}``
                            entries, carried through unchanged for the audit
                            trail.

        Returns:
            Full return dict with ``requires_human_review=True``, zero
            confidence, and an ``error`` explaining exhaustion.
        """
        findings: dict[str, Any] = {
            "possible_causes":       [],
            "overall_confidence":    0.0,
            "requires_human_review": True,
            "retrieved_count":       0,
            "validation_passed":     False,
            "raw_llm_response":      None,
            "error": (
                f"All {len(prior_attempts)} query variants exhausted; "
                "no relevant documents matched."
            ),
            "query":            None,
            "query_attempt":    None,
            "query_strategy":   "exhausted",
            "prior_attempts":   prior_attempts,
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
    def _extract_prior_rag_attempts(memory: ShortTermMemory) -> list[dict[str, Any]]:
        """
        Reconstruct the list of query attempts already made this incident.

        Scans ``memory.get("findings", [])`` for entries the Orchestrator
        appends with ``type == "rag"`` (one per investigation depth this
        agent was invoked) and extracts each pass's query/strategy/retrieval
        count from its nested ``data`` dict (the findings dict this agent
        itself returned on that pass).

        Args:
            memory: Active ShortTermMemory for the current investigation.

        Returns:
            Ordered list of ``{"query", "query_strategy", "retrieved_count"}``
            dicts, oldest first. Empty list if RAG has not run yet this
            incident (or memory has no findings recorded).
        """
        raw_findings = memory.get("findings", []) or []
        attempts: list[dict[str, Any]] = []
        for entry in raw_findings:
            if not isinstance(entry, dict) or entry.get("type") != "rag":
                continue
            data = entry.get("data") or {}
            if not isinstance(data, dict):
                continue
            # Skip an already-exhausted marker so it is never double-counted.
            if data.get("query_strategy") == "exhausted":
                continue
            attempts.append({
                "query":           data.get("query"),
                "query_strategy":  data.get("query_strategy"),
                "retrieved_count": data.get("retrieved_count", 0),
            })
        return attempts

    @staticmethod
    def _formulate_query_variant(event: Event, attempt: int) -> tuple[str, str]:
        """
        Build the query for a given attempt number using a deterministic,
        non-hallucinating rewrite/broaden strategy.

        Strategy by attempt:
        1. ``"original"``  — identical to :meth:`_formulate_query`: event
           description + metric + full metadata context. Most specific.
        2. ``"rewritten"`` — event description + metric only, metadata
           dropped. Shorter queries measurably score higher cosine
           similarity against this corpus than metadata-heavy ones.
        3. ``"broadened"`` — a short, hand-curated 2-3 word category phrase
           from :data:`_EVENT_TYPE_BROAD` only. Widest net.

        All three variants are static lookups/string concatenation — no LLM
        call, no invented vocabulary.

        Args:
            event:   The event under investigation.
            attempt: Attempt number, 1-3 (values outside this range are
                     clamped).

        Returns:
            Tuple of ``(query_string, strategy_name)``.
        """
        attempt = max(1, min(attempt, _MAX_QUERY_ATTEMPTS))
        strategy = _QUERY_STRATEGY_NAMES[attempt]

        if attempt == 1:
            return RAGAgent._formulate_query(event), strategy

        if attempt == 2:
            event_desc = _EVENT_TYPE_NL.get(
                event.event_type,
                event.event_type.replace("_", " ").lower(),
            )
            metric_nl = RAGAgent._normalise_query_fragment(event.metric)
            parts = [event_desc]
            if metric_nl:
                parts.append(metric_nl)
            return " ".join(parts), strategy

        # attempt == 3: broadened.
        broad = _EVENT_TYPE_BROAD.get(
            event.event_type,
            event.event_type.replace("_", " ").lower(),
        )
        return broad, strategy

    def __repr__(self) -> str:
        return (
            f"RAGAgent("
            f"top_k={self._top_k}, "
            f"retrieval={self._retrieval!r})"
        )