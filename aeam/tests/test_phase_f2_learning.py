"""
aeam/tests/test_phase_f2_learning.py

Phase F2 — Adaptive Learning, Feedback Loop & Confidence Recalibration.

Acceptance criteria under test:

1. **Calibration measurably improves on held-out history.** ECE (distance
   of the reliability curve from the diagonal) drops by a recorded margin
   on data the fit never saw, and Brier does not regress — so the
   improvement cannot be the degenerate "predict the base rate" solution.
2. **Both raw and calibrated confidence are persisted and shown.**
3. **No past incident record is mutated** (MEM-2) — asserted by hashing
   every row of every historical table before and after a learning run.
4. **Every threshold change is human-approved and audit-logged** (AGENT-5)
   — and the agent has no method that applies one.
5. **Disabling the flag reverts to raw confidence exactly.**
6. **Drift metrics state their semantics** (OBS-2).

Infrastructure: in-process only — real SQLite, real Orchestrator, real
FastAPI TestClient, deterministic fixtures (TEST-3). No network, no LLM.
"""

from __future__ import annotations

import hashlib
import json
import random
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aeam.agents.learning.learning_agent import (
    LearningAgent,
    LearningError,
    ProposalConflictError,
    calibrate_confidence,
)
from aeam.agents.orchestrator.decision_engine import DecisionEngine
from aeam.agents.orchestrator.evaluation_engine import EvaluationEngine
from aeam.agents.orchestrator.orchestrator import Orchestrator
from aeam.api.learning import router as learning_router
from aeam.config.settings import Settings
from aeam.core.event_bus import EventBus
from aeam.core.event_models import Event
from aeam.integrations.database import DatabaseClient
from aeam.intelligence.calibration import (
    CalibrationEngine,
    LabeledSample,
    apply_calibration,
    calibration_curve,
    extract_labeled_samples,
    fit_isotonic,
)
from aeam.memory.long_term import LongTermMemory

_BASELINE_PATH = Path(__file__).parent / "fixtures" / "calibration_baseline.json"


@pytest.fixture(scope="module")
def baseline() -> dict:
    """The recorded calibration baseline. A missing file fails loudly."""
    assert _BASELINE_PATH.exists(), f"Calibration baseline missing at {_BASELINE_PATH}"
    return json.loads(_BASELINE_PATH.read_text(encoding="utf-8"))


def _overconfident_samples(count: int = 400, seed: int = 42) -> list[LabeledSample]:
    """An overconfident platform: stated confidence far above actual rate.

    Deterministic. This is the failure mode AEAM's additive confidence
    actually produces — independent components each contributing a little,
    with nothing checking the total against reality.
    """
    rng = random.Random(seed)
    actual = {0.2: 0.15, 0.4: 0.25, 0.6: 0.35, 0.8: 0.45, 0.9: 0.55}
    return [
        LabeledSample(
            incident_id=f"inc-{i}",
            confidence=(stated := rng.choice(list(actual))),
            outcome=rng.random() < actual[stated],
            source="status",
        )
        for i in range(count)
    ]


# ===========================================================================
# 1. Calibration improves on held-out history
# ===========================================================================


def test_calibration_beats_the_recorded_baseline(baseline):
    fit = CalibrationEngine().fit(_overconfident_samples())
    recorded = baseline["recorded"]
    margins = baseline["required_margins"]

    assert fit.usable, f"Calibration was not adopted: {fit.reason}"
    assert fit.training_samples == recorded["training_samples"]
    assert fit.holdout_samples == recorded["holdout_samples"]

    absolute = fit.before.ece - fit.after.ece
    relative = absolute / fit.before.ece

    assert absolute >= margins["min_ece_absolute_improvement"], (
        f"Held-out ECE improved by only {absolute:.6f} "
        f"({fit.before.ece:.6f} -> {fit.after.ece:.6f}); "
        f"{margins['min_ece_absolute_improvement']} required."
    )
    assert relative >= margins["min_ece_relative_improvement"], (
        f"Relative ECE improvement {relative:.4f} is below the stated "
        f"{margins['min_ece_relative_improvement']} margin."
    )
    assert fit.after.brier <= fit.before.brier, (
        "Brier regressed while ECE improved — the mapping is collapsing toward "
        "the base rate, which is perfectly calibrated and entirely uninformative."
    )


