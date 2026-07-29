"""
aeam/intelligence/policy_extraction.py

Enterprise Policy Intelligence Engine (Phase C2).

Turns the text of an already-ingested document into structured, traceable
business-policy records — additional structured knowledge extracted FROM the
document, never a replacement for it. Reuses the existing document ingestion
flow entirely:

- Text is already extracted by ``aeam.ingestion.extraction.extract_text``
  before this module ever runs (see ``aeam.ingestion.processor
  .DocumentIngestJobProcessor``, which calls this AFTER the RAG
  ``IngestionPipeline.ingest_document`` call succeeds).
- The SAME :class:`~aeam.agents.rag.chunking.TextChunker` configuration the
  RAG ``IngestionPipeline`` uses internally (``chunk_size=300, overlap=50,
  strategy="sentence"``) is reused here — read-only, purely to recover which
  chunk (by position, matching ``IngestionPipeline.ingest_document``'s
  returned ``chunk_ids``, which are produced in the same chunk order) a
  policy's source text came from. No new chunking algorithm, no second
  chunking of the document for retrieval purposes, no Qdrant writes.
- The SAME :class:`~aeam.services.llm_service.LLMService` and
  :func:`~aeam.agents.rag.rag_agent.parse_llm_json` already used by
  RAGAgent/DecisionEngine are reused for the extraction call and its
  tolerant JSON parsing — no second LLM client, no second JSON-parsing
  strategy.

This module performs NO database writes, NO Qdrant calls, and NO RuleEngine
integration — it is a pure function of (text, chunk_ids) -> list[dict].
Persistence is the caller's responsibility (see
``aeam.ingestion.processor.DocumentIngestJobProcessor``, which writes results
via the existing repository pattern, exactly like it already does for
Document/Version rows).

Honesty contract: only policies explicitly stated in the source text are
returned. A document with no recognizable policy yields an empty list —
never a fabricated "default" policy. A field the source text doesn't specify
is simply absent from the result dict, never guessed or defaulted.
"""

from __future__ import annotations

import difflib
import logging
from typing import Any

from aeam.agents.rag.chunking import TextChunker
from aeam.agents.rag.rag_agent import parse_llm_json
from aeam.security.llm_guardrails import sanitize_input, validate_output
from aeam.services.llm_service import LLMService

logger = logging.getLogger(__name__)

#: Matches the RAG IngestionPipeline's own default chunker configuration
#: exactly (see aeam.agents.rag.ingestion_pipeline.IngestionPipeline), so
#: chunk boundaries — and therefore chunk_ids by position — line up with
#: what was actually stored in Qdrant for this document.
_CHUNKER_CHUNK_SIZE = 300
_CHUNKER_OVERLAP = 50
_CHUNKER_STRATEGY = "sentence"

#: Cap on how much document text is sent to the LLM per extraction call —
#: a technical prompt-size constraint, not a claim about the document.
_MAX_PROMPT_CHARS = 8000

#: Minimum fuzzy-match ratio (difflib.SequenceMatcher) between a policy's
#: raw_text and a chunk's text before that chunk is accepted as the
#: source_chunk attribution. Below this, source_chunk is left as None —
#: an honest "couldn't confidently attribute" rather than a weak guess.
_CHUNK_MATCH_THRESHOLD = 0.35

