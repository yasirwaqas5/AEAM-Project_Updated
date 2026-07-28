"""
aeam/tests/test_phase_f1_detection.py

Phase F1 — Detection, Statistical & Forecast Intelligence Uplift.

Acceptance criteria under test:

1. **No finalized incident emits a placeholder-sourced root cause.** The
   placeholder path is *deleted*, not bypassed — asserted structurally
   (the method no longer exists, the string no longer appears in the
   orchestrator) as well as behaviourally.
2. **Detection precision/recall beats the recorded Phase-5 baseline by a
   stated margin**, on a labeled synthetic dataset, measured through the
   real MonitorAgent path.
3. **Forecast holdout MAPE is measured and gated.** The Phase-5 baseline
   is "never measured"; F1 produces a real number and refuses a model that
   fails its ceiling (TECH-6).
4. **Every existing detector's contract and default output is
   byte-identical when the new detectors are flag-off** (COMPAT-2).
5. **KPI Agent grounding**: it never fabricates a cause, reports honest
   insufficiency, and never raises.

Infrastructure: in-process only — real detectors, real MonitorAgent, real
Orchestrator, deterministic synthetic data (TEST-3). No Prophet, no
database, no network.
"""

from __future__ import annotations

import inspect
import json
import time
from pathlib import Path

import pytest

import aeam.agents.orchestrator.orchestrator as orchestrator_module
from aeam.agents.forecast.backtesting import (
    SeasonalNaiveForecaster,
    backtest,
    mean_absolute_percentage_error,
    select_best_model,
)
from aeam.agents.kpi.advanced_detectors import ChangepointDetector, SeasonalHybridDetector
from aeam.agents.kpi.kpi_agent import KPIAgent
from aeam.agents.kpi.rule_engine import RuleEngine
from aeam.agents.kpi.statistical_detector import StatisticalDetector
from aeam.agents.orchestrator.decision_engine import DecisionEngine
from aeam.agents.orchestrator.evaluation_engine import EvaluationEngine
from aeam.agents.orchestrator.orchestrator import Orchestrator
from aeam.config.settings import Settings
from aeam.core.event_bus import EventBus
from aeam.core.event_models import Event
from aeam.memory.long_term import LongTermMemory
from aeam.tests.detection_benchmark import (
    default_dataset,
    generate_labeled_series,
    score_configuration,
)

_BASELINES_PATH = Path(__file__).parent / "fixtures" / "detection_baselines.json"


@pytest.fixture(scope="module")
def baselines() -> dict:
    """The recorded Phase-5 baseline. A missing file fails loudly: silently
    skipping would let the gate disappear without anyone noticing."""
    assert _BASELINES_PATH.exists(), f"Detection baselines missing at {_BASELINES_PATH}"
    return json.loads(_BASELINES_PATH.read_text(encoding="utf-8"))


# ===========================================================================
# 1. The placeholder is DELETED, not bypassed
# ===========================================================================


def test_placeholder_method_no_longer_exists():
    """PHIL-1 is satisfied by removal. A placeholder left behind a flag is
    still a placeholder that can reach an operator."""
    assert not hasattr(Orchestrator, "_run_kpi_investigation_placeholder"), (
        "The KPI placeholder method still exists — F1 requires it be deleted."
    )
    assert hasattr(Orchestrator, "_run_kpi_investigation")


def test_orchestrator_source_contains_no_simulated_root_cause():
    """The literal the placeholder emitted must not survive anywhere in the
    investigation path — including in a disabled branch, which would be a
    bypass rather than a deletion."""
    source = inspect.getsource(orchestrator_module)
    assert "Simulated root cause" not in source
    assert 'root_cause_source", "placeholder"' not in source
    assert '"placeholder": True' not in source


def test_eng5_quarantine_is_retained_for_pre_f1_incidents():
    """COMPAT-1: incidents persisted before F1 still carry the marker, and
    the quarantine that governs them must still be in the code."""
    source = inspect.getsource(orchestrator_module)
    assert 'root_cause_source == "placeholder"' in source, (
        "The ENG-5 quarantine was removed along with the placeholder producer. "
        "Historical incidents carrying the marker would now be remembered."
    )


class _FakeLTM(LongTermMemory):
    """Records the finalized incident; serves a fixed metric history."""

    def __init__(self, history: list[float] | None = None) -> None:
        self.recorded: dict | None = None
        self._history = history or []

    def record_incident(self, data: dict) -> str:
        self.recorded = data
        return data.get("incident_id", "fake")

    def log_decision(self, incident_id, decision):
        return None

    def get_metric_history(self, metric_name: str, limit: int | None = None):
        values = self._history[-limit:] if limit else self._history
        return [{"timestamp": f"2026-01-{i + 1:02d}", "value": v} for i, v in enumerate(values)]


def _audit_summary(recorded: dict) -> dict:
    """Pull the audit_summary findings entry out of a persisted incident.

    ``root_cause_source`` is not a top-level incident column — it lives in
    the consolidated ``audit_summary`` finding (Phase E1). Reading it the
    way the API and the console read it keeps these assertions honest about
    the shape the platform actually persists.
    """
    findings = recorded["findings"]
    findings = json.loads(findings) if isinstance(findings, str) else findings
    for entry in findings:
        if entry.get("type") == "audit_summary":
            return entry
    raise AssertionError("No audit_summary finding in the persisted incident.")


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