def test_measurement_is_on_held_out_data_not_training_data():
    """Guards the comparison itself. Measuring on training data reports a
    near-zero ECE for any mapping and proves nothing."""
    samples = _overconfident_samples()
    fit = CalibrationEngine().fit(samples)

    assert fit.holdout_samples > 0
    assert fit.training_samples + fit.holdout_samples == len(samples)
    # A real holdout cannot be driven to zero error.
    assert fit.after.ece > 0.0


def test_calibration_is_reproducible():
    """A governance decision that cannot be re-run and reproduced is not
    auditable — which is why the split is deterministic, not random."""
    first = CalibrationEngine().fit(_overconfident_samples())
    second = CalibrationEngine().fit(_overconfident_samples())

    assert first.knots == second.knots
    assert first.before.ece == second.before.ece
    assert first.after.ece == second.after.ece


def test_calibrated_values_track_the_true_rates():
    fit = CalibrationEngine().fit(_overconfident_samples())
    actual = {0.2: 0.15, 0.4: 0.25, 0.6: 0.35, 0.8: 0.45, 0.9: 0.55}

    for stated, true_rate in actual.items():
        calibrated = apply_calibration(stated, fit.knots)
        assert abs(calibrated - true_rate) < 0.15, (
            f"Stated {stated} calibrated to {calibrated}, true rate {true_rate}."
        )
        # The whole point: an overconfident number must come DOWN.
        assert calibrated <= stated + 0.01


def test_isotonic_pools_tied_confidences():
    """Regression for a defect found on the first fixture run.

    AEAM's confidence is heavily discretized, so a training set contains a
    handful of distinct values repeated many times. Running PAV over raw
    samples made the knot list multivalued at those x's and interpolation
    read whichever it hit — mapping 0.9 to 1.0 where the observed rate was
    0.55, i.e. making an overconfident platform MORE overconfident.
    """
    predictions = [0.9] * 100
    outcomes = [i < 55 for i in range(100)]

    knots = fit_isotonic(predictions, outcomes)

    assert len({x for x, _ in knots}) == len(knots), "Knots are multivalued in x."
    assert apply_calibration(0.9, knots) == pytest.approx(0.55, abs=0.01)


def test_isotonic_output_is_monotone_non_decreasing():
    fit = CalibrationEngine().fit(_overconfident_samples())
    values = [y for _, y in fit.knots]
    assert values == sorted(values), f"Mapping is not monotone: {fit.knots}"


def test_calibration_refuses_below_the_sample_floor():
    """Isotonic on 30 points reproduces the training set and generalises to
    nothing. Refusing is the honest outcome (PHIL-1)."""
    fit = CalibrationEngine().fit(_overconfident_samples(count=30))

    assert not fit.usable
    assert fit.knots == []
    assert "60 required" in fit.reason


def test_calibration_refuses_a_single_outcome_class():
    samples = [LabeledSample(f"i{i}", 0.5 + i * 0.001, True, "status") for i in range(100)]
    fit = CalibrationEngine().fit(samples)

    assert not fit.usable
    assert "successes" in fit.reason


def test_calibration_not_adopted_when_improvement_is_noise():
    """An already-calibrated platform must not adopt a new mapping just
    because one could be computed."""
    rng = random.Random(7)
    samples = [
        LabeledSample(f"i{i}", (c := rng.choice([0.2, 0.5, 0.8])), rng.random() < c, "status")
        for i in range(400)
    ]
    fit = CalibrationEngine().fit(samples)

    if fit.usable:
        pytest.fail(
            f"Adopted a calibration on already-calibrated data: "
            f"ECE {fit.before.ece} -> {fit.after.ece}"
        )
    assert "below the" in fit.reason and "threshold" in fit.reason


