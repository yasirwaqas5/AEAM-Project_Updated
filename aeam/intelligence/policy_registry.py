"""
aeam/intelligence/policy_registry.py

Enterprise Policy Registry (Phase C3).

Turns the policies already extracted and persisted by
:class:`~aeam.intelligence.policy_extraction.PolicyExtractor` (Phase C2)
into a queryable registry that participates in investigations as an
additional, clearly-separate evidence source — never a replacement for
Knowledge Documents (RAG) or Enterprise Memory (Phase C1), and never a
second Rule Engine.

Reuses, unmodified:
- :class:`~aeam.registry.repositories.PolicyRepository` (Phase C2) — the
  registry loads policies straight from the existing ``policies`` table.
  No second policy store, no new schema.
- :class:`~aeam.agents.kpi.rule_engine.RuleEngine` — read-only, exactly the
  same "construct a fresh instance, read ``loaded_domains``" pattern already
  used by ``aeam/api/data_center.py``'s dataset-profile endpoint. Used ONLY
  to recognise which of a policy's ``related_metrics`` is a curated
  domain name (e.g. "sales") for indexing/matching purposes — this module
  never calls ``RuleEngine.evaluate()`` and never influences a rule
  decision. Policies remain advisory; they cannot trigger or suppress a
  deterministic rule.
- :class:`~aeam.integrations.embedding_service.EmbeddingService` — the SAME
  shared instance already used by the RAG ingestion/retrieval pipelines and
  Enterprise Memory. Used here only to score semantic relevance between an
  incident's query and a policy's text, computed in-memory (no new Qdrant
  collection, no second retrieval pipeline) since the structured fields
  PolicyExtractor already captures (``related_metrics``, ``department``)
  make deterministic matching the primary mechanism; embeddings are a
  secondary, honestly-labelled fallback, not the primary retrieval path.

Matching is two-tier, in priority order, and every match is labelled with
exactly which tier produced it — never blended into an unexplained single
score:

1. ``"metric"``   — the incident's metric string appears (case-insensitive)
                    in the policy's ``related_metrics``. Deterministic,
                    exact, most reliable.
2. ``"semantic"`` — only considered when NO metric-tier match exists at all;
                    ranks every policy by cosine similarity between the
                    incident's query and the policy's raw/business-rule
                    text, keeping only those above a similarity floor.

This module never fabricates a match: if nothing clears either tier, the
caller gets an empty list — never an invented policy or a guessed score.
It also never feeds a decision back into RuleEngine, DecisionEngine, or
ActionAgent — matched policies are advisory findings only (see
Orchestrator.investigate(), which appends them as their own
``type: "policy"`` STM finding, structurally distinct from
``type: "rag"`` and ``type: "memory"``).
"""

from __future__ import annotations

import logging
import math
from typing import Any

from aeam.agents.kpi.rule_engine import RuleEngine
from aeam.integrations.embedding_service import EmbeddingService
from aeam.registry.repositories import PolicyRepository

logger = logging.getLogger(__name__)

#: Default number of policy matches returned per investigation.
DEFAULT_TOP_K: int = 3

#: Minimum cosine similarity for a semantic-tier match to be considered
#: genuine rather than noise — never a fabricated "close enough" match.
_SEMANTIC_THRESHOLD: float = 0.4


