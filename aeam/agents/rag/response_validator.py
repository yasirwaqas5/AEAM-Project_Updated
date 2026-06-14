"""
aeam/agents/rag/response_validator.py

LLM output validator for grounding and schema compliance in the AEAM RAG system.

Pure validation logic — no Qdrant, no EmbeddingService, no LLM calls, no DB writes.
Accepts a structured LLM output dict and the retrieved chunks it was generated from,
then returns a (bool, str) verdict.

Validation enforces:
1. Schema compliance  — required keys are present and correctly typed.
2. Grounding         — every cause must reference a chunk_id from retrieved_chunks.
3. Confidence bounds — all confidence values must be in [0, 1].
4. Hallucination rejection — causes that reference IDs not in retrieved_chunks are flagged.
5. No external references — URLs, domain names, and external citations are rejected.
"""

from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Required top-level keys in the LLM output dict.
_REQUIRED_KEYS: frozenset[str] = frozenset({
    "possible_causes",
    "overall_confidence",
    "requires_human_review",
})

# Pattern that matches common external reference indicators.
# Matches: http(s)://, www., bare domain-like strings (e.g. "openai.com").
_EXTERNAL_REF_PATTERN: re.Pattern[str] = re.compile(
    r"(https?://|www\.|[a-zA-Z0-9\-]+\.[a-zA-Z]{2,}/)",
    re.IGNORECASE,
)

# Pattern for detecting external citation markers like "[1]", "(Smith, 2020)".
_CITATION_PATTERN: re.Pattern[str] = re.compile(
    r"(\[\d+\]|\(\w[\w\s,\.]+\d{4}\))",
)