_EXTRACTION_PROMPT_TEMPLATE = """You are extracting structured BUSINESS POLICIES from an internal company document.

Only extract a policy if it is EXPLICITLY stated in the text below — a rule of the
form "if X then Y", a threshold, an escalation or approval requirement, or an
explicit department/role responsibility. Do NOT invent, generalize, or infer a
policy that is not directly stated in the text. If the document contains no
recognizable policy, return exactly {{"policies": []}}.

For each policy you find, include ONLY the fields that are actually present in
the text — omit a field entirely (do not guess, default, or leave a placeholder)
if the text does not specify it.

Return STRICT JSON only, no prose, no markdown fence, in exactly this shape:
{{
  "policies": [
    {{
      "raw_text": "<verbatim sentence(s) this policy is based on>",
      "business_rule": "<short human-readable summary>",
      "condition": "<the trigger condition, e.g. 'sales_drop > 30%'>",
      "threshold": "<numeric or qualitative threshold, if any>",
      "actions": ["<action_1>", "<action_2>"],
      "escalation_rule": "<escalation condition/path, if stated>",
      "approval_required": true or false,
      "department": "<department name, if stated>",
      "role": "<responsible role/title, if stated>",
      "time_constraint": "<deadline/SLA/time window, if stated>",
      "priority": "<low|medium|high|critical, only if stated or unambiguously implied>",
      "related_metrics": ["<metric_name>"]
    }}
  ]
}}

DOCUMENT TEXT:
\"\"\"
{text}
\"\"\"
"""

#: Fields the LLM may return per policy, and the shape they must be to survive
#: into the result (kept in sync with `_EXTRACTION_PROMPT_TEMPLATE`'s schema).
_STRING_FIELDS = (
    "raw_text", "business_rule", "condition", "threshold",
    "escalation_rule", "department", "role", "time_constraint", "priority",
)
_LIST_FIELDS = ("actions", "related_metrics")

# ---------------------------------------------------------------------------
# Phase F3 — Tier-3 extraction (tabular / nested-conditional structure)
# ---------------------------------------------------------------------------
#
# Tier-1/2 extraction (above) uses a prompt written for FLAT, sentence-shaped
# policy statements ("if X then Y"). Fed a table — the shape a PDF's
# extracted text degrades tables into most often: rows of short cells with
# no explicit "if/then" language at all — that prompt has no instruction
# telling the model rows belong together, so it either recovers only the
# first row, merges every row into one vague policy, or drops the table
# entirely as "not a recognizable policy". None of that is a prompt BUG; it
# is simply a prompt that was never told tables exist.
#
# Tier-3 is the same LLM boundary, the same guardrails, the same JSON
# parser, and the same chunk-attribution logic — the ONLY difference is a
# prompt that explicitly instructs the model to treat each table row (or
# each branch of a nested if/elif/else) as its OWN policy, linked to its
# siblings by a shared `table_group` id. This is additive: `extract()`
# above is completely unchanged, and a caller who never invokes
# `extract_tabular()` sees byte-identical Tier-1/2 behaviour (COMPAT-2).

_TIER3_EXTRACTION_PROMPT_TEMPLATE = """You are extracting structured BUSINESS POLICIES from an internal company \
document. This text may contain a TABLE (rows of short cells, possibly with \
lost column alignment from PDF extraction) or a NESTED CONDITIONAL \
structure (if / else if / otherwise branches, or severity tiers). Treat \
EACH ROW of a table, and EACH BRANCH of a nested conditional, as its OWN \
separate policy — do NOT merge multiple rows/branches into one policy, and \
do NOT extract only the first row.

Only extract a policy if it is EXPLICITLY stated in the text below. Do NOT \
invent, generalize, or infer a policy that is not directly stated in the \
text. If the document contains no recognizable policy, return exactly \
{{"policies": []}}.

For each policy you find, include ONLY the fields that are actually present \
in the text — omit a field entirely (do not guess, default, or leave a \
placeholder) if the text does not specify it.

Every policy that came from the SAME table or the SAME nested conditional \
block MUST share the identical "table_group" string (e.g. "table_1"), and \
each MUST carry its own "table_row" integer (0-based, in the row/branch \
order it appeared in the text). A policy that did not come from a \
table/nested block must omit both fields.

Return STRICT JSON only, no prose, no markdown fence, in exactly this shape:
{{
  "policies": [
    {{
      "raw_text": "<verbatim row/branch text this policy is based on>",
      "business_rule": "<short human-readable summary>",
      "condition": "<the trigger condition, e.g. 'severity == high'>",
      "threshold": "<numeric or qualitative threshold, if any>",
      "actions": ["<action_1>", "<action_2>"],
      "escalation_rule": "<escalation condition/path, if stated>",
      "approval_required": true or false,
      "department": "<department name, if stated>",
      "role": "<responsible role/title, if stated>",
      "time_constraint": "<deadline/SLA/time window, if stated>",
      "priority": "<low|medium|high|critical, only if stated or unambiguously implied>",
      "related_metrics": ["<metric_name>"],
      "table_group": "<shared id for sibling rows/branches, omit if not applicable>",
      "table_row": <0-based row/branch index, omit if not applicable>
    }}
  ]
}}

DOCUMENT TEXT:
\"\"\"
{text}
\"\"\"
"""

