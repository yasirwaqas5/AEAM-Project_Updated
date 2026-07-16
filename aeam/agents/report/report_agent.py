"""
aeam/agents/report/report_agent.py

Report Agent for human-readable incident report and alert generation in AEAM Phase 7.

Extracts investigation data from ShortTermMemory, loads text templates from
the filesystem, and produces structured report and alert dicts. Optionally
uses an injected LLM for narrative enrichment — but works fully in
deterministic fallback mode when no LLM is provided.

Phase 7 constraints (all enforced):
- Content generation only. No action execution.
- No decision logic.
- No database writes.
- No external API calls.
- LLM usage only inside this agent, and always optional.
- Memory is never modified.
- All missing fields handled safely via .get().
- Always returns a valid dict — no exceptions leak to callers.
- Templates loaded from aeam/templates/ on the filesystem.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from aeam.monitoring.logging_config import get_logger

from aeam.config.settings import Settings
from aeam.memory.short_term import ShortTermMemory

logger = get_logger(__name__, agent="report")

# LLM generation parameters (Phase 7 spec).
_LLM_TEMPERATURE: float = 0.4
_LLM_MAX_TOKENS: int = 1200

# Filesystem paths for templates, relative to this file's package root.
# Resolved at runtime so the agent works regardless of the working directory.
_TEMPLATE_DIR: Path = Path(__file__).resolve().parents[2] / "templates"
_INCIDENT_REPORT_TEMPLATE: Path = _TEMPLATE_DIR / "incident_report.txt"
_SLACK_ALERT_TEMPLATE: Path = _TEMPLATE_DIR / "slack_alert.txt"

# Fallback confidence when not present in memory.
_DEFAULT_CONFIDENCE: float = 0.5


class ReportAgent:
    """
    Generates human-readable incident reports and Slack alerts from investigation data.

    Extracts structured data from :class:`~aeam.memory.short_term.ShortTermMemory`,
    fills text templates loaded from the filesystem, and optionally enriches
    output with LLM-generated narratives.

    The agent is fully functional without an LLM — in fallback mode it
    produces deterministic, template-driven output from memory fields alone.
    When an LLM is provided and available, it generates richer executive
    summaries and detailed narratives.

    This agent:
    - Generates content only.
    - Makes no external API calls.
    - Makes no decisions.
    - Writes nothing to any database.
    - Never modifies the ShortTermMemory it reads from.
    - Always returns a valid dict (exceptions are caught and surfaced as
      error fields, never propagated to callers).

    Args:
        settings: Application configuration.
        llm:      Optional LLM service. Must expose
                  ``query(prompt: str) -> str`` when provided.
                  Pass ``None`` (default) for deterministic fallback mode.

    Example::

        # With LLM
        agent = ReportAgent(settings=settings, llm=llm_service)

        # Without LLM (fallback mode)
        agent = ReportAgent(settings=settings)

        report = agent.generate_report(memory=stm)
        alert  = agent.generate_alert(memory=stm)
    """

    def __init__(
        self,
        settings: Settings,
        llm: Any = None,
    ) -> None:
        """
        Initialise the ReportAgent.

        Args:
            settings: Application Settings instance.
            llm:      Optional LLM service. When ``None``, the agent operates
                      in deterministic fallback mode.
        """
        self._settings: Settings = settings
        self._llm: Any = llm

        mode = "LLM-enabled" if llm is not None else "deterministic fallback"
        logger.info("ReportAgent initialised | mode=%s", mode)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def generate_report(self, memory: ShortTermMemory) -> dict[str, Any]:
        """
        Generate a structured incident report from investigation memory.

        Extracts investigation fields from ``memory``, loads the incident
        report template, and produces an executive summary, detailed report,
        and confidence value.

        When an LLM is available, the template is formatted into a prompt
        and the LLM generates the narrative content. When no LLM is
        available (or if LLM generation fails), deterministic content is
        produced directly from the memory fields.

        Memory keys read (all via ``.get()`` with safe defaults):
        - ``event_type``, ``metric``, ``severity``
        - ``findings``, ``root_cause``, ``evidence``, ``actions_taken``
        - ``confidence``

        The memory object is never modified.

        Args:
            memory: Active :class:`~aeam.memory.short_term.ShortTermMemory`
                    for the current investigation. Read-only.

        Returns:
            Dict::

                {
                    "executive_summary": str,
                    "detailed_report":   str,
                    "confidence":        float,
                }

            On unexpected error, returns the same schema with error context
            in ``executive_summary`` and ``confidence`` set to 0.0.

        Note:
            This method never raises. All exceptions are caught, logged,
            and returned as structured error output.
        """
        try:
            return self._generate_report_inner(memory)
        except Exception as exc:  # noqa: BLE001
            logger.error("generate_report | unexpected error: %s", exc)
            return {
                "executive_summary": f"Report generation failed: {exc}",
                "detailed_report":   "Unable to generate report due to an internal error.",
                "confidence":        0.0,
            }

    def generate_alert(self, memory: ShortTermMemory) -> dict[str, Any]:
        """
        Generate a concise Slack alert message from investigation memory.

        Extracts alert fields from ``memory``, loads the Slack alert template,
        fills placeholders, and returns a dict ready for
        :class:`~aeam.agents.action.slack_actions.SlackActions`.

        Memory keys read (all via ``.get()`` with safe defaults):
        - ``event_type``, ``severity``
        - ``summary`` (fallback: ``findings``, then ``"No summary available"``)
        - ``action`` (fallback: ``actions_taken``, then ``"No action recorded"``)

        The memory object is never modified.

        Args:
            memory: Active :class:`~aeam.memory.short_term.ShortTermMemory`
                    for the current investigation. Read-only.

        Returns:
            Dict::

                {
                    "message":    str,  # filled template string
                    "severity":   str,
                    "event_type": str,
                }

            On unexpected error, returns the same schema with error context
            in ``message``.

        Note:
            This method never raises. All exceptions are caught, logged,
            and returned as structured error output.
        """
        try:
            return self._generate_alert_inner(memory)
        except Exception as exc:  # noqa: BLE001
            logger.error("generate_alert | unexpected error: %s", exc)
            return {
                "message":    f"Alert generation failed: {exc}",
                "severity":   "UNKNOWN",
                "event_type": "UNKNOWN",
            }

    # ------------------------------------------------------------------
    # Inner implementations (called by public wrappers)
    # ------------------------------------------------------------------

    def _generate_report_inner(self, memory: ShortTermMemory) -> dict[str, Any]:
        """
        Core report generation logic. Called by :meth:`generate_report`.

        Args:
            memory: ShortTermMemory instance (read-only).

        Returns:
            Report dict with ``executive_summary``, ``detailed_report``,
            ``confidence``.
        """
        # Step 1: extract fields safely from memory.
        event_type: str = str(memory.get("event_type") or "Unknown")
        severity: str = str(memory.get("severity") or "Unknown")
        metric: str = str(memory.get("metric") or "Unknown")
        findings: str = self._coerce_to_str(memory.get("findings"), "No findings recorded.")
        root_cause: str = self._coerce_to_str(memory.get("root_cause"), "Root cause not determined.")
        evidence: str = self._coerce_to_str(memory.get("evidence"), "No evidence recorded.")
        actions_taken: str = self._coerce_to_str(memory.get("actions_taken"), "No actions taken.")
        try:
            confidence: float = float(memory.get("confidence") or _DEFAULT_CONFIDENCE)
        except (TypeError, ValueError):
            confidence = _DEFAULT_CONFIDENCE

        # Step 2: load template.
        template: str = self._load_template(_INCIDENT_REPORT_TEMPLATE)

        # Fill template with memory values.
        filled_template: str = template.format(
            event_type=event_type,
            metric=metric,
            severity=severity,
            root_cause=root_cause,
            findings=findings,
            evidence=evidence,
            actions_taken=actions_taken,
        )

        # Step 3 / 4: LLM or fallback.
        if self._llm is not None:
            executive_summary, detailed_report = self._llm_generate_report(
                filled_template=filled_template,
                event_type=event_type,
                severity=severity,
                metric=metric,
                root_cause=root_cause,
                confidence=confidence,
            )
        else:
            executive_summary = self._fallback_executive_summary(
                event_type=event_type,
                severity=severity,
                metric=metric,
                root_cause=root_cause,
                confidence=confidence,
            )
            detailed_report = filled_template

        # Phase C3: append the "Matched Enterprise Policies" section.
        # Additive post-processing, not a template/prompt change -- the LLM
        # path above is untouched, so this never risks the LLM narrating
        # (or hallucinating about) policies it wasn't designed around.
        # Advisory only: this section is a report of what the Policy
        # Registry found; it never altered root_cause, confidence, or the
        # runbook action plan above.
        detailed_report = f"{detailed_report}\n\n{self._format_matched_policies(memory)}"

        # Phase C4: append the "Cross-Dataset Analysis" section. Same
        # additive-post-processing rationale as Phase C3 above.
        detailed_report = f"{detailed_report}\n\n{self._format_cross_dataset_analysis(memory)}"

        # Phase C5: append the "Adaptive Detection" section. Same
        # additive-post-processing rationale as Phase C3/C4 above.
        detailed_report = f"{detailed_report}\n\n{self._format_adaptive_detection(memory)}"

        # Phase C7: append the "Enterprise Execution Plan" section. Same
        # additive-post-processing rationale as Phase C3/C4/C5 above.
        detailed_report = f"{detailed_report}\n\n{self._format_execution_plan(memory)}"

        # Phase D1: append the "Enterprise Explainability" section. Same
        # additive-post-processing rationale as Phase C3/C4/C5/C7 above.
        detailed_report = f"{detailed_report}\n\n{self._format_explainability(memory)}"

        # Phase D2: append the "Enterprise AI Evaluation" section. Same
        # additive-post-processing rationale as Phase C3/C4/C5/C7/D1 above.
        detailed_report = f"{detailed_report}\n\n{self._format_ai_evaluation(memory)}"

        return {
            "executive_summary": executive_summary,
            "detailed_report":   detailed_report,
            "confidence":        round(confidence, 4),
        }

    @staticmethod
    def _format_matched_policies(memory: ShortTermMemory) -> str:
        """
        Build the "Matched Enterprise Policies" report section (Phase C3)
        from the policy-match finding the Orchestrator already appended to
        STM (``type == "policy"``, see Orchestrator.investigate()).

        Never fabricates a match: an incident predating Phase C3 (no policy
        finding recorded at all) or one where the Policy Registry
        genuinely found nothing both render an honest "none" statement,
        distinguishable from each other by wording, never by a guessed value.
        """
        findings = memory.get("findings") or []
        found_policy_stage = False
        matches: list[dict[str, Any]] = []
        for entry in findings:
            if isinstance(entry, dict) and entry.get("type") == "policy":
                found_policy_stage = True
                data = entry.get("data") or {}
                matches = data.get("matches") or []

        lines = ["Matched Enterprise Policies:"]
        if not found_policy_stage:
            lines.append("  Policy Registry was not consulted for this investigation.")
        elif not matches:
            lines.append("  No matched enterprise policies for this investigation.")
        else:
            for m in matches:
                label = m.get("business_rule") or m.get("condition") or "(unlabeled policy)"
                reason = m.get("match_reason", "unknown")
                source = m.get("source_document") or "unknown source"
                lines.append(
                    f"  - {label} "
                    f"[policy_id={m.get('policy_id')}, matched_by={reason}, source={source}]"
                )
        return "\n".join(lines)

    @staticmethod
    def _format_cross_dataset_analysis(memory: ShortTermMemory) -> str:
        """
        Build the "Cross-Dataset Analysis" report section (Phase C4) from
        the cross-dataset finding the Orchestrator already appended to STM
        (``type == "cross_dataset"``, see Orchestrator.investigate() /
        aeam.intelligence.cross_dataset_analyzer.CrossDatasetAnalyzer).

        Never fabricates a relationship: an incident predating Phase C4, an
        analysis that ran but had insufficient activated datasets, and a
        genuinely correlation-free result are each rendered with their own
        honest wording -- never conflated with an invented correlation.
        """
        findings = memory.get("findings") or []
        found_stage = False
        data: dict[str, Any] = {}
        for entry in findings:
            if isinstance(entry, dict) and entry.get("type") == "cross_dataset":
                found_stage = True
                data = entry.get("data") or {}

        lines = ["Cross-Dataset Analysis:"]
        if not found_stage:
            lines.append("  Cross-Dataset Intelligence was not consulted for this investigation.")
            return "\n".join(lines)

        if data.get("insufficient_data"):
            lines.append(f"  Insufficient data: {data.get('reason', 'not enough activated datasets.')}")
            return "\n".join(lines)

        supporting = data.get("supporting") or []
        contradicting = data.get("contradicting") or []
        strong_correlations = data.get("strong_correlations") or []
        missing_signals = data.get("missing_signals") or []

        if not (supporting or contradicting or strong_correlations):
            lines.append(
                f"  No supporting, contradicting, or strongly-correlated signals found "
                f"across {data.get('candidates_checked', 0)} other activated dataset(s)."
            )
        else:
            for s in supporting:
                lines.append(f"  - Supporting: {s.get('dataset_name')} / {s.get('metric')} (z={s.get('z_score')}, relation={s.get('relation')})")
            for c in contradicting:
                lines.append(f"  - Contradicting: {c.get('dataset_name')} / {c.get('metric')} stayed normal (relation={c.get('relation')})")
            for r in strong_correlations:
                lines.append(f"  - Strong correlation: {r.get('dataset_name')} / {r.get('metric')} (r={r.get('correlation')}, overlapping_dates={r.get('overlapping_dates')})")

        if missing_signals:
            lines.append(f"  Missing signals ({len(missing_signals)}): " + "; ".join(
                f"{m.get('dataset_name')} ({m.get('reason')})" for m in missing_signals
            ))

        return "\n".join(lines)

    @staticmethod
    def _format_adaptive_detection(memory: ShortTermMemory) -> str:
        """
        Build the "Adaptive Detection" report section (Phase C5) from the
        adaptive finding the Orchestrator already appended to STM
        (``type == "adaptive"``, see Orchestrator.investigate() /
        aeam.intelligence.adaptive_detection.AdaptiveDetectionEngine).

        Never fabricates a baseline or seasonality judgement: an incident
        predating Phase C5, one with too little history for an adaptive
        baseline, and one with too little dated history for a seasonality
        judgement are each rendered with their own honest wording -- never
        conflated with an invented threshold or pattern.
        """
        findings = memory.get("findings") or []
        found_stage = False
        data: dict[str, Any] = {}
        for entry in findings:
            if isinstance(entry, dict) and entry.get("type") == "adaptive":
                found_stage = True
                data = entry.get("data") or {}

        lines = ["Adaptive Detection:"]
        if not found_stage:
            lines.append("  Adaptive Detection Engine was not consulted for this investigation.")
            return "\n".join(lines)

        baseline = data.get("adaptive_baseline")
        baseline_insufficient = data.get("adaptive_baseline_insufficient")
        if baseline_insufficient:
            lines.append(f"  Adaptive baseline: insufficient data -- {baseline_insufficient}")
        elif baseline:
            lines.append(
                f"  Adaptive baseline: moving_avg={baseline.get('moving_avg')}, "
                f"z_score={baseline.get('z_score')}, "
                f"anomaly={baseline.get('statistical_anomaly')} "
                f"(history_points={data.get('history_points_used', 0)})"
            )

        seasonality = data.get("seasonality")
        seasonality_insufficient = data.get("seasonality_insufficient")
        if seasonality_insufficient:
            lines.append(f"  Seasonality: insufficient data -- {seasonality_insufficient}")
        elif seasonality:
            if seasonality.get("detected"):
                lines.append(
                    f"  Seasonality: detected (strength={seasonality.get('strength')}, "
                    f"highest={seasonality.get('highest_weekday')}, "
                    f"lowest={seasonality.get('lowest_weekday')})"
                )
            else:
                lines.append(f"  Seasonality: not detected -- {seasonality.get('reason')}")

        corroborating = data.get("corroborating_signals") or []
        if data.get("combined_signal"):
            lines.append(f"  Combined signal: corroborated by {', '.join(corroborating)}.")
        else:
            lines.append("  Combined signal: no corroborating evidence across adaptive baseline, statistical, or forecast signals.")

        return "\n".join(lines)

    @staticmethod
    def _format_execution_plan(memory: ShortTermMemory) -> str:
        """
        Build the "Enterprise Execution Plan" report section (Phase C7) from
        the execution-plan finding the Orchestrator already appended to STM
        (``type == "execution_plan"``, see Orchestrator.finalize_incident() /
        aeam.intelligence.execution_planning.ExecutionPlanningEngine).

        Never fabricates a recommendation, a confidence value, or a business
        impact: an incident predating Phase C7 and one where evidence was
        genuinely insufficient are each rendered with their own honest
        wording -- never conflated with an invented plan.
        """
        findings = memory.get("findings") or []
        found_stage = False
        data: dict[str, Any] = {}
        for entry in findings:
            if isinstance(entry, dict) and entry.get("type") == "execution_plan":
                found_stage = True
                data = entry.get("data") or {}

        lines = ["Enterprise Execution Plan:"]
        if not found_stage:
            lines.append("  Enterprise Action Planning Engine was not consulted for this investigation.")
            return "\n".join(lines)

        lines.append(f"  Summary: {data.get('executive_summary', 'not available')}")

        if data.get("insufficient_evidence"):
            lines.append("  Insufficient evidence: this plan intentionally does not fabricate recommendations beyond standard runbook guidance.")

        actions = data.get("recommended_actions") or []
        if not actions:
            lines.append("  Recommendations: none synthesized.")
        else:
            lines.append(f"  Recommendations ({len(actions)}, in order):")
            for a in actions:
                lines.append(
                    f"    {a.get('order')}. [{a.get('classification')}] {a.get('action')} "
                    f"(source={a.get('source')}) -- {a.get('rationale')}"
                )

        evidence = data.get("supporting_evidence") or []
        if evidence:
            lines.append(f"  Supporting evidence ({len(evidence)}):")
            for e in evidence:
                lines.append(f"    - [{e.get('source')}] {e.get('summary')}")

        lines.append(f"  Confidence: {data.get('confidence')}")
        lines.append(f"  Evidence quality: {data.get('evidence_quality')}")

        conflicts = data.get("evidence_conflicts") or []
        if conflicts:
            lines.append(f"  Evidence conflicts ({len(conflicts)}):")
            for c in conflicts:
                lines.append(f"    - {c.get('description')}")
        else:
            lines.append("  Evidence conflicts: none detected.")

        lines.append(f"  Human approval required: {data.get('human_approval_required')}")
        if data.get("business_risk_assessment"):
            lines.append(f"  Business risk: {data.get('business_risk_assessment')}")
        if data.get("expected_impact"):
            lines.append(f"  Expected impact: {data.get('expected_impact')}")
        if data.get("explanation"):
            lines.append(f"  Explanation: {data.get('explanation')}")

        return "\n".join(lines)

    @staticmethod
    def _format_explainability(memory: ShortTermMemory) -> str:
        """
        Build the "Enterprise Explainability" report section (Phase D1) from
        the explainability finding the Orchestrator already appended to STM
        (``type == "explainability"``, see Orchestrator.finalize_incident() /
        aeam.intelligence.explainability.ExplainabilityEngine).

        Never fabricates a confidence delta, an evidence reference, or an
        assumption: this section only ever restates what
        ExplainabilityEngine already honestly derived from the
        execution-plan and findings data -- it never explains a
        recommendation that does not exist, and never invents traceability
        for one that has none.
        """
        findings = memory.get("findings") or []
        found_stage = False
        data: dict[str, Any] = {}
        for entry in findings:
            if isinstance(entry, dict) and entry.get("type") == "explainability":
                found_stage = True
                data = entry.get("data") or {}

        lines = ["Enterprise Explainability:"]
        if not found_stage:
            lines.append("  Enterprise Explainability Engine was not consulted for this investigation.")
            return "\n".join(lines)

        trace = data.get("recommendation_trace") or []
        if trace:
            lines.append(f"  Why these recommendations exist ({len(trace)}):")
            for t in trace:
                lines.append(f"    - {t}")
        else:
            lines.append("  Why these recommendations exist: no recommendations to explain.")

        cb = data.get("confidence_breakdown") or {}
        lines.append(
            f"  Confidence: raw={cb.get('raw_confidence')}, plan={cb.get('plan_confidence')}, "
            f"adjustment={cb.get('adjustment')} -- {cb.get('adjustment_reason', 'not available')}"
        )
        for s in cb.get("per_source") or []:
            lines.append(
                f"    - {s.get('source')}: consulted={s.get('consulted')}, signal={s.get('has_signal')}, "
                f"value={s.get('raw_value')} ({s.get('raw_value_label')})"
            )

        lines.append("  Evidence attribution:")
        decision_graph = data.get("decision_graph") or []
        if not decision_graph:
            lines.append("    No recommendations to attribute.")
        else:
            for node in decision_graph:
                evidence_ref = node.get("evidence_id") if node.get("evidence_id") is not None else "none"
                lines.append(
                    f"    {node.get('order')}. {node.get('recommendation')} -- source={node.get('source')}, "
                    f"evidence_id={evidence_ref}, report_section={node.get('report_section')}"
                )

        conflicts = data.get("contradictions") or []
        if conflicts:
            lines.append(f"  Contradictions ({len(conflicts)}):")
            for c in conflicts:
                lines.append(f"    - {c.get('description')}")
        else:
            lines.append("  Contradictions: none detected.")

        missing = data.get("missing_evidence") or []
        if missing:
            lines.append(f"  Missing evidence ({len(missing)}):")
            for m in missing:
                lines.append(f"    - {m.get('source')}: {m.get('reason')}")
        else:
            lines.append("  Missing evidence: none -- every evidence source consulted produced a usable signal.")

        assumptions = data.get("assumptions") or []
        if assumptions:
            lines.append(f"  Assumptions ({len(assumptions)}):")
            for a in assumptions:
                lines.append(f"    - {a.get('assumption')} (based on: {a.get('based_on')})")
        else:
            lines.append("  Assumptions: none identified.")

        lines.append(f"  Evidence quality: {data.get('evidence_quality')}")

        lpj = data.get("lower_priority_justification") or {}
        if lpj.get("lower_priority_used"):
            lines.append(f"  Lower-priority evidence used: {lpj.get('reason')}")

        return "\n".join(lines)

    @staticmethod
    def _format_ai_evaluation(memory: ShortTermMemory) -> str:
        """
        Build the "Enterprise AI Evaluation" report section (Phase D2) from
        the AI-evaluation finding the Orchestrator already appended to STM
        (``type == "ai_evaluation"``, see Orchestrator.finalize_incident() /
        aeam.intelligence.ai_evaluation.AIEvaluationEngine).

        Never fabricates a score: every component score this section prints
        is either a real number AIEvaluationEngine derived from
        already-computed evidence, or explicitly ``None`` ("not computable")
        when the underlying evidence source was never consulted -- these two
        states are never conflated.
        """
        findings = memory.get("findings") or []
        found_stage = False
        data: dict[str, Any] = {}
        for entry in findings:
            if isinstance(entry, dict) and entry.get("type") == "ai_evaluation":
                found_stage = True
                data = entry.get("data") or {}

        lines = ["Enterprise AI Evaluation:"]
        if not found_stage:
            lines.append("  Enterprise AI Evaluation & Quality Engine was not consulted for this investigation.")
            return "\n".join(lines)

        lines.append(f"  Overall score: {data.get('overall_score')} ({data.get('overall_score_formula', 'formula not available')})")
        lines.append(f"  Summary: {data.get('quality_summary', 'not available')}")

        components = data.get("component_scores") or {}
        if components:
            lines.append(f"  Component scores ({len(components)}):")
            for key, comp in components.items():
                score = comp.get("score") if isinstance(comp, dict) else None
                reason = comp.get("reason") if isinstance(comp, dict) else None
                lines.append(f"    - {key}: {score if score is not None else 'not computable'} -- {reason}")

        strengths = data.get("strengths") or []
        if strengths:
            lines.append(f"  Strengths ({len(strengths)}):")
            for s in strengths:
                lines.append(f"    - {s}")
        else:
            lines.append("  Strengths: none identified.")

        weaknesses = data.get("weaknesses") or []
        if weaknesses:
            lines.append(f"  Weaknesses ({len(weaknesses)}):")
            for w in weaknesses:
                lines.append(f"    - {w}")
        else:
            lines.append("  Weaknesses: none identified.")

        missing = data.get("missing_evidence") or []
        if missing:
            lines.append(f"  Missing evidence ({len(missing)}):")
            for m in missing:
                lines.append(f"    - {m.get('source')}: {m.get('reason')}")
        else:
            lines.append("  Missing evidence: none.")

        opportunities = data.get("improvement_opportunities") or []
        if opportunities:
            lines.append(f"  Improvement opportunities ({len(opportunities)}):")
            for o in opportunities:
                lines.append(f"    - {o}")
        else:
            lines.append("  Improvement opportunities: none identified.")

        return "\n".join(lines)

    def _generate_alert_inner(self, memory: ShortTermMemory) -> dict[str, Any]:
        """
        Core alert generation logic. Called by :meth:`generate_alert`.

        Args:
            memory: ShortTermMemory instance (read-only).

        Returns:
            Alert dict with ``message``, ``severity``, ``event_type``.
        """
        # Step 1: extract fields safely.
        event_type: str = str(memory.get("event_type") or "Unknown")
        severity: str = str(memory.get("severity") or "UNKNOWN").upper()
        # Standardize severity (Phase 7 spec)
        allowed_severities = {"LOW", "MEDIUM", "HIGH", "CRITICAL"}
        if severity not in allowed_severities:
            severity = "UNKNOWN"

        # summary: prefer explicit "summary" key, then "findings", then default.
        summary_raw = (
            memory.get("summary")
            or memory.get("findings")
            or "No summary available."
        )
        summary: str = self._coerce_to_str(summary_raw, "No summary available.")

        # action: prefer explicit "action" key, then "actions_taken", then default.
        action_raw = (
            memory.get("action")
            or memory.get("actions_taken")
            or "No action recorded."
        )
        action: str = self._coerce_to_str(action_raw, "No action recorded.")

        # Step 2: load template.
        template: str = self._load_template(_SLACK_ALERT_TEMPLATE)

        # Step 3: fill template.
        message: str = template.format(
            severity=severity,
            event_type=event_type,
            summary=summary,
            action=action,
        )

        return {
            "message":    message,
            "severity":   severity,
            "event_type": event_type,
        }

    # ------------------------------------------------------------------
    # LLM generation
    # ------------------------------------------------------------------

    def _llm_generate_report(
        self,
        filled_template: str,
        event_type: str,
        severity: str,
        metric: str,
        root_cause: str,
        confidence: float,
    ) -> tuple[str, str]:
        """
        Use the LLM to generate an executive summary and detailed report.

        Formats the filled template into a structured prompt and calls the
        LLM. Falls back to deterministic output if the LLM call fails.

        Args:
            filled_template: Pre-filled incident report template string.
            event_type:      Incident event type.
            severity:        Incident severity level.
            metric:          Affected metric name.
            root_cause:      Identified root cause string.
            confidence:      Investigation confidence score.

        Returns:
            Tuple of (``executive_summary``, ``detailed_report``) strings.
        """
        prompt = (
            "You are an expert Site Reliability Engineer writing an incident report.\n"
            "Based on the following incident data, produce:\n"
            "1. A concise executive summary (2-3 sentences, non-technical).\n"
            "2. A detailed technical report expanding on each section.\n\n"
            "Respond in this exact format:\n"
            "EXECUTIVE_SUMMARY:\n<summary here>\n\n"
            "DETAILED_REPORT:\n<report here>\n\n"
            f"--- INCIDENT DATA ---\n{filled_template}"
        )

        try:
            raw: str = self._llm.query(
                prompt,
                temperature=_LLM_TEMPERATURE,
                max_tokens=_LLM_MAX_TOKENS,
            )
            executive_summary, detailed_report = self._parse_llm_report(
                raw=raw,
                fallback_summary=self._fallback_executive_summary(
                    event_type=event_type,
                    severity=severity,
                    metric=metric,
                    root_cause=root_cause,
                    confidence=confidence,
                ),
                fallback_detail=filled_template,
            )
            logger.info(
                "_llm_generate_report | LLM generation succeeded | "
                "event_type=%s | severity=%s",
                event_type, severity,
            )
            return executive_summary, detailed_report

        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "_llm_generate_report | LLM call failed (%s) — "
                "falling back to deterministic output.", exc,
            )
            return (
                self._fallback_executive_summary(
                    event_type=event_type,
                    severity=severity,
                    metric=metric,
                    root_cause=root_cause,
                    confidence=confidence,
                ),
                filled_template,
            )

    @staticmethod
    def _parse_llm_report(
        raw: str,
        fallback_summary: str,
        fallback_detail: str,
    ) -> tuple[str, str]:
        """
        Parse LLM output into (executive_summary, detailed_report).

        Looks for ``EXECUTIVE_SUMMARY:`` and ``DETAILED_REPORT:`` markers.
        Falls back to the provided defaults if either section is missing.

        Args:
            raw:              Raw LLM response string.
            fallback_summary: Deterministic summary to use if parsing fails.
            fallback_detail:  Deterministic detail to use if parsing fails.

        Returns:
            Tuple of (executive_summary, detailed_report).
        """
        executive_summary = fallback_summary
        detailed_report = fallback_detail

        if "EXECUTIVE_SUMMARY:" in raw and "DETAILED_REPORT:" in raw:
            try:
                after_summary = raw.split("EXECUTIVE_SUMMARY:", 1)[1]
                summary_part, rest = after_summary.split("DETAILED_REPORT:", 1)
                executive_summary = summary_part.strip()
                detailed_report = rest.strip()
            except (IndexError, ValueError):
                pass  # fallbacks already set above

        elif "EXECUTIVE_SUMMARY:" in raw:
            try:
                executive_summary = raw.split("EXECUTIVE_SUMMARY:", 1)[1].strip()
            except IndexError:
                pass

        return executive_summary, detailed_report

    # ------------------------------------------------------------------
    # Deterministic fallback generators
    # ------------------------------------------------------------------

    @staticmethod
    def _fallback_executive_summary(
        event_type: str,
        severity: str,
        metric: str,
        root_cause: str,
        confidence: float,
    ) -> str:
        """
        Produce a deterministic executive summary from raw field values.

        Args:
            event_type:  Incident event type string.
            severity:    Severity level string.
            metric:      Affected metric name.
            root_cause:  Root cause description.
            confidence:  Investigation confidence score.

        Returns:
            One-sentence summary string.
        """
        return (
            f"A {severity} severity incident of type '{event_type}' was detected "
            f"on metric '{metric}'. "
            f"Root cause: {root_cause} "
            f"(investigation confidence: {round(confidence * 100, 1)}%)."
        )

    # ------------------------------------------------------------------
    # Template loading
    # ------------------------------------------------------------------

    @staticmethod
    def _load_template(path: Path) -> str:
        """
        Load a text template from the filesystem.

        Args:
            path: Absolute :class:`pathlib.Path` to the template file.

        Returns:
            Template content as a string.

        Raises:
            FileNotFoundError: If the template file does not exist.
            OSError:           If the file cannot be read.
        """
        if not path.exists():
            raise FileNotFoundError(
                f"Template not found: '{path}'. "
                f"Ensure aeam/templates/ exists and contains the required files."
            )
        content = path.read_text(encoding="utf-8")
        logger.debug("_load_template | loaded %s (%d chars)", path.name, len(content))
        return content

    # ------------------------------------------------------------------
    # Utility helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _coerce_to_str(value: Any, default: str) -> str:
        """
        Safely coerce ``value`` to a non-empty string.

        Returns ``default`` if ``value`` is ``None``, an empty string, or an
        empty / whitespace-only string after conversion.

        Dicts and lists are JSON-serialised for readability. All other types
        use ``str()``.

        Args:
            value:   Any value from ShortTermMemory.
            default: Fallback string when ``value`` is absent or blank.

        Returns:
            Non-empty string representation of ``value``, or ``default``.
        """
        if value is None:
            return default

        import json as _json

        if isinstance(value, dict):
            try:
                text = _json.dumps(value, indent=2, default=str)
            except Exception:  # noqa: BLE001
                text = str(value)
        elif isinstance(value, list):
            try:
                text = _json.dumps(value, indent=2, default=str)
            except Exception:  # noqa: BLE001
                text = str(value)
        else:
            text = str(value)

        return text.strip() or default

    def __repr__(self) -> str:
        mode = "llm" if self._llm is not None else "fallback"
        return f"ReportAgent(mode={mode!r})"