"""
aeam/intelligence/ai_evaluation.py

Enterprise AI Evaluation & Quality Engine (Phase D2).

Scores the QUALITY of an already-completed investigation -- it never
changes ``findings``, the Execution Plan (Phase C7), or the Explainability
object (Phase D1); it only reads them and produces a separate, additive
``evaluation`` findings entry. This module performs NO retrieval, NO
detection, and NO LLM call: every component score is a fully transparent,
deterministic function of fields that already exist on the ``execution_plan``
/ ``explainability`` data (or, when explainability was never wired, the same
underlying findings those two engines themselves read) -- reused, never
recomputed independently.

Design rationale (Architecture Gate conclusion):
- No new reasoning pipeline is necessary. Every metric below is either a
  direct reuse of a value ExecutionPlanningEngine/ExplainabilityEngine
  already computed (``sources_consulted``, ``sources_with_signal``,
  ``evidence_conflicts``, ``confidence_breakdown.adjustment``,
  ``missing_evidence``), or a simple, fully-disclosed ratio/count over
  already-computed lists (e.g. "fraction of recommended_actions that cite
  real evidence" = count(source != "runbook") / count(all)). Nothing here
  re-runs a detector, re-queries Qdrant, re-matches a policy, or re-derives a
  root cause.
- Distinct from "confidence": ExecutionPlanningEngine's ``confidence`` and
  ExplainabilityEngine's ``confidence_breakdown`` describe how sure the
  SYSTEM is about the incident's root cause. This module's scores describe
  how THOROUGH the investigation itself was -- a different, orthogonal
  question ("did we check everything we could have?" vs "how sure are we
  about what we found?"). Neither module invents a confidence value for the
  other; scores here are always heuristic quality ratios over real counts,
  clearly labelled as such, never presented as a probability.
- Honesty: a component score is ``None`` (not zero) whenever the underlying
  evidence source was never consulted at all -- zero and "not computable"
  are different, truthful states and this module never conflates them.
"""

from __future__ import annotations

from typing import Any

from aeam.intelligence.execution_planning import _SOURCE_PRIORITY as _SOURCES

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_STRENGTH_THRESHOLD: float = 0.7
_WEAKNESS_THRESHOLD: float = 0.4

# Weight of conflict severity as a penalty subtracted from the mean of the
# other component scores when computing the overall score. Fully disclosed
# here (and in the returned ``overall_score_formula`` string) -- never a
# hidden coefficient.
_CONFLICT_PENALTY_WEIGHT: float = 0.2

# Penalty subtracted from Memory Quality's average-similarity score when
# past similar incidents recorded inconsistent (mixed) outcomes.
_MEMORY_MIXED_OUTCOME_PENALTY: float = 0.15

_COMPONENT_LABELS: dict[str, str] = {
    "evidence_coverage": "Evidence Coverage",
    "retrieval_quality": "Retrieval Quality",
    "memory_quality": "Memory Quality",
    "policy_coverage": "Policy Coverage",
    "cross_dataset_coverage": "Cross-Dataset Coverage",
    "adaptive_detection_coverage": "Adaptive Detection Coverage",
    "conflict_severity": "Conflict Severity",
    "evidence_diversity": "Evidence Diversity",
    "recommendation_quality": "Recommendation Quality",
    "investigation_completeness": "Investigation Completeness",
}