def _build_orchestrator(ltm: _FakeLTM, settings: Settings | None = None) -> Orchestrator:
    settings = settings or _settings()
    return Orchestrator(
        event_bus=EventBus(),
        decision_engine=DecisionEngine(settings=settings),
        evaluation_engine=EvaluationEngine(settings=settings),
        long_term_memory=ltm,
        settings=settings,
    )


def _event(metric: str = "sales", current: float = 400.0, expected: float = 1000.0) -> Event:
    return Event(
        event_id="f1-evt",
        event_type="KPI_ANOMALY",
        metric=metric,
        severity="HIGH",
        current_value=current,
        expected_value=expected,
        detection_methods=["statistical:z_score(-4.10)"],
        timestamp="2026-07-01T00:00:00Z",
        metadata={
            "statistical": {
                "statistical_anomaly": True,
                "z_score": -4.1,
                "moving_avg": 1000.0,
                "percentile_low": 900.0,
                "percentile_high": 1100.0,
            }
        },
    )


def test_finalized_incident_never_carries_a_placeholder_source():
    ltm = _FakeLTM(history=[1000.0] * 30)
    orchestrator = _build_orchestrator(ltm)

    orchestrator.handle_event(_event())

    assert ltm.recorded is not None
    audit = _audit_summary(ltm.recorded)
    assert audit["root_cause_source"] != "placeholder"
    assert audit["root_cause_source"] == "kpi_analysis"
    assert "Simulated root cause" not in (ltm.recorded.get("root_cause") or "")


def test_kpi_agent_disabled_records_not_consulted_rather_than_nothing():
    """EXPL-3: 'not consulted' is its own state and must be visible in the
    record, not inferred from an absence."""
    ltm = _FakeLTM(history=[1000.0] * 30)
    orchestrator = _build_orchestrator(ltm, _settings(KPI_AGENT_ENABLED=False))

    orchestrator.handle_event(_event())

    findings = json.loads(ltm.recorded["findings"]) if isinstance(ltm.recorded["findings"], str) else ltm.recorded["findings"]
    kpi_findings = [f for f in findings if f.get("type") == "kpi_analysis"]
    assert kpi_findings, "No kpi_analysis finding recorded when the agent is disabled."
    assert "not_consulted" in kpi_findings[0]["data"]
    assert _audit_summary(ltm.recorded)["root_cause_source"] != "kpi_analysis"


# ===========================================================================
# 2. KPI Agent grounding (AI-2, EXPL-1)
# ===========================================================================


def test_kpi_agent_characterisation_is_traceable_to_real_numbers():
    agent = KPIAgent(long_term_memory=_FakeLTM(history=[1000.0 + i for i in range(40)]))

    result = agent.analyze(
        metric="sales",
        current_value=400.0,
        expected_value=1000.0,
        event_metadata=_event().metadata,
    )

    assert result["root_cause_source"] == "kpi_analysis"
    assert result["deviation"]["percent"] == pytest.approx(-60.0, abs=0.01)
    assert result["deviation"]["direction"] == "below"
    assert "statistical" in result["detectors_fired"]
    # Every number quoted in the sentence appears in the structured result.
    assert "60.00%" in result["root_cause"]
    assert "below" in result["root_cause"]
    assert "statistical" in result["root_cause"]


def test_kpi_agent_never_asserts_a_causal_explanation():
    """The characterisation states WHAT changed. Asserting WHY from a
    z-score is the fabricated traceability Article X forbids."""
    agent = KPIAgent(long_term_memory=_FakeLTM(history=[1000.0] * 40))
    result = agent.analyze(
        metric="sales", current_value=400.0, expected_value=1000.0,
        event_metadata=_event().metadata,
    )

    statement = result["root_cause"].lower()
    for causal_word in ("because", "caused by", "due to", "root cause is", "resulted from"):
        assert causal_word not in statement, (
            f"The KPI Agent asserted a cause ({causal_word!r}): {result['root_cause']!r}"
        )


def test_kpi_agent_reports_insufficiency_rather_than_inventing_analysis():
    """No baseline, no history, no fired detector — the honest output is a
    stated reason and no root cause at all."""
    agent = KPIAgent(long_term_memory=None)

    result = agent.analyze(metric="unknown", current_value=5.0, expected_value=None, event_metadata={})

    assert result["root_cause"] is None
    assert result["root_cause_source"] is None
    assert result["confidence"] == 0.0
    assert result["insufficient_data"]
    assert "nothing measured" in result["insufficient_data"]


def test_kpi_agent_falls_back_to_history_when_event_has_no_expected_value():
    agent = KPIAgent(long_term_memory=_FakeLTM(history=[100.0] * 30))

    result = agent.analyze(metric="sales", current_value=40.0, expected_value=None)

    assert result["baseline_source"] == "history"
    assert result["expected_value"] == 100.0
    assert result["deviation"]["percent"] == pytest.approx(-60.0, abs=0.01)


