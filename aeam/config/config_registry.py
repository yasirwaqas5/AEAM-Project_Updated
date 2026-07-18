"""
aeam/config/config_registry.py

Enterprise Administration & Settings UI (Phase D5) — configuration metadata
registry.

This module is pure data + introspection: it maps each Phase D4 Settings
field to the UI section/label it belongs under, and to its REAL engine
default value by IMPORTING that value directly from the owning engine's own
module — never re-typing the literal, exactly the same "no duplicated
constants" discipline Phase D4 established (see e.g.
``aeam.intelligence.ai_evaluation`` importing
``aeam.intelligence.execution_planning._SOURCE_PRIORITY`` instead of
redefining it). Validation bounds (gt/ge/lt/le) are read directly off each
Settings field's own Pydantic constraints at runtime by
:func:`field_constraints`, not re-declared here either.

No I/O, no engine construction, no new configuration mechanism — this is
metadata ABOUT the one existing Configuration Engine (``aeam.config.settings.Settings``),
consumed only by ``aeam/api/administration.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, get_args

from aeam.agents.rag import advanced_retrieval
from aeam.config.settings import Settings
from aeam.intelligence import (
    adaptive_detection,
    ai_evaluation,
    cross_dataset_analyzer,
    execution_planning,
    observability,
    policy_registry,
)


@dataclass(frozen=True)
class ConfigField:
    """One Settings field, described for the Administration UI."""

    key: str
    section: str
    label: str
    #: The engine's real hardcoded default (imported, not re-typed). ``None``
    #: for the two fields whose "default" is not a number -- see
    #: ``default_note`` for those.
    default: Any
    #: Set only for fields where ``default=None`` is a genuine value (an
    #: additive filter / an unbounded read), not "no default was found".
    default_note: str | None = None
    #: Set only for the one field with a fixed vocabulary
    #: (``HUMAN_APPROVAL_QUALITY_LEVELS``).
    choices: tuple[str, ...] | None = None


CONFIG_FIELDS: tuple[ConfigField, ...] = (
    ConfigField(
        "MEMORY_SIMILARITY_THRESHOLD", "Memory", "Similarity threshold",
        default=None,
        default_note=(
            "No extra filter — Enterprise Memory recall relies on "
            "RetrievalPipeline's own similarity threshold unless this is set."
        ),
    ),
    ConfigField(
        "POLICY_SIMILARITY_THRESHOLD", "Policy", "Semantic match threshold",
        default=policy_registry._SEMANTIC_THRESHOLD,
    ),
    ConfigField(
        "CROSS_DATASET_CORRELATION_THRESHOLD", "Cross Dataset", "Correlation threshold",
        default=cross_dataset_analyzer.DEFAULT_CORRELATION_THRESHOLD,
    ),
    ConfigField(
        "ADAPTIVE_MIN_BASELINE_POINTS", "Adaptive Detection", "Minimum baseline points",
        default=adaptive_detection.MIN_BASELINE_POINTS,
    ),
    ConfigField(
        "ADAPTIVE_MIN_SEASONALITY_POINTS", "Adaptive Detection", "Minimum seasonality points",
        default=adaptive_detection.MIN_SEASONALITY_POINTS,
    ),
    ConfigField(
        "ADAPTIVE_SEASONALITY_STRENGTH_THRESHOLD", "Adaptive Detection", "Seasonality strength threshold",
        default=adaptive_detection.SEASONALITY_STRENGTH_THRESHOLD,
    ),
    ConfigField(
        "ADAPTIVE_WINDOW_SIZE", "Adaptive Detection", "Baseline window size (days)",
        default=adaptive_detection.DEFAULT_ADAPTIVE_WINDOW,
    ),
    ConfigField(
        "RETRIEVAL_ENTITY_BONUS_PER_MATCH", "Retrieval", "Entity bonus per match",
        default=advanced_retrieval._ENTITY_BONUS_PER_MATCH,
    ),
    ConfigField(
        "RETRIEVAL_MAX_ENTITY_BONUS", "Retrieval", "Maximum entity bonus",
        default=advanced_retrieval._MAX_ENTITY_BONUS,
    ),
    ConfigField(
        "RETRIEVAL_DOC_TYPE_BONUS", "Retrieval", "Actionable doc-type bonus",
        default=advanced_retrieval._DOC_TYPE_BONUS,
    ),
    ConfigField(
        "RETRIEVAL_RECENCY_BONUS", "Retrieval", "Recency bonus",
        default=advanced_retrieval._RECENCY_BONUS,
    ),
    ConfigField(
        "RETRIEVAL_RECENCY_WINDOW_DAYS", "Retrieval", "Recency window (days)",
        default=advanced_retrieval._RECENCY_WINDOW_DAYS,
    ),
    ConfigField(
        "EXECUTION_PLAN_AMBIGUOUS_CAUSE_GAP", "Execution Planning", "Ambiguous-cause confidence gap",
        default=execution_planning._AMBIGUOUS_CAUSE_GAP,
    ),
    ConfigField(
        "EXECUTION_PLAN_CONFLICT_CONFIDENCE_CAP", "Execution Planning", "Conflict confidence cap",
        default=execution_planning._CONFLICT_CONFIDENCE_CAP,
    ),
    ConfigField(
        "HUMAN_APPROVAL_QUALITY_LEVELS", "Execution Planning",
        "Evidence-quality levels requiring human approval",
        default=f"{execution_planning._QUALITY_INSUFFICIENT},{execution_planning._QUALITY_LOW}",
        choices=(
            execution_planning._QUALITY_INSUFFICIENT,
            execution_planning._QUALITY_LOW,
            execution_planning._QUALITY_MEDIUM,
            execution_planning._QUALITY_HIGH,
        ),
    ),
    ConfigField(
        "AI_EVAL_STRENGTH_THRESHOLD", "AI Evaluation", "Strength-signal threshold",
        default=ai_evaluation._STRENGTH_THRESHOLD,
    ),
    ConfigField(
        "AI_EVAL_WEAKNESS_THRESHOLD", "AI Evaluation", "Weakness-signal threshold",
        default=ai_evaluation._WEAKNESS_THRESHOLD,
    ),
    ConfigField(
        "AI_EVAL_CONFLICT_PENALTY_WEIGHT", "AI Evaluation", "Conflict penalty weight",
        default=ai_evaluation._CONFLICT_PENALTY_WEIGHT,
    ),
    ConfigField(
        "AI_EVAL_MEMORY_MIXED_OUTCOME_PENALTY", "AI Evaluation", "Memory mixed-outcome penalty",
        default=ai_evaluation._MEMORY_MIXED_OUTCOME_PENALTY,
    ),
    ConfigField(
        "OBSERVABILITY_TREND_WINDOW", "Observability", "Trend display window (points)",
        default=observability._TREND_WINDOW,
    ),
    ConfigField(
        "OBSERVABILITY_RETENTION_LIMIT", "Observability", "Retention limit (most recent incidents)",
        default=None,
        default_note="Unbounded — every persisted incident is considered unless this is set.",
    ),
)

CONFIG_FIELDS_BY_KEY: dict[str, ConfigField] = {f.key: f for f in CONFIG_FIELDS}

#: Section display order for the Administration UI.
SECTION_ORDER: tuple[str, ...] = (
    "Memory", "Policy", "Cross Dataset", "Adaptive Detection", "Retrieval",
    "Execution Planning", "AI Evaluation", "Observability",
)


def field_type(key: str) -> str:
    """
    ``"float" | "int" | "string"`` for ``key``, derived from the Settings
    field's own type annotation (``float | None`` / ``int | None`` /
    ``str | None``) — never redeclared independently of the real field.
    """
    annotation = Settings.model_fields[key].annotation
    args = [a for a in get_args(annotation) if a is not type(None)]
    py_type = args[0] if args else annotation
    if py_type is float:
        return "float"
    if py_type is int:
        return "int"
    return "string"


def field_constraints(key: str) -> dict[str, float]:
    """
    ``{"gt": ..., "ge": ..., "lt": ..., "le": ...}`` for ``key``, read
    directly from the Settings field's own ``annotated_types`` constraint
    metadata (the exact ``Field(gt=..., le=...)`` bounds Phase D4 already
    declared) — never re-typed here.
    """
    constraints: dict[str, float] = {}
    for constraint in Settings.model_fields[key].metadata:
        for attr in ("gt", "ge", "lt", "le"):
            value = getattr(constraint, attr, None)
            if value is not None:
                constraints[attr] = value
    return constraints


def field_description(key: str) -> str:
    """The Settings field's own ``Field(description=...)`` text, reused verbatim."""
    return Settings.model_fields[key].description or ""