class AIEvaluationEngine:
    """
    Scores investigation quality -- never recomputes or alters what it scores.

    Stateless and dependency-free, exactly like ExecutionPlanningEngine and
    ExplainabilityEngine. The constructor is entirely OPTIONAL -- every
    existing zero-arg ``AIEvaluationEngine()`` call site keeps working
    unchanged.

    Args:
        strength_threshold, weakness_threshold, conflict_penalty_weight,
        memory_mixed_outcome_penalty: Override the corresponding module
                             constants (Phase D4 Enterprise Configuration
                             Engine). Each ``None`` (the default) preserves
                             the module default unchanged.
    """

    def __init__(
        self,
        strength_threshold: float | None = None,
        weakness_threshold: float | None = None,
        conflict_penalty_weight: float | None = None,
        memory_mixed_outcome_penalty: float | None = None,
    ) -> None:
        self._strength_threshold = (
            strength_threshold if strength_threshold is not None else _STRENGTH_THRESHOLD
        )
        self._weakness_threshold = (
            weakness_threshold if weakness_threshold is not None else _WEAKNESS_THRESHOLD
        )
        self._conflict_penalty_weight = (
            conflict_penalty_weight if conflict_penalty_weight is not None else _CONFLICT_PENALTY_WEIGHT
        )
        self._memory_mixed_outcome_penalty = (
            memory_mixed_outcome_penalty
            if memory_mixed_outcome_penalty is not None
            else _MEMORY_MIXED_OUTCOME_PENALTY
        )

    def assess(
        self,
        *,
        findings: list[dict[str, Any]],
        execution_plan: dict[str, Any],
        explainability: dict[str, Any] | None,
        root_cause: str | None,
        confidence: float | None,
    ) -> dict[str, Any]:
        """
        Build the AI Evaluation object for one incident's already-completed
        investigation.

        Args:
            findings:        The FULL, already-populated STM ``findings``
                              list for this incident.
            execution_plan:  The ``data`` dict ExecutionPlanningEngine.plan()
                              already returned this lifecycle.
            explainability:  The ``data`` dict ExplainabilityEngine.explain()
                              already returned this lifecycle, or ``None`` if
                              the explainability engine was not wired -- this
                              module degrades gracefully, re-reading the SAME
                              underlying booleans explainability itself would
                              have read, never re-deriving new ones.
            root_cause:       Already-established STM root_cause value.
            confidence:       Already-established STM confidence value.

        Returns:
            A JSON-serialisable dict: ``overall_score``,
            ``overall_score_formula``, ``component_scores``, ``strengths``,
            ``weaknesses``, ``missing_evidence``,
            ``improvement_opportunities``, ``quality_summary``.

        Raises:
            Never raises -- caught by the Orchestrator caller, same
            resilience contract as every other C/D-phase engine.
        """
        memory_data = _latest_finding_data(findings, "memory")
        policy_data = _latest_finding_data(findings, "policy")
        cross_dataset_data = _latest_finding_data(findings, "cross_dataset")
        adaptive_data = _latest_finding_data(findings, "adaptive")
        rag_data = _latest_finding_data(findings, "rag")

        sources_consulted = execution_plan.get("sources_consulted") or {}
        sources_with_signal = execution_plan.get("sources_with_signal") or {}
        recommended_actions = execution_plan.get("recommended_actions") or []
        evidence_conflicts = execution_plan.get("evidence_conflicts") or []

        plan_present = bool(execution_plan)

        components: dict[str, dict[str, Any]] = {
            "evidence_coverage": _score_evidence_coverage(sources_consulted, sources_with_signal),
            "retrieval_quality": _score_retrieval_quality(rag_data),
            "memory_quality": _score_memory_quality(
                memory_data, evidence_conflicts, self._memory_mixed_outcome_penalty,
            ),
            "policy_coverage": _score_policy_coverage(policy_data),
            "cross_dataset_coverage": _score_cross_dataset_coverage(cross_dataset_data),
            "adaptive_detection_coverage": _score_adaptive_coverage(adaptive_data),
            "conflict_severity": _score_conflict_severity(evidence_conflicts, explainability, plan_present),
            "evidence_diversity": _score_evidence_diversity(sources_with_signal, rag_data),
            "recommendation_quality": _score_recommendation_quality(recommended_actions, plan_present),
            "investigation_completeness": _score_completeness(
                sources_consulted, root_cause, confidence, execution_plan, explainability,
            ),
        }

        overall_score, overall_formula = _compute_overall_score(components, self._conflict_penalty_weight)
        strengths, weaknesses = _derive_strengths_weaknesses(
            components, self._strength_threshold, self._weakness_threshold,
        )
        missing_evidence = _derive_missing_evidence(explainability, sources_consulted, sources_with_signal)
        improvement_opportunities = _derive_improvement_opportunities(components, evidence_conflicts)
        quality_summary = _build_quality_summary(overall_score, strengths, weaknesses, evidence_conflicts)

        return {
            "overall_score": overall_score,
            "overall_score_formula": overall_formula,
            "component_scores": components,
            "strengths": strengths,
            "weaknesses": weaknesses,
            "missing_evidence": missing_evidence,
            "improvement_opportunities": improvement_opportunities,
            "quality_summary": quality_summary,
        }

    def __repr__(self) -> str:
        return "AIEvaluationEngine()"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _latest_finding_data(findings: list[dict[str, Any]], type_name: str) -> dict[str, Any] | None:
    """Identical scan pattern to ExecutionPlanningEngine/ExplainabilityEngine's own helper."""
    latest: dict[str, Any] | None = None
    for entry in findings or []:
        if isinstance(entry, dict) and entry.get("type") == type_name:
            latest = entry.get("data") or {}
    return latest