def test_kpi_agent_distinguishes_a_spike_from_a_sustained_shift():
    sustained = KPIAgent(long_term_memory=_FakeLTM(history=[100.0] * 20 + [40.0] * 6))
    spike = KPIAgent(long_term_memory=_FakeLTM(history=[100.0] * 26))

    sustained_result = sustained.analyze("sales", 40.0, expected_value=100.0)
    spike_result = spike.analyze("sales", 40.0, expected_value=100.0)

    assert sustained_result["persistence"]["sustained"] is True
    assert sustained_result["persistence"]["consecutive_observations"] >= 6
    assert spike_result["persistence"]["consecutive_observations"] == 1
    assert spike_result["persistence"]["sustained"] is False


def test_kpi_agent_never_raises_on_a_broken_memory():
    class _Broken:
        def get_metric_history(self, metric_name, limit=None):
            raise RuntimeError("database on fire")

    agent = KPIAgent(long_term_memory=_Broken())
    result = agent.analyze("sales", 40.0, expected_value=100.0)

    assert result["history_points_used"] == 0
    assert result["deviation"] is not None  # the event's own baseline still works


def test_kpi_agent_reports_trend_direction_from_real_history():
    rising = KPIAgent(long_term_memory=_FakeLTM(history=[100.0 + 5 * i for i in range(20)]))
    result = rising.analyze("sales", 250.0, expected_value=195.0)

    assert result["trend"]["direction"] == "rising"
    assert result["trend"]["slope"] > 0


# ===========================================================================
# 3. Flag-off byte identity (COMPAT-2)
# ===========================================================================


def test_statistical_detector_output_is_unchanged():
    """The Phase-5 detector's contract is frozen. Its keys and values are
    asserted literally: a new key would break every persisted consumer."""
    detector = StatisticalDetector(window_size=7)
    result = detector.detect(120.0, [100.0, 101.0, 99.0, 100.0, 102.0, 98.0, 100.0])

    assert set(result) == {
        "moving_avg", "z_score", "percentile_low", "percentile_high", "statistical_anomaly",
    }


def test_monitor_agent_constructs_no_f1_detectors_by_default():
    from aeam.tests.detection_benchmark import build_agent

    agent = build_agent(changepoint=False, seasonal=False)
    assert agent._changepoint is None
    assert agent._seasonal is None


def test_flag_off_event_metadata_has_no_f1_keys():
    from aeam.tests.detection_benchmark import build_agent, generate_labeled_series

    series = generate_labeled_series("sales", length=120)
    agent = build_agent(changepoint=False, seasonal=False)

    index = next(i for i in range(40, len(series)) if series.labels[i])
    event = agent.process_kpi(
        metric_name="sales",
        current=series.values[index],
        previous=series.values[index - 1],
        history=series.values[:index],
    )

    assert event is not None
    assert set(event.metadata) == {"rule", "statistical", "forecast"}, (
        f"Flag-off metadata gained keys: {sorted(event.metadata)}"
    )
    assert not any("changepoint" in s or "seasonal" in s for s in event.detection_methods)


def test_settings_default_to_phase5_detection():
    settings = _settings()
    assert settings.DETECTION_CHANGEPOINT_ENABLED is False
    assert settings.DETECTION_SEASONAL_HYBRID_ENABLED is False
    assert settings.FORECAST_BACKTEST_ENABLED is False
    assert settings.FORECAST_MODEL_SELECTION_ENABLED is False
    # The one deliberate exception — see the F1 settings block's comment.
    assert settings.KPI_AGENT_ENABLED is True


def test_flags_require_a_real_boolean_true():
    """A Settings-shaped object whose attributes are auto-created must not
    silently enable detection changes."""
    from aeam.agents.monitor.monitor_agent import _flag_enabled

    class _Auto:
        def __getattr__(self, name):
            return object()

    assert _flag_enabled(_Auto(), "DETECTION_CHANGEPOINT_ENABLED") is False
    assert _flag_enabled(object(), "DETECTION_CHANGEPOINT_ENABLED") is False


# ===========================================================================
# 4. The new detectors themselves
# ===========================================================================


def test_changepoint_finds_a_sustained_level_shift():
    result = ChangepointDetector().detect(60.0, [100, 99, 101, 100, 102, 61, 59, 60])

    assert result["changepoint_detected"] is True
    assert result["shift_magnitude"] < 0
    assert result["before_level"] > result["after_level"]


def test_changepoint_stays_quiet_on_a_stable_series():
    result = ChangepointDetector().detect(100.0, [100, 99, 101, 100, 102, 99, 101, 100])
    assert result["changepoint_detected"] is False


def test_changepoint_distinguishes_no_signal_from_no_dispersion():
    """EXPL-3: a flat healthy metric and an unmeasurable one are different
    states and must not share a message."""
    stable = ChangepointDetector().detect(100.0, [100, 99, 101, 100, 102, 99, 101, 100])
    flat = ChangepointDetector().detect(5.0, [5, 5, 5, 5, 5, 5, 5, 5])

    assert stable["insufficient_data"] is None
    assert stable["shift_score"] is not None
    assert flat["insufficient_data"] is not None


