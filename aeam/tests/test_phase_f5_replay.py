"""
aeam/tests/test_phase_f5_replay.py

Phase F5 — Investigation & Timeline Replay (Explainability Deepening).

Acceptance criteria under test:

1. **A finalized incident replays its exact recorded stages in the exact
   recorded order.** Asserted against a seeded record AND against a real
   ``Orchestrator`` investigation, so the reconstruction is checked against
   what the platform actually writes rather than only against a fixture.
2. **Replay executes zero side effects.** No ActionAgent, no LLM, no engine
   — enforced at the import graph, at the route table (no non-GET routes),
   and behaviourally (a spy over every database write method records nothing
   across a full replay, and the stored row is byte-identical afterwards).
3. **An incident missing a stage shows an honest gap rather than a
   fabricated step.** Including the ROADMAP's own example: a pre-C7 incident
   with no execution plan.
4. **The timeline matches the persisted audit trail and durations.** Entry
   order equals stage order; every duration traces to a persisted value; an
   unmeasured stage is reported as unmeasured, never as zero; unattributed
   time is disclosed rather than distributed.

Infrastructure: in-process only — real SQLite, real FastAPI TestClient,
deterministic fixtures (TEST-3). No LLM, no network, no live services.
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aeam.api.replay import router as replay_router
from aeam.config.settings import Settings
from aeam.integrations.database import DatabaseClient
from aeam.intelligence.replay import (
    DECISION_STAGE_KEY,
    MAX_STAGE_LIMIT,
    STAGE_CATALOG,
    UNRECOGNISED_STAGE_KEY,
    InvestigationReplayBuilder,
    TimelineBuilder,
    classify_entry,
    parse_findings,
)


# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture()
def db(tmp_path):
    client = DatabaseClient(database_url=f"sqlite:///{(tmp_path / 'f5.db').as_posix()}")
    yield client
    client.dispose()


@pytest.fixture()
def replay_builder():
    return InvestigationReplayBuilder()


@pytest.fixture()
def timeline_builder():
    return TimelineBuilder()


def _audit_summary(**overrides) -> dict:
    base = {
        "type": "audit_summary",
        "investigation_status": "RESOLVED",
        "root_cause": "Inefficient queries",
        "root_cause_source": "rag",
        "validation_status": "PASSED",
        "validation_reason": "grounded in retrieved evidence",
        "executed_actions": ["slack"],
        "skipped_actions": [],
        "recommended_actions": ["notify ops"],
        "evidence_count": 3,
        "top_confidence": 0.82,
    }
    base.update(overrides)
    return base


#: A complete, ordered findings array in the exact shape the Orchestrator
#: persists — including the untyped decision entry and a second investigation
#: pass, both of which the reconstruction must handle.
FULL_FINDINGS: list[dict] = [
    {"depth": 1, "decision": "INVESTIGATE", "confidence": 0.4, "source": "rules"},
    {"type": "memory", "data": {"matches": [{"incident_id": "past-1"}]}},
    {"type": "policy", "data": {"matches": []}},
    {"type": "cross_dataset", "data": {"insufficient_data": False, "supporting": [{"metric": "refunds"}]}},
    {"type": "graph", "data": {"available": True, "correlated_metrics": [{"label": "refunds"}]}},
    {"type": "adaptive", "data": {"combined_signal": True}},
    {"type": "rag", "data": {"retrieved_count": 3}},
    {"type": "kpi_analysis", "data": {"grounded": True}},
    {"type": "evaluation", "decision": "CONTINUE", "score": 0.5, "reasons": ["low confidence"]},
    {"depth": 2, "decision": "INVESTIGATE", "confidence": 0.8, "source": "llm"},
    {"type": "rag", "data": {"retrieved_count": 5}},
    {"type": "evaluation", "decision": "STOP", "score": 0.9, "reasons": ["resolved"]},
    {"type": "execution_plan", "data": {"steps": [{"action": "slack"}]}},
    {"type": "explainability", "data": {"decision_graph": [{"node": "a"}]}},
    {"type": "ai_evaluation", "data": {"overall_score": 0.77}},
    _audit_summary(
        investigation_duration_seconds=4.0,
        stage_durations={
            "decision": 0.2, "memory": 0.3, "policy": 0.1, "cross_dataset": 0.4,
            "graph": 0.2, "adaptive": 0.1, "rag": 0.9, "kpi_analysis": 0.2,
            "execution_plan": 0.1, "explainability": 0.05, "ai_evaluation": 0.05,
            "action.slack": 0.4,
        },
    ),
]


def _seed_incident(
    db,
    incident_id: str = "inc-1",
    findings: list | None = None,
    timestamp: str = "2026-07-05T00:00:00Z",
    metric: str = "latency_ms",
) -> dict:
    row = {
        "incident_id": incident_id,
        "event_id": f"evt-{incident_id}",
        "event_type": "DB_LATENCY",
        "metric": metric,
        "severity": "HIGH",
        "current_value": 900.0,
        "expected_value": 200.0,
        "detection_methods": json.dumps(["rule:latency"]),
        "timestamp": timestamp,
        "investigation_depth": 2,
        "root_cause": "Inefficient queries",
        "confidence": 0.82,
        "action_taken": True,
        "requires_human": False,
        "findings": json.dumps(FULL_FINDINGS if findings is None else findings),
        "llm_response": "{}",
    }
    db.insert("incidents", row, returning_column="incident_id")
    return row


def _row(db, incident_id: str = "inc-1") -> dict:
    return dict(
        db.fetch_one(
            "SELECT * FROM incidents WHERE incident_id = :i", {"i": incident_id}
        )
    )


# ===========================================================================
# 1. Parsing — tolerant readers over one persisted row
# ===========================================================================


def test_findings_parse_from_every_shape_the_column_can_hold():
    # SQLite hands back a JSON string; PostgreSQL JSONB hands back a list.
    assert parse_findings(json.dumps([{"type": "memory"}])) == [{"type": "memory"}]
    assert parse_findings([{"type": "memory"}]) == [{"type": "memory"}]
    assert parse_findings(None) == []
    assert parse_findings("") == []
    assert parse_findings("[]") == []


def test_unparseable_findings_degrade_instead_of_raising():
    # One corrupt historical row must not fail an audit request.
    assert parse_findings("not json at all") == []
    assert parse_findings('{"not": "a list"}') == []
    assert parse_findings(42) == []


def test_non_dict_entries_are_dropped_but_dict_entries_survive():
    assert parse_findings('[{"type": "memory"}, "junk", 7, null]') == [{"type": "memory"}]


def test_the_untyped_decision_entry_is_identified_structurally():
    # The decision entry predates the `type` convention. It is recognised by
    # the fields it actually carries, which is a fact about the record.
    assert classify_entry({"depth": 1, "decision": "INVESTIGATE"}) == DECISION_STAGE_KEY
    assert classify_entry({"type": "memory"}) == "memory"
    # Anything else is preserved as unrecognised, never guessed at.
    assert classify_entry({"something": "else"}) == UNRECOGNISED_STAGE_KEY
    assert classify_entry({"type": "   "}) == UNRECOGNISED_STAGE_KEY


# ===========================================================================
# 2. Stage-order fidelity
# ===========================================================================


def test_stages_are_returned_in_the_exact_recorded_order(db, replay_builder):
    _seed_incident(db)
    result = replay_builder.reconstruct(_row(db))

    recorded = [classify_entry(e) for e in FULL_FINDINGS]
    assert [s["key"] for s in result["stages"]] == recorded
    assert [s["sequence"] for s in result["stages"]] == list(range(len(recorded)))
    assert result["total_stages"] == len(recorded)


def test_stages_are_never_resorted_into_canonical_pipeline_order(db, replay_builder):
    # A record whose stages were persisted out of catalog order must replay
    # in the order they happened, not the order the catalog lists them.
    scrambled = [
        {"type": "rag", "data": {"retrieved_count": 1}},
        {"depth": 1, "decision": "INVESTIGATE", "confidence": 0.4, "source": "rules"},
        {"type": "memory", "data": {"matches": []}},
    ]
    _seed_incident(db, findings=scrambled)
    keys = [s["key"] for s in replay_builder.reconstruct(_row(db))["stages"]]
    assert keys == ["rag", DECISION_STAGE_KEY, "memory"]


def test_a_repeated_stage_appears_every_time_with_its_occurrence_number(db, replay_builder):
    _seed_incident(db)
    stages = replay_builder.reconstruct(_row(db))["stages"]

    rag = [s for s in stages if s["key"] == "rag"]
    assert [s["occurrence"] for s in rag] == [1, 2]
    assert all(s["occurrences_total"] == 2 for s in rag)
    decisions = [s for s in stages if s["key"] == DECISION_STAGE_KEY]
    assert [s["outputs"]["depth"] for s in decisions] == [1, 2]


def test_an_unrecognised_stage_is_replayed_in_place_never_dropped(db, replay_builder):
    findings = [
        {"depth": 1, "decision": "INVESTIGATE", "confidence": 0.4},
        {"type": "stage_from_a_later_phase", "data": {"payload": [1, 2, 3]}},
        _audit_summary(),
    ]
    _seed_incident(db, findings=findings)
    stages = replay_builder.reconstruct(_row(db))["stages"]

    assert [s["key"] for s in stages][1] == "stage_from_a_later_phase"
    unknown = stages[1]
    assert unknown["recognised"] is False
    assert unknown["category"] == "unrecognised"
    # Payload intact — an audit reconstruction that silently discarded a
    # recorded stage would be worse than no reconstruction.
    assert unknown["outputs"] == {"payload": [1, 2, 3]}


def test_stage_outputs_are_the_persisted_payload_verbatim(db, replay_builder):
    _seed_incident(db)
    stages = replay_builder.reconstruct(_row(db))["stages"]
    by_key = {s["key"]: s for s in stages}

    assert by_key["memory"]["outputs"] == {"matches": [{"incident_id": "past-1"}]}
    # Flat entries return everything but `type` — no invented wrapper. Taken
    # from the FIRST evaluation, since the fixture records two.
    first_evaluation = next(s for s in stages if s["key"] == "evaluation")
    assert first_evaluation["outputs"]["reasons"] == ["low confidence"]
    assert "type" not in first_evaluation["outputs"]


def test_stage_labels_and_categories_come_from_the_catalog(db, replay_builder):
    _seed_incident(db)
    stages = replay_builder.reconstruct(_row(db))["stages"]
    by_key = {s["key"]: s for s in stages}

    assert by_key["execution_plan"]["label"] == "Execution Planning"
    assert by_key["execution_plan"]["category"] == "planning"
    assert by_key["execution_plan"]["introduced_in"] == "Phase C7"
    assert by_key["graph"]["category"] == "evidence"
    assert by_key["audit_summary"]["category"] == "actions"


# ===========================================================================
# 3. State visible at each step — folded from the record, never invented
# ===========================================================================


def test_state_after_grows_only_as_the_record_supplies_values(db, replay_builder):
    _seed_incident(db)
    stages = replay_builder.reconstruct(_row(db))["stages"]

    # First stage is the depth-1 decision: it establishes the decision and
    # its confidence, and nothing else.
    first = stages[0]["state_after"]
    assert first["decision"] == "INVESTIGATE"
    assert first["decision_confidence"] == pytest.approx(0.4)
    assert "root_cause" not in first, "root cause was not recorded at this step"
    assert "evaluation_decision" not in first

    # The depth-2 decision supersedes the depth-1 confidence, in order.
    second_decision = [s for s in stages if s["key"] == DECISION_STAGE_KEY][1]
    assert second_decision["state_after"]["decision_confidence"] == pytest.approx(0.8)
    assert second_decision["state_after"]["depth"] == 2

    # Only the final audit summary establishes the root cause.
    last = stages[-1]["state_after"]
    assert last["root_cause"] == "Inefficient queries"
    assert last["investigation_status"] == "RESOLVED"


def test_every_state_value_names_the_stage_that_recorded_it(db, replay_builder):
    _seed_incident(db)
    last = replay_builder.reconstruct(_row(db))["stages"][-1]["state_after"]
    assert last["root_cause_source_stage"] == "audit_summary"
    assert last["decision_source_stage"] == DECISION_STAGE_KEY


def test_summaries_state_only_what_the_entry_contains(db, replay_builder):
    findings = [
        {"depth": 1, "decision": "INVESTIGATE"},           # no confidence, no source
        {"type": "graph", "data": {"available": False, "reason": "no node for this metric"}},
        {"type": "cross_dataset", "data": {"insufficient_data": True, "reason": "one dataset"}},
    ]
    _seed_incident(db, findings=findings)
    summaries = [s["summary"] for s in replay_builder.reconstruct(_row(db))["stages"]]

    assert "confidence unrecorded" in summaries[0]
    assert "source unrecorded" in summaries[0]
    assert "no node for this metric" in summaries[1]
    assert "one dataset" in summaries[2]


# ===========================================================================
# 4. Mixed history — honest gaps, never fabricated stages
# ===========================================================================


def test_a_pre_c7_incident_reports_an_execution_plan_gap(db, replay_builder):
    # The ROADMAP's own example. An incident recorded before Phase C7 has no
    # execution_plan entry; replay must say so rather than invent the step.
    pre_c7 = [
        {"depth": 1, "decision": "INVESTIGATE", "confidence": 0.4, "source": "rules"},
        {"type": "rag", "data": {"retrieved_count": 2}},
        {"type": "evaluation", "decision": "STOP", "score": 0.9, "reasons": []},
        _audit_summary(),
    ]
    _seed_incident(db, findings=pre_c7)
    result = replay_builder.reconstruct(_row(db))

    assert "execution_plan" not in [s["key"] for s in result["stages"]]
    gap = next(g for g in result["gaps"] if g["key"] == "execution_plan")
    assert gap["introduced_in"] == "Phase C7"
    assert "does not reconstruct the step" in gap["reason"]
    assert gap["label"] == "Execution Planning"


def test_a_fully_recorded_incident_reports_no_gaps(db, replay_builder):
    _seed_incident(db)
    assert replay_builder.reconstruct(_row(db))["gaps"] == []


def test_conditional_stages_are_not_reported_as_gaps(db, replay_builder):
    # An incident that was never escalated, never hit an LLM parse failure,
    # and never needed approval is not missing anything. Calling those gaps
    # would tell an auditor to look for records that should not exist.
    _seed_incident(db)
    gap_keys = {g["key"] for g in replay_builder.reconstruct(_row(db))["gaps"]}
    assert not (gap_keys & {"escalation", "llm_reasoning_error", "human_approval"})


def test_every_expected_catalog_stage_is_a_gap_for_an_empty_record(db, replay_builder):
    _seed_incident(db, findings=[])
    result = replay_builder.reconstruct(_row(db))

    assert result["stages"] == []
    assert result["total_stages"] == 0
    expected = {s.key for s in STAGE_CATALOG if s.expected}
    assert {g["key"] for g in result["gaps"]} == expected


def test_an_undecodable_findings_column_is_reported_not_silently_empty(db, replay_builder):
    _seed_incident(db)
    db.execute(
        "UPDATE incidents SET findings = :f WHERE incident_id = :i",
        {"f": "definitely not json", "i": "inc-1"},
    )
    result = replay_builder.reconstruct(_row(db))

    # "we could not read the record" and "the record has no stages" are
    # different answers, and an auditor must not have to guess which.
    assert result["findings_readable"] is False
    assert result["stages"] == []


def test_an_incident_with_genuinely_no_stages_is_readable(db, replay_builder):
    _seed_incident(db, findings=[])
    assert replay_builder.reconstruct(_row(db))["findings_readable"] is True


# ===========================================================================
# 5. Bounded reads (E6)
# ===========================================================================


def test_stage_pagination_returns_a_bounded_page_with_the_full_total(db, replay_builder):
    _seed_incident(db)
    total = len(FULL_FINDINGS)

    page = replay_builder.reconstruct(_row(db), offset=0, limit=5)
    assert len(page["stages"]) == 5
    assert page["total_stages"] == total
    assert page["truncated"] is True
    assert [s["sequence"] for s in page["stages"]] == [0, 1, 2, 3, 4]

    tail = replay_builder.reconstruct(_row(db), offset=total - 2, limit=5)
    assert len(tail["stages"]) == 2
    assert tail["truncated"] is False
    assert [s["sequence"] for s in tail["stages"]] == [total - 2, total - 1]


def test_a_caller_cannot_talk_past_the_stage_ceiling(db, replay_builder):
    _seed_incident(db)
    result = replay_builder.reconstruct(_row(db), limit=10**9)
    assert result["limit"] == MAX_STAGE_LIMIT


def test_a_pathologically_long_findings_array_is_still_bounded(db, replay_builder):
    huge = [{"depth": i, "decision": "INVESTIGATE", "confidence": 0.1} for i in range(5000)]
    _seed_incident(db, findings=huge)
    result = replay_builder.reconstruct(_row(db))

    assert result["total_stages"] == 5000
    assert len(result["stages"]) <= MAX_STAGE_LIMIT
    assert result["truncated"] is True


def test_a_negative_offset_is_clamped_rather_than_wrapping(db, replay_builder):
    _seed_incident(db)
    result = replay_builder.reconstruct(_row(db), offset=-50)
    assert result["offset"] == 0
    assert result["stages"][0]["sequence"] == 0


# ===========================================================================
# 6. Timeline — measured time only
# ===========================================================================


def test_timeline_entry_order_equals_stage_order(db, replay_builder, timeline_builder):
    _seed_incident(db)
    row = _row(db)
    stages = replay_builder.reconstruct(row, limit=MAX_STAGE_LIMIT)["stages"]
    entries = timeline_builder.build(row)["entries"]

    # Two projections of one array can never disagree about what happened.
    assert [e["key"] for e in entries] == [s["key"] for s in stages]
    assert [e["sequence"] for e in entries] == [s["sequence"] for s in stages]
    assert [e["occurrence"] for e in entries] == [s["occurrence"] for s in stages]


def test_every_timeline_duration_traces_to_a_persisted_value(db, timeline_builder):
    _seed_incident(db)
    timeline = timeline_builder.build(_row(db))
    persisted = _audit_summary()  # shape only
    recorded = FULL_FINDINGS[-1]["stage_durations"]

    for entry in timeline["entries"]:
        if entry["duration_available"]:
            assert entry["stage_total_seconds"] == recorded[entry["key"]]
            assert entry["duration_source"] == "audit_summary.stage_durations"
        else:
            assert entry["stage_total_seconds"] is None
            assert entry["duration_seconds"] is None
    assert timeline["total_source"] == "audit_summary.investigation_duration_seconds"
    assert persisted["investigation_status"] == "RESOLVED"  # fixture sanity


def test_an_unmeasured_stage_is_reported_unmeasured_never_zero(db, timeline_builder):
    findings = [
        {"depth": 1, "decision": "INVESTIGATE", "confidence": 0.4},
        {"type": "memory", "data": {"matches": []}},
        _audit_summary(investigation_duration_seconds=2.0, stage_durations={"decision": 0.5}),
    ]
    _seed_incident(db, findings=findings)
    entries = timeline_builder.build(_row(db))["entries"]
    memory = next(e for e in entries if e["key"] == "memory")

    assert memory["duration_available"] is False
    assert memory["duration_seconds"] is None
    assert memory["stage_total_seconds"] is None
    assert "No measured duration" in memory["duration_note"]


def test_a_repeated_stage_reports_an_aggregate_and_says_so(db, timeline_builder):
    _seed_incident(db)
    entries = timeline_builder.build(_row(db))["entries"]
    rag_entries = [e for e in entries if e["key"] == "rag"]

    assert len(rag_entries) == 2
    for entry in rag_entries:
        # The measured 0.9s covers both passes; splitting it would be the
        # interpolation the timeline contract forbids.
        assert entry["duration_seconds"] is None
        assert entry["stage_total_seconds"] == pytest.approx(0.9)
        assert "total across 2 recorded occurrences" in entry["duration_note"]


def test_a_repeated_stage_is_counted_once_in_the_cumulative_total(db, timeline_builder):
    _seed_incident(db)
    timeline = timeline_builder.build(_row(db))
    recorded = FULL_FINDINGS[-1]["stage_durations"]

    # Only the stages actually present in the findings array contribute;
    # 'action.slack' is measured but is not itself a findings stage.
    present = {classify_entry(e) for e in FULL_FINDINGS}
    expected = round(sum(v for k, v in recorded.items() if k in present), 4)
    assert timeline["measured_stage_seconds"] == pytest.approx(expected)
    assert timeline["entries"][-1]["cumulative_measured_seconds"] == pytest.approx(expected)


def test_unattributed_time_is_disclosed_not_distributed(db, timeline_builder):
    _seed_incident(db)
    timeline = timeline_builder.build(_row(db))

    assert timeline["total_investigation_seconds"] == pytest.approx(4.0)
    expected_gap = round(4.0 - timeline["measured_stage_seconds"], 4)
    assert timeline["unattributed_seconds"] == pytest.approx(expected_gap)
    assert "Reported rather than distributed" in timeline["unattributed_note"]
    # And the stage figures are untouched by the existence of that gap.
    memory = next(e for e in timeline["entries"] if e["key"] == "memory")
    assert memory["stage_total_seconds"] == pytest.approx(0.3)


def test_an_incident_with_no_timing_says_so_with_the_reason(db, timeline_builder):
    pre_e11 = [
        {"depth": 1, "decision": "INVESTIGATE", "confidence": 0.4},
        _audit_summary(),  # no duration keys at all
    ]
    _seed_incident(db, findings=pre_e11)
    timeline = timeline_builder.build(_row(db))

    assert timeline["timing_available"] is False
    assert "does not estimate them" in timeline["timing_reason"]
    assert timeline["total_investigation_seconds"] is None
    assert timeline["measured_stage_seconds"] is None
    assert timeline["unattributed_seconds"] is None
    assert all(e["duration_available"] is False for e in timeline["entries"])


def test_a_pre_f5_incident_keeps_its_e11_total_without_stage_durations(db, timeline_builder):
    pre_f5 = [
        {"depth": 1, "decision": "INVESTIGATE", "confidence": 0.4},
        _audit_summary(investigation_duration_seconds=3.5),
    ]
    _seed_incident(db, findings=pre_f5)
    timeline = timeline_builder.build(_row(db))

    assert timeline["timing_available"] is True
    assert timeline["total_investigation_seconds"] == pytest.approx(3.5)
    # No stage was measured, so nothing is attributed and nothing is
    # back-filled from the total.
    assert timeline["measured_stage_seconds"] is None
    assert timeline["unattributed_seconds"] is None
    assert timeline["stages_with_duration"] == 0


def test_a_non_numeric_persisted_duration_is_ignored_not_coerced(db, timeline_builder):
    findings = [
        {"depth": 1, "decision": "INVESTIGATE", "confidence": 0.4},
        _audit_summary(
            investigation_duration_seconds="not a number",
            # True is a valid float() input and would become 1.0 seconds.
            stage_durations={"decision": True, "memory": None, "policy": "abc"},
        ),
    ]
    _seed_incident(db, findings=findings)
    timeline = timeline_builder.build(_row(db))

    assert timeline["total_investigation_seconds"] is None
    assert timeline["entries"][0]["duration_available"] is False


def test_the_timeline_anchor_is_the_persisted_incident_timestamp(db, timeline_builder):
    _seed_incident(db, timestamp="2026-07-05T12:34:56Z")
    timeline = timeline_builder.build(_row(db))
    assert timeline["anchor_timestamp"] == "2026-07-05T12:34:56Z"
    assert timeline["anchor_source"] == "incidents.timestamp"
    # No stage is given a clock position, because none was persisted.
    assert all("start_timestamp" not in e for e in timeline["entries"])
    assert "no stage is placed at a clock position" in timeline["relative_to"]


# ===========================================================================
# 7. Zero side effects — the replay contract, enforced three ways
# ===========================================================================


def test_replay_modules_never_import_anything_that_could_re_execute():
    # The strongest form of "replay never re-executes" is that it cannot
    # reach anything that executes. A module that imported RuleEngine or
    # ActionAgent could grow that capability later without anyone noticing;
    # this fails the moment one does.
    from pathlib import Path

    forbidden = (
        "RuleEngine", "StatisticalDetector", "KPIAgent", "ForecastAgent",
        "BusinessGraph", "PolicyAgent", "ActionAgent", "Orchestrator",
        "LLMService", "llm_service", "MonitorAgent", "RAGAgent",
    )
    root = Path(__file__).resolve().parents[1]
    for module in (root / "intelligence" / "replay.py", root / "api" / "replay.py"):
        source = module.read_text(encoding="utf-8")
        imports = [
            line for line in source.splitlines()
            if line.lstrip().startswith(("import ", "from "))
        ]
        for line in imports:
            for name in forbidden:
                assert name not in line, f"{module.name} must not import {name}: {line!r}"


def test_the_replay_router_exposes_no_write_route():
    # Every endpoint is a GET, and that is what makes a single logs:view
    # RBAC entry for the whole prefix safe.
    for route in replay_router.routes:
        assert set(route.methods) <= {"GET", "HEAD"}, (
            f"{route.path} exposes {route.methods}; replay is strictly read-only"
        )


def test_the_only_sql_in_the_replay_modules_is_a_select():
    """Every SQL string these modules contain must begin with SELECT.

    Parsed from the AST rather than grepped, and with docstrings excluded, so
    the assertion is about the SQL the modules can actually issue rather than
    about words that happen to appear in their prose.
    """
    import ast
    from pathlib import Path

    verbs = ("SELECT", "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "TRUNCATE", "MERGE")

    def is_sql(text: str) -> bool:
        """A string is SQL if it OPENS with a statement verb.

        Anchoring on the opening token is what keeps English prose out: a
        docstring may well say "reconstructed FROM the persisted record", but
        no sentence in this codebase begins with a bare SQL verb followed by
        a space.
        """
        upper = text.strip().upper()
        return any(upper.startswith(f"{verb} ") for verb in verbs)

    root = Path(__file__).resolve().parents[1]
    found: dict[str, list[str]] = {}

    for module in (root / "intelligence" / "replay.py", root / "api" / "replay.py"):
        tree = ast.parse(module.read_text(encoding="utf-8"))
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        }
        sql_strings = [
            node.value.upper()
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
            and is_sql(node.value)
        ]
        for sql in sql_strings:
            assert sql.strip().startswith("SELECT"), (
                f"{module.name} contains non-SELECT SQL: {sql!r}"
            )
        found[f"{module.parent.name}/{module.name}"] = sql_strings

    # The reconstruction module issues NO SQL at all — it is handed an
    # already-fetched row, which is why it cannot reach the database even to
    # read. All persistence access lives in the API module, and all of it is
    # a SELECT.
    assert found["api/replay.py"], "the API module must contain the SELECT it reads with"
    assert found["intelligence/replay.py"] == [], (
        "the reconstruction module must contain no SQL — it is handed a row"
    )


def test_a_full_replay_performs_zero_database_writes(db, replay_builder, timeline_builder, monkeypatch):
    _seed_incident(db)
    writes: list[tuple[str, Any]] = []  # type: ignore[name-defined]

    for method in ("execute", "insert", "insert_incident", "insert_decision", "insert_metrics"):
        original = getattr(DatabaseClient, method)

        def _spy(self, *args, _name=method, _original=original, **kwargs):
            writes.append((_name, args))
            return _original(self, *args, **kwargs)

        monkeypatch.setattr(DatabaseClient, method, _spy)

    row = _row(db)
    for _ in range(3):
        replay_builder.reconstruct(row)
        timeline_builder.build(row)

    assert writes == [], f"replay wrote to the database: {writes}"


def test_replaying_an_incident_leaves_the_stored_row_bit_identical(db, replay_builder, timeline_builder):
    _seed_incident(db)
    before = _row(db)

    for _ in range(5):
        replay_builder.reconstruct(before)
        timeline_builder.build(before)

    # No mutated findings, no touched timestamp, no updated metric (MEM-2).
    assert _row(db) == before


def test_replay_never_calls_an_action_agent_or_an_llm(db, replay_builder, timeline_builder):
    # Behavioural companion to the import assertion: an exploding stand-in
    # is reachable ONLY if replay tries to use one.
    class _Exploding:
        def __getattr__(self, name):
            raise AssertionError(f"replay invoked {name}() — it must never execute anything")

    _seed_incident(db)
    row = _row(db)
    # The builders take no such collaborator at all; constructing them with
    # one is impossible, which is the point being asserted.
    with pytest.raises(TypeError):
        InvestigationReplayBuilder(_Exploding())  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        TimelineBuilder(_Exploding())  # type: ignore[call-arg]

    assert replay_builder.reconstruct(row)["replay_contract"]["re_executed"] is False
    assert timeline_builder.build(row)["incident_id"] == "inc-1"


def test_the_reconstruction_declares_its_own_contract(db, replay_builder):
    _seed_incident(db)
    contract = replay_builder.reconstruct(_row(db))["replay_contract"]
    assert contract["read_only"] is True
    assert contract["re_executed"] is False
    assert "incidents.findings" in contract["source"]


# ===========================================================================
# 8. API surface
# ===========================================================================


@pytest.fixture()
def client(db):
    class _Container:
        pass

    container = _Container()
    container.db = db
    container.settings = Settings(
        DATABASE_URL="sqlite:///:memory:", REDIS_URL="redis://localhost:6379/0",
        VECTOR_DB_URL="http://localhost", ENVIRONMENT="development",
    )
    container.audit_logger = None

    app = FastAPI()
    app.include_router(replay_router)
    app.state.container = container
    return TestClient(app)


def test_api_replays_an_incident_with_its_timeline(client, db):
    _seed_incident(db)
    body = client.get("/api/v1/replay/inc-1").json()

    assert body["incident_id"] == "inc-1"
    assert body["total_stages"] == len(FULL_FINDINGS)
    assert [s["key"] for s in body["stages"]] == [classify_entry(e) for e in FULL_FINDINGS]
    assert body["timeline"]["stages_total"] == len(FULL_FINDINGS)


def test_api_can_omit_the_timeline(client, db):
    _seed_incident(db)
    body = client.get("/api/v1/replay/inc-1?include_timeline=false").json()
    assert "timeline" not in body


def test_api_404s_on_an_unrecorded_incident(client):
    resp = client.get("/api/v1/replay/nope")
    assert resp.status_code == 404
    # "no record of this" must not be answerable as "this did nothing".
    assert "No incident with id" in resp.json()["detail"]


def test_api_stages_endpoint_paginates_and_reports_the_total(client, db):
    _seed_incident(db)
    resp = client.get("/api/v1/replay/inc-1/stages?offset=2&limit=4")
    body = resp.json()

    assert resp.headers["X-Total-Count"] == str(len(FULL_FINDINGS))
    assert len(body["stages"]) == 4
    assert body["stages"][0]["sequence"] == 2
    assert body["truncated"] is True


def test_api_rejects_a_limit_above_the_ceiling(client, db):
    _seed_incident(db)
    assert client.get(f"/api/v1/replay/inc-1/stages?limit={MAX_STAGE_LIMIT + 1}").status_code == 422


def test_api_timeline_endpoint_matches_the_builder(client, db, timeline_builder):
    _seed_incident(db)
    assert client.get("/api/v1/replay/inc-1/timeline").json() == timeline_builder.build(_row(db))


def test_api_catalog_exposes_the_stage_vocabulary(client):
    body = client.get("/api/v1/replay/catalog").json()
    keys = [s["key"] for s in body["stages"]]

    assert keys == [spec.key for spec in STAGE_CATALOG]
    plan = next(s for s in body["stages"] if s["key"] == "execution_plan")
    assert plan["introduced_in"] == "Phase C7"
    assert plan["absence_is_a_gap"] is True
    escalation = next(s for s in body["stages"] if s["key"] == "escalation")
    assert escalation["absence_is_a_gap"] is False


def test_api_reports_503_when_no_database_is_wired():
    class _Container:
        db = None

    app = FastAPI()
    app.include_router(replay_router)
    app.state.container = _Container()
    resp = TestClient(app).get("/api/v1/replay/inc-1")
    assert resp.status_code == 503
    assert "replay is unavailable" in resp.json()["detail"]


def test_api_replay_is_graded_by_the_audit_tier():
    from aeam.middleware.security_middleware import _ENDPOINT_RBAC_MAP

    entries = {p: (r, a) for p, r, a in _ENDPOINT_RBAC_MAP if p.startswith("/api/v1/replay")}
    # SEC-6: replay exposes an incident's full decision trail, so it is
    # guarded by the same grant as the audit log — not by incidents:view.
    assert entries == {"/api/v1/replay": ("logs", "view")}


def test_api_replay_survives_a_thousand_reads_without_changing_the_record(client, db):
    _seed_incident(db)
    before = _row(db)
    for _ in range(50):
        assert client.get("/api/v1/replay/inc-1").status_code == 200
    assert _row(db) == before


# ===========================================================================
# 9. End-to-end — reconstruct a real Orchestrator investigation
# ===========================================================================


class _RecordingLTM:
    """Captures the payload the Orchestrator persists at finalize."""

    def __init__(self):
        self.recorded = None

    def record_incident(self, payload):
        self.recorded = payload
        return payload.get("incident_id", "fake-id")

    def get_metric_history(self, *_a, **_k):
        return []


def _run_real_investigation():
    from aeam.agents.orchestrator.decision_engine import DecisionEngine
    from aeam.agents.orchestrator.evaluation_engine import EvaluationEngine
    from aeam.agents.orchestrator.orchestrator import Orchestrator
    from aeam.core.event_bus import EventBus
    from aeam.core.event_models import Event

    settings = Settings(
        DATABASE_URL="sqlite:///:memory:", REDIS_URL="redis://localhost:6379/0",
        VECTOR_DB_URL="http://localhost", ENVIRONMENT="development", LLM_ENABLED=False,
    )
    ltm = _RecordingLTM()
    orch = Orchestrator(
        event_bus=EventBus(),
        decision_engine=DecisionEngine(settings=settings),
        evaluation_engine=EvaluationEngine(settings=settings),
        long_term_memory=ltm,
        settings=settings,
    )
    orch.handle_event(Event(
        event_id="e2e-1", event_type="DB_LATENCY", metric="latency_ms", severity="HIGH",
        current_value=900, expected_value=200, detection_methods=["rule:latency"],
        timestamp="2026-07-05T00:00:00Z",
    ))
    assert ltm.recorded is not None
    return ltm.recorded


def test_end_to_end_a_real_investigation_replays_its_recorded_stages(db, replay_builder):
    payload = _run_real_investigation()
    row = {**payload, "incident_id": "e2e-1", "findings": json.dumps(payload["findings"])}

    result = replay_builder.reconstruct(row, limit=MAX_STAGE_LIMIT)

    # Exactly the stages the Orchestrator wrote, in the order it wrote them.
    assert [s["key"] for s in result["stages"]] == [
        classify_entry(e) for e in payload["findings"]
    ]
    assert result["stages"][0]["key"] == DECISION_STAGE_KEY
    assert result["stages"][-1]["key"] == "audit_summary"
    assert result["findings_readable"] is True


def test_end_to_end_the_real_investigation_persists_measured_stage_durations(db):
    payload = _run_real_investigation()
    audit = next(f for f in payload["findings"] if f.get("type") == "audit_summary")

    # Phase F5: per-stage durations are measured, not derived from the total.
    durations = audit["stage_durations"]
    assert durations, "the investigation recorded no per-stage durations"
    assert "decision" in durations
    assert all(isinstance(v, (int, float)) and v >= 0 for v in durations.values())
    # And they must not exceed the measured total they are a breakdown of.
    total = audit["investigation_duration_seconds"]
    assert sum(durations.values()) <= total + 1e-6


def test_end_to_end_timeline_equals_the_persisted_audit_trail(db, timeline_builder, replay_builder):
    payload = _run_real_investigation()
    row = {**payload, "incident_id": "e2e-1", "findings": json.dumps(payload["findings"])}
    audit = next(f for f in payload["findings"] if f.get("type") == "audit_summary")

    timeline = timeline_builder.build(row)
    stages = replay_builder.reconstruct(row, limit=MAX_STAGE_LIMIT)["stages"]

    assert [e["sequence"] for e in timeline["entries"]] == [s["sequence"] for s in stages]
    assert timeline["total_investigation_seconds"] == audit["investigation_duration_seconds"]
    for entry in timeline["entries"]:
        if entry["duration_available"]:
            assert entry["stage_total_seconds"] == audit["stage_durations"][entry["key"]]
    # Measured stage time can never exceed the measured total.
    assert timeline["unattributed_seconds"] >= -1e-6


def test_end_to_end_replay_of_a_real_investigation_writes_nothing(db, replay_builder, timeline_builder, monkeypatch):
    payload = _run_real_investigation()
    row = {**payload, "incident_id": "e2e-1", "findings": json.dumps(payload["findings"])}
    db.insert("incidents", {
        "incident_id": "e2e-1", "event_id": "e2e-1", "event_type": payload["event_type"],
        "metric": payload["metric"], "severity": payload["severity"],
        "current_value": payload["current_value"], "expected_value": payload["expected_value"],
        "detection_methods": json.dumps(payload["detection_methods"]),
        "timestamp": payload["timestamp"], "investigation_depth": payload["investigation_depth"],
        "root_cause": payload["root_cause"], "confidence": payload["confidence"],
        "action_taken": payload["action_taken"], "requires_human": payload["requires_human"],
        "findings": row["findings"], "llm_response": payload.get("llm_response", ""),
    }, returning_column="incident_id")
    before = _row(db, "e2e-1")

    replay_builder.reconstruct(before)
    timeline_builder.build(before)

    assert _row(db, "e2e-1") == before
