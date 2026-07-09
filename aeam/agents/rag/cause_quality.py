"""
aeam/agents/rag/cause_quality.py

Root-cause quality gate for the AEAM RAG pipeline.

Pure validation logic — no I/O, no LLM calls, no external dependencies.
Rejects root-cause strings that are too vague to act on (e.g. a single
orphaned noun like "queries" or "loss") so that the highest-*confidence*
grounded cause is not blindly treated as the highest-*quality* one.

This is a defense-in-depth complement to the chunking fix that stops
truncated word fragments from entering the corpus in the first place —
it protects against any source of a low-information cause (truncation,
LLM terseness, future corpus changes) reaching the operator.
"""

from __future__ import annotations

# Bare words that convey no actionable meaning on their own. Matched only
# against the *entire* (lowercased, stripped) cause string, never as a
# substring — "replication lag" is fine even though "lag" alone would not be.
_VAGUE_ROOT_CAUSES: frozenset[str] = frozenset({
    "queries", "query", "issues", "issue", "errors", "error", "gateway",
    "loss", "latency", "failure", "failures", "problem", "problems",
    "anomaly", "anomalies", "timeout", "timeouts", "spike", "delay",
    "delays", "unknown", "n/a", "none", "unavailable",
})

# A root cause must contain at least this many words to be considered
# descriptive rather than a bare noun fragment.
MIN_ROOT_CAUSE_WORDS: int = 2


def is_meaningful_root_cause(cause: str | None) -> bool:
    """
    Return ``True`` if ``cause`` is descriptive enough to act on.

    Rejects:
    - Empty / whitespace-only strings.
    - Single-word causes (regardless of content — a lone word is rarely a
      complete root-cause statement, e.g. "gateway" vs. "payment gateway
      downtime").
    - Exact matches (case-insensitive) against a small blacklist of bare
      vague words, even if the LLM padded them with punctuation.

    Args:
        cause: Candidate root-cause string, or ``None``.

    Returns:
        ``True`` if ``cause`` passes the quality gate.

    Example::

        is_meaningful_root_cause("queries")                      # False
        is_meaningful_root_cause("inefficient queries")          # True
        is_meaningful_root_cause("Replication lag on read replica")  # True
    """
    if not cause:
        return False

    text = cause.strip()
    if not text:
        return False

    normalised = text.strip(".,;:!? ").lower()
    if normalised in _VAGUE_ROOT_CAUSES:
        return False

    words = text.split()
    if len(words) < MIN_ROOT_CAUSE_WORDS:
        return False

    return True


def best_meaningful_cause(
    sorted_causes: list[dict],
    cause_key: str = "cause",
) -> dict | None:
    """
    Return the highest-ranked entry in ``sorted_causes`` whose cause text
    passes :func:`is_meaningful_root_cause`.

    ``sorted_causes`` is expected to already be ordered by descending
    confidence (or whatever priority the caller wants); this function does
    not re-sort — it just skips entries that fail the quality gate.

    Args:
        sorted_causes: List of cause dicts, highest-priority first.
        cause_key:     Dict key holding the cause text. Defaults to ``"cause"``.

    Returns:
        The first entry passing the quality gate, or ``None`` if every
        candidate is too vague.
    """
    for entry in sorted_causes:
        if is_meaningful_root_cause(entry.get(cause_key)):
            return entry
    return None