def _noisy_weekly(cycles: int = 6, seed: int = 5) -> list[float]:
    """A realistic weekly-seasonal series. Deliberately noisy: a perfectly
    repeating pattern has such a large MAD that no split scores above
    threshold, which would make the assertions below pass without
    exercising anything."""
    import random

    rng = random.Random(seed)
    return [
        value + rng.uniform(-4, 4)
        for _ in range(cycles)
        for value in (100, 102, 98, 101, 99, 40, 42)
    ]


def test_changepoint_deseasonalizing_suppresses_the_spurious_seasonal_shift():
    """Scanning raw seasonal data, any split straddling a weekend compares a
    weekday-heavy segment against a weekend-heavy one and scores a large,
    entirely spurious shift.

    Deseasonalizing does not eliminate the artefact — the residual cycle
    still contributes — but it roughly halves the spurious score, which on
    the F1 benchmark dataset cut this detector's false positives from 177
    to 140 and turned its precision from BELOW the Phase-5 baseline
    (0.2063) to above it (0.2473). The assertion is that measured effect,
    not a binary flip it does not deliver.
    """
    weekly = _noisy_weekly()

    naive = ChangepointDetector(period=None).detect(100.0, weekly)
    aware = ChangepointDetector(period=7).detect(100.0, weekly)

    assert naive["shift_score"] is not None and aware["shift_score"] is not None
    assert naive["changepoint_detected"] is True, "Fixture no longer exercises the artefact."
    assert aware["shift_score"] < naive["shift_score"] * 0.7, (
        f"Deseasonalizing barely moved the spurious score: "
        f"raw={naive['shift_score']}, deseasonalized={aware['shift_score']}."
    )


def test_changepoint_still_finds_a_real_shift_in_seasonal_data():
    """The suppression above must not cost the detector its actual job."""
    weekly = _noisy_weekly()
    shifted = weekly + [v * 0.5 for v in weekly[:14]]

    result = ChangepointDetector(period=7).detect(20.0, shifted)

    assert result["changepoint_detected"] is True
    # A genuine halving must score far above the residual seasonal artefact,
    # or the two are indistinguishable in practice.
    assert result["shift_score"] > 10.0


def test_seasonal_detector_accepts_a_normal_seasonal_low():
    """The Phase-5 blind spot: a flat rolling mean calls every weekend an
    anomaly."""
    import random

    rng = random.Random(11)
    history = []
    for _ in range(5):
        for value in (100, 102, 98, 101, 99, 40, 42):
            history.append(value + rng.uniform(-2, 2))
    history = history[:-2]  # next phase is a weekend

    detector = SeasonalHybridDetector(period=7)
    normal_weekend = detector.detect(41.0, history)
    abnormal_weekend = detector.detect(5.0, history)

    assert normal_weekend["seasonal_anomaly"] is False
    assert abnormal_weekend["seasonal_anomaly"] is True
    assert normal_weekend["seasonal_expected"] < 60  # a weekend-scale expectation


def test_seasonal_detector_phase_alignment_survives_partial_cycles():
    """Trimming to whole cycles happens at the START of the series, so the
    window's index 0 is not phase 0. Getting this wrong compares Saturday
    against Wednesday's baseline and reports a confident anomaly on a
    perfectly normal day."""
    import random

    rng = random.Random(3)
    # Every phase has its own level, including among weekdays. The value
    # that is "normal" for the next observation is therefore this pattern's
    # own entry for that phase — asserting a single flat weekday number
    # would be testing against data the fixture does not contain.
    base = [100.0, 102.0, 98.0, 101.0, 99.0, 40.0, 42.0]
    detector = SeasonalHybridDetector(period=7)

    for trim in range(7):
        history = []
        for _ in range(6):
            for value in base:
                history.append(value + rng.uniform(-1, 1))
        history = history[: len(history) - trim] if trim else history

        phase = len(history) % 7
        normal_for_this_phase = base[phase]

        result = detector.detect(normal_for_this_phase, history)

        assert result["seasonal_anomaly"] is False, (
            f"Phase {phase}: the value that is normal for this phase "
            f"({normal_for_this_phase}) was flagged. "
            f"expected={result['seasonal_expected']}, score={result['residual_score']}"
        )
        # The baseline must be the one for THIS phase, which is the whole
        # point: a misaligned window would return a weekday expectation on
        # a weekend and still not flag it, passing for the wrong reason.
        assert result["seasonal_expected"] == pytest.approx(normal_for_this_phase, abs=1.5), (
            f"Phase {phase}: baseline {result['seasonal_expected']} is not this "
            f"phase's level ({normal_for_this_phase}) — the window is misaligned."
        )


def test_seasonal_detector_reports_insufficient_cycles_honestly():
    result = SeasonalHybridDetector(period=7, min_cycles=3).detect(1.0, [1, 2, 3])
    assert result["seasonal_anomaly"] is False
    assert "3 complete cycles" in result["insufficient_data"]


# ===========================================================================
# 5. The benchmark — precision/recall beat the recorded baseline
# ===========================================================================


@pytest.fixture(scope="module")
def dataset():
    return default_dataset()


def test_benchmark_dataset_is_deterministic_and_labeled(dataset, baselines):
    declared = baselines["dataset"]
    assert sum(sum(s.labels) for s in dataset) == declared["labeled_anomalies"]
    assert len(dataset) == len(declared["series"])
    # Regenerating must reproduce the same data, or a score change cannot be
    # attributed to a code change.
    again = generate_labeled_series("sales", length=120)
    assert again.values == dataset[0].values