class PolicyRegistry:
    """
    Queryable registry over already-extracted, already-persisted policies.

    Args:
        policy_repository: Existing :class:`PolicyRepository` (Phase C2) —
                           policies are loaded fresh from this on every
                           match call, so the registry is always current
                           with whatever documents have been ingested (no
                           stale in-memory cache to invalidate).
        rule_engine:       A :class:`RuleEngine` instance, used read-only
                           for its ``loaded_domains`` property only (never
                           ``evaluate()``).
        embedding_service: The shared :class:`EmbeddingService` instance
                           (same one RAG/Enterprise Memory already use).
        top_k:             Default number of matches returned per call.

    Raises:
        ValueError: If any dependency is ``None``.
    """

    def __init__(
        self,
        policy_repository: PolicyRepository,
        rule_engine: RuleEngine,
        embedding_service: EmbeddingService,
        top_k: int = DEFAULT_TOP_K,
    ) -> None:
        if policy_repository is None:
            raise ValueError("policy_repository must not be None.")
        if rule_engine is None:
            raise ValueError("rule_engine must not be None.")
        if embedding_service is None:
            raise ValueError("embedding_service must not be None.")
        self._policies = policy_repository
        self._rules = rule_engine
        self._embed = embedding_service
        self._top_k = max(1, int(top_k))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def match_for_incident(
        self,
        metric: str | None,
        query: str,
        top_k: int | None = None,
    ) -> list[dict[str, Any]]:
        """
        Retrieve policies relevant to the current incident.

        Args:
            metric: ``event.metric`` for the incident under investigation
                    (e.g. ``"sales"``, ``"latency_ms"``). Used for the
                    deterministic metric-tier match. May be ``None``/blank
                    — the registry then falls straight to the semantic
                    tier.
            query:  Natural-language description of the incident. Reuse the
                    SAME query the current investigation's RAG/Enterprise
                    Memory stage already computed (e.g. via
                    ``RAGAgent._formulate_query(event)``) so all three
                    evidence sources search on identical vocabulary.
            top_k:  Max matches to return. Defaults to the value passed to
                    ``__init__``.

        Returns:
            List of match dicts (empty if nothing genuinely matched),
            each carrying full traceability
            (``policy_id``/``source_document``/``source_chunk``) plus
            ``match_reason`` ("metric" or "semantic") and, for semantic
            matches only, a ``similarity`` score. Never fabricated: a
            field the source policy doesn't have is simply absent.
        """
        k = max(1, int(top_k or self._top_k))

        try:
            policies = self._policies.list_all()
        except Exception as exc:  # noqa: BLE001
            logger.error("PolicyRegistry | failed to load policies: %s", exc)
            return []

        if not policies:
            return []

        metric_norm = (metric or "").strip().lower()

        metric_matches = [
            p for p in policies
            if metric_norm and metric_norm in {m.lower() for m in (p.related_metrics or [])}
        ]
        if metric_matches:
            return [self._to_match_dict(p, reason="metric") for p in metric_matches[:k]]

        # No deterministic metric match at all -- fall back to semantic
        # ranking across ALL policies, honestly labelled as such.
        if not query or not query.strip():
            return []

        try:
            query_vector = self._embed.encode_text(query.strip())
        except Exception as exc:  # noqa: BLE001
            logger.error("PolicyRegistry | query embedding failed: %s", exc)
            return []

        scored: list[tuple[float, Any]] = []
        for p in policies:
            text = p.raw_text or p.business_rule or ""
            if not text.strip():
                continue
            try:
                policy_vector = self._embed.encode_text(text)
            except Exception as exc:  # noqa: BLE001
                logger.warning("PolicyRegistry | policy embedding failed | policy_id=%s | error=%s", p.policy_id, exc)
                continue
            sim = _cosine_similarity(query_vector, policy_vector)
            if sim >= _SEMANTIC_THRESHOLD:
                scored.append((sim, p))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [
            self._to_match_dict(p, reason="semantic", similarity=round(sim, 4))
            for sim, p in scored[:k]
        ]

    @property
    def curated_domains(self) -> list[str]:
        """The RuleEngine's known metric domains (read-only passthrough)."""
        return self._rules.loaded_domains

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _to_match_dict(policy: Any, reason: str, similarity: float | None = None) -> dict[str, Any]:
        match: dict[str, Any] = {
            "policy_id": policy.policy_id,
            "source_document": policy.source_document,
            "source_chunk": policy.source_chunk,
            "business_rule": policy.business_rule,
            "condition": policy.condition,
            "threshold": policy.threshold,
            "actions": policy.actions,
            "escalation_rule": policy.escalation_rule,
            "approval_required": policy.approval_required,
            "department": policy.department,
            "role": policy.role,
            "time_constraint": policy.time_constraint,
            "priority": policy.priority,
            "related_metrics": policy.related_metrics,
            "match_reason": reason,
        }
        if similarity is not None:
            match["similarity"] = similarity
        return match

    def __repr__(self) -> str:
        return f"PolicyRegistry(top_k={self._top_k})"


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Plain-Python cosine similarity — no new vector-search dependency."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)