def _component(score: float | None, reason: str) -> dict[str, Any]:
    return {"score": round(score, 4) if isinstance(score, (int, float)) else None, "reason": reason}


def _score_evidence_coverage(sources_consulted: dict[str, bool], sources_with_signal: dict[str, bool]) -> dict[str, Any]:
    if not sources_consulted:
        return _component(None, "No execution plan available -- coverage cannot be computed.")
    with_signal = sum(1 for s in _SOURCES if sources_with_signal.get(s))
    return _component(
        with_signal / len(_SOURCES),
        f"{with_signal}/{len(_SOURCES)} evidence sources produced a usable signal.",
    )


def _score_retrieval_quality(rag_data: dict[str, Any] | None) -> dict[str, Any]:
    if rag_data is None:
        return _component(None, "Retrieval (RAG) was not consulted for this investigation.")
    retrieved_count = rag_data.get("retrieved_count") or 0
    if retrieved_count == 0:
        return _component(0.0, "No chunks were retrieved.")
    possible_causes = rag_data.get("possible_causes") or []
    top_conf = max((float(c.get("confidence", 0.0) or 0.0) for c in possible_causes), default=None)
    validated = rag_data.get("validation_passed") is True
    terms = [1.0, 1.0 if validated else 0.0]
    if top_conf is not None:
        terms.append(top_conf)
    score = sum(terms) / len(terms)
    return _component(
        score,
        f"retrieved_count={retrieved_count}, validation_passed={validated}, "
        f"top_cause_confidence={top_conf if top_conf is not None else 'unavailable'}.",
    )


def _score_memory_quality(
    memory_data: dict[str, Any] | None,
    evidence_conflicts: list[dict[str, Any]],
    mixed_outcome_penalty: float = _MEMORY_MIXED_OUTCOME_PENALTY,
) -> dict[str, Any]:
    if memory_data is None:
        return _component(None, "Enterprise Memory was not consulted for this investigation.")
    matches = memory_data.get("matches") or []
    if not matches:
        return _component(0.0, "No similar past incidents were found.")
    sims = [m.get("similarity") for m in matches if isinstance(m.get("similarity"), (int, float))]
    base = (sum(sims) / len(sims)) if sims else 0.0
    mixed_outcomes = any("inconsistent outcomes" in (c.get("description") or "") for c in evidence_conflicts)
    score = max(0.0, base - mixed_outcome_penalty) if mixed_outcomes else base
    reason = f"{len(matches)} similar past incident(s), average similarity {round(base, 4) if sims else 'unavailable'}."
    if mixed_outcomes:
        reason += " Penalised for inconsistent historical outcomes (see contradictions)."
    return _component(score, reason)


def _score_policy_coverage(policy_data: dict[str, Any] | None) -> dict[str, Any]:
    if policy_data is None:
        return _component(None, "Enterprise Policy Registry was not consulted for this investigation.")
    matches = policy_data.get("matches") or []
    if not matches:
        return _component(0.0, "No enterprise policy matched this incident.")
    return _component(min(len(matches) / 2.0, 1.0), f"{len(matches)} enterprise polic{'y' if len(matches) == 1 else 'ies'} matched.")


def _score_cross_dataset_coverage(cross_dataset_data: dict[str, Any] | None) -> dict[str, Any]:
    if cross_dataset_data is None:
        return _component(None, "Cross-Dataset Intelligence was not consulted for this investigation.")
    if cross_dataset_data.get("insufficient_data"):
        return _component(0.0, cross_dataset_data.get("reason") or "Insufficient activated datasets to compare.")
    checked = cross_dataset_data.get("candidates_checked") or 0
    if checked == 0:
        return _component(0.0, "No other activated datasets were available to check.")
    found = len(cross_dataset_data.get("supporting") or []) + len(cross_dataset_data.get("strong_correlations") or [])
    return _component(min(found / checked, 1.0), f"{found}/{checked} checked dataset(s) showed a correlated/supporting signal.")