def test_phase5_baseline_still_measures_as_recorded(dataset, baselines):
    """Guards the comparison itself. If the baseline configuration drifted,
    every 'improvement' below would be measured against the wrong number."""
    recorded = baselines["phase5_baseline"]
    score = score_configuration(dataset)

    assert score.precision == pytest.approx(recorded["precision"], abs=0.02), (
        f"Phase-5 baseline precision moved: recorded {recorded['precision']}, "
        f"measured {score.precision}. Re-record deliberately or fix the regression."
    )
    assert score.recall == pytest.approx(recorded["recall"], abs=0.02)
    assert score.true_positives == recorded["true_positives"]


def test_f1_detection_beats_the_phase5_baseline_by_the_stated_margin(dataset, baselines):
    recorded = baselines["phase5_baseline"]
    margins = baselines["required_margins"]

    score = score_configuration(dataset, changepoint=True, seasonal=True)

    precision_gain = (score.precision - recorded["precision"]) / recorded["precision"]
    recall_gain = (score.recall - recorded["recall"]) / recorded["recall"]
    f1_gain = (score.f1 - recorded["f1"]) / recorded["f1"]

    assert precision_gain >= margins["min_precision_relative_gain"], (
        f"Precision gain {precision_gain:.4f} is below the stated "
        f"{margins['min_precision_relative_gain']} margin "
        f"({recorded['precision']} -> {score.precision})."
    )
    assert recall_gain >= margins["min_recall_relative_gain"], (
        f"Recall gain {recall_gain:.4f} is below the stated "
        f"{margins['min_recall_relative_gain']} margin "
        f"({recorded['recall']} -> {score.recall})."
    )
    assert f1_gain >= margins["min_f1_relative_gain"], (
        f"F1 gain {f1_gain:.4f} is below the stated "
        f"{margins['min_f1_relative_gain']} margin ({recorded['f1']} -> {score.f1})."
    )


def test_phase5_misses_are_exactly_the_blind_spots_f1_closes(dataset, baselines):
    """The improvement must come from the classes it claims to come from.

    A change that raised recall by catching more spikes while still missing
    every sustained shift would pass the aggregate margin test and deliver
    none of F1's actual purpose.
    """
    from collections import Counter

    from aeam.tests.detection_benchmark import WARMUP, build_agent

    def misses_by_class(**flags) -> dict[str, int]:
        agent = build_agent(**flags)
        counts: Counter = Counter()
        for series in dataset:
            for index in range(WARMUP, len(series)):
                if not series.labels[index]:
                    continue
                event = agent.process_kpi(
                    series.name, series.values[index],
                    series.values[index - 1], series.values[:index],
                )
                if event is None:
                    counts[series.anomaly_kinds.get(index, "?")] += 1
        return dict(counts)

    baseline_misses = misses_by_class()
    f1_misses = misses_by_class(changepoint=True, seasonal=True)

    recorded = baselines["f1_measured"]["phase5_misses_by_class"]
    assert baseline_misses == {k: v for k, v in recorded.items() if v}, (
        f"Phase-5 miss profile changed: measured {baseline_misses}, recorded {recorded}."
    )

    assert not f1_misses, f"F1 still misses labeled anomalies: {f1_misses}"
    assert baseline_misses.get("spike", 0) == 0, (
        "Phase 5 missed a spike — the fixture no longer isolates the blind spots."
    )


def test_f1_detection_misses_nothing_the_baseline_caught(dataset):
    """An improvement bought by trading away existing capability is not an
    improvement."""
    baseline = score_configuration(dataset)
    improved = score_configuration(dataset, changepoint=True, seasonal=True)

    assert improved.true_positives >= baseline.true_positives
    assert improved.false_negatives <= baseline.false_negatives


def test_each_f1_detector_independently_improves_recall(dataset, baselines):
    """Both detectors must earn their place on their own, so a future change
    that quietly breaks one cannot hide behind the other."""
    baseline = score_configuration(dataset)
    changepoint_only = score_configuration(dataset, changepoint=True)
    seasonal_only = score_configuration(dataset, seasonal=True)

    assert changepoint_only.recall > baseline.recall
    assert seasonal_only.recall > baseline.recall
    assert changepoint_only.precision >= baseline.precision, (
        "The changepoint detector now costs precision — check that it is still "
        "deseasonalizing before scanning."
    )


# ===========================================================================
# 6. Forecast backtesting (TECH-6)
# ===========================================================================


def _weekly_frame(cycles: int = 8, noise: float = 0.0):
    import pandas as pd

    base = [100.0, 102.0, 98.0, 101.0, 99.0, 40.0, 42.0]
    values = []
    for c in range(cycles):
        for i, v in enumerate(base):
            values.append(v + (noise * ((c + i) % 3 - 1)))
    return pd.DataFrame({
        "ds": pd.date_range("2026-01-01", periods=len(values), freq="D"),
        "y": values,
    })