#: Additive to `_STRING_FIELDS`/`_LIST_FIELDS` — only ever populated by
#: `extract_tabular()`; `extract()` never emits these keys.
_TIER3_INT_FIELDS = ("table_row",)
_TIER3_STRING_FIELDS = ("table_group",)


class PolicyExtractionError(Exception):
    """Raised only for a structural misuse of this module (e.g. no text)."""


class PolicyExtractor:
    """
    Extracts structured business policies from document text via the
    existing LLM service, and attributes each one back to a source chunk
    via the existing chunking class (read-only, no re-embedding).

    Args:
        llm_service: The already-constructed, shared
                     :class:`~aeam.services.llm_service.LLMService` instance
                     (same one RAGAgent/DecisionEngine use — no second LLM
                     client is created here).
        chunker:     Optional :class:`TextChunker` override, e.g. for tests.
                     Defaults to the exact configuration
                     ``IngestionPipeline`` itself uses, so chunk boundaries
                     match what's actually in Qdrant.
    """

    def __init__(self, llm_service: LLMService, chunker: TextChunker | None = None) -> None:
        if llm_service is None:
            raise ValueError("llm_service must not be None.")
        self._llm = llm_service
        self._chunker = chunker or TextChunker(
            chunk_size=_CHUNKER_CHUNK_SIZE, overlap=_CHUNKER_OVERLAP, strategy=_CHUNKER_STRATEGY,
        )

    def extract(
        self,
        text: str,
        chunk_ids: list[str] | None = None,
        chunk_metadata: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Extract structured policies from ``text``.

        Args:
            text:           The document's full extracted text (same text
                            already passed to
                            ``IngestionPipeline.ingest_document``).
            chunk_ids:      The Qdrant point ids
                            ``IngestionPipeline.ingest_document`` returned
                            for this document, in chunk order. Used ONLY to
                            attribute each policy to a ``source_chunk`` —
                            pass ``None``/``[]`` to skip attribution (every
                            policy's ``source_chunk`` will be ``None``).
            chunk_metadata: The SAME metadata dict passed to
                            ``ingest_document`` — reused here (unmodified)
                            only so this module's own read-only re-chunking
                            call produces byte-identical chunk boundaries.

        Returns:
            List of policy dicts. Empty list if ``text`` is blank, the LLM
            found nothing, or the LLM response could not be parsed as JSON
            (logged, never raised, never fabricated).
        """
        if not text or not text.strip():
            return []

        # Phase E8 (AI-1): this is THE canonical injection surface named by
        # the roadmap — the full extracted text of an uploaded document,
        # completely untrusted. sanitize_input runs before truncation so an
        # injection phrase cannot dodge detection by falling exactly on the
        # truncation boundary.
        sanitized_text = sanitize_input(text.strip())
        prompt = _EXTRACTION_PROMPT_TEMPLATE.format(text=sanitized_text[:_MAX_PROMPT_CHARS])

        try:
            raw_response = self._llm.query(prompt, temperature=0.0, max_tokens=1500)
        except Exception as exc:  # noqa: BLE001
            logger.error("PolicyExtractor | LLM call failed: %s", exc)
            return []

        # Phase E8 (AI-1): validate_output before this text is parsed,
        # persisted to the policies table, or displayed in the Knowledge
        # Center UI. A response matching a sensitive-data pattern is
        # rejected exactly like an unparsable one — never fabricated,
        # never silently passed through.
        if not validate_output(raw_response):
            logger.warning(
                "PolicyExtractor | LLM output failed validate_output "
                "(sensitive pattern detected) — rejecting extraction."
            )
            return []

        parsed = parse_llm_json(raw_response)
        if not isinstance(parsed, dict):
            logger.warning("PolicyExtractor | LLM response could not be parsed as JSON.")
            return []

        raw_policies = parsed.get("policies")
        if not isinstance(raw_policies, list):
            return []

        chunks = self._rechunk_for_attribution(text, chunk_metadata) if chunk_ids else []

        results: list[dict[str, Any]] = []
        for item in raw_policies:
            if not isinstance(item, dict):
                continue
            policy = _sanitize_policy(item)
            if policy is None:
                continue  # genuinely empty/unusable entry — never fabricate a placeholder
            policy["source_chunk"] = _attribute_chunk(policy.get("raw_text"), chunks, chunk_ids)
            results.append(policy)

        logger.info("PolicyExtractor | extracted %d polic%s", len(results), "y" if len(results) == 1 else "ies")
        return results

    def extract_tabular(
        self,
        text: str,
        chunk_ids: list[str] | None = None,
        chunk_metadata: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Tier-3 extraction: recover tabular / nested-conditional policy
        structure that :meth:`extract`'s flat prompt misses.

        Identical contract to :meth:`extract` in every respect — same
        sanitize/validate/parse pipeline, same chunk attribution, same
        empty-list-on-nothing-found honesty — the ONLY difference is the
        prompt (see :data:`_TIER3_EXTRACTION_PROMPT_TEMPLATE`), which
        explicitly instructs the model to treat each table row or
        conditional branch as its own policy rather than merging them.

        Results additionally carry ``table_group``/``table_row`` when the
        source text was recognised as a table/nested-conditional block —
        both fields are simply absent (never fabricated) for a policy that
        was not part of one.

        Args:
            text:           Same as :meth:`extract`.
            chunk_ids:      Same as :meth:`extract`.
            chunk_metadata: Same as :meth:`extract`.

        Returns:
            List of policy dicts, exactly as :meth:`extract` returns them,
            with the two additive table fields where applicable.
        """
        if not text or not text.strip():
            return []

        # Same E8 (AI-1) injection-surface handling as extract().
        sanitized_text = sanitize_input(text.strip())
        prompt = _TIER3_EXTRACTION_PROMPT_TEMPLATE.format(text=sanitized_text[:_MAX_PROMPT_CHARS])

        try:
            raw_response = self._llm.query(prompt, temperature=0.0, max_tokens=2000)
        except Exception as exc:  # noqa: BLE001
            logger.error("PolicyExtractor.extract_tabular | LLM call failed: %s", exc)
            return []

        if not validate_output(raw_response):
            logger.warning(
                "PolicyExtractor.extract_tabular | LLM output failed validate_output "
                "(sensitive pattern detected) — rejecting extraction."
            )
            return []

        parsed = parse_llm_json(raw_response)
        if not isinstance(parsed, dict):
            logger.warning("PolicyExtractor.extract_tabular | LLM response could not be parsed as JSON.")
            return []

        raw_policies = parsed.get("policies")
        if not isinstance(raw_policies, list):
            return []

        chunks = self._rechunk_for_attribution(text, chunk_metadata) if chunk_ids else []

        results: list[dict[str, Any]] = []
        for item in raw_policies:
            if not isinstance(item, dict):
                continue
            policy = _sanitize_policy(
                item,
                extra_string_fields=_TIER3_STRING_FIELDS,
                extra_int_fields=_TIER3_INT_FIELDS,
            )
            if policy is None:
                continue
            policy["source_chunk"] = _attribute_chunk(policy.get("raw_text"), chunks, chunk_ids)
            results.append(policy)

        logger.info(
            "PolicyExtractor.extract_tabular | extracted %d polic%s (tier-3)",
            len(results), "y" if len(results) == 1 else "ies",
        )
        return results

    def _rechunk_for_attribution(
        self, text: str, chunk_metadata: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        """
        Re-run the SAME TextChunker configuration IngestionPipeline used
        internally, purely to recover chunk TEXT for source attribution —
        this text is discarded immediately after; nothing is re-embedded or
        re-written to Qdrant.
        """
        try:
            return self._chunker.chunk_text(text=text, metadata=chunk_metadata or {})
        except Exception as exc:  # noqa: BLE001
            logger.warning("PolicyExtractor | re-chunk for attribution failed: %s", exc)
            return []


def _sanitize_policy(
    item: dict[str, Any],
    extra_string_fields: tuple[str, ...] = (),
    extra_int_fields: tuple[str, ...] = (),
) -> dict[str, Any] | None:
    """
    Keep only the recognised fields, coerce them to the right shape, and
    drop the entry entirely if it carries no genuine content (defends
    against a stray near-empty object in the LLM's response — never turned
    into a fabricated "policy" with no actual rule in it).

    Args:
        item: One raw policy dict from the LLM's parsed JSON.
        extra_string_fields: Phase F3 additive — extra string keys to keep
                             beyond `_STRING_FIELDS` (e.g. ``"table_group"``).
                             Empty by default, so `extract()`'s Tier-1/2
                             behaviour is exactly what it always was.
        extra_int_fields: Phase F3 additive — extra integer keys to keep
                          (e.g. ``"table_row"``). Same default-empty
                          contract as above.
    """
    policy: dict[str, Any] = {}

    for key in _STRING_FIELDS + extra_string_fields:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            policy[key] = value.strip()

    for key in extra_int_fields:
        value = item.get(key)
        if isinstance(value, bool):
            continue  # bool is an int subclass in Python; never mistaken for a row index
        if isinstance(value, int):
            policy[key] = value
        elif isinstance(value, str) and value.strip().lstrip("-").isdigit():
            policy[key] = int(value.strip())

    for key in _LIST_FIELDS:
        value = item.get(key)
        if isinstance(value, list):
            cleaned = [str(v).strip() for v in value if str(v).strip()]
            if cleaned:
                policy[key] = cleaned

    approval = item.get("approval_required")
    if isinstance(approval, bool):
        policy["approval_required"] = approval

    has_rule_content = any(
        policy.get(k) for k in ("condition", "actions", "business_rule", "escalation_rule")
    )
    if not has_rule_content:
        return None

    return policy


def _attribute_chunk(
    raw_text: str | None, chunks: list[dict[str, Any]], chunk_ids: list[str] | None,
) -> str | None:
    """
    Find which chunk (by position, matching ``chunk_ids``' order) a policy's
    ``raw_text`` most plausibly came from, via fuzzy string matching. Returns
    ``None`` (never a weak/fabricated guess) if there's no confident match.
    """
    if not raw_text or not chunks or not chunk_ids:
        return None

    best_ratio = 0.0
    best_index: int | None = None
    needle = raw_text.lower()

    for i, chunk in enumerate(chunks):
        if i >= len(chunk_ids):
            break
        haystack = str(chunk.get("text", "")).lower()
        if not haystack:
            continue
        if needle in haystack or haystack in needle:
            return chunk_ids[i]
        ratio = difflib.SequenceMatcher(None, needle, haystack).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_index = i

    if best_index is not None and best_ratio >= _CHUNK_MATCH_THRESHOLD:
        return chunk_ids[best_index]
    return None
