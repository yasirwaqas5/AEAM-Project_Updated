"""
aeam/intelligence/replay.py

Investigation & Timeline Replay (Phase F5 — Explainability Deepening).

Two read-only builders over one already-persisted incident record:

- :class:`InvestigationReplayBuilder` — reconstructs the investigation as an
  ordered, navigable sequence of stages, exactly as recorded.
- :class:`TimelineBuilder` — places those stages against measured time,
  using persisted durations only.

Why this exists
---------------
The findings model has always recorded every investigation stage in order,
and the console has always had a Replay page — but nothing reconstructed an
investigation *as a sequence*, so the page derived its own narrative
client-side from the incident's summary fields. D1 explains the *final*
plan; nothing explained the *unfolding*. Auditors and post-incident
reviewers need "show me exactly what happened, in order, and why".

What this is NOT
----------------
**Replay reconstructs history. It never re-executes it.** This module reads
one row and returns a projection of it. It does not import — and therefore
cannot reach — ``RuleEngine``, ``StatisticalDetector``, ``KPIAgent``,
``ForecastAgent``, the business graph, ``PolicyAgent``, ``ActionAgent``, or
any LLM. It issues no writes of any kind: no incident, finding, memory,
audit record, timestamp, or metric is modified, and no new row is created
(MEM-2). Replaying an incident a thousand times leaves the database
bit-identical.

Honesty contract
----------------
Three rules, each enforced by construction rather than by convention:

1. **Recorded order is the order.** Stages are emitted in the sequence they
   appear in the persisted findings array — never re-sorted into a canonical
   pipeline order. A stage recorded twice (the investigation loop genuinely
   runs the decision and RAG stages once per depth) appears twice, with its
   occurrence number.
2. **Absence is reported, never filled.** A stage the record does not
   contain becomes an explicit *gap* carrying the phase that introduced it,
   so an incident predating a stage reads as "no such entry was recorded"
   instead of a fabricated step (COMPAT-1, EXPL-3).
3. **Time is measured or absent.** Every duration comes from
   ``audit_summary.stage_durations`` / ``investigation_duration_seconds``
   (both measured at finalize). Nothing is interpolated, apportioned, or
   estimated; an unmeasured stage reports ``duration_available: false``.
   Where measured stage time does not add up to the measured total, the
   remainder is disclosed as unattributed rather than distributed.

Bounded reads (E6)
------------------
The reconstruction pages over stages with a hard ceiling, so an incident
with a pathologically long findings array cannot produce an unbounded
response. Callers receive ``total_stages`` alongside the page so they can
paginate without a second endpoint.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

#: Default stages returned when a caller does not paginate. Generous enough
#: that a normal investigation (roughly 10-16 stages) is never truncated.
DEFAULT_STAGE_LIMIT: int = 100

#: Hard ceiling applied after any caller-supplied limit, so "bounded" is a
#: property of this module rather than of how its callers were configured.
MAX_STAGE_LIMIT: int = 500

#: The findings key of the decision stage. The decision entry is the ONE
#: findings entry the Orchestrator writes without a ``type`` field (it
#: predates the convention); it is identified structurally instead — see
#: :func:`classify_entry`.
DECISION_STAGE_KEY: str = "decision"

#: Stage key used for a recorded entry this module does not recognise.
#: Such an entry is still replayed in place, with its payload intact — a
#: stage added by a later phase must never be silently dropped from an
#: audit reconstruction (COMPAT-4).
UNRECOGNISED_STAGE_KEY: str = "unrecognised"


@dataclass(frozen=True)
class ReplayStageSpec:
    """
    One stage of the canonical investigation sequence.

    Attributes:
        key:           The findings ``type`` that records this stage (or
                       :data:`DECISION_STAGE_KEY`).
        label:         Operator-facing name.
        category:      Which part of the pipeline this belongs to —
                       ``decision`` / ``evidence`` / ``planning`` /
                       ``explainability`` / ``governance`` / ``actions``.
                       Mirrors the ROADMAP's own stage grouping.
        introduced_in: The phase that introduced the stage. Reported with a
                       gap so "this incident has no execution plan" reads
                       as "planning arrived in C7" rather than as an error.
        expected:      Whether absence is worth reporting as a gap. False
                       for stages that are conditional BY DESIGN (an
                       escalation, an LLM parse failure, an approval gate):
                       their absence means the condition did not occur, and
                       calling that a gap would be misleading.
    """

    key: str
    label: str
    category: str
    introduced_in: str
    expected: bool = True


#: The canonical stage sequence, in the order ``Orchestrator._investigate``
#: and ``_finalize_incident`` write them. Used ONLY for labelling and for
#: computing gaps — never to re-order what was recorded.
STAGE_CATALOG: tuple[ReplayStageSpec, ...] = (
    ReplayStageSpec(DECISION_STAGE_KEY, "Decision", "decision", "Phase 3"),
    ReplayStageSpec("memory", "Enterprise Memory", "evidence", "Phase C1"),
    ReplayStageSpec("policy", "Enterprise Policies", "evidence", "Phase C3"),
    ReplayStageSpec("cross_dataset", "Cross-Dataset Intelligence", "evidence", "Phase C4"),
    ReplayStageSpec("graph", "Business Graph", "evidence", "Phase F4"),
    ReplayStageSpec("adaptive", "Adaptive Detection", "evidence", "Phase C5"),
    ReplayStageSpec("rag", "Knowledge Retrieval (RAG)", "evidence", "Phase 4"),
    ReplayStageSpec(
        "llm_reasoning_error", "LLM Reasoning Error", "evidence", "Phase 4", expected=False
    ),
    ReplayStageSpec("kpi_analysis", "KPI Analysis", "evidence", "Phase F1"),
    ReplayStageSpec("evaluation", "Investigation Evaluation", "decision", "Phase 3"),
    ReplayStageSpec("escalation", "Escalation", "governance", "Phase 3", expected=False),
    ReplayStageSpec("execution_plan", "Execution Planning", "planning", "Phase C7"),
    ReplayStageSpec("explainability", "Explainability", "explainability", "Phase D1"),
    ReplayStageSpec("ai_evaluation", "Quality Evaluation", "explainability", "Phase D2"),
    ReplayStageSpec(
        "human_approval", "Human Approval Gate", "governance", "Phase E9", expected=False
    ),
    ReplayStageSpec("audit_summary", "Audit Summary & Actions", "actions", "Phase 7"),
)

_SPEC_BY_KEY: dict[str, ReplayStageSpec] = {spec.key: spec for spec in STAGE_CATALOG}
_CATALOG_ORDER: dict[str, int] = {spec.key: i for i, spec in enumerate(STAGE_CATALOG)}


# ---------------------------------------------------------------------------
# Parsing helpers — tolerant readers over one persisted incident row
# ---------------------------------------------------------------------------

def parse_findings(raw: Any) -> list[dict[str, Any]]:
    """
    Decode an incident row's ``findings`` column into a list of entries.

    The column is declared TEXT and holds JSON (see
    ``DatabaseClient._create_tables_if_not_exist``), so SQLite hands back a
    string while PostgreSQL JSONB hands back a decoded list. Both are
    accepted, as is ``None``.

    An unparseable or wrongly-shaped value yields an EMPTY list rather than
    raising: one corrupt historical row must degrade to "nothing could be
    reconstructed for this incident", never to a failed audit request. The
    caller distinguishes the two cases via ``findings_readable``.
    """
    if raw is None:
        return []
    data = raw
    if isinstance(data, (bytes, bytearray)):
        try:
            data = data.decode("utf-8")
        except UnicodeDecodeError:
            return []
    if isinstance(data, str):
        text = data.strip()
        if not text:
            return []
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            return []
    if not isinstance(data, list):
        return []
    return [entry for entry in data if isinstance(entry, dict)]


def classify_entry(entry: dict[str, Any]) -> str:
    """
    The stage key one recorded findings entry belongs to.

    A ``type`` field is authoritative when present. The decision entry has
    none — it carries ``decision``/``depth``/``confidence`` instead — so it
    is identified by that structure, which is an inspectable fact about the
    record rather than a guess. Anything else is
    :data:`UNRECOGNISED_STAGE_KEY`: still replayed, never dropped.
    """
    declared = entry.get("type")
    if isinstance(declared, str) and declared.strip():
        return declared.strip()
    if "decision" in entry and "depth" in entry:
        return DECISION_STAGE_KEY
    return UNRECOGNISED_STAGE_KEY


def stage_payload(entry: dict[str, Any]) -> Any:
    """
    The recorded output of one stage.

    Most engines wrap their result in ``data``; the decision, evaluation,
    escalation, and audit-summary entries are flat. Both are returned
    verbatim — replay surfaces what was persisted and adds nothing, so a
    reader comparing the response against the raw row sees the same values.
    """
    data = entry.get("data")
    if isinstance(data, (dict, list)):
        return data
    return {k: v for k, v in entry.items() if k != "type"}


def find_audit_summary(findings: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The last ``audit_summary`` entry, or ``None`` when the investigation
    never reached finalize (an in-flight or crashed incident)."""
    latest: dict[str, Any] | None = None
    for entry in findings:
        if entry.get("type") == "audit_summary":
            latest = entry
    return latest