def test_mape_excludes_zero_actuals_and_reports_the_exclusion():
    """MAPE is undefined at zero. Dropping the point silently would
    overstate quality; returning infinity would let one zero dominate."""
    mape, scored, excluded = mean_absolute_percentage_error([100.0, 0.0, 200.0], [110.0, 5.0, 180.0])

    assert excluded == 1
    assert scored == 2
    assert mape == pytest.approx(10.0, abs=0.01)


def test_mape_is_none_when_every_actual_is_zero():
    mape, scored, excluded = mean_absolute_percentage_error([0.0, 0.0], [1.0, 2.0])
    assert mape is None
    assert scored == 0
    assert excluded == 2


def test_backtest_measures_a_seasonal_naive_model_on_a_clean_series(baselines):
    result = backtest(_weekly_frame(), lambda: SeasonalNaiveForecaster(period=7), holdout=7)

    assert result.usable
    ceiling = baselines["forecast_backtest"]["max_seasonal_naive_mape_on_clean_weekly_series"]
    assert result.mape <= ceiling, (
        f"Seasonal-naive MAPE {result.mape}% exceeds {ceiling}% on a clean weekly series."
    )
    assert result.points_scored == 7
    assert result.folds == 1


def test_backtest_reports_insufficient_data_rather_than_a_number():
    import pandas as pd

    tiny = pd.DataFrame({"ds": pd.date_range("2026-01-01", periods=5), "y": [1.0] * 5})
    result = backtest(tiny, lambda: SeasonalNaiveForecaster(period=7), holdout=7)

    assert not result.usable
    assert result.mape is None
    assert "rows available" in result.insufficient_data


def test_backtest_never_raises_when_a_model_fails():
    class _Broken:
        def train(self, df):
            raise RuntimeError("training exploded")

        def predict(self, periods=1):
            raise RuntimeError("never reached")

    result = backtest(_weekly_frame(), _Broken, holdout=7)

    assert not result.usable
    assert "training exploded" in result.failed


def test_backtest_folds_hold_out_data_the_model_never_saw():
    """A model that memorised the holdout would score ~0. The seasonal-naive
    model cannot cheat, so a nonzero error proves the split is real."""
    result = backtest(_weekly_frame(noise=3.0), lambda: SeasonalNaiveForecaster(period=7), holdout=7, folds=2)

    assert result.usable
    assert result.folds == 2
    assert result.mape > 0.0
    assert len(result.per_fold) == 2


def test_select_best_model_picks_the_lowest_mape():
    class _Terrible:
        def train(self, df):
            self._n = 0

        def predict(self, periods=1):
            import pandas as pd
            return pd.DataFrame({"yhat": [0.5] * periods})

    winner, result, all_results = select_best_model(
        _weekly_frame(),
        {"terrible": _Terrible, "seasonal_naive": lambda: SeasonalNaiveForecaster(period=7)},
        holdout=7,
    )

    assert winner == "seasonal_naive"
    assert result.mape < all_results["terrible"].mape


def test_select_best_model_cannot_be_won_by_an_unmeasurable_candidate():
    """'We could not measure it' is never evidence that a model is best."""
    class _Broken:
        def train(self, df):
            raise RuntimeError("nope")

        def predict(self, periods=1):
            raise RuntimeError("nope")

    winner, result, all_results = select_best_model(
        _weekly_frame(),
        {"broken": _Broken, "seasonal_naive": lambda: SeasonalNaiveForecaster(period=7)},
        holdout=7,
    )

    assert winner == "seasonal_naive"
    assert not all_results["broken"].usable


def test_forecast_holdout_mape_improves_on_the_unmeasured_phase5_baseline(baselines):
    """F1 acceptance: 'forecast holdout MAPE improves against the same
    baseline'. The Phase-5 baseline is the honest null — no harness existed,
    so quality was never measured at all. What is asserted is that a real
    number now exists and that a better model measures lower than a worse
    one; claiming a numeric improvement over a number nobody recorded would
    be inventing a comparison."""
    assert baselines["forecast_backtest"]["phase5_baseline_mape"] is None

    good = backtest(_weekly_frame(), lambda: SeasonalNaiveForecaster(period=7), holdout=7)

    class _Constant:
        def train(self, df):
            self._mean = float(sum(df["y"]) / len(df["y"]))

        def predict(self, periods=1):
            import pandas as pd
            return pd.DataFrame({"yhat": [self._mean] * periods})

    poor = backtest(_weekly_frame(), _Constant, holdout=7)

    assert good.usable and poor.usable
    assert good.mape < poor.mape, (
        "The harness cannot tell a seasonal model from a flat mean on seasonal "
        "data; it is not measuring what it claims to."
    )


# ===========================================================================
# 7. TECH-6 gate in ForecastAgent
# ===========================================================================


class _StubPipeline:
    def prepare(self, df):
        return df


def _forecast_agent(settings, history_frame, db=None):
    from aeam.agents.forecast.forecast_agent import ForecastAgent

    # The real LongTermMemory contract is a list of {timestamp, value}
    # dicts, which ForecastAgent._fetch_historical converts to a frame.
    # Returning a DataFrame here would take the agent's exception path
    # (`if not rows` is ambiguous on a DataFrame) and every assertion below
    # would pass or fail for the wrong reason.
    rows = [
        {"timestamp": ts, "value": float(y)}
        for ts, y in zip(history_frame["ds"], history_frame["y"])
    ]

    class _LTM:
        def get_metric_history(self, metric_name, limit=None):
            return rows

    agent = ForecastAgent(
        long_term_memory=_LTM(),
        data_pipeline=_StubPipeline(),
        settings=settings,
        model_dir="/tmp/aeam-f1-models",
        database_client=db,
    )
    return agent