# ===========================================================================
# 2. Labeled-signal extraction
# ===========================================================================


def test_human_verdict_outranks_derived_status():
    """A reviewer who rejected an analysis has said something the status
    vocabulary cannot express."""
    incidents = [{"incident_id": "a", "confidence": 0.9, "investigation_status": "RESOLVED"}]
    verdicts = [{"incident_id": "a", "verdict": "rejected"}]

    samples, _ = extract_labeled_samples(incidents, verdicts)

    assert len(samples) == 1
    assert samples[0].outcome is False
    assert samples[0].source == "verdict"


def test_escalated_incidents_are_not_scored_as_failures():
    """Escalation means a human was asked, not that the analysis was wrong.
    Scoring it negative would train the platform to be under-confident
    precisely on the incidents that matter most."""
    incidents = [{"incident_id": "a", "confidence": 0.8, "investigation_status": "ESCALATED"}]

    samples, skipped = extract_labeled_samples(incidents, [])

    assert samples == []
    assert skipped["no_outcome_signal"] == 1


def test_in_flight_verdicts_carry_no_signal():
    incidents = [
        {"incident_id": "a", "confidence": 0.8, "investigation_status": "RESOLVED"},
        {"incident_id": "b", "confidence": 0.8, "investigation_status": "RESOLVED"},
    ]
    verdicts = [
        {"incident_id": "a", "verdict": "changes_requested"},
        {"incident_id": "b", "verdict": "escalated"},
    ]

    samples, skipped = extract_labeled_samples(incidents, verdicts)

    assert samples == []
    assert skipped["neutral_verdict"] == 2


def test_unusable_confidences_are_counted_not_defaulted():
    incidents = [
        {"incident_id": "a", "confidence": None, "investigation_status": "RESOLVED"},
        {"incident_id": "b", "confidence": 1.5, "investigation_status": "RESOLVED"},
        {"incident_id": "c", "confidence": "nonsense", "investigation_status": "RESOLVED"},
    ]

    samples, skipped = extract_labeled_samples(incidents, [])

    assert samples == []
    assert skipped["no_confidence"] == 2
    assert skipped["confidence_out_of_range"] == 1


def test_calibration_curve_reports_its_own_semantics():
    """OBS-2: an ECE without its bucket count and sample size is unreadable."""
    curve = calibration_curve([0.1, 0.9, 0.5], [False, True, True], buckets=10)

    assert curve.samples == 3
    assert curve.bucket_count == 10
    assert len(curve.buckets) == 10
    assert all("count" in b and "range" in b for b in curve.buckets)


def test_empty_curve_is_not_perfect_calibration():
    curve = calibration_curve([], [])
    assert curve.samples == 0
    assert curve.ece == 0.0  # callers must check `samples`, which is why it is reported


# ===========================================================================
# 3. MEM-2 — a learning run mutates nothing
# ===========================================================================


@pytest.fixture()
def db(tmp_path):
    client = DatabaseClient(database_url=f"sqlite:///{(tmp_path / 'f2.db').as_posix()}")
    yield client
    client.dispose()


def _seed_history(db: DatabaseClient, count: int = 400, seed: int = 42) -> None:
    """Persist resolved incidents with an overconfident confidence profile."""
    rng = random.Random(seed)
    actual = {0.2: 0.15, 0.4: 0.25, 0.6: 0.35, 0.8: 0.45, 0.9: 0.55}
    for i in range(count):
        stated = rng.choice(list(actual))
        resolved = rng.random() < actual[stated]
        db.insert(
            table="incidents",
            data={
                "incident_id": f"inc-{i:04d}",
                "event_id": f"evt-{i:04d}",
                "event_type": "kpi_anomaly",
                "metric": "sales",
                "severity": "HIGH",
                "timestamp": f"2026-06-{(i % 28) + 1:02d}T00:00:00Z",
                "confidence": stated,
                "requires_human": False,
                "findings": json.dumps([
                    {
                        "type": "audit_summary",
                        "investigation_status": "RESOLVED" if resolved else "FAILED",
                    }
                ]),
            },
        )


