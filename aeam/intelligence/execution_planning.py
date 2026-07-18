"""
aeam/intelligence/execution_planning.py

Enterprise Action Planning Engine (Phase C7).

Synthesizes ALL evidence already produced during an incident's investigation
lifecycle — Enterprise Memory (C1), Enterprise Policies (C3), Cross-Dataset
Intelligence (C4), Adaptive Detection (C5), and Advanced Retrieval (C6) --
into ONE explainable execution plan. This module performs NO retrieval, NO
detection, and NO LLM call of its own: it is pure, deterministic synthesis
over the ``findings`` list the Orchestrator has already assembled by the time
:meth:`ExecutionPlanningEngine.plan` is called (see
``Orchestrator.finalize_incident()``, invoked once, immediately before the
runbook's ActionAgent execution loop).

Design rationale (Architecture Gate conclusion):
- A new planning PIPELINE is not necessary. The existing investigation
  lifecycle already accumulates every evidence source this engine needs as
  plain ``{"type": ..., "data": ...}`` dicts inside STM's ``findings`` list.
  This engine is invoked exactly like CrossDatasetAnalyzer/
  AdaptiveDetectionEngine/PolicyRegistry -- a plain class with one public
  method, composed into the Orchestrator by constructor injection, guarded by
  the same idempotency convention, appended as one more findings entry
  (``type == "execution_plan"``). ReportAgent, the Investigation Workspace,
  and LongTermMemory persistence all already generalize over "whatever is in
  findings" -- so nothing about them needs to change shape, only grow one
  more section, exactly like every prior C-phase.
- Deterministic (no LLM) by design: every output field must be traceable to
  a specific already-computed evidence value. An LLM re-summarizing evidence
  would reintroduce exactly the fabrication risk the mission explicitly
  forbids ("never fabricate recommendations/confidence/business impact").
  Plain-Python synthesis over structured dicts is the only way to guarantee
  every recommendation and every conflict is a fact about the input, not a
  guess.
- Priority order (Policies > Memory > Cross-Dataset > Adaptive > Retrieval)
  governs which evidence source is allowed to PRODUCE a recommended action.
  Lower-priority sources may still contribute supporting evidence, risk
  commentary, and informational-only recommendations, but the engine always
  states explicitly when no higher-priority evidence was available and a
  lower-priority source had to be used instead (see ``_priority_used`` in the
  returned explanation).
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Priority order for which evidence source is allowed to generate a
# recommended ACTION (not merely supporting evidence). Lower index = higher
# priority, per the mission's explicit priority rules.
_SOURCE_PRIORITY: tuple[str, ...] = ("policy", "memory", "cross_dataset", "adaptive", "retrieval")

_CLASSIFICATION_EXECUTE = "execute_immediately"
_CLASSIFICATION_APPROVAL = "requires_human_approval"
_CLASSIFICATION_INFO = "informational_only"

# Confidence gap below which the top two RAG possible_causes are considered
# "not clearly distinguished" -- an honest ambiguity signal, not a fabricated
# one (both confidences are real values already produced by RAGAgent's LLM).
_AMBIGUOUS_CAUSE_GAP: float = 0.15

# Confidence ceiling applied to the plan's overall confidence whenever any
# evidence_conflicts were recorded -- conflicting evidence should never let
# a plan present itself as highly confident, regardless of individual
# source confidences.
_CONFLICT_CONFIDENCE_CAP: float = 0.5

_QUALITY_INSUFFICIENT = "insufficient"
_QUALITY_LOW = "low"
_QUALITY_MEDIUM = "medium"
_QUALITY_HIGH = "high"


class ExecutionPlanningEngine:
    """
    Synthesizes investigation findings into one explainable execution plan.

    Stateless and dependency-free -- every input needed to synthesize a plan
    is passed explicitly to :meth:`plan`. No retrieval pipeline, no LLM
    service, no database handle; this engine only reads dicts the
    Orchestrator already computed. The constructor is entirely OPTIONAL --
    every existing zero-arg ``ExecutionPlanningEngine()`` call site keeps
    working unchanged.

    Args:
        ambiguous_cause_gap: Overrides ``_AMBIGUOUS_CAUSE_GAP`` (Phase D4
                             Enterprise Configuration Engine). ``None`` (the
                             default) preserves the module default (0.15).
        conflict_confidence_cap: Overrides the confidence ceiling applied
                             when evidence conflicts exist. ``None`` (the
                             default) preserves the module default (0.5).
        approval_required_quality_levels: Overrides which
                             ``evidence_quality`` levels force
                             ``human_approval_required=True``. ``None`` (the
                             default) preserves the module default
                             (``insufficient``, ``low``).
    """

    def __init__(
        self,
        ambiguous_cause_gap: float | None = None,
        conflict_confidence_cap: float | None = None,
        approval_required_quality_levels: tuple[str, ...] | None = None,
    ) -> None:
        self._ambiguous_cause_gap = (
            ambiguous_cause_gap if ambiguous_cause_gap is not None else _AMBIGUOUS_CAUSE_GAP
        )
        self._conflict_confidence_cap = (
            conflict_confidence_cap if conflict_confidence_cap is not None else _CONFLICT_CONFIDENCE_CAP
        )
        self._approval_required_quality_levels = (
            approval_required_quality_levels
            if approval_required_quality_levels is not None
            else (_QUALITY_INSUFFICIENT, _QUALITY_LOW)
        )

    def plan(
        self,
        *,
        event_type: str,
        metric: str,
        severity: str,
        current_value: float | None,
        expected_value: float | None,
        findings: list[dict[str, Any]],
        root_cause: str | None,
        confidence: float | None,
        requires_human: bool,
        runbook_recommended_actions: list[str],
    ) -> dict[str, Any]:
        """
        Build the execution plan for one incident.

        Args:
            event_type, metric, severity, current_value, expected_value:
                Plain fields already on the incident's ``Event`` -- read-only,
                never re-derived.
            findings: The FULL, already-populated STM ``findings`` list for
                      this incident (every C1/C3/C4/C5/C6 entry, plus
                      internal ones this engine ignores).
            root_cause, confidence, requires_human: Already-established STM
                      values at finalize time -- reused, never recomputed.
            runbook_recommended_actions: The event_type's existing runbook
                      advisory text (``aeam.agents.orchestrator.runbooks``),
                      reused as the deterministic baseline action set rather
                      than invented.

        Returns:
            A JSON-serialisable dict -- see module docstring's field list
            (mirrors the mission's 11 required output fields exactly):
            ``executive_summary``, ``recommended_actions``, ``order_rationale``,
            ``supporting_evidence``, ``business_risk_assessment``,
            ``expected_impact``, ``confidence``, ``evidence_quality``,
            ``evidence_conflicts``, ``human_approval_required``,
            ``explanation``, ``insufficient_evidence``.

        Raises:
            Never raises -- any internal error is caught by the Orchestrator
            caller (same resilience contract as every other C-phase engine);
            this method itself has no I/O to fail on.
        """
        memory_data = _latest_finding_data(findings, "memory")
        policy_data = _latest_finding_data(findings, "policy")
        cross_dataset_data = _latest_finding_data(findings, "cross_dataset")
        adaptive_data = _latest_finding_data(findings, "adaptive")
        rag_data = _latest_finding_data(findings, "rag")

        memory_matches = (memory_data or {}).get("matches") or []
        policy_matches = (policy_data or {}).get("matches") or []
        possible_causes = (rag_data or {}).get("possible_causes") or []

        sources_consulted = {
            "policy": policy_data is not None,
            "memory": memory_data is not None,
            "cross_dataset": cross_dataset_data is not None,
            "adaptive": adaptive_data is not None,
            "retrieval": rag_data is not None,
        }
        sources_with_signal = {
            "policy": bool(policy_matches),
            "memory": bool(memory_matches),
            "cross_dataset": bool((cross_dataset_data or {}).get("supporting")
                                   or (cross_dataset_data or {}).get("strong_correlations")),
            "adaptive": bool((adaptive_data or {}).get("combined_signal")),
            "retrieval": bool(possible_causes),
        }
        signal_count = sum(1 for v in sources_with_signal.values() if v)

        recommended_actions: list[dict[str, Any]] = []
        supporting_evidence: list[dict[str, str]] = []
        evidence_conflicts: list[dict[str, Any]] = []

        # --- 1. Policy (highest priority): each matched policy's own
        # business-rule action text becomes a recommended action. ---
        for p in policy_matches:
            actions_text = _stringify_policy_actions(p.get("actions")) or p.get("business_rule") or "Follow matched enterprise policy guidance."
            approval_required = bool(p.get("approval_required"))
            recommended_actions.append({
                "action": actions_text,
                "source": "policy",
                "rationale": (
                    f"Matched enterprise policy '{p.get('business_rule') or p.get('policy_id')}' "
                    f"(matched_by={p.get('match_reason')}"
                    + (f", similarity={p.get('similarity')}" if p.get("similarity") is not None else "")
                    + f") -- condition: {p.get('condition') or 'not recorded'}."
                ),
                "classification": _CLASSIFICATION_APPROVAL if approval_required else _CLASSIFICATION_EXECUTE,
            })
            supporting_evidence.append({
                "source": "policy",
                "summary": f"Policy '{p.get('business_rule') or p.get('policy_id')}' from {p.get('source_document') or 'unknown source'}.",
            })

        if policy_matches:
            approval_flags = {bool(p.get("approval_required")) for p in policy_matches}
            if len(approval_flags) > 1:
                evidence_conflicts.append({
                    "between": ["policy", "policy"],
                    "description": (
                        "Multiple matched policies disagree on whether human approval is "
                        "required for this incident -- defaulting to the stricter (approval-required) reading."
                    ),
                })

        # --- 2. Memory: corroborative only (no historical action data is
        # persisted in matches), so it never generates its own action -- it
        # only strengthens/weakens confidence and is surfaced as evidence. ---
        if memory_matches:
            resolved = [m for m in memory_matches if m.get("resolution_status") == "RESOLVED"]
            escalated = [m for m in memory_matches if m.get("resolution_status") == "ESCALATED"]
            supporting_evidence.append({
                "source": "memory",
                "summary": (
                    f"{len(memory_matches)} similar past incident(s) found -- "
                    f"{len(resolved)} resolved, {len(escalated)} escalated."
                ),
            })
            if resolved and escalated:
                evidence_conflicts.append({
                    "between": ["memory", "memory"],
                    "description": (
                        f"Similar past incidents had inconsistent outcomes "
                        f"({len(resolved)} resolved vs {len(escalated)} escalated) -- "
                        "no single historical action reliably resolved this pattern."
                    ),
                })

        # --- 3. Cross-Dataset: contradicting evidence is a genuine,
        # already-computed conflict signal -- surfaced directly. ---
        if cross_dataset_data is not None and not cross_dataset_data.get("insufficient_data"):
            supporting = cross_dataset_data.get("supporting") or []
            contradicting = cross_dataset_data.get("contradicting") or []
            strong_corr = cross_dataset_data.get("strong_correlations") or []
            for s in supporting:
                supporting_evidence.append({
                    "source": "cross_dataset",
                    "summary": f"{s.get('dataset_name')}/{s.get('metric')} also anomalous (z={s.get('z_score')}, relation={s.get('relation')}).",
                })
            for r in strong_corr:
                recommended_actions.append({
                    "action": f"Investigate correlated dataset '{r.get('dataset_name')}' (metric: {r.get('metric')})",
                    "source": "cross_dataset",
                    "rationale": f"Strong correlation (r={r.get('correlation')}, overlapping_dates={r.get('overlapping_dates')}) with this incident's metric.",
                    "classification": _CLASSIFICATION_INFO,
                })
            if contradicting:
                evidence_conflicts.append({
                    "between": ["cross_dataset", "current_incident"],
                    "description": (
                        f"{len(contradicting)} correlated dataset(s) remained statistically normal "
                        "despite this incident, weakening the case for a systemic/shared root cause."
                    ),
                })

        # --- 4. Adaptive Detection: seasonality becomes an informational
        # caveat/action; corroborating signals feed supporting evidence. ---
        if adaptive_data is not None:
            seasonality = adaptive_data.get("seasonality")
            if seasonality and seasonality.get("detected"):
                recommended_actions.append({
                    "action": (
                        f"Account for detected weekday seasonality (peak: {seasonality.get('highest_weekday')}, "
                        f"trough: {seasonality.get('lowest_weekday')}) when judging baseline deviation."
                    ),
                    "source": "adaptive",
                    "rationale": f"Seasonality strength {seasonality.get('strength')} exceeds the significance threshold.",
                    "classification": _CLASSIFICATION_INFO,
                })
            if adaptive_data.get("combined_signal"):
                supporting_evidence.append({
                    "source": "adaptive",
                    "summary": f"Adaptive baseline corroborated by: {', '.join(adaptive_data.get('corroborating_signals') or [])}.",
                })

        # --- 5. Retrieval (lowest priority): possible_causes become
        # informational actions; ambiguity between top causes is a genuine,
        # already-computed conflict (real confidence values, no guessing). ---
        if possible_causes:
            ranked = sorted(possible_causes, key=lambda c: float(c.get("confidence", 0.0) or 0.0), reverse=True)
            top = ranked[0]
            recommended_actions.append({
                "action": f"Investigate: {top.get('cause')}",
                "source": "retrieval",
                "rationale": f"Top-ranked retrieval-grounded cause (confidence={top.get('confidence')}, chunk_id={top.get('chunk_id')}).",
                "classification": _CLASSIFICATION_INFO,
            })
            supporting_evidence.append({
                "source": "retrieval",
                "summary": f"{len(possible_causes)} possible cause(s) retrieved; top confidence {top.get('confidence')}.",
            })
            if len(ranked) >= 2:
                gap = float(ranked[0].get("confidence", 0.0) or 0.0) - float(ranked[1].get("confidence", 0.0) or 0.0)
                if gap < self._ambiguous_cause_gap:
                    evidence_conflicts.append({
                        "between": ["retrieval", "retrieval"],
                        "description": (
                            f"Top two retrieved causes ('{ranked[0].get('cause')}' vs '{ranked[1].get('cause')}') "
                            f"are not clearly distinguished by confidence (gap={round(gap, 3)}) -- treat as ambiguous causation."
                        ),
                    })

        # --- Baseline: the event_type's existing runbook advisory text is
        # ALWAYS included (deterministic, pre-existing -- never invented by
        # this engine) so the plan never claims "no recommendation" when a
        # standard runbook already exists. ---
        for text in runbook_recommended_actions:
            recommended_actions.append({
                "action": text,
                "source": "runbook",
                "rationale": f"Standard runbook guidance for event_type={event_type!r}.",
                "classification": _CLASSIFICATION_APPROVAL,
            })

        # --- Order: by source priority, ties broken by original insertion
        # order (stable sort). ---
        priority_index = {**{s: i for i, s in enumerate(_SOURCE_PRIORITY)}, "runbook": len(_SOURCE_PRIORITY)}
        recommended_actions.sort(key=lambda a: priority_index.get(a["source"], 99))
        for i, a in enumerate(recommended_actions, start=1):
            a["order"] = i

        used_sources = [s for s in _SOURCE_PRIORITY if sources_with_signal[s]]
        highest_available = next((s for s in _SOURCE_PRIORITY if sources_with_signal[s]), None)
        order_rationale = (
            f"Recommendations are ordered by evidence priority (policy > memory > cross_dataset > "
            f"adaptive > retrieval), then standard runbook guidance last. "
            + (
                f"For this incident, {', '.join(used_sources)} produced usable signal."
                if used_sources
                else "No evidence source produced a usable signal for this incident; only runbook guidance is available."
            )
        )

        # --- Evidence quality + insufficiency ---
        insufficient_evidence = signal_count == 0
        if insufficient_evidence:
            evidence_quality = _QUALITY_INSUFFICIENT
        elif signal_count == 1:
            evidence_quality = _QUALITY_LOW
        elif signal_count in (2, 3):
            evidence_quality = _QUALITY_MEDIUM
        else:
            evidence_quality = _QUALITY_HIGH

        # --- Confidence: reuse the SAME confidence value already
        # established by investigate() -- never invented -- honestly capped
        # downward when conflicts exist or evidence is thin. ---
        base_confidence = float(confidence) if isinstance(confidence, (int, float)) else 0.0
        if insufficient_evidence:
            plan_confidence = 0.0
        elif evidence_conflicts:
            plan_confidence = min(base_confidence, self._conflict_confidence_cap)
        else:
            plan_confidence = base_confidence

        human_approval_required = bool(
            requires_human
            or evidence_conflicts
            or evidence_quality in self._approval_required_quality_levels
            or any(a["classification"] == _CLASSIFICATION_APPROVAL for a in recommended_actions)
        )

        # --- Business risk / expected impact ---
        deviation_pct = _deviation_percent(current_value, expected_value)
        risk_parts = [f"{severity} severity {event_type} incident on metric '{metric}'"]
        if deviation_pct is not None:
            risk_parts.append(f"with a {deviation_pct}% deviation from the expected baseline")
        if policy_matches:
            depts = sorted({p.get("department") for p in policy_matches if p.get("department")})
            if depts:
                risk_parts.append(f"escalation policy names {', '.join(depts)} as the responsible department")
        if sources_with_signal["cross_dataset"]:
            risk_parts.append("correlated datasets suggest potential broader systemic impact")
        business_risk_assessment = "; ".join(risk_parts) + "."

        if sources_with_signal["cross_dataset"]:
            expected_impact = (
                f"Potential impact beyond '{metric}': correlated anomalies detected in "
                f"{len(cross_dataset_data.get('supporting') or [])} other dataset(s)."
            )
        else:
            expected_impact = f"Impact currently confined to '{metric}' -- no correlated dataset confirms broader systemic effect."

        # --- Executive summary ---
        if insufficient_evidence:
            executive_summary = (
                f"Insufficient evidence to synthesize a confident execution plan for this "
                f"{severity} {event_type} incident. No enterprise policy, historical memory, "
                f"cross-dataset correlation, adaptive baseline, or retrieval evidence produced a usable signal."
            )
        else:
            executive_summary = (
                f"{severity} {event_type} incident on '{metric}'. "
                f"Root cause: {root_cause or 'not yet established'}. "
                f"{len(recommended_actions)} recommendation(s) synthesized from {', '.join(used_sources) or 'runbook guidance only'}. "
                f"{'Evidence conflicts were detected -- see below.' if evidence_conflicts else 'No evidence conflicts detected.'}"
            )

        # --- Explanation: WHY this plan looks the way it does, including
        # the mandatory "lower-priority-override" justification. ---
        explanation_parts = [order_rationale]
        if highest_available and highest_available != "policy":
            explanation_parts.append(
                f"No enterprise policy matched this incident (the highest-priority evidence source), "
                f"so recommendations instead rely on '{highest_available}' evidence; confidence and "
                f"human-approval requirements are adjusted accordingly rather than treating it as policy-grade certainty."
            )
        if evidence_conflicts:
            explanation_parts.append(
                f"{len(evidence_conflicts)} evidence conflict(s) were detected and are surfaced explicitly "
                f"rather than silently resolved -- plan confidence was capped at {plan_confidence} as a result."
            )
        if insufficient_evidence:
            explanation_parts.append(
                "No corroborating or contradicting evidence exists for this incident from any consulted source; "
                "this plan intentionally does not fabricate a recommendation beyond standard runbook guidance."
            )
        explanation = " ".join(explanation_parts)

        return {
            "executive_summary": executive_summary,
            "recommended_actions": recommended_actions,
            "order_rationale": order_rationale,
            "supporting_evidence": supporting_evidence,
            "business_risk_assessment": business_risk_assessment,
            "expected_impact": expected_impact,
            "confidence": round(plan_confidence, 4),
            "evidence_quality": evidence_quality,
            "evidence_conflicts": evidence_conflicts,
            "human_approval_required": human_approval_required,
            "explanation": explanation,
            "insufficient_evidence": insufficient_evidence,
            "sources_consulted": sources_consulted,
            "sources_with_signal": sources_with_signal,
        }

    def __repr__(self) -> str:
        return "ExecutionPlanningEngine()"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _latest_finding_data(findings: list[dict[str, Any]], type_name: str) -> dict[str, Any] | None:
    """
    Return the ``data`` dict of the most recent findings entry of ``type_name``.

    Mirrors the exact scan pattern used by every ReportAgent
    ``_format_xxx`` method and every ``Orchestrator._has_xxx_finding()``
    helper. Returns ``None`` (never ``{}``) when the source was never
    consulted at all, so callers can distinguish "not consulted" from
    "consulted and found nothing."
    """
    latest: dict[str, Any] | None = None
    for entry in findings or []:
        if isinstance(entry, dict) and entry.get("type") == type_name:
            latest = entry.get("data") or {}
    return latest


def _stringify_policy_actions(actions: Any) -> str | None:
    """
    Render a policy's ``actions`` field (a ``list[str]`` per
    ``aeam.registry.models.Policy``) as one readable action string.

    A recommended action must always be a single string (per the mission's
    "recommended_actions" schema) -- a policy's own ``actions`` list is
    joined honestly, never truncated or reinterpreted.
    """
    if not actions:
        return None
    if isinstance(actions, str):
        return actions
    if isinstance(actions, list):
        return "; ".join(str(a) for a in actions if a)
    return str(actions)


def _deviation_percent(current_value: float | None, expected_value: float | None) -> float | None:
    """Honest percentage deviation, or ``None`` if either value is missing/zero-baseline."""
    if current_value is None or expected_value in (None, 0):
        return None
    try:
        return round(abs(float(current_value) - float(expected_value)) / abs(float(expected_value)) * 100, 1)
    except (TypeError, ZeroDivisionError, ValueError):
        return None