def test_forecast_backtest_disabled_by_default_leaves_the_phase5_path(monkeypatch):
    """COMPAT-2: with the flag off, no backtest runs at all."""
    from aeam.agents.forecast import forecast_agent as fa

    called = {"selected": False}

    def _spy(*args, **kwargs):
        called["selected"] = True
        raise AssertionError("select_best_model must not run with the flag off.")

    monkeypatch.setattr(fa, "select_best_model", _spy)

    agent = _forecast_agent(_settings(), _weekly_frame())
    monkeypatch.setattr(fa.ForecastModel, "train", lambda self, df: None)
    monkeypatch.setattr(fa.ForecastModel, "save_model", lambda self, path: None)

    agent.load_or_train("sales")
    assert called["selected"] is False


def test_forecast_model_refused_when_holdout_mape_exceeds_the_ceiling(monkeypatch):
    """TECH-6: an unvalidated model does not get to serve."""
    from aeam.agents.forecast import forecast_agent as fa

    monkeypatch.setattr(fa.ForecastModel, "train", lambda self, df: None)
    monkeypatch.setattr(fa.ForecastModel, "save_model", lambda self, path: None)
    monkeypatch.setattr(
        fa, "select_best_model",
        lambda df, candidates, holdout=7, folds=1: (
            "prophet",
            fa.__dict__["select_best_model"].__globals__["SeasonalNaiveForecaster"] and _fake_result(80.0),
            {},
        ),
    )

    settings = _settings(FORECAST_BACKTEST_ENABLED=True, FORECAST_MAX_HOLDOUT_MAPE=10.0)
    agent = _forecast_agent(settings, _weekly_frame())

    result = agent.load_or_train("sales")

    assert isinstance(result, dict)
    assert result["insufficient_data"] is True
    assert result["backtest"]["refused"] is True
    assert "exceeds" in result["backtest"]["reason"]


def test_forecast_model_serves_when_holdout_mape_is_within_the_ceiling(monkeypatch):
    from aeam.agents.forecast import forecast_agent as fa

    monkeypatch.setattr(fa.ForecastModel, "train", lambda self, df: None)
    monkeypatch.setattr(fa.ForecastModel, "save_model", lambda self, path: None)
    monkeypatch.setattr(
        fa, "select_best_model",
        lambda df, candidates, holdout=7, folds=1: ("prophet", _fake_result(2.5), {}),
    )

    settings = _settings(FORECAST_BACKTEST_ENABLED=True, FORECAST_MAX_HOLDOUT_MAPE=10.0)
    agent = _forecast_agent(settings, _weekly_frame())

    result = agent.load_or_train("sales")
    assert isinstance(result, fa.ForecastModel)


def test_unmeasurable_backtest_serves_rather_than_refusing(monkeypatch):
    """Refusing to serve because a metric has too little history would be a
    regression against Phase 5, which served happily without measuring."""
    from aeam.agents.forecast import forecast_agent as fa
    from aeam.agents.forecast.backtesting import BacktestResult

    monkeypatch.setattr(fa.ForecastModel, "train", lambda self, df: None)
    monkeypatch.setattr(fa.ForecastModel, "save_model", lambda self, path: None)
    monkeypatch.setattr(
        fa, "select_best_model",
        lambda df, candidates, holdout=7, folds=1: (
            None,
            None,
            {"prophet": BacktestResult(
                mape=None, mae=None, points_scored=0, points_excluded=0,
                folds=0, holdout=7, insufficient_data="not enough rows",
            )},
        ),
    )

    settings = _settings(FORECAST_BACKTEST_ENABLED=True, FORECAST_MAX_HOLDOUT_MAPE=10.0)
    agent = _forecast_agent(settings, _weekly_frame())

    result = agent.load_or_train("sales")
    assert isinstance(result, fa.ForecastModel), "An unmeasured model must still serve."


def test_backtest_record_persists_when_a_database_is_wired(monkeypatch):
    from aeam.agents.forecast import forecast_agent as fa

    monkeypatch.setattr(fa.ForecastModel, "train", lambda self, df: None)
    monkeypatch.setattr(fa.ForecastModel, "save_model", lambda self, path: None)
    monkeypatch.setattr(
        fa, "select_best_model",
        lambda df, candidates, holdout=7, folds=1: ("prophet", _fake_result(3.0), {}),
    )

    class _DB:
        def __init__(self):
            self.rows = []

        def insert(self, table, data, returning_column="incident_id"):
            self.rows.append((table, data))
            return "row-1"

    db = _DB()
    settings = _settings(FORECAST_BACKTEST_ENABLED=True)
    agent = _forecast_agent(settings, _weekly_frame(), db=db)

    agent.load_or_train("sales")

    assert db.rows, "No backtest record was written."
    table, data = db.rows[0]
    assert table == "forecast_backtests"
    assert data["metric"] == "sales"
    assert data["holdout_mape"] == 3.0
    assert data["refused"] is False