def _table_digest(db: DatabaseClient, table: str) -> str:
    """SHA-256 over a table's full contents, order-normalised."""
    try:
        rows = db.fetch_all(f"SELECT * FROM {table}")
    except Exception:  # noqa: BLE001
        return "<absent>"
    canonical = sorted(json.dumps(row, sort_keys=True, default=str) for row in rows)
    return hashlib.sha256("".join(canonical).encode("utf-8")).hexdigest()


#: Every table holding history a learning run must not touch.
HISTORICAL_TABLES = (
    "incidents", "decisions", "action_logs", "audit_logs", "metrics",
    "documents", "policies", "incident_approvals", "review_verdicts",
)


def test_learning_run_mutates_no_historical_row(db):
    """MEM-2, asserted structurally rather than by inspection: every
    historical table is hashed before and after a full recalibration."""
    _seed_history(db)
    before = {t: _table_digest(db, t) for t in HISTORICAL_TABLES}

    result = LearningAgent(database_client=db).recalibrate(created_by="test")

    after = {t: _table_digest(db, t) for t in HISTORICAL_TABLES}

    assert result["adopted"] is True, f"Nothing was learned: {result.get('reason')}"
    changed = [t for t in HISTORICAL_TABLES if before[t] != after[t]]
    assert not changed, f"Learning mutated historical tables: {changed}"


def test_dry_run_persists_nothing_at_all(db):
    _seed_history(db)

    result = LearningAgent(database_client=db).recalibrate(created_by="test", dry_run=True)

    assert result["adopted"] is False
    assert result["dry_run"] is True
    assert result["ece_after"] < result["ece_before"], "A dry run must still measure."
    assert db.fetch_all("SELECT * FROM calibration_models") == []


# ===========================================================================
# 4. Versioning and rollback
# ===========================================================================


def test_adoption_versions_and_supersedes(db):
    _seed_history(db)
    agent = LearningAgent(database_client=db)

    first = agent.recalibrate(created_by="alice")
    second = agent.recalibrate(created_by="bob")

    assert first["version"] == 1
    assert second["version"] == 2

    rows = db.fetch_all("SELECT version, status FROM calibration_models ORDER BY version")
    assert [r["status"] for r in rows] == ["superseded", "active"]
    # Superseding never deletes: the mapping that produced a historical
    # incident's calibrated confidence stays inspectable (COMPAT-7).
    assert len(rows) == 2


def test_restore_reactivates_a_previous_version(db):
    _seed_history(db)
    agent = LearningAgent(database_client=db)
    agent.recalibrate(created_by="alice")
    agent.recalibrate(created_by="bob")

    agent.restore_calibration(version=1, restored_by="carol")

    active = agent.active_calibration()
    assert int(active["version"]) == 1
    statuses = {
        int(r["version"]): r["status"]
        for r in db.fetch_all("SELECT version, status FROM calibration_models")
    }
    assert statuses == {1: "active", 2: "superseded"}


def test_restoring_an_unknown_version_is_refused(db):
    _seed_history(db)
    agent = LearningAgent(database_client=db)
    agent.recalibrate(created_by="alice")

    with pytest.raises(LearningError):
        agent.restore_calibration(version=99, restored_by="carol")


def test_no_active_calibration_is_a_state_not_a_failure(db):
    assert LearningAgent(database_client=db).active_calibration() is None


# ===========================================================================
# 5. AGENT-5 — advisory boundary
# ===========================================================================


def test_learning_agent_has_no_method_that_applies_a_proposal():
    """The enforcement IS the absence. An advisory agent that can enact its
    own advice is not advisory."""
    forbidden = [
        name for name in dir(LearningAgent)
        if name.startswith("apply") or name.startswith("enact") or name.startswith("set_threshold")
    ]
    assert not forbidden, f"LearningAgent gained an apply path: {forbidden}"


