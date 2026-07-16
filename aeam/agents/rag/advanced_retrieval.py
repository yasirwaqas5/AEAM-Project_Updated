"""
aeam/agents/rag/advanced_retrieval.py

Advanced Retrieval Engine (Phase C6).

Adds three capabilities on top of the existing, unmodified retrieval stack
(dense Qdrant search -> BM25 -> RRF fusion -> multi-query expansion ->
cross-encoder reranking -> evidence diversity, all Phase 4/7.1-7.4, all
reused exactly as-is by composition):

1. :class:`IncidentEntityExtractor` — deterministic extraction of structured
   entities (service/host/component/etc.) from ``event.metadata``, reusing
   the EXACT SAME key vocabulary and normalisation
   :class:`~aeam.agents.rag.rag_agent.RAGAgent` already trusts for query
   formulation (``_METADATA_QUERY_ORDER`` / ``_METADATA_QUERY_LABELS`` /
   ``_normalise_query_fragment``) — imported, never redefined. Deterministic
   (no LLM call) because this data is already structured; asking an LLM to
   "extract" a field AEAM already has as a typed dict entry would only add a
   second, unnecessary hallucination surface for something with a ground
   truth.
2. Metadata-aware filtering — the extracted entities become a
   ``filter_criteria`` dict, which every existing pipeline stage (dense,
   hybrid, multi-query, reranking, diversity) ALREADY forwards end-to-end via
   its existing ``search(query, filter_criteria, top_k)`` contract. No new
   filtering mechanism is introduced. The one new behaviour is the safety net
   :class:`AdvancedRetrievalPipeline` adds: if a metadata filter legitimately
   matches nothing (e.g. the knowledge base was never tagged with this
   incident's entity vocabulary), the search is automatically relaxed
   (retried unfiltered) rather than silently reporting "no evidence" for a
   tagging mismatch that has nothing to do with actual relevance. Every
   relaxed result is marked ``metadata_filter_relaxed=True`` — never
   fabricated as a genuine filtered match.
3. :class:`BusinessRelevanceScorer` — a bounded, fully explainable
   REORDERING signal layered on top of the diversity-filtered output. It
   never replaces cross-encoder relevance (``rerank_score`` /  ``similarity``
   remain the base), it only adds small, reasoned bonuses for real, already-
   present signals: entity/metadata overlap with this incident, an
   "actionable" ``doc_type`` (runbook/incident-report/post-mortem vs generic
   reference material), and document recency. Every bonus is recorded as a
   human-readable reason in ``ranking_reasons`` — never a bare number with no
   explanation, and never a reason for a signal that was not actually found.

:class:`AdvancedRetrievalPipeline` is a drop-in wrapper — composition only,
exactly like every prior Phase 7 stage: same ``search(query, filter_criteria,
top_k)`` contract, same ``similarity_threshold``/``collection`` passthrough
properties, every existing evidence key preserved untouched. It is meant to
sit as the OUTERMOST layer, wrapping whatever the fully-composed existing
pipeline was (typically ``EvidenceDiversityPipeline``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from aeam.agents.rag.rag_agent import (
    RAGAgent,
    _METADATA_QUERY_IGNORED,
    _METADATA_QUERY_LABELS,
    _METADATA_QUERY_ORDER,
)
from aeam.monitoring.logging_config import get_logger

logger = get_logger(__name__, agent="rag")

DEFAULT_CANDIDATE_MULTIPLIER: int = 2
DEFAULT_MIN_CANDIDATES: int = 10

# doc_type values treated as "actionable" business evidence (curated allowlist
# derived from this repo's own ingestion vocabulary — see
# aeam/agents/rag/ingestion_pipeline.py's doc_type examples) versus generic /
# background reference material that happens to also live in the corpus.
DEFAULT_ACTIONABLE_DOC_TYPES: frozenset[str] = frozenset({
    "incident_report", "runbook", "sre_runbook", "post_mortem", "postmortem",
    "startup_runbook",
})

_ENTITY_BONUS_PER_MATCH: float = 0.15
_MAX_ENTITY_BONUS: float = 0.45
_DOC_TYPE_BONUS: float = 0.05
_RECENCY_BONUS: float = 0.05
_RECENCY_WINDOW_DAYS: int = 30


# ---------------------------------------------------------------------------
# 1. Entity extraction
# ---------------------------------------------------------------------------


class IncidentEntityExtractor:
    """
    Extracts structured entities from an incident's ``event.metadata``.

    Reuses :class:`~aeam.agents.rag.rag_agent.RAGAgent`'s own metadata key
    vocabulary (``_METADATA_QUERY_ORDER`` / ``_METADATA_QUERY_LABELS`` /
    ``_METADATA_QUERY_IGNORED``) and normalisation helper
    (``_normalise_query_fragment``) — the SAME fields RAGAgent already folds
    into its natural-language query text — so "entities" means exactly the
    structured context AEAM already recognises as query-relevant, not a
    second, independently-invented vocabulary.
    """

    def extract(self, metadata: dict[str, Any] | None) -> list[dict[str, str]]:
        """
        Return structured entities found in ``metadata``.

        Args:
            metadata: ``event.metadata`` (or an equivalent dict). May be
                      ``None`` or empty — returns ``[]`` in that case.

        Returns:
            List of ``{"key": <raw metadata key>, "label": <normalised
            label>, "value": <normalised value>}`` dicts, in
            ``_METADATA_QUERY_ORDER`` priority followed by any remaining
            recognised keys in sorted order (mirrors
            ``RAGAgent._metadata_query_fragments`` exactly, so the same
            metadata always yields the same entities RAGAgent already
            reasons about in its query text).
        """
        if not metadata:
            return []

        entities: list[dict[str, str]] = []
        used_keys: set[str] = set()

        for key in _METADATA_QUERY_ORDER:
            if key not in metadata:
                continue
            value = metadata.get(key)
            if value in (None, ""):
                continue
            value_nl = RAGAgent._normalise_query_fragment(value)
            if not value_nl:
                continue
            label = _METADATA_QUERY_LABELS.get(key, key)
            entities.append({"key": key, "label": label, "value": value_nl})
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
            entities.append({"key": key, "label": key_nl, "value": value_nl})

        return entities

    @staticmethod
    def to_filter_criteria(entities: list[dict[str, str]]) -> dict[str, str]:
        """
        Convert extracted entities into a Qdrant ``filter_criteria`` dict.

        Uses each entity's normalised ``label`` (not its raw incident
        metadata key) as the filter field — the same normalised vocabulary
        (e.g. ``"service"``, ``"host"``) is the more predictable convention
        a document's own metadata is likely to use, and is exactly what
        RAGAgent's query text already assumes. First occurrence wins per
        label (``_METADATA_QUERY_ORDER`` priority is preserved from
        :meth:`extract`).

        Args:
            entities: Output of :meth:`extract`.

        Returns:
            Flat ``{label: value}`` dict. Empty if ``entities`` is empty.
        """
        criteria: dict[str, str] = {}
        for entity in entities:
            criteria.setdefault(entity["label"], entity["value"])
        return criteria

    def __repr__(self) -> str:
        return "IncidentEntityExtractor()"


# ---------------------------------------------------------------------------
# 2. Business relevance scoring
# ---------------------------------------------------------------------------


class BusinessRelevanceScorer:
    """
    Bounded, fully explainable business-relevance score for a retrieved chunk.

    The score always starts from the chunk's own existing relevance signal
    (``rerank_score`` if the reranker ran, else ``similarity``) and only ever
    ADDS small, reasoned bonuses for real signals already present on the
    chunk — it never invents a signal, and never lets business-relevance
    bonuses dominate genuine semantic relevance.

    Args:
        actionable_doc_types: doc_type values considered "actionable"
                              business evidence (runbooks/incident
                              reports/post-mortems) versus generic reference
                              material.
        recency_window_days:  Documents dated within this many days of "now"
                              receive the recency bonus.
    """

    def __init__(
        self,
        actionable_doc_types: frozenset[str] = DEFAULT_ACTIONABLE_DOC_TYPES,
        recency_window_days: int = _RECENCY_WINDOW_DAYS,
    ) -> None:
        self._actionable_doc_types = actionable_doc_types
        self._recency_window_days = max(1, int(recency_window_days))

    def score(
        self,
        chunk: dict[str, Any],
        filter_criteria: dict[str, str] | None,
    ) -> tuple[float, list[str]]:
        """
        Compute ``(business_relevance_score, ranking_reasons)`` for ``chunk``.

        Args:
            chunk:           A retrieved chunk dict (``chunk_id`` / ``text``
                             / ``metadata`` plus whatever upstream stages
                             already attached — ``rerank_score``,
                             ``similarity``, ``diversity_kept_reason``, etc.).
            filter_criteria: The incident's extracted entities as a flat
                             ``{label: value}`` dict (see
                             :meth:`IncidentEntityExtractor.to_filter_criteria`),
                             or ``None``/``{}`` if no entities were extracted.

        Returns:
            Tuple of the clamped ``[0.0, 1.0]`` score and a non-empty list of
            plain-English reasons. When no bonus applies, the single honest
            reason ``"ranked by existing semantic relevance only"`` is
            returned — never a fabricated justification.
        """
        metadata = chunk.get("metadata") or {}
        base = chunk.get("rerank_score")
        if not isinstance(base, (int, float)):
            base = chunk.get("similarity")
        if not isinstance(base, (int, float)):
            base = 0.0
        base = max(0.0, min(1.0, float(base)))

        bonus = 0.0
        reasons: list[str] = []

        entity_bonus, entity_reasons = self._entity_overlap(metadata, filter_criteria)
        bonus += entity_bonus
        reasons.extend(entity_reasons)

        doc_type = metadata.get("doc_type")
        if isinstance(doc_type, str) and doc_type.strip().lower() in self._actionable_doc_types:
            bonus += _DOC_TYPE_BONUS
            reasons.append(f"authoritative source (doc_type={doc_type})")

        if self._is_recent(metadata.get("date")):
            bonus += _RECENCY_BONUS
            reasons.append(f"recent document (within {self._recency_window_days} days)")

        diversity_reason = chunk.get("diversity_kept_reason")
        if diversity_reason == "diverse":
            reasons.append("kept for evidence diversity (distinct from higher-ranked chunks)")

        if chunk.get("metadata_filter_relaxed"):
            reasons.append("metadata filter found no match — relaxed to unfiltered search")

        final = max(0.0, min(1.0, base + bonus))
        if not reasons:
            reasons = ["ranked by existing semantic relevance only"]

        return round(final, 6), reasons

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _entity_overlap(
        metadata: dict[str, Any],
        filter_criteria: dict[str, str] | None,
    ) -> tuple[float, list[str]]:
        if not filter_criteria:
            return 0.0, []
        matched = 0
        reasons: list[str] = []
        for label, value in filter_criteria.items():
            candidate = metadata.get(label)
            if candidate is None:
                continue
            if str(candidate).strip().lower() == str(value).strip().lower():
                matched += 1
                reasons.append(f"matches incident entity {label}={value}")
        bonus = min(_MAX_ENTITY_BONUS, matched * _ENTITY_BONUS_PER_MATCH)
        return bonus, reasons

    def _is_recent(self, raw_date: Any) -> bool:
        if not raw_date:
            return False
        parsed = _parse_loose_date(raw_date)
        if parsed is None:
            return False
        age_days = (datetime.now(tz=parsed.tzinfo) - parsed).days
        return 0 <= age_days <= self._recency_window_days

    def __repr__(self) -> str:
        return f"BusinessRelevanceScorer(recency_window_days={self._recency_window_days})"


def _parse_loose_date(raw: Any) -> datetime | None:
    """Best-effort date parse (ISO date or datetime); never raises."""
    if isinstance(raw, datetime):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    for candidate in (text, f"{text}T00:00:00"):
        try:
            return datetime.fromisoformat(candidate)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# 3. Drop-in pipeline wrapper
# ---------------------------------------------------------------------------


class AdvancedRetrievalPipeline:
    """
    Metadata-aware-filtering + business-relevance-ranking wrapper — a
    drop-in for any existing retrieval pipeline (typically
    :class:`~aeam.agents.rag.evidence_diversity.EvidenceDiversityPipeline`,
    used unchanged).

    Args:
        inner_pipeline:      The fully-composed existing pipeline (dense /
                             hybrid / multi-query / reranked / diversified),
                             used unchanged via its existing ``search``
                             contract.
        relevance_scorer:    Configured :class:`BusinessRelevanceScorer`.
        candidate_multiplier: Candidate pool multiplier fetched from the
                             inner pipeline before final top_k selection.
        min_candidates:      Lower bound on the candidate pool.
    """

    def __init__(
        self,
        inner_pipeline: Any,
        relevance_scorer: BusinessRelevanceScorer,
        candidate_multiplier: int = DEFAULT_CANDIDATE_MULTIPLIER,
        min_candidates: int = DEFAULT_MIN_CANDIDATES,
    ) -> None:
        if inner_pipeline is None:
            raise ValueError("inner_pipeline must not be None.")
        if relevance_scorer is None:
            raise ValueError("relevance_scorer must not be None.")
        self._inner = inner_pipeline
        self._scorer = relevance_scorer
        self._candidate_multiplier = max(1, int(candidate_multiplier))
        self._min_candidates = max(1, int(min_candidates))

    # ------------------------------------------------------------------
    # Public API — mirrors RetrievalPipeline.search
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        filter_criteria: dict[str, Any] | None = None,
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        """
        Retrieve (with automatic metadata-filter relaxation), score by
        business relevance, and return the top ``top_k``.

        Args:
            query:           Natural-language query. Must be non-empty.
            filter_criteria: Incident entities as ``{label: value}`` (see
                             :meth:`IncidentEntityExtractor.to_filter_criteria`),
                             or ``None`` to skip metadata-aware filtering.
            top_k:           Final number of results (>= 1).

        Returns:
            Up to ``top_k`` results (best first by
            ``business_relevance_score``), each preserving every existing
            evidence key and adding ``business_relevance_score``,
            ``ranking_reasons``, ``retrieval_confidence`` (identical value to
            ``business_relevance_score`` — the same honestly-computed number
            serves both as the reordering key and the confidence surfaced to
            operators) and ``metadata_filter_relaxed``.

        Raises:
            ValueError: If ``query`` is empty/whitespace or ``top_k`` < 1.
        """
        if not query or not query.strip():
            raise ValueError("query must be a non-empty string.")
        if top_k < 1:
            raise ValueError(f"top_k must be >= 1. Got: {top_k}.")

        candidate_k = max(top_k * self._candidate_multiplier, self._min_candidates)

        results = self._inner.search(query=query, filter_criteria=filter_criteria, top_k=candidate_k)
        relaxed = False
        if not results and filter_criteria:
            logger.info(
                "AdvancedRetrievalPipeline | metadata filter matched nothing "
                "(filter_criteria=%r) — relaxing to unfiltered search | query=%r",
                filter_criteria, query,
            )
            results = self._inner.search(query=query, filter_criteria=None, top_k=candidate_k)
            relaxed = True

        scored: list[dict[str, Any]] = []
        for chunk in results:
            item = dict(chunk)
            if relaxed:
                item["metadata_filter_relaxed"] = True
            business_score, reasons = self._scorer.score(item, filter_criteria)
            item["business_relevance_score"] = business_score
            item["ranking_reasons"] = reasons
            item["retrieval_confidence"] = business_score
            scored.append(item)

        scored.sort(
            key=lambda x: (x["business_relevance_score"], str(x.get("chunk_id"))),
            reverse=True,
        )

        top = scored[:top_k]
        logger.info(
            "AdvancedRetrievalPipeline.search | candidates=%d | returned=%d | "
            "metadata_filter_relaxed=%s | query=%r",
            len(results), len(top), relaxed, query,
        )
        return top

    # ------------------------------------------------------------------
    # Drop-in compatibility surface (read by RAGAgent / reporting)
    # ------------------------------------------------------------------

    @property
    def similarity_threshold(self) -> float:
        return getattr(self._inner, "similarity_threshold", 0.0)

    @property
    def collection(self) -> str:
        return getattr(self._inner, "collection", "")

    def __repr__(self) -> str:
        return f"AdvancedRetrievalPipeline(inner={self._inner!r}, scorer={self._scorer!r})"