def test_refused_model_is_still_recorded(monkeypatch):
    """A refusal is the most important measurement to keep: it is the answer
    to "why did this metric stop forecasting?", and what the
    idx_forecast_backtests_refused index exists to serve.

    Regression for a defect the live Prophet run surfaced — the refusal path
    returned before the record was written, so the refused row never existed
    and that index queried an empty set forever.
    """
    from aeam.agents.forecast import forecast_agent as fa

    monkeypatch.setattr(fa.ForecastModel, "train", lambda self, df: None)
    monkeypatch.setattr(fa.ForecastModel, "save_model", lambda self, path: None)
    monkeypatch.setattr(
        fa, "select_best_model",
        lambda df, candidates, holdout=7, folds=1: ("prophet", _fake_result(80.0), {}),
    )

    class _DB:
        def __init__(self):
            self.rows = []

        def insert(self, table, data, returning_column="incident_id"):
            self.rows.append((table, data))
            return "row-1"

    db = _DB()
    settings = _settings(FORECAST_BACKTEST_ENABLED=True, FORECAST_MAX_HOLDOUT_MAPE=10.0)
    agent = _forecast_agent(settings, _weekly_frame(), db=db)

    result = agent.load_or_train("sales")

    assert isinstance(result, dict) and result["insufficient_data"] is True
    assert db.rows, "The refusal was not recorded."
    table, data = db.rows[0]
    assert table == "forecast_backtests"
    assert data["refused"] is True
    assert data["holdout_mape"] == 80.0
    assert "exceeds" in data["reason"]


def test_backtest_record_failure_never_breaks_the_forecast_path(monkeypatch):
    from aeam.agents.forecast import forecast_agent as fa

    monkeypatch.setattr(fa.ForecastModel, "train", lambda self, df: None)
    monkeypatch.setattr(fa.ForecastModel, "save_model", lambda self, path: None)
    monkeypatch.setattr(
        fa, "select_best_model",
        lambda df, candidates, holdout=7, folds=1: ("prophet", _fake_result(3.0), {}),
    )

    class _BrokenDB:
        def insert(self, table, data, returning_column="incident_id"):
            raise RuntimeError("table missing")

    settings = _settings(FORECAST_BACKTEST_ENABLED=True)
    agent = _forecast_agent(settings, _weekly_frame(), db=_BrokenDB())

    result = agent.load_or_train("sales")
    assert isinstance(result, fa.ForecastModel)


def _fake_result(mape: float):
    from aeam.agents.forecast.backtesting import BacktestResult

    return BacktestResult(
        mape=mape, mae=mape, points_scored=7, points_excluded=0, folds=1, holdout=7,
    )


# ===========================================================================
# 8. End-to-end: an investigation produces a grounded, remembered analysis
# ===========================================================================


def test_investigation_end_to_end_produces_a_grounded_kpi_finding():
    ltm = _FakeLTM(history=[1000.0 + (i % 5) for i in range(40)])
    orchestrator = _build_orchestrator(ltm)

    orchestrator.handle_event(_event(current=400.0, expected=1000.0))

    recorded = ltm.recorded
    assert recorded is not None

    findings = recorded["findings"]
    findings = json.loads(findings) if isinstance(findings, str) else findings
    kpi = [f for f in findings if f.get("type") == "kpi_analysis"]

    assert kpi, "No kpi_analysis finding in the persisted record."
    data = kpi[0]["data"]
    assert data["history_points_used"] == 40
    assert data["detectors_fired"] == ["statistical"]
    assert data["deviation"]["percent"] == pytest.approx(-60.0, abs=0.01)
    assert data["analysis_failed"] is None
    assert _audit_summary(recorded)["root_cause_source"] == "kpi_analysis"
    assert float(recorded["confidence"]) > 0.0


def test_kpi_agent_does_not_override_a_grounded_rag_root_cause():
    """AGENT-5: the KPI Agent is advisory. A chunk-cited causal explanation
    must always win over a statistical characterisation."""
    class _RAG:
        def investigate(self, event, memory):
            findings = {
                "possible_causes": [
                    {"cause": "checkout service deploy at 14:02", "chunk_id": "c-9", "confidence": 0.92},
                ],
                "overall_confidence": 0.92,
                "requires_human_review": False,
                "retrieved_count": 1,
                "validation_passed": True,
                "raw_llm_response": "{}",
            }
            return {
                "findings": findings,
                "confidence": 0.92,
                "memory_updates": {"rag_findings": findings, "hypotheses": [], "confidence": 0.92},
            }

    ltm = _FakeLTM(history=[1000.0] * 40)
    settings = _settings()
    orchestrator = Orchestrator(
        event_bus=EventBus(),
        decision_engine=DecisionEngine(settings=settings),
        evaluation_engine=EvaluationEngine(settings=settings),
        long_term_memory=ltm,
        settings=settings,
        rag_agent=_RAG(),
    )

    orchestrator.handle_event(_event())

    assert _audit_summary(ltm.recorded)["root_cause_source"] == "rag"
    assert "checkout service deploy" in ltm.recorded["root_cause"]