def test_proposal_starts_pending_and_changes_nothing(db):
    agent = LearningAgent(database_client=db)

    result = agent.propose_threshold(
        subject="AUTO_EXECUTE_CONFIDENCE_THRESHOLD",
        current_value=0.8,
        proposed_value=0.62,
        rationale="Calibration v1 shows a stated 0.8 resolves 45% of the time.",
        evidence={"ece_before": 0.21, "ece_after": 0.09},
    )

    assert result["status"] == "pending"
    row = db.fetch_all("SELECT * FROM learning_proposals")[0]
    assert row["status"] == "pending"
    assert row["reviewer_id"] is None


def test_proposal_requires_a_rationale(db):
    agent = LearningAgent(database_client=db)
    with pytest.raises(ValueError, match="rationale"):
        agent.propose_threshold(
            subject="X", current_value=1, proposed_value=2, rationale="   ",
        )


def test_deciding_a_proposal_records_attribution_and_does_not_apply(db):
    agent = LearningAgent(database_client=db)
    proposal = agent.propose_threshold(
        subject="AUTO_EXECUTE_CONFIDENCE_THRESHOLD",
        current_value=0.8, proposed_value=0.62, rationale="measured",
    )

    outcome = agent.decide_proposal(
        proposal_id=proposal["proposal_id"], verdict="approved",
        reviewer_id="alice", reviewer_roles=["admin"], attribution_source="jwt",
        note="Agreed.",
    )

    assert outcome["status"] == "approved"
    assert outcome["applied"] is False, "Approval must not apply the change."
    assert "NOT auto-applied" in outcome["note"]

    row = db.fetch_all("SELECT * FROM learning_proposals")[0]
    assert row["reviewer_id"] == "alice"
    assert row["attribution_source"] == "jwt"
    assert row["decided_at"]


def test_a_decided_proposal_cannot_be_redecided(db):
    agent = LearningAgent(database_client=db)
    proposal = agent.propose_threshold(
        subject="X", current_value=1, proposed_value=2, rationale="measured",
    )
    agent.decide_proposal(proposal["proposal_id"], "approved", reviewer_id="alice")

    with pytest.raises(ProposalConflictError):
        agent.decide_proposal(proposal["proposal_id"], "rejected", reviewer_id="mallory")


def test_an_unattributed_decision_is_refused(db):
    agent = LearningAgent(database_client=db)
    proposal = agent.propose_threshold(
        subject="X", current_value=1, proposed_value=2, rationale="measured",
    )

    with pytest.raises(ValueError, match="reviewer_id"):
        agent.decide_proposal(proposal["proposal_id"], "approved", reviewer_id="  ")


# ===========================================================================
# 6. Applying calibration — disclosure and flag-off identity
# ===========================================================================


def test_calibrate_confidence_discloses_the_adjustment():
    """EXPL-4: an adjustment is reported with its magnitude and its reason."""
    calibration = {
        "version": 3,
        "calibration_id": "cal-3",
        "knots": json.dumps([[0.2, 0.15], [0.9, 0.55]]),
        "training_samples": 266,
        "holdout_samples": 134,
        "ece_before": 0.21,
        "ece_after": 0.09,
    }

    value, disclosure = calibrate_confidence(0.9, calibration)

    assert value == pytest.approx(0.55, abs=0.01)
    assert disclosure["applied"] is True
    assert disclosure["confidence_raw"] == 0.9
    assert disclosure["confidence_calibrated"] == value
    assert disclosure["adjustment"] == pytest.approx(-0.35, abs=0.01)
    assert disclosure["calibration_version"] == 3
    assert "266" in disclosure["reason"] and "134" in disclosure["reason"]


def test_calibrate_confidence_passes_through_with_no_active_calibration():
    value, disclosure = calibrate_confidence(0.77, None)

    assert value == 0.77
    assert disclosure["applied"] is False
    assert "No active calibration" in disclosure["reason"]