def _score_adaptive_coverage(adaptive_data: dict[str, Any] | None) -> dict[str, Any]:
    if adaptive_data is None:
        return _component(None, "Adaptive Detection was not consulted for this investigation.")
    baseline_ok = not adaptive_data.get("adaptive_baseline_insufficient")
    seasonality_ok = not adaptive_data.get("seasonality_insufficient")
    score = (0.5 if baseline_ok else 0.0) + (0.5 if seasonality_ok else 0.0)
    parts = []
    parts.append("adaptive baseline computable" if baseline_ok else "adaptive baseline insufficient history")
    parts.append("seasonality judgement computable" if seasonality_ok else "seasonality insufficient history")
    return _component(score, "; ".join(parts) + ".")


def _score_conflict_severity(
    evidence_conflicts: list[dict[str, Any]], explainability: dict[str, Any] | None, plan_present: bool,
) -> dict[str, Any]:
    if not plan_present:
        return _component(None, "No execution plan available -- conflict severity cannot be computed.")
    n = len(evidence_conflicts)
    score = min(n * 0.25, 1.0)
    reason = f"{n} evidence conflict(s) detected (higher = more severe)."
    if explainability is not None:
        adjustment = (explainability.get("confidence_breakdown") or {}).get("adjustment")
        if isinstance(adjustment, (int, float)) and adjustment < 0:
            reason += f" Confidence was reduced by {abs(adjustment)} as a result (see Explainability)."
    return _component(score, reason)


def _score_evidence_diversity(sources_with_signal: dict[str, bool], rag_data: dict[str, Any] | None) -> dict[str, Any]:
    if not sources_with_signal:
        return _component(None, "No execution plan available -- diversity cannot be computed.")
    distinct_sources = sum(1 for s in _SOURCES if sources_with_signal.get(s))
    source_ratio = distinct_sources / len(_SOURCES)

    chunk_ratio = None
    if rag_data is not None:
        chunks = rag_data.get("retrieved_chunks") or []
        if chunks:
            distinct_chunk_sources = len({c.get("source") for c in chunks if c.get("source")})
            chunk_ratio = distinct_chunk_sources / len(chunks)

    if chunk_ratio is not None:
        score = (source_ratio + chunk_ratio) / 2
        reason = f"{distinct_sources}/{len(_SOURCES)} evidence types contributed; {round(chunk_ratio, 4)} of retrieved chunks came from distinct documents."
    else:
        score = source_ratio
        reason = f"{distinct_sources}/{len(_SOURCES)} evidence types contributed (no retrieved chunks to assess document diversity)."
    return _component(score, reason)


def _score_recommendation_quality(recommended_actions: list[dict[str, Any]], plan_present: bool) -> dict[str, Any]:
    if not plan_present:
        return _component(None, "No execution plan available -- recommendation quality cannot be computed.")
    if not recommended_actions:
        return _component(0.0, "No recommendations were synthesized.")
    evidence_backed = sum(1 for a in recommended_actions if a.get("source") != "runbook")
    total = len(recommended_actions)
    return _component(
        evidence_backed / total,
        f"{evidence_backed}/{total} recommendation(s) are backed by real evidence (not standard runbook guidance alone).",
    )


def _score_completeness(
    sources_consulted: dict[str, bool],
    root_cause: str | None,
    confidence: float | None,
    execution_plan: dict[str, Any],
    explainability: dict[str, Any] | None,
) -> dict[str, Any]:
    if not sources_consulted:
        return _component(None, "No execution plan available -- completeness cannot be computed.")
    consulted_count = sum(1 for s in _SOURCES if sources_consulted.get(s))
    signals = [
        consulted_count / len(_SOURCES),
        1.0 if root_cause else 0.0,
        1.0 if isinstance(confidence, (int, float)) else 0.0,
        1.0 if execution_plan else 0.0,
        1.0 if explainability is not None else 0.0,
    ]
    score = sum(signals) / len(signals)
    return _component(
        score,
        f"{consulted_count}/{len(_SOURCES)} evidence sources consulted; root_cause={'present' if root_cause else 'absent'}; "
        f"execution_plan={'present' if execution_plan else 'absent'}; explainability={'present' if explainability is not None else 'absent'}.",
    )


