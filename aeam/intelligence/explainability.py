"""
aeam/intelligence/explainability.py

Enterprise Explainability Engine (Phase D1).

Explains WHY the Enterprise Action Planning Engine (Phase C7) reached every
recommendation it did -- it never changes a recommendation, never recomputes
a decision, and never re-runs retrieval/memory/policy/cross-dataset/adaptive
detection. It reads ONLY the ``findings`` list the Orchestrator has already
assembled by the time :meth:`ExplainabilityEngine.explain` is called (see
``Orchestrator.finalize_incident()``, invoked once, immediately after the
Phase C7 ``execution_plan`` finding is appended).

Design rationale (Architecture Gate conclusion):
- No new reasoning pipeline is necessary. Every field this engine returns is
  either (a) a direct passthrough of a value ExecutionPlanningEngine or an
  earlier C-phase already computed, or (b) a purely STRUCTURAL
  re-organization of already-computed dicts (e.g. reverse-indexing "which
  recommendation cites which evidence item" using the exact, fixed
  construction order ExecutionPlanningEngine.plan() itself used to build
  ``recommended_actions`` -- never a new judgement about the evidence).
  Composed into the Orchestrator by constructor injection, guarded by the
  same idempotency convention as every prior C-phase, appended as one more
  findings entry (``type == "explainability"``).
- Confidence honesty: the mission's own worked example
  ("Enterprise Policies +0.30, ... Final Confidence 0.75") implies an
  additive per-source weighting scheme. No such scheme exists anywhere in
  this codebase -- ``confidence`` is set by RAGAgent's LLM output or by
  ExecutionPlanningEngine's own conflict-capping logic (``min(base, 0.5)``),
  never a linear combination of per-source weights. Inventing fake per-source
  deltas that cosmetically sum to the real final value would be exactly the
  fabrication the mission forbids. Instead, this engine reports the ONE real,
  honestly-derived delta available -- ``raw_confidence`` (the STM confidence
  BEFORE ExecutionPlanningEngine's own adjustment) versus ``plan_confidence``
  (the value AFTER it) -- and, per source, whatever REAL scalar signal that
  source's own finding already carries (a policy match's similarity, a
  memory match's confidence, a retrieval cause's confidence, etc.), never a
  fabricated weight. Sources with no real scalar confidence concept (e.g.
  cross-dataset correlation, which is a structural/statistical signal, not a
  probability) are labelled "no scalar confidence value" rather than forced
  into a fake number.
- Evidence attribution reuses ExecutionPlanningEngine's own FIXED, ordered
  construction: it appends exactly one recommended action per policy match
  (in match order), one per cross-dataset strong correlation (in order), at
  most one for adaptive seasonality, at most one for the top retrieval cause,
  and one per runbook line -- see execution_planning.py's ``plan()``. This
  engine re-walks ``recommended_actions`` grouped by ``source`` and zips each
  group, in order, against the SAME original evidence list already read from
  ``findings`` -- a structural cross-reference, not a new inference -- to
  recover the exact Memory ID / Policy ID / dataset name / chunk_id each
  recommendation traces back to. Recommendations with no such originating
  evidence item (runbook baseline text) are honestly labelled as such, never
  assigned a fabricated ID.
"""

from __future__ import annotations

import re
from typing import Any

from aeam.intelligence.execution_planning import _SOURCE_PRIORITY

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Which existing ReportAgent section (if any) already documents each evidence
# source -- a static, honest mapping of what genuinely exists (see
# report_agent.py's _generate_report_inner() append sequence). Memory and
# retrieval have NO dedicated appended section of their own (they feed the
# main LLM-or-fallback narrative directly) -- labelled honestly, never
# assigned an invented section name.
_REPORT_SECTION_BY_SOURCE: dict[str, str] = {
    "policy": "Matched Enterprise Policies",
    "cross_dataset": "Cross-Dataset Analysis",
    "adaptive": "Adaptive Detection",
    "retrieval": "Reasoning (main investigation narrative -- no dedicated appended section)",
    "memory": "Enterprise Memory (surfaced in Investigation Workspace only -- no dedicated ReportAgent section)",
    "runbook": "Enterprise Execution Plan (baseline recommendation, not evidence-derived)",
}

