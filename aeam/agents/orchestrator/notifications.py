"""
aeam/agents/orchestrator/notifications.py

Structured Slack and Jira message formatters for the AEAM Orchestrator.

Pure functions — no I/O, no LLM calls. Both formatters build their output
from explicit, named fields only. Neither ever embeds a raw ``json.dumps()``
of findings, evidence, or the LLM response — every value shown is a specific,
human-labelled field, so the resulting message is always scannable rather
than a data dump.

Used exclusively by :meth:`Orchestrator.finalize_incident` to build the
parameters passed to :class:`~aeam.agents.action.slack_actions.SlackActions`
and :class:`~aeam.agents.action.jira_actions.JiraActions` via ActionAgent.
"""

from __future__ import annotations

from typing import Any

from aeam.agents.orchestrator.runbooks import ACTION_LABELS


def format_slack_message(payload: dict[str, Any]) -> str:
    """
    Build a structured, human-scannable Slack alert message.

    Every value comes from an explicit, named field on ``payload`` — never a
    serialised dump of findings/evidence/LLM output.

    Args:
        payload: Dict containing:

            - ``incident_id``         (str)
            - ``metric``              (str)
            - ``severity``            (str)
            - ``investigation_status`` (str) — canonical status, e.g. "RESOLVED"
            - ``root_cause``          (str | None)
            - ``confidence``          (float | None)
            - ``evidence_count``      (int)
            - ``recommended_actions`` (list[str])
            - ``executed_actions``    (list[str]) — action_type keys that
              actually ran with status SUCCESS
            - ``requires_human``      (bool)

    Returns:
        Fully formatted Slack message string (plain text with light
        markdown), ready to pass as ``params["message"]`` to SlackActions.

    Example::

        format_slack_message({
            "incident_id": "INC-42", "metric": "latency_ms", "severity": "HIGH",
            "investigation_status": "RESOLVED",
            "root_cause": "Inefficient queries and replication lag",
            "confidence": 0.83, "evidence_count": 5,
            "recommended_actions": ["Optimize indexes"],
            "executed_actions": ["jira", "slack"],
            "requires_human": False,
        })
    """
    status = payload.get("investigation_status", "COMPLETE")
    status_label = "Resolved" if status == "RESOLVED" else status.title()

    root_cause = payload.get("root_cause") or "Not yet identified"
    confidence = payload.get("confidence")
    confidence_str = f"{round(float(confidence) * 100)}%" if confidence is not None else "N/A"

    evidence_count = payload.get("evidence_count", 0)
    recommended = payload.get("recommended_actions") or ["None"]
    executed = payload.get("executed_actions") or []
    executed_labels = [ACTION_LABELS.get(a, a) for a in executed] or ["None"]

    human_review = "Required" if payload.get("requires_human") else "Not Required"

    lines = [
        "🚨 AEAM INCIDENT",
        "",
        f"Incident ID: {payload.get('incident_id', 'unknown')}",
        f"Metric: {payload.get('metric', 'unknown')}",
        f"Severity: {payload.get('severity', 'unknown')}",
        f"Investigation Status: {status_label}",
        "",
        f"Root Cause: {root_cause}",
        f"Confidence: {confidence_str}",
        f"Evidence: {evidence_count} chunk{'s' if evidence_count != 1 else ''}",
        "",
        f"Recommended Action: {'; '.join(recommended)}",
        f"Executed Actions: {', '.join(executed_labels)}",
        "",
        f"Human Review: {human_review}",
    ]
    return "\n".join(lines)


def format_jira_description(payload: dict[str, Any]) -> str:
    """
    Build a structured, non-repetitive Jira issue description.

    Replaces the previous verbose format (raw evidence dicts + full LLM
    response text dumped inline) with a scannable summary: an investigation
    checklist, the root cause, an evidence summary (count / top confidence /
    chunk IDs — not full text), recommended and executed actions, and the
    LLM reasoning collapsed under a Jira ``{expand}`` (Markdown-style
    collapsible) section rather than inlined at full length.

    Args:
        payload: Dict containing:

            - ``incident_id``, ``metric``, ``severity`` (str)
            - ``current_value``, ``expected_value``     (float | None)
            - ``retrieval_completed``  (bool)
            - ``evidence_count``       (int)
            - ``validation_status``    (str) — "PASSED" | "FAILED" | "SKIPPED"
            - ``root_cause``           (str | None)
            - ``top_confidence``       (float | None)
            - ``chunk_ids``            (list[str])
            - ``recommended_actions``  (list[str])
            - ``executed_actions``     (list[str])
            - ``llm_reasoning``        (str | None) — raw LLM text, shown
              collapsed, never inlined at top level.

    Returns:
        Fully formatted Jira description string (Jira wiki markup).
    """
    root_cause_found = bool(payload.get("root_cause"))
    evidence_count = payload.get("evidence_count", 0)
    validation_status = payload.get("validation_status", "SKIPPED")

    checklist = [
        f"{'✓' if payload.get('retrieval_completed') else '✗'} Retrieval completed",
        f"{'✓' if evidence_count > 0 else '✗'} Retrieved {evidence_count} chunk{'s' if evidence_count != 1 else ''}",
        f"{'✓' if validation_status == 'PASSED' else '✗'} Validation {validation_status.lower()}",
        f"{'✓' if root_cause_found else '✗'} Root cause identified",
    ]

    chunk_ids = payload.get("chunk_ids") or []
    top_confidence = payload.get("top_confidence")
    top_confidence_str = f"{round(float(top_confidence) * 100)}%" if top_confidence is not None else "N/A"

    recommended = payload.get("recommended_actions") or ["None"]
    executed = payload.get("executed_actions") or []
    executed_labels = [ACTION_LABELS.get(a, a) for a in executed] or ["None"]

    llm_reasoning = (payload.get("llm_reasoning") or "").strip()

    sections = [
        "h3. Incident",
        f"Metric: {payload.get('metric', 'unknown')}",
        f"Severity: {payload.get('severity', 'unknown')}",
        f"Current: {payload.get('current_value', 'N/A')}",
        f"Expected: {payload.get('expected_value', 'N/A')}",
        "",
        "h3. Investigation Summary",
        *checklist,
        "",
        "h3. Root Cause",
        payload.get("root_cause") or "Not identified — see escalation notes.",
        "",
        "h3. Evidence Summary",
        f"Chunk count: {evidence_count}",
        f"Top confidence: {top_confidence_str}",
        f"Chunk IDs: {', '.join(chunk_ids) if chunk_ids else 'None'}",
        "",
        "h3. Recommended Action",
        "; ".join(recommended),
        "",
        "h3. Executed Actions",
        ", ".join(executed_labels),
    ]

    if llm_reasoning:
        sections += [
            "",
            "h3. LLM Reasoning",
            "{expand}",
            llm_reasoning,
            "{expand}",
        ]

    return "\n".join(sections)