class RAGResponseValidator:
    """
    Validates LLM-generated RAG responses for grounding and schema compliance.

    Ensures that:
    - The output contains all required schema keys.
    - All ``possible_causes`` reference chunk IDs that were actually retrieved
      (grounding check — prevents hallucinated source attribution).
    - All confidence values are numeric and within ``[0, 1]``.
    - No external URLs, domain names, or citation markers appear in cause text
      (no external references).
    - ``requires_human_review`` is a boolean.

    This class is pure validation logic. It imports nothing from Qdrant,
    EmbeddingService, LLM, or any AEAM infrastructure module.

    Example::

        validator = RAGResponseValidator()
        ok, reason = validator.validate(
            output={
                "possible_causes": [
                    {"cause": "Runaway thread", "chunk_id": "a3f...", "confidence": 0.9}
                ],
                "overall_confidence": 0.85,
                "requires_human_review": False,
            },
            retrieved_chunks=[
                {"chunk_id": "a3f...", "text": "...", "metadata": {}, "similarity": 0.91}
            ],
        )
        # (True, "valid")
    """

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def validate(
        self,
        output: dict[str, Any],
        retrieved_chunks: list[dict[str, Any]],
    ) -> tuple[bool, str]:
        """
        Validate ``output`` against ``retrieved_chunks``.

        Runs all validation rules in sequence. Returns on the first failure so
        the caller receives a specific, actionable rejection reason rather than
        a concatenated list of all errors.

        Validation rules (in order):
        1. ``output`` is a non-empty dict.
        2. All required keys are present (``possible_causes``,
           ``overall_confidence``, ``requires_human_review``).
        3. ``possible_causes`` is a non-empty list.
        4. Each cause entry is a dict with ``"cause"``, ``"chunk_id"``,
           and ``"confidence"`` keys.
        5. Each ``"chunk_id"`` in causes matches a chunk_id from
           ``retrieved_chunks`` (grounding / hallucination check).
        6. Each cause ``"confidence"`` is a float in ``[0, 1]``.
        7. ``"overall_confidence"`` is a float in ``[0, 1]``.
        8. ``"requires_human_review"`` is a bool.
        9. No cause ``"cause"`` string contains external URLs, domain
           references, or citation markers.

        Args:
            output:           Structured dict produced by the LLM. Expected
                              to contain ``possible_causes``,
                              ``overall_confidence``, and
                              ``requires_human_review``.
            retrieved_chunks: List of chunk dicts returned by the retrieval
                              pipeline. Each must contain a ``"chunk_id"`` key.
                              May be empty (causes all grounding checks to fail).

        Returns:
            ``(True, "valid")`` if all rules pass.
            ``(False, "<reason>")`` on the first failing rule.

        Example::

            ok, reason = validator.validate(output, retrieved_chunks)
            if not ok:
                logger.warning("LLM output rejected: %s", reason)
        """
        # Rule 1: output must be a non-empty dict.
        if not output or not isinstance(output, dict):
            return False, "output must be a non-empty dict."

        # Rule 2: required keys.
        missing = _REQUIRED_KEYS - set(output.keys())
        if missing:
            return False, (
                f"output is missing required keys: {sorted(missing)}."
            )

        # Rule 3: possible_causes must be a non-empty list.
        causes = output.get("possible_causes")
        if not isinstance(causes, list):
            return False, (
                "'possible_causes' must be a list, got "
                f"{type(causes).__name__!r}."
            )
        if len(causes) == 0:
            return False, "'possible_causes' must not be empty."

        # Build the set of valid chunk IDs from retrieved results.
        valid_chunk_ids: frozenset[str] = frozenset(
            str(c.get("chunk_id", ""))
            for c in retrieved_chunks
            if c.get("chunk_id")
        )

        # Rule 4–9: validate each cause entry.
        for idx, cause_entry in enumerate(causes):
            ok, reason = self._validate_cause_entry(
                cause_entry=cause_entry,
                index=idx,
                valid_chunk_ids=valid_chunk_ids,
            )
            if not ok:
                return False, reason

        # Rule 7: overall_confidence in [0, 1].
        overall_conf = output.get("overall_confidence")
        ok, reason = self._validate_confidence(
            value=overall_conf,
            field="overall_confidence",
        )
        if not ok:
            return False, reason

        # Rule 8: requires_human_review must be a bool.
        rhr = output.get("requires_human_review")
        if not isinstance(rhr, bool):
            return False, (
                "'requires_human_review' must be a bool, got "
                f"{type(rhr).__name__!r}."
            )

        return True, "valid"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _validate_cause_entry(
        self,
        cause_entry: Any,
        index: int,
        valid_chunk_ids: frozenset[str],
    ) -> tuple[bool, str]:
        """
        Validate a single entry from ``possible_causes``.

        Checks applied:
        - Entry is a dict.
        - Contains ``"cause"``, ``"chunk_id"``, and ``"confidence"`` keys.
        - ``"chunk_id"`` is present in ``valid_chunk_ids`` (grounding).
        - ``"confidence"`` is a float in ``[0, 1]``.
        - ``"cause"`` text contains no external references or citation markers.

        Args:
            cause_entry:     The cause dict to validate.
            index:           Zero-based position in ``possible_causes`` list
                             (for error messages).
            valid_chunk_ids: Frozenset of chunk_ids from retrieved chunks.

        Returns:
            ``(True, "valid")`` or ``(False, "<reason>")``.
        """
        prefix = f"possible_causes[{index}]"

        # Rule 4a: must be a dict.
        if not isinstance(cause_entry, dict):
            return False, (
                f"{prefix} must be a dict, got {type(cause_entry).__name__!r}."
            )

        # Rule 4b: required cause keys.
        required_cause_keys = {"cause", "chunk_id", "confidence"}
        missing_cause_keys = required_cause_keys - set(cause_entry.keys())
        if missing_cause_keys:
            return False, (
                f"{prefix} is missing required keys: "
                f"{sorted(missing_cause_keys)}."
            )

        # Rule 5: grounding — chunk_id must reference a retrieved chunk.
        chunk_id = cause_entry.get("chunk_id")
        if not isinstance(chunk_id, str) or not chunk_id.strip():
            return False, (
                f"{prefix}['chunk_id'] must be a non-empty string."
            )

        if not valid_chunk_ids:
            return False, (
                f"{prefix} references chunk_id {chunk_id!r}, but no chunks "
                "were retrieved. Cannot verify grounding."
            )

        if chunk_id not in valid_chunk_ids:
            return False, (
                f"Hallucination detected: {prefix}['chunk_id'] = {chunk_id!r} "
                f"does not match any retrieved chunk. "
                f"Valid IDs: {sorted(valid_chunk_ids)}."
            )

        # Rule 6: cause confidence in [0, 1].
        ok, reason = self._validate_confidence(
            value=cause_entry.get("confidence"),
            field=f"{prefix}['confidence']",
        )
        if not ok:
            return False, reason

        # Rule 9: no external references in cause text.
        cause_text = cause_entry.get("cause", "")
        if not isinstance(cause_text, str):
            return False, (
                f"{prefix}['cause'] must be a string, got "
                f"{type(cause_text).__name__!r}."
            )
        ok, reason = self._check_no_external_references(
            text=cause_text,
            field=f"{prefix}['cause']",
        )
        if not ok:
            return False, reason

        return True, "valid"

    @staticmethod
    def _validate_confidence(
        value: Any,
        field: str,
    ) -> tuple[bool, str]:
        """
        Validate that ``value`` is a numeric float in ``[0, 1]``.

        Args:
            value: The value to validate.
            field: Human-readable field name for error messages.

        Returns:
            ``(True, "valid")`` or ``(False, "<reason>")``.
        """
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return False, (
                f"'{field}' must be a numeric float, got "
                f"{type(value).__name__!r} = {value!r}."
            )

        if not (0.0 <= float(value) <= 1.0):
            return False, (
                f"'{field}' must be in [0, 1]. Got: {value}."
            )

        return True, "valid"

    @staticmethod
    def _check_no_external_references(
        text: str,
        field: str,
    ) -> tuple[bool, str]:
        """
        Reject ``text`` if it contains external URLs, domain names, or
        citation markers.

        External references indicate the LLM may have drawn on knowledge
        beyond the retrieved chunks, violating the grounding constraint.

        Patterns detected:
        - ``http://`` / ``https://`` URLs.
        - ``www.`` prefixes.
        - Bare domain-like strings (e.g. ``example.com/path``).
        - Numbered citation markers (``[1]``, ``[12]``).
        - Author-year citations (``(Smith, 2020)``).

        Args:
            text:  The cause text string to inspect.
            field: Human-readable field name for error messages.

        Returns:
            ``(True, "valid")`` or ``(False, "<reason>")``.
        """
        if _EXTERNAL_REF_PATTERN.search(text):
            match = _EXTERNAL_REF_PATTERN.search(text)
            return False, (
                f"External reference detected in {field!r}: "
                f"found {match.group()!r}. "
                "LLM output must be grounded in retrieved chunks only."
            )

        if _CITATION_PATTERN.search(text):
            match = _CITATION_PATTERN.search(text)
            return False, (
                f"External citation marker detected in {field!r}: "
                f"found {match.group()!r}. "
                "LLM output must not reference external literature."
            )

        return True, "valid"

    def __repr__(self) -> str:
        return "RAGResponseValidator()"