_CHUNK_ID_RE = re.compile(r"chunk_id=(\S+?)\)")


class ExplainabilityEngine:
    """
    Explains an already-computed execution plan -- never recomputes it.

    Stateless and dependency-free, exactly like
    :class:`~aeam.intelligence.execution_planning.ExecutionPlanningEngine`.
    """

    def explain(
        self,
        *,
        findings: list[dict[str, Any]],
        execution_plan: dict[str, Any],
        raw_confidence: float | None,
    ) -> dict[str, Any]:
        """
        Build the explainability object for one incident's already-computed
        execution plan.

        Args:
            findings:        The FULL, already-populated STM ``findings``
                              list for this incident (same list
                              ExecutionPlanningEngine itself read).
            execution_plan:  The ``data`` dict ExecutionPlanningEngine.plan()
                              already returned this lifecycle (read, never
                              recomputed).
            raw_confidence:  The STM ``confidence`` value as it stood BEFORE
                              ExecutionPlanningEngine's own adjustment --
                              i.e. the same ``confidence`` argument
                              finalize_incident() passed into
                              ``ExecutionPlanningEngine.plan()``. Used only to
                              honestly report the real delta (if any) between
                              the raw and plan-adjusted confidence -- never
                              to invent a per-source breakdown.

        Returns:
            A JSON-serialisable dict: ``decision_graph``,
            ``evidence_graph``, ``recommendation_trace``,
            ``confidence_breakdown``, ``evidence_contribution``,
            ``contradictions``, ``missing_evidence``, ``assumptions``,
            ``evidence_quality``, ``lower_priority_justification``,
            ``insufficient_evidence``.

        Raises:
            Never raises -- caught by the Orchestrator caller, same
            resilience contract as every other C/D-phase engine.
        """
        memory_data = _latest_finding_data(findings, "memory")
        policy_data = _latest_finding_data(findings, "policy")
        cross_dataset_data = _latest_finding_data(findings, "cross_dataset")
        adaptive_data = _latest_finding_data(findings, "adaptive")
        rag_data = _latest_finding_data(findings, "rag")

        memory_matches = (memory_data or {}).get("matches") or []
        policy_matches = (policy_data or {}).get("matches") or []
        possible_causes = sorted(
            (rag_data or {}).get("possible_causes") or [],
            key=lambda c: float(c.get("confidence", 0.0) or 0.0), reverse=True,
        )
        strong_correlations = (cross_dataset_data or {}).get("strong_correlations") or []

        sources_consulted = execution_plan.get("sources_consulted") or {}
        sources_with_signal = execution_plan.get("sources_with_signal") or {}
        recommended_actions = execution_plan.get("recommended_actions") or []

        evidence_graph = _build_evidence_graph(memory_matches, policy_matches, cross_dataset_data, adaptive_data, possible_causes)
        decision_graph = _build_decision_graph(recommended_actions, policy_matches, strong_correlations, possible_causes)
        recommendation_trace = _build_recommendation_trace(decision_graph)
        confidence_breakdown = _build_confidence_breakdown(
            raw_confidence, execution_plan.get("confidence"), execution_plan.get("evidence_conflicts") or [],
            memory_matches, policy_matches, cross_dataset_data, adaptive_data, possible_causes,
        )
        evidence_contribution = _build_evidence_contribution(
            sources_consulted, sources_with_signal, memory_matches, policy_matches,
            cross_dataset_data, adaptive_data, possible_causes, decision_graph,
        )
        contradictions = _build_contradictions(execution_plan.get("evidence_conflicts") or [])
        missing_evidence = _build_missing_evidence(sources_consulted, sources_with_signal)
        assumptions = _build_assumptions(policy_matches, cross_dataset_data, adaptive_data, rag_data)
        lower_priority_justification = _build_lower_priority_justification(sources_with_signal)

        return {
            "decision_graph": decision_graph,
            "evidence_graph": evidence_graph,
            "recommendation_trace": recommendation_trace,
            "confidence_breakdown": confidence_breakdown,
            "evidence_contribution": evidence_contribution,
            "contradictions": contradictions,
            "missing_evidence": missing_evidence,
            "assumptions": assumptions,
            "evidence_quality": execution_plan.get("evidence_quality"),
            "lower_priority_justification": lower_priority_justification,
            "insufficient_evidence": bool(execution_plan.get("insufficient_evidence")),
        }

    def __repr__(self) -> str:
        return "ExplainabilityEngine()"


# ---------------------------------------------------------------------------
# Internal builders
# ---------------------------------------------------------------------------

def _latest_finding_data(findings: list[dict[str, Any]], type_name: str) -> dict[str, Any] | None:
    """Identical scan pattern to ExecutionPlanningEngine's own helper -- reused, not reimplemented differently."""
    latest: dict[str, Any] | None = None
    for entry in findings or []:
        if isinstance(entry, dict) and entry.get("type") == type_name:
            latest = entry.get("data") or {}
    return latest


def _build_evidence_graph(
    memory_matches: list[dict[str, Any]],
    policy_matches: list[dict[str, Any]],
    cross_dataset_data: dict[str, Any] | None,
    adaptive_data: dict[str, Any] | None,
    possible_causes: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """
    Every piece of evidence collected this lifecycle, reorganized as graph
    nodes with a stable, traceable ``id`` -- independent of which
    recommendation (if any) ended up referencing it.
    """
    memory_nodes = [
        {"id": m.get("incident_id"), "similarity": m.get("similarity"),
         "root_cause": m.get("root_cause"), "resolution_status": m.get("resolution_status")}
        for m in memory_matches
    ]
    policy_nodes = [
        {"id": p.get("policy_id"), "business_rule": p.get("business_rule"),
         "source_document": p.get("source_document"), "similarity": p.get("similarity")}
        for p in policy_matches
    ]
    cross_dataset_nodes: list[dict[str, Any]] = []
    if cross_dataset_data is not None:
        for s in cross_dataset_data.get("supporting") or []:
            cross_dataset_nodes.append({"id": f"{s.get('dataset_name')}:{s.get('metric')}", "relation": "supporting", "z_score": s.get("z_score")})
        for r in cross_dataset_data.get("strong_correlations") or []:
            cross_dataset_nodes.append({"id": f"{r.get('dataset_name')}:{r.get('metric')}", "relation": "strong_correlation", "correlation": r.get("correlation")})
        for c in cross_dataset_data.get("contradicting") or []:
            cross_dataset_nodes.append({"id": f"{c.get('dataset_name')}:{c.get('metric')}", "relation": "contradicting"})
    adaptive_nodes: list[dict[str, Any]] = []
    if adaptive_data is not None:
        if adaptive_data.get("adaptive_baseline"):
            adaptive_nodes.append({"id": "adaptive_baseline", "z_score": adaptive_data["adaptive_baseline"].get("z_score")})
        if adaptive_data.get("seasonality") and adaptive_data["seasonality"].get("detected"):
            adaptive_nodes.append({"id": "seasonality", "strength": adaptive_data["seasonality"].get("strength")})
    retrieval_nodes = [
        {"id": c.get("chunk_id"), "cause": c.get("cause"), "confidence": c.get("confidence")}
        for c in possible_causes
    ]
    return {
        "memory": memory_nodes,
        "policy": policy_nodes,
        "cross_dataset": cross_dataset_nodes,
        "adaptive": adaptive_nodes,
        "retrieval": retrieval_nodes,
    }


def _build_decision_graph(
    recommended_actions: list[dict[str, Any]],
    policy_matches: list[dict[str, Any]],
    strong_correlations: list[dict[str, Any]],
    possible_causes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    For every recommendation: Recommendation -> supporting finding(s) ->
    supporting evidence (concrete ID) -> confidence signal -> report
    section. Zipped by ``source`` in ExecutionPlanningEngine's OWN fixed
    construction order (see module docstring) -- never re-matched by
    fuzzy/guessed text similarity.
    """
    graph: list[dict[str, Any]] = []
    cursors = {"policy": 0, "cross_dataset": 0, "retrieval": 0}
    for action in recommended_actions:
        source = action.get("source")
        evidence_id = None
        evidence_summary = None
        confidence_signal = None

        if source == "policy" and cursors["policy"] < len(policy_matches):
            p = policy_matches[cursors["policy"]]
            cursors["policy"] += 1
            evidence_id = p.get("policy_id")
            evidence_summary = f"Policy match: {p.get('business_rule')} ({p.get('source_document')})"
            confidence_signal = p.get("similarity")
        elif source == "cross_dataset" and cursors["cross_dataset"] < len(strong_correlations):
            r = strong_correlations[cursors["cross_dataset"]]
            cursors["cross_dataset"] += 1
            evidence_id = f"{r.get('dataset_name')}:{r.get('metric')}"
            evidence_summary = f"Strong correlation with {r.get('dataset_name')}/{r.get('metric')}"
            confidence_signal = r.get("correlation")
        elif source == "retrieval" and cursors["retrieval"] < len(possible_causes):
            c = possible_causes[cursors["retrieval"]]
            cursors["retrieval"] += 1
            evidence_id = c.get("chunk_id")
            evidence_summary = f"Retrieved cause: {c.get('cause')}"
            confidence_signal = c.get("confidence")
        elif source == "adaptive":
            match = _CHUNK_ID_RE.search(action.get("rationale") or "")
            evidence_id = "seasonality"
            evidence_summary = "Adaptive Detection seasonality signal"
        elif source == "runbook":
            evidence_id = None
            evidence_summary = "No originating evidence item -- standard, pre-existing runbook guidance."

        graph.append({
            "order": action.get("order"),
            "recommendation": action.get("action"),
            "source": source,
            "supporting_finding_type": source,
            "evidence_id": evidence_id,
            "evidence_summary": evidence_summary,
            "confidence_contribution": confidence_signal,
            "classification": action.get("classification"),
            "report_section": _REPORT_SECTION_BY_SOURCE.get(source, "not documented in any existing report section"),
        })
    return graph


def _build_recommendation_trace(decision_graph: list[dict[str, Any]]) -> list[str]:
    """One honest narrative sentence per recommendation, built entirely from decision_graph's own fields."""
    trace: list[str] = []
    for node in decision_graph:
        if node["evidence_id"] is not None:
            trace.append(
                f"Recommendation {node['order']} ('{node['recommendation']}') exists because "
                f"{node['source']} evidence [{node['evidence_id']}] -- {node['evidence_summary']}."
            )
        else:
            trace.append(
                f"Recommendation {node['order']} ('{node['recommendation']}') exists because "
                f"{node['evidence_summary']}"
            )
    return trace


def _build_confidence_breakdown(
    raw_confidence: float | None,
    plan_confidence: float | None,
    evidence_conflicts: list[dict[str, Any]],
    memory_matches: list[dict[str, Any]],
    policy_matches: list[dict[str, Any]],
    cross_dataset_data: dict[str, Any] | None,
    adaptive_data: dict[str, Any] | None,
    possible_causes: list[dict[str, Any]],
) -> dict[str, Any]:
    raw = float(raw_confidence) if isinstance(raw_confidence, (int, float)) else None
    plan = float(plan_confidence) if isinstance(plan_confidence, (int, float)) else None
    adjustment = round(plan - raw, 4) if raw is not None and plan is not None else None

    if adjustment is not None and adjustment < 0:
        adjustment_reason = (
            f"Confidence was reduced by {abs(adjustment)} because {len(evidence_conflicts)} "
            f"evidence conflict(s) were detected (see contradictions)."
        )
    elif adjustment is not None and adjustment == 0:
        adjustment_reason = "No adjustment -- no evidence conflicts were detected."
    else:
        adjustment_reason = "Raw pre-plan confidence was not available -- adjustment cannot be honestly computed."

    per_source: list[dict[str, Any]] = []

    if policy_matches:
        sims = [p.get("similarity") for p in policy_matches if isinstance(p.get("similarity"), (int, float))]
        per_source.append({
            "source": "policy", "consulted": True, "has_signal": True,
            "raw_value": round(max(sims), 4) if sims else None,
            "raw_value_label": "top policy match similarity" if sims else "no similarity score recorded",
        })
    else:
        per_source.append({"source": "policy", "consulted": policy_matches is not None, "has_signal": False, "raw_value": None, "raw_value_label": "no policy matched"})

    if memory_matches:
        confs = [m.get("confidence") for m in memory_matches if isinstance(m.get("confidence"), (int, float))]
        per_source.append({
            "source": "memory", "consulted": True, "has_signal": True,
            "raw_value": round(sum(confs) / len(confs), 4) if confs else None,
            "raw_value_label": "average confidence of similar past incidents" if confs else "no confidence recorded on past incidents",
        })
    else:
        per_source.append({"source": "memory", "consulted": memory_matches is not None, "has_signal": False, "raw_value": None, "raw_value_label": "no similar past incidents found"})

    if cross_dataset_data is not None and not cross_dataset_data.get("insufficient_data"):
        corrs = [r.get("correlation") for r in (cross_dataset_data.get("strong_correlations") or []) if isinstance(r.get("correlation"), (int, float))]
        per_source.append({
            "source": "cross_dataset", "consulted": True, "has_signal": bool(corrs) or bool(cross_dataset_data.get("supporting")),
            "raw_value": round(max(corrs), 4) if corrs else None,
            "raw_value_label": "no scalar confidence value -- cross-dataset correlation is a structural/statistical signal, not a probability",
        })
    else:
        per_source.append({"source": "cross_dataset", "consulted": cross_dataset_data is not None, "has_signal": False, "raw_value": None, "raw_value_label": "insufficient activated datasets to compare" if cross_dataset_data else "not consulted"})

    if adaptive_data is not None:
        seasonality = adaptive_data.get("seasonality")
        strength = seasonality.get("strength") if seasonality and seasonality.get("detected") else None
        per_source.append({
            "source": "adaptive", "consulted": True, "has_signal": bool(adaptive_data.get("combined_signal")),
            "raw_value": strength,
            "raw_value_label": "seasonality strength (not a probability)" if strength is not None else "no seasonality/baseline signal available",
        })
    else:
        per_source.append({"source": "adaptive", "consulted": False, "has_signal": False, "raw_value": None, "raw_value_label": "not consulted"})

    if possible_causes:
        top_conf = possible_causes[0].get("confidence")
        per_source.append({
            "source": "retrieval", "consulted": True, "has_signal": True,
            "raw_value": top_conf, "raw_value_label": "top retrieval-grounded cause confidence",
        })
    else:
        per_source.append({"source": "retrieval", "consulted": possible_causes is not None, "has_signal": False, "raw_value": None, "raw_value_label": "no possible causes retrieved"})

    return {
        "raw_confidence": raw,
        "plan_confidence": plan,
        "adjustment": adjustment,
        "adjustment_reason": adjustment_reason,
        "per_source": per_source,
    }


def _build_evidence_contribution(
    sources_consulted: dict[str, bool],
    sources_with_signal: dict[str, bool],
    memory_matches: list[dict[str, Any]],
    policy_matches: list[dict[str, Any]],
    cross_dataset_data: dict[str, Any] | None,
    adaptive_data: dict[str, Any] | None,
    possible_causes: list[dict[str, Any]],
    decision_graph: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    counts = {
        "policy": len(policy_matches),
        "memory": len(memory_matches),
        "cross_dataset": len((cross_dataset_data or {}).get("supporting") or []) + len((cross_dataset_data or {}).get("strong_correlations") or []) + len((cross_dataset_data or {}).get("contradicting") or []),
        "adaptive": (1 if adaptive_data and adaptive_data.get("adaptive_baseline") else 0) + (1 if adaptive_data and adaptive_data.get("seasonality") and adaptive_data["seasonality"].get("detected") else 0),
        "retrieval": len(possible_causes),
    }
    contribution: list[dict[str, Any]] = []
    for source in _SOURCE_PRIORITY:
        influenced = [n["order"] for n in decision_graph if n["source"] == source]
        contribution.append({
            "source": source,
            "consulted": bool(sources_consulted.get(source)),
            "has_signal": bool(sources_with_signal.get(source)),
            "evidence_count": counts.get(source, 0),
            "recommendations_influenced": influenced,
        })
    return contribution


def _build_contradictions(evidence_conflicts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Direct passthrough of ExecutionPlanningEngine's own conflict detection -- never recomputed."""
    return [
        {"between": c.get("between"), "description": c.get("description")}
        for c in evidence_conflicts
    ]


def _build_missing_evidence(sources_consulted: dict[str, bool], sources_with_signal: dict[str, bool]) -> list[dict[str, Any]]:
    missing: list[dict[str, Any]] = []
    for source in _SOURCE_PRIORITY:
        consulted = bool(sources_consulted.get(source))
        has_signal = bool(sources_with_signal.get(source))
        if not consulted:
            missing.append({"source": source, "reason": f"{source} was not consulted for this investigation (engine unavailable or not wired)."})
        elif not has_signal:
            missing.append({"source": source, "reason": f"{source} was consulted but produced no usable signal for this incident."})
    return missing


def _build_assumptions(
    policy_matches: list[dict[str, Any]],
    cross_dataset_data: dict[str, Any] | None,
    adaptive_data: dict[str, Any] | None,
    rag_data: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """
    Only genuinely traceable assumptions -- each tied to a specific,
    already-computed field. Empty list if none of these conditions actually
    hold, never padded to look complete.
    """
    assumptions: list[dict[str, Any]] = []

    if any(p.get("match_reason") == "metric" for p in policy_matches):
        assumptions.append({
            "assumption": "A metric-name match was assumed sufficient to apply the policy, without independent semantic verification.",
            "based_on": "policy match_reason == 'metric'",
        })

    if cross_dataset_data is not None and cross_dataset_data.get("insufficient_data"):
        assumptions.append({
            "assumption": "No systemic/correlated cause was assumed, because too few datasets were activated to check for one.",
            "based_on": "cross_dataset.insufficient_data == true",
        })

    if adaptive_data is not None and adaptive_data.get("adaptive_baseline_insufficient"):
        assumptions.append({
            "assumption": "A standard (non-adaptive) threshold was assumed, because too little history existed to compute an adaptive baseline.",
            "based_on": "adaptive.adaptive_baseline_insufficient is set",
        })

    if rag_data is not None and rag_data.get("metadata_filter_applied") and any(
        c.get("metadata_filter_relaxed") for c in (rag_data.get("retrieved_chunks") or [])
    ):
        assumptions.append({
            "assumption": "Metadata-based evidence filtering was assumed too strict and was relaxed to an unfiltered search, because no tagged evidence matched.",
            "based_on": "rag.retrieved_chunks[].metadata_filter_relaxed == true",
        })

    return assumptions


def _build_lower_priority_justification(sources_with_signal: dict[str, bool]) -> dict[str, Any]:
    """
    Re-expresses the SAME priority constant and the SAME booleans
    ExecutionPlanningEngine already computed as a structured field --
    re-deriving a structural fact already implicit in its own explanation
    text, never re-analyzing evidence.
    """
    highest_available = next((s for s in _SOURCE_PRIORITY if sources_with_signal.get(s)), None)
    lower_priority_used = highest_available is not None and highest_available != _SOURCE_PRIORITY[0]
    if highest_available is None:
        reason = "No evidence source produced a usable signal; recommendations rely on standard runbook guidance only."
    elif lower_priority_used:
        reason = (
            f"No higher-priority evidence ({', '.join(_SOURCE_PRIORITY[:_SOURCE_PRIORITY.index(highest_available)])}) "
            f"was available, so '{highest_available}' -- a lower-priority source -- was used instead."
        )
    else:
        reason = "The highest-priority evidence source (policy) was available and used."
    return {
        "lower_priority_used": lower_priority_used,
        "highest_priority_available": highest_available,
        "reason": reason,
    }