def test_calibrate_confidence_survives_unreadable_knots():
    value, disclosure = calibrate_confidence(0.77, {"version": 1, "knots": "not-json"})

    assert value == 0.77, "A broken calibration must not corrupt the confidence."
    assert disclosure["applied"] is False
    assert "unreadable" in disclosure["reason"]


class _FakeLTM(LongTermMemory):
    def __init__(self) -> None:
        self.recorded: dict | None = None

    def record_incident(self, data: dict) -> str:
        self.recorded = data
        return data.get("incident_id", "fake")

    def log_decision(self, incident_id, decision):
        return None

    def get_metric_history(self, metric_name, limit=None):
        return []


def _settings(**overrides) -> Settings:
    base = dict(
        DATABASE_URL="sqlite:///:memory:",
        REDIS_URL="redis://localhost:6379/0",
        VECTOR_DB_URL="http://localhost",
        ENVIRONMENT="development",
        LLM_ENABLED=False,
    )
    base.update(overrides)
    return Settings(**base)


def _event() -> Event:
    return Event(
        event_id="f2-evt", event_type="KPI_ANOMALY", metric="sales", severity="HIGH",
        current_value=400.0, expected_value=1000.0, detection_methods=["rule"],
        timestamp="2026-07-01T00:00:00Z",
        metadata={"statistical": {"statistical_anomaly": True, "z_score": -4.1,
                                  "moving_avg": 1000.0}},
    )


def _audit_summary(recorded: dict) -> dict:
    findings = recorded["findings"]
    findings = json.loads(findings) if isinstance(findings, str) else findings
    for entry in findings:
        if entry.get("type") == "audit_summary":
            return entry
    raise AssertionError("No audit_summary in the persisted incident.")


def _orchestrator(ltm, settings, learning_agent=None) -> Orchestrator:
    return Orchestrator(
        event_bus=EventBus(),
        decision_engine=DecisionEngine(settings=settings),
        evaluation_engine=EvaluationEngine(settings=settings),
        long_term_memory=ltm,
        settings=settings,
        learning_agent=learning_agent,
    )


def test_flag_off_reports_raw_confidence_exactly():
    """The stated rollback: flag-off is byte-identical to F1."""
    ltm = _FakeLTM()
    _orchestrator(ltm, _settings()).handle_event(_event())

    audit = _audit_summary(ltm.recorded)
    assert audit["calibration"]["applied"] is False
    assert "disabled" in audit["calibration"]["reason"]
    # Nothing adjusted the number.
    assert "confidence_calibrated" not in audit["calibration"]


def test_calibration_applied_persists_both_values():
    """Acceptance: both raw and calibrated confidence are persisted."""
    class _Agent:
        def active_calibration(self):
            return {
                "version": 1, "calibration_id": "cal-1",
                "knots": json.dumps([[0.0, 0.05], [1.0, 0.5]]),
                "training_samples": 266, "holdout_samples": 134,
                "ece_before": 0.21, "ece_after": 0.09,
            }

    ltm = _FakeLTM()
    _orchestrator(ltm, _settings(), learning_agent=_Agent()).handle_event(_event())

    audit = _audit_summary(ltm.recorded)
    calibration = audit["calibration"]

    assert calibration["applied"] is True
    assert calibration["confidence_raw"] is not None
    assert calibration["confidence_calibrated"] is not None
    assert calibration["confidence_calibrated"] != calibration["confidence_raw"]
    # The persisted confidence is the calibrated one; the raw is retained.
    assert ltm.recorded["confidence"] == calibration["confidence_calibrated"]


def test_a_failing_learning_agent_never_costs_an_incident():
    class _Broken:
        def active_calibration(self):
            raise RuntimeError("calibration store on fire")

    ltm = _FakeLTM()
    _orchestrator(ltm, _settings(), learning_agent=_Broken()).handle_event(_event())

    assert ltm.recorded is not None, "The incident was lost to a calibration failure."
    audit = _audit_summary(ltm.recorded)
    assert audit["calibration"]["applied"] is False
    assert "on fire" in audit["calibration"]["reason"]


def test_settings_default_to_calibration_off():
    settings = _settings()
    assert settings.LEARNING_CALIBRATION_ENABLED is False


def test_orchestrator_builds_no_learning_agent_when_flag_off():
    orchestrator = _orchestrator(_FakeLTM(), _settings())
    assert orchestrator._learning_agent is None


# ===========================================================================
# 7. API surface
# ===========================================================================


@pytest.fixture()
def client(db):
    class _Container:
        pass

    container = _Container()
    container.db = db
    container.settings = _settings(LEARNING_CALIBRATION_ENABLED=True)
    container.audit_logger = None

    app = FastAPI()
    app.include_router(learning_router)
    app.state.container = container
    return TestClient(app)


def test_state_reports_honestly_when_nothing_is_calibrated(client):
    body = client.get("/api/v1/learning/state").json()

    assert body["active"] is False
    assert "reported raw" in body["reason"]


def test_recalibrate_then_state_reports_the_measurement(client, db):
    _seed_history(db)

    run = client.post("/api/v1/learning/recalibrate", json={"actor_id": "alice"}).json()
    assert run["adopted"] is True
    assert run["ece_after"] < run["ece_before"]

    state = client.get("/api/v1/learning/state").json()
    assert state["active"] is True
    assert state["version"] == 1
    assert state["created_by"] == "alice"
    assert state["ece_after"] < state["ece_before"]
    assert state["curve_after"]["bucket_count"] == 10


def test_dry_run_via_api_persists_nothing(client, db):
    _seed_history(db)

    run = client.post("/api/v1/learning/recalibrate", json={"dry_run": True}).json()

    assert run["adopted"] is False
    assert run["dry_run"] is True
    assert client.get("/api/v1/learning/state").json()["active"] is False


def test_history_and_restore_round_trip(client, db):
    _seed_history(db)
    client.post("/api/v1/learning/recalibrate", json={"actor_id": "alice"})
    client.post("/api/v1/learning/recalibrate", json={"actor_id": "bob"})

    history = client.get("/api/v1/learning/history").json()
    assert history["count"] == 2
    assert [v["version"] for v in history["versions"]] == [2, 1]

    restored = client.post("/api/v1/learning/restore", json={"version": 1}).json()
    assert restored["restored_version"] == 1
    assert client.get("/api/v1/learning/state").json()["version"] == 1


def test_restoring_a_missing_version_is_404(client, db):
    _seed_history(db)
    client.post("/api/v1/learning/recalibrate", json={})
    assert client.post("/api/v1/learning/restore", json={"version": 42}).status_code == 404


def test_proposal_decision_via_api(client, db):
    agent = LearningAgent(database_client=db)
    proposal = agent.propose_threshold(
        subject="AUTO_EXECUTE_CONFIDENCE_THRESHOLD",
        current_value=0.8, proposed_value=0.62, rationale="measured",
    )

    listed = client.get("/api/v1/learning/proposals?status=pending").json()
    assert listed["count"] == 1

    decided = client.post(
        f"/api/v1/learning/decisions/{proposal['proposal_id']}",
        json={"verdict": "approved", "reviewer_id": "alice", "note": "ok"},
    )
    assert decided.status_code == 200
    body = decided.json()
    assert body["status"] == "approved"
    assert body["applied"] is False


@pytest.mark.parametrize(
    "verdict, status",
    [("maybe", 400), ("approved", 200)],
)
def test_proposal_verdict_vocabulary_is_enforced(client, db, verdict, status):
    agent = LearningAgent(database_client=db)
    proposal = agent.propose_threshold(
        subject="X", current_value=1, proposed_value=2, rationale="measured",
    )
    response = client.post(
        f"/api/v1/learning/decisions/{proposal['proposal_id']}",
        json={"verdict": verdict, "reviewer_id": "alice"},
    )
    assert response.status_code == status