def _compute_overall_score(
    components: dict[str, dict[str, Any]],
    conflict_penalty_weight: float = _CONFLICT_PENALTY_WEIGHT,
) -> tuple[float | None, str]:
    quality_keys = [k for k in components if k != "conflict_severity"]
    available = [components[k]["score"] for k in quality_keys if components[k]["score"] is not None]
    conflict_score = components["conflict_severity"]["score"] or 0.0

    formula = (
        f"mean of {len(available)}/{len(quality_keys)} computable quality components, "
        f"minus (conflict_severity * {conflict_penalty_weight}), clamped to [0, 1]."
    )
    if not available:
        return None, formula + " No components were computable."

    mean_quality = sum(available) / len(available)
    overall = max(0.0, min(1.0, mean_quality - conflict_score * conflict_penalty_weight))
    return round(overall, 4), formula


def _derive_strengths_weaknesses(
    components: dict[str, dict[str, Any]],
    strength_threshold: float = _STRENGTH_THRESHOLD,
    weakness_threshold: float = _WEAKNESS_THRESHOLD,
) -> tuple[list[str], list[str]]:
    strengths: list[str] = []
    weaknesses: list[str] = []
    for key, comp in components.items():
        score = comp["score"]
        if score is None:
            continue
        label = _COMPONENT_LABELS.get(key, key)
        if key == "conflict_severity":
            if score >= strength_threshold:
                weaknesses.append(f"{label} is high ({score}) -- significant evidence conflicts detected.")
            elif score == 0.0:
                strengths.append(f"{label} is zero -- no evidence conflicts detected.")
            continue
        if score >= strength_threshold:
            strengths.append(f"{label} is strong ({score}).")
        elif score < weakness_threshold:
            weaknesses.append(f"{label} is weak ({score}) -- {comp['reason']}")
    return strengths, weaknesses


def _derive_missing_evidence(
    explainability: dict[str, Any] | None,
    sources_consulted: dict[str, bool],
    sources_with_signal: dict[str, bool],
) -> list[dict[str, Any]]:
    if explainability is not None and explainability.get("missing_evidence") is not None:
        return list(explainability["missing_evidence"])
    missing: list[dict[str, Any]] = []
    for source in _SOURCES:
        if not sources_consulted.get(source):
            missing.append({"source": source, "reason": f"{source} was not consulted for this investigation."})
        elif not sources_with_signal.get(source):
            missing.append({"source": source, "reason": f"{source} was consulted but produced no usable signal."})
    return missing


def _derive_improvement_opportunities(components: dict[str, dict[str, Any]], evidence_conflicts: list[dict[str, Any]]) -> list[str]:
    opportunities: list[str] = []
    cd = components.get("cross_dataset_coverage", {})
    if cd.get("score") == 0.0:
        opportunities.append("Activate additional related datasets to enable cross-dataset correlation checks.")
    ad = components.get("adaptive_detection_coverage", {})
    if ad.get("score") is not None and ad["score"] < 1.0:
        opportunities.append("Accumulate more historical data points for this metric to enable a full adaptive baseline/seasonality judgement.")
    pc = components.get("policy_coverage", {})
    if pc.get("score") == 0.0:
        opportunities.append("Consider authoring an enterprise policy for this event type if this pattern recurs.")
    rq = components.get("recommendation_quality", {})
    if rq.get("score") is not None and rq["score"] < 0.5:
        opportunities.append("Most recommendations are standard runbook guidance only -- consider strengthening evidence sources for this event type.")
    if evidence_conflicts:
        opportunities.append(f"Resolve or investigate the {len(evidence_conflicts)} evidence conflict(s) surfaced in Explainability to improve confidence.")
    return opportunities


def _build_quality_summary(
    overall_score: float | None,
    strengths: list[str],
    weaknesses: list[str],
    evidence_conflicts: list[dict[str, Any]],
) -> str:
    if overall_score is None:
        return "Investigation quality could not be scored -- no execution plan was available to evaluate."
    parts = [f"Overall investigation quality score: {overall_score} (0-1 scale, heuristic, never a probability)."]
    if strengths:
        parts.append(f"Strongest aspect: {strengths[0]}")
    if weaknesses:
        parts.append(f"Weakest aspect: {weaknesses[0]}")
    if evidence_conflicts:
        parts.append(f"{len(evidence_conflicts)} evidence conflict(s) were factored into this score as a penalty.")
    return " ".join(parts)