def _clamp_limit(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_STAGE_LIMIT
    return max(1, min(int(limit), MAX_STAGE_LIMIT))


def _as_float(value: Any) -> float | None:
    """A float, or ``None`` for anything that is not a real number.

    Booleans are rejected deliberately: ``True`` is a valid Python float
    input and would silently become a 1.0-second duration.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and result not in (float("inf"), float("-inf")) else None


# ---------------------------------------------------------------------------
# Investigation replay
# ---------------------------------------------------------------------------

class InvestigationReplayBuilder:
    """
    Reconstructs one investigation from its persisted record.

    Stateless and dependency-free: it takes an incident row dict (as
    ``SELECT * FROM incidents`` returns) and returns a projection of it.
    That is the whole of its capability — there is no engine to invoke, no
    client to write through, and no method that mutates anything.
    """

    def reconstruct(
        self,
        incident: dict[str, Any],
        offset: int = 0,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """
        Rebuild the ordered stage sequence for ``incident``.

        Args:
            incident: One incident row.
            offset:   Stages to skip (E6 pagination).
            limit:    Maximum stages to return, clamped to
                      :data:`MAX_STAGE_LIMIT`.

        Returns:
            A dict of the same shape every time::

                {
                    "incident_id": str | None,
                    "event_type": ..., "metric": ..., "severity": ...,
                    "recorded_at": str | None,
                    "findings_readable": bool,
                    "total_stages": int,
                    "offset": int, "limit": int, "truncated": bool,
                    "stages": [...],
                    "gaps": [...],
                    "stage_categories": {...},
                    "replay_contract": {...},
                }

            Each stage carries ``sequence`` (its index in the recorded
            array), ``occurrence`` (1-based, for stages recorded more than
            once), ``key``/``label``/``category``/``introduced_in``,
            ``recognised``, ``outputs`` (the persisted payload, verbatim),
            ``summary`` (a short factual line), ``duration`` (measured or
            explicitly unavailable) and ``state_after`` (values the record
            itself established up to and including this step).
        """
        raw_findings = incident.get("findings")
        findings = parse_findings(raw_findings)
        findings_readable = bool(findings) or raw_findings in (None, "", [], "[]")

        occurrences: dict[str, int] = {}
        total_by_key: dict[str, int] = {}
        for entry in findings:
            key = classify_entry(entry)
            total_by_key[key] = total_by_key.get(key, 0) + 1

        audit = find_audit_summary(findings) or {}
        stage_durations = audit.get("stage_durations")
        stage_durations = stage_durations if isinstance(stage_durations, dict) else {}

        state: dict[str, Any] = {}
        stages: list[dict[str, Any]] = []
        for index, entry in enumerate(findings):
            key = classify_entry(entry)
            occurrences[key] = occurrences.get(key, 0) + 1
            spec = _SPEC_BY_KEY.get(key)
            payload = stage_payload(entry)
            self._fold_state(state, key, entry, payload)

            stages.append({
                "sequence": index,
                "occurrence": occurrences[key],
                "occurrences_total": total_by_key.get(key, 1),
                "key": key,
                "declared_type": entry.get("type"),
                "label": spec.label if spec else f"Unrecognised stage ({key})",
                "category": spec.category if spec else "unrecognised",
                "introduced_in": spec.introduced_in if spec else None,
                # False means "recorded, but this module has no catalog
                # entry for it" — a stage from a phase this build does not
                # know about. It is still replayed, in place, with its
                # payload intact.
                "recognised": spec is not None,
                "summary": self._summarise(key, entry, payload),
                "outputs": payload,
                "duration": self._stage_duration(
                    key, stage_durations, total_by_key.get(key, 1)
                ),
                "state_after": dict(state),
            })

        page_limit = _clamp_limit(limit)
        start = max(0, int(offset))
        page = stages[start:start + page_limit]

        return {
            "incident_id": incident.get("incident_id"),
            "event_id": incident.get("event_id"),
            "event_type": incident.get("event_type"),
            "metric": incident.get("metric"),
            "severity": incident.get("severity"),
            "recorded_at": incident.get("timestamp"),
            "investigation_depth": incident.get("investigation_depth"),
            "root_cause": incident.get("root_cause"),
            "confidence": incident.get("confidence"),
            "requires_human": incident.get("requires_human"),
            # False ONLY when the findings column held something that could
            # not be decoded — distinct from an incident that legitimately
            # recorded no stages.
            "findings_readable": findings_readable,
            "total_stages": len(stages),
            "offset": start,
            "limit": page_limit,
            "truncated": start + len(page) < len(stages),
            "stages": page,
            "gaps": self._gaps(total_by_key),
            "stage_categories": self._category_counts(stages),
            "replay_contract": {
                "read_only": True,
                "re_executed": False,
                "source": "incidents.findings (persisted at finalize)",
                "note": (
                    "Reconstructed from the persisted record only. No engine, "
                    "agent, action, or LLM is invoked, and nothing is written."
                ),
            },
        }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _stage_duration(
        key: str, stage_durations: dict[str, Any], occurrences_total: int
    ) -> dict[str, Any]:
        """
        The measured time for one stage, or an explicit statement that none
        was recorded.

        ``stage_durations`` (Phase F5) holds a per-stage TOTAL across every
        occurrence, because the investigation loop can run a stage once per
        depth. So a measured number is attributed to an individual step only
        when that step occurs exactly once in the record. When it occurs
        more than once, the aggregate is reported as an aggregate — dividing
        it between occurrences would be the interpolation this phase forbids.
        """
        measured = _as_float(stage_durations.get(key))
        if measured is None:
            return {
                "available": False,
                "seconds": None,
                "scope": None,
                "source": None,
                "reason": (
                    "No measured duration is recorded for this stage. Per-stage "
                    "timing was introduced in Phase F5; incidents recorded "
                    "before it carry only a total investigation duration."
                ),
            }
        if occurrences_total > 1:
            return {
                "available": True,
                "seconds": None,
                "stage_total_seconds": measured,
                "occurrences": occurrences_total,
                "scope": "stage_total",
                "source": "audit_summary.stage_durations",
                "reason": (
                    f"This stage was recorded {occurrences_total} times; the "
                    f"measured {measured}s is the total across all of them. "
                    "Replay does not divide it between occurrences."
                ),
            }
        return {
            "available": True,
            "seconds": measured,
            "stage_total_seconds": measured,
            "occurrences": 1,
            "scope": "stage",
            "source": "audit_summary.stage_durations",
            "reason": None,
        }

    @staticmethod
    def _fold_state(
        state: dict[str, Any], key: str, entry: dict[str, Any], payload: Any
    ) -> None:
        """
        Update the running "state visible at this step" view.

        Strictly a fold over values the entries THEMSELVES carry — the
        decision recorded at that depth, the evaluation's own verdict, the
        audit summary's own status. Per-step root causes and confidences
        were never persisted, so none is reconstructed: the view grows only
        as the record supplies real values, and each is tagged with the
        stage that supplied it.
        """
        def put(field: str, value: Any) -> None:
            if value is not None:
                state[field] = value
                state[f"{field}_source_stage"] = key

        if key == DECISION_STAGE_KEY:
            put("decision", entry.get("decision"))
            put("decision_confidence", _as_float(entry.get("confidence")))
            put("decision_source", entry.get("source"))
            put("depth", entry.get("depth"))
        elif key == "evaluation":
            put("evaluation_decision", entry.get("decision"))
            put("evaluation_score", _as_float(entry.get("score")))
        elif key == "escalation":
            put("escalation_reason", entry.get("reason"))
        elif key == "audit_summary":
            put("investigation_status", entry.get("investigation_status"))
            put("root_cause", entry.get("root_cause"))
            put("root_cause_source", entry.get("root_cause_source"))
            put("validation_status", entry.get("validation_status"))
        elif key == "human_approval" and isinstance(payload, dict):
            put("approval_status", payload.get("status"))

    @staticmethod
    def _summarise(key: str, entry: dict[str, Any], payload: Any) -> str:
        """
        One factual line per stage, stating only what the record contains.

        Deliberately dull: it counts and quotes persisted values. Anything
        this method cannot read off the entry it does not say.
        """
        if key == DECISION_STAGE_KEY:
            confidence = _as_float(entry.get("confidence"))
            confidence_text = f"{confidence:.2f}" if confidence is not None else "unrecorded"
            return (
                f"Decision {entry.get('decision', 'unrecorded')} at depth "
                f"{entry.get('depth', 'unrecorded')} "
                f"(confidence {confidence_text}, source {entry.get('source', 'unrecorded')})."
            )
        if key == "evaluation":
            score = _as_float(entry.get("score"))
            score_text = f"{score:.2f}" if score is not None else "unrecorded"
            return (
                f"Evaluation returned {entry.get('decision', 'unrecorded')} "
                f"(score {score_text})."
            )
        if key == "escalation":
            return f"Escalated: {entry.get('reason', 'no reason recorded')}."
        if key == "audit_summary":
            executed = entry.get("executed_actions") or []
            skipped = entry.get("skipped_actions") or []
            return (
                f"Finalized as {entry.get('investigation_status', 'unrecorded')} — "
                f"{len(executed)} action(s) executed, {len(skipped)} skipped."
            )
        if key == "unrecognised":
            return "Recorded stage with no catalog entry in this build; payload preserved."
        if isinstance(payload, dict):
            # Advisory engines all disclose their own availability the same
            # way, so the honest one-liner is that disclosure, not a
            # re-derived judgement.
            if payload.get("available") is False:
                return f"No evidence available: {payload.get('reason') or 'reason not recorded'}."
            if payload.get("insufficient_data") is True:
                return f"Insufficient data: {payload.get('reason') or 'reason not recorded'}."
            populated = sorted(
                field for field, value in payload.items()
                if isinstance(value, list) and value
            )
            if populated:
                counts = ", ".join(f"{field}={len(payload[field])}" for field in populated)
                return f"Recorded with {counts}."
            return f"Recorded with {len(payload)} field(s)."
        if isinstance(payload, list):
            return f"Recorded {len(payload)} item(s)."
        return "Recorded."

    @staticmethod
    def _gaps(total_by_key: dict[str, int]) -> list[dict[str, Any]]:
        """
        Catalog stages this record does not contain (EXPL-3, COMPAT-1).

        Only stages whose absence is genuinely informative are listed:
        conditional-by-design stages (escalation, LLM parse failure,
        approval gate) are excluded, because their absence means the
        condition did not arise, not that a step is missing.

        The reason states what is TRUE — that no such entry is present —
        and gives the phase that introduced the stage as context. It never
        claims to know why: an incident can lack an execution plan because
        it predates C7, or because the engine was unwired, and the record
        does not distinguish those.
        """
        gaps: list[dict[str, Any]] = []
        for spec in STAGE_CATALOG:
            if not spec.expected or total_by_key.get(spec.key):
                continue
            gaps.append({
                "key": spec.key,
                "label": spec.label,
                "category": spec.category,
                "introduced_in": spec.introduced_in,
                "reason": (
                    f"No '{spec.label}' entry is present in this incident's recorded "
                    f"findings. This stage was introduced in {spec.introduced_in}; an "
                    "incident recorded before it — or one where the engine was not "
                    "wired — has no such entry. Replay does not reconstruct the step."
                ),
            })
        return gaps

    @staticmethod
    def _category_counts(stages: list[dict[str, Any]]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for stage in stages:
            counts[stage["category"]] = counts.get(stage["category"], 0) + 1
        return counts

    def __repr__(self) -> str:
        return "InvestigationReplayBuilder()"


# ---------------------------------------------------------------------------
# Timeline replay
# ---------------------------------------------------------------------------

class TimelineBuilder:
    """
    Places an investigation's recorded stages against measured time.

    Every number this class emits was measured at investigation time and
    persisted: ``audit_summary.stage_durations`` (per stage, Phase F5) and
    ``audit_summary.investigation_duration_seconds`` (the total, Phase E11).
    The anchor is the incident's own ``timestamp`` column.

    What it deliberately does NOT do
    --------------------------------
    * It does not compute wall-clock offsets. Per-stage start times were
      never persisted, and summing durations would place stages earlier
      than they actually ran (real investigations spend time between
      stages). The cumulative figure it reports is labelled as a sum of
      measured stage time, not a clock position.
    * It does not distribute the total across unmeasured stages.
    * It does not fill an unmeasured stage with zero.

    Where measured stage time falls short of the measured total, the
    difference is reported as ``unattributed_seconds`` with a plain
    explanation. That gap is real information — it is the investigation's
    uninstrumented work — and hiding it by scaling the stage figures up
    would make the timeline a fabrication.
    """

    def build(self, incident: dict[str, Any]) -> dict[str, Any]:
        """
        Build the timeline for ``incident``.

        Returns:
            A dict of the same shape every time, including for an incident
            with no timing data at all (``timing_available: false`` plus the
            reason). Entry order is the recorded order, identical to
            :meth:`InvestigationReplayBuilder.reconstruct`'s — the two views
            are projections of the same array, so they can never disagree
            about what happened or in what order.
        """
        findings = parse_findings(incident.get("findings"))
        audit = find_audit_summary(findings) or {}

        raw_durations = audit.get("stage_durations")
        stage_durations: dict[str, float] = {}
        if isinstance(raw_durations, dict):
            for key, value in raw_durations.items():
                seconds = _as_float(value)
                if seconds is not None:
                    stage_durations[str(key)] = seconds

        total_seconds = _as_float(audit.get("investigation_duration_seconds"))

        total_by_key: dict[str, int] = {}
        for entry in findings:
            key = classify_entry(entry)
            total_by_key[key] = total_by_key.get(key, 0) + 1

        entries: list[dict[str, Any]] = []
        occurrences: dict[str, int] = {}
        cumulative = 0.0
        counted_keys: set[str] = set()

        for index, entry in enumerate(findings):
            key = classify_entry(entry)
            occurrences[key] = occurrences.get(key, 0) + 1
            spec = _SPEC_BY_KEY.get(key)
            measured = stage_durations.get(key)
            occurrences_total = total_by_key.get(key, 1)

            # A stage's measured total is added to the cumulative figure
            # ONCE, however many times the stage was recorded — the number
            # already covers every occurrence.
            if measured is not None and key not in counted_keys:
                cumulative += measured
                counted_keys.add(key)

            entries.append({
                "sequence": index,
                "occurrence": occurrences[key],
                "occurrences_total": occurrences_total,
                "key": key,
                "label": spec.label if spec else f"Unrecognised stage ({key})",
                "category": spec.category if spec else "unrecognised",
                "duration_available": measured is not None,
                # Per-occurrence seconds, and only when the attribution is
                # unambiguous (see InvestigationReplayBuilder._stage_duration).
                "duration_seconds": (
                    measured if measured is not None and occurrences_total == 1 else None
                ),
                "stage_total_seconds": measured,
                "duration_source": (
                    "audit_summary.stage_durations" if measured is not None else None
                ),
                "duration_note": self._duration_note(measured, occurrences_total),
                # Sum of measured stage time up to and including this stage.
                # NOT a wall-clock offset — see the class docstring.
                "cumulative_measured_seconds": round(cumulative, 4),
            })

        measured_total = round(sum(stage_durations.get(k, 0.0) for k in counted_keys), 4)
        unattributed = None
        if total_seconds is not None and counted_keys:
            unattributed = round(total_seconds - measured_total, 4)

        stages_with_duration = sum(1 for e in entries if e["duration_available"])

        return {
            "incident_id": incident.get("incident_id"),
            "anchor_timestamp": incident.get("timestamp"),
            "anchor_source": "incidents.timestamp",
            "relative_to": (
                "Durations are measured per stage. No wall-clock start time was "
                "persisted per stage, so no stage is placed at a clock position."
            ),
            "timing_available": bool(stage_durations) or total_seconds is not None,
            "timing_reason": (
                None
                if (stage_durations or total_seconds is not None)
                else (
                    "This incident carries no measured durations. Total investigation "
                    "duration was introduced in Phase E11 and per-stage durations in "
                    "Phase F5; an incident recorded before those has none, and replay "
                    "does not estimate them."
                )
            ),
            "total_investigation_seconds": total_seconds,
            "total_source": (
                "audit_summary.investigation_duration_seconds"
                if total_seconds is not None
                else None
            ),
            "measured_stage_seconds": measured_total if counted_keys else None,
            "unattributed_seconds": unattributed,
            "unattributed_note": (
                "Measured total minus the sum of measured stage time — the "
                "investigation's uninstrumented work (event handling, state "
                "transitions, persistence). Reported rather than distributed "
                "across stages."
                if unattributed is not None
                else "Not computable: the total, the per-stage durations, or both are absent."
            ),
            "stages_total": len(entries),
            "stages_with_duration": stages_with_duration,
            "stages_without_duration": len(entries) - stages_with_duration,
            "entries": entries,
            "by_stage": {
                key: {
                    "seconds": stage_durations[key],
                    "occurrences": total_by_key.get(key, 0),
                    "label": _SPEC_BY_KEY[key].label if key in _SPEC_BY_KEY else key,
                }
                for key in sorted(counted_keys, key=lambda k: _CATALOG_ORDER.get(k, 10_000))
            },
        }

    @staticmethod
    def _duration_note(measured: float | None, occurrences_total: int) -> str | None:
        if measured is None:
            return "No measured duration recorded for this stage."
        if occurrences_total > 1:
            return (
                f"{measured}s is the measured total across {occurrences_total} "
                "recorded occurrences of this stage, not this occurrence alone."
            )
        return None

    def __repr__(self) -> str:
        return "TimelineBuilder()"