def test_redeciding_via_api_is_409(client, db):
    agent = LearningAgent(database_client=db)
    proposal = agent.propose_threshold(
        subject="X", current_value=1, proposed_value=2, rationale="measured",
    )
    path = f"/api/v1/learning/decisions/{proposal['proposal_id']}"
    client.post(path, json={"verdict": "approved", "reviewer_id": "alice"})

    assert client.post(path, json={"verdict": "rejected", "reviewer_id": "mallory"}).status_code == 409


def test_state_discloses_when_a_calibration_exists_but_is_not_applied(db):
    """A stored calibration on a deployment with the flag off is NOT in
    force, and reporting it as active would misdescribe what every
    incident's confidence currently means (EXPL-5)."""
    class _Container:
        pass

    container = _Container()
    container.db = db
    container.settings = _settings(LEARNING_CALIBRATION_ENABLED=False)
    container.audit_logger = None

    app = FastAPI()
    app.include_router(learning_router)
    app.state.container = container
    local = TestClient(app)

    _seed_history(db)
    local.post("/api/v1/learning/recalibrate", json={})

    body = local.get("/api/v1/learning/state").json()
    assert body["active"] is True
    assert body["enabled"] is False
    assert body["applied_to_new_incidents"] is False
    assert "raw confidence" in body["note"]


# ===========================================================================
# 8. RBAC parity (SEC-3) and drift metrics (OBS-2)
# ===========================================================================


def test_every_learning_route_is_rbac_mapped():
    from aeam.middleware.security_middleware import _ENDPOINT_RBAC_MAP

    for path in (
        "/api/v1/learning/state",
        "/api/v1/learning/history",
        "/api/v1/learning/proposals",
        "/api/v1/learning/recalibrate",
        "/api/v1/learning/restore",
        "/api/v1/learning/decisions/abc",
    ):
        assert any(path.startswith(prefix) for prefix, _, _ in _ENDPOINT_RBAC_MAP), path


def test_learning_writes_require_admin_config_and_reads_do_not():
    """The collision that matters: a read path nested under a write prefix
    would let an auditor approve a proposal."""
    from aeam.middleware.security_middleware import _ENDPOINT_RBAC_MAP

    def resolve(path):
        for prefix, resource, action in _ENDPOINT_RBAC_MAP:
            if path.startswith(prefix):
                return resource, action
        return None

    assert resolve("/api/v1/learning/state") == ("logs", "view")
    assert resolve("/api/v1/learning/history") == ("logs", "view")
    assert resolve("/api/v1/learning/proposals") == ("logs", "view")

    assert resolve("/api/v1/learning/recalibrate") == ("admin", "config")
    assert resolve("/api/v1/learning/restore") == ("admin", "config")
    assert resolve("/api/v1/learning/decisions/abc") == ("admin", "config")


def test_drift_metrics_declare_their_semantics():
    """OBS-2: every published metric states window, reset behaviour, source."""
    from aeam.monitoring import metrics as m

    for name in ("calibration_ece", "calibration_version", "calibration_samples"):
        assert hasattr(m, name), f"{name} is not published."

    module_source = Path(m.__file__).read_text(encoding="utf-8")
    ece_doc = module_source.split('calibration_ece: Gauge')[1].split('calibration_version: Gauge')[0]
    for required in ("Window:", "Source:", "Reset behaviour:", "What it measures:"):
        assert required in ece_doc, f"calibration_ece does not declare {required!r}"


def test_calibration_tables_are_covered_by_the_dr_drill():
    """Calibration state is the rollback ledger; a recovery that lost it
    would silently revert the platform to raw confidence."""
    import importlib.util
    import sys

    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("aeam_dr_drill_f2", root / "scripts" / "dr_drill.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    try:
        assert "calibration_models" in module.BACKED_UP_TABLES
        assert "learning_proposals" in module.BACKED_UP_TABLES
    finally:
        sys.modules.pop(spec.name, None)
