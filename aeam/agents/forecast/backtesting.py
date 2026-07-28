"""
aeam/agents/forecast/backtesting.py

Rolling-origin backtesting and holdout quality measurement for AEAM's
forecast models (Phase F1 — Detection, Statistical & Forecast Intelligence
Uplift).

Why this exists
---------------
Before F1, a forecast model was trained and immediately trusted. Nothing
measured whether it was any good, so a model that had silently become
useless — a changed data shape, a metric that stopped being seasonal, a
retrain on a corrupted window — served predictions with the same confidence
as a good one, and the deviations it flagged were noise wearing a forecast's
clothing.

This module measures a model against data it never saw. That measurement
does two jobs:

1. **TECH-6 re-validation.** A model artifact must earn the right to serve.
   :class:`~aeam.agents.forecast.forecast_agent.ForecastAgent` runs a
   holdout backtest before a newly trained model is allowed to answer, and
   refuses it when the error exceeds the configured ceiling.
2. **Quality tracking (OBS-2).** The holdout MAPE is recorded with the
   window it was measured over and the number of points it covered, so
   "the forecast is good" is a number with stated semantics rather than an
   assertion.

Design constraint: **model-agnostic**. This module imports no forecasting
library and knows nothing about Prophet. It takes a callable that trains on
a frame and returns predictions, which is what makes it testable in-process
against a trivial model — Prophet's fit time (seconds per call) would
otherwise make backtesting untestable in CI, and an untested test harness
is not a gate.

No I/O. No global state. Pure measurement over data the caller supplies.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

logger = logging.getLogger(__name__)

# Engine-owned defaults (ENG-6).
_DEFAULT_HOLDOUT: int = 7
# Below this many training rows a backtest measures the training-set size,
# not the model. Prophet's own floor is 30 rows (ForecastAgent's
# _MIN_TRAINING_ROWS); a fold must not be thinner than half of that.
_MIN_TRAIN_ROWS: int = 15
# MAPE is undefined when an actual is zero. Rather than dropping to
# infinity or silently skipping the point, such points are excluded and
# the exclusion is reported.
_ZERO_EPSILON: float = 1e-9


class Forecaster(Protocol):
    """The minimal shape a backtestable model must satisfy.

    Deliberately narrower than :class:`~aeam.agents.forecast.forecast_model.ForecastModel`:
    a backtest needs only "learn from this, then predict that many steps".
    Anything satisfying these two methods can be measured — including a
    seasonal-naive baseline, which is exactly what makes the candidate
    comparison in :func:`select_best_model` meaningful.
    """

    def train(self, df: Any) -> None:
        """Fit on a training frame with ``ds`` and ``y`` columns."""

    def predict(self, periods: int = 1) -> Any:
        """Return a frame with at least a ``yhat`` column, ``periods`` rows."""


@dataclass
class BacktestResult:
    """Outcome of a backtest, with the semantics of its own numbers (OBS-2)."""

    mape: float | None
    mae: float | None
    points_scored: int
    points_excluded: int
    folds: int
    holdout: int
    insufficient_data: str | None = None
    failed: str | None = None
    per_fold: list[dict[str, Any]] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        """True when a MAPE was actually produced.

        Callers must check this rather than testing ``mape is not None``
        plus remembering the failure fields — an unusable result carries a
        reason, and treating "no measurement" as "passed" is exactly the
        silent-degradation failure the harness exists to prevent.
        """
        return self.mape is not None and self.failed is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "mape": self.mape,
            "mae": self.mae,
            "points_scored": self.points_scored,
            "points_excluded": self.points_excluded,
            "folds": self.folds,
            "holdout": self.holdout,
            "insufficient_data": self.insufficient_data,
            "failed": self.failed,
            "per_fold": self.per_fold,
        }


def mean_absolute_percentage_error(
    actuals: list[float],
    predictions: list[float],
) -> tuple[float | None, int, int]:
    """Compute MAPE as a percentage, excluding points whose actual is zero.

    MAPE is undefined at ``actual == 0``. Returning infinity there would
    let one zero dominate an otherwise good score; silently dropping the
    point would overstate quality without saying so. Both problems are
    solved the same way: exclude, and report how many were excluded.

    Args:
        actuals:     Observed values.
        predictions: Predicted values, positionally aligned with ``actuals``.

    Returns:
        ``(mape_percent, points_scored, points_excluded)``. ``mape_percent``
        is ``None`` when every point was excluded.

    Raises:
        ValueError: If the two sequences differ in length — a misalignment
                    would silently score the wrong pairs.
    """
    if len(actuals) != len(predictions):
        raise ValueError(
            f"actuals ({len(actuals)}) and predictions ({len(predictions)}) "
            "must be the same length."
        )

    errors: list[float] = []
    excluded = 0
    for actual, predicted in zip(actuals, predictions):
        if abs(actual) < _ZERO_EPSILON:
            excluded += 1
            continue
        errors.append(abs((actual - predicted) / actual))

    if not errors:
        return None, 0, excluded

    return round((sum(errors) / len(errors)) * 100.0, 4), len(errors), excluded


def mean_absolute_error(actuals: list[float], predictions: list[float]) -> float | None:
    """Mean absolute error in the metric's own units.

    Reported alongside MAPE because a percentage hides scale: 20% MAPE on a
    metric that lives near zero and 20% on one in the millions are very
    different operational facts.
    """
    if len(actuals) != len(predictions):
        raise ValueError("actuals and predictions must be the same length.")
    if not actuals:
        return None
    return round(sum(abs(a - p) for a, p in zip(actuals, predictions)) / len(actuals), 6)


def backtest(
    df: Any,
    model_factory: Callable[[], Forecaster],
    holdout: int = _DEFAULT_HOLDOUT,
    folds: int = 1,
) -> BacktestResult:
    """Measure a model against data it was not trained on.

    Rolling-origin evaluation: for each fold, the model trains on
    everything up to a cut point and predicts the ``holdout`` observations
    that follow. More than one fold measures stability across origins
    rather than luck at one.

    Never raises (declared boundary, CODE-5). A model that fails to train
    or predict returns a result whose ``failed`` field names the failure —
    a forecast subsystem must not take an investigation down with it, and
    a crash during *validation* least of all.

    Args:
        df:            Prepared frame with ``ds`` and ``y`` columns, sorted
                       ascending. Not mutated.
        model_factory: Zero-argument callable returning a fresh, untrained
                       model. A factory rather than an instance because
                       every fold must start from an untrained model —
                       reusing one would leak the holdout into training.
        holdout:       Observations withheld per fold.
        folds:         Number of rolling origins, most recent first.

    Returns:
        A :class:`BacktestResult`. Check :attr:`BacktestResult.usable`
        before reading ``mape``.

    Raises:
        ValueError: If ``holdout`` < 1 or ``folds`` < 1. These are caller
                    programming errors, not data conditions, so they fail
                    loudly rather than returning a result object.
    """
    if holdout < 1:
        raise ValueError("holdout must be >= 1.")
    if folds < 1:
        raise ValueError("folds must be >= 1.")

    try:
        total = len(df)
    except Exception as exc:  # noqa: BLE001
        return BacktestResult(
            mape=None, mae=None, points_scored=0, points_excluded=0,
            folds=0, holdout=holdout,
            failed=f"Backtest input is not a sized frame: {exc}",
        )

    required = _MIN_TRAIN_ROWS + holdout * folds
    if total < required:
        return BacktestResult(
            mape=None, mae=None, points_scored=0, points_excluded=0,
            folds=0, holdout=holdout,
            insufficient_data=(
                f"{total} rows available; {required} required for {folds} fold(s) "
                f"of {holdout} with a {_MIN_TRAIN_ROWS}-row training minimum."
            ),
        )

    all_actuals: list[float] = []
    all_predictions: list[float] = []
    per_fold: list[dict[str, Any]] = []

    for fold in range(folds):
        # Fold 0 is the most recent origin; each subsequent fold steps one
        # holdout further back.
        end = total - (fold * holdout)
        cut = end - holdout
        train_df = df.iloc[:cut]
        holdout_df = df.iloc[cut:end]

        try:
            model = model_factory()
            model.train(train_df)
            forecast = model.predict(periods=holdout)
            predictions = [float(v) for v in list(forecast["yhat"])[:holdout]]
        except Exception as exc:  # noqa: BLE001
            logger.warning("backtest | fold %d failed: %s", fold, exc)
            return BacktestResult(
                mape=None, mae=None, points_scored=0, points_excluded=0,
                folds=fold, holdout=holdout,
                failed=f"Fold {fold} failed: {exc}",
                per_fold=per_fold,
            )

        actuals = [float(v) for v in list(holdout_df["y"])]
        if len(predictions) != len(actuals):
            return BacktestResult(
                mape=None, mae=None, points_scored=0, points_excluded=0,
                folds=fold, holdout=holdout,
                failed=(
                    f"Fold {fold} returned {len(predictions)} predictions for "
                    f"{len(actuals)} holdout points."
                ),
                per_fold=per_fold,
            )

        fold_mape, scored, excluded = mean_absolute_percentage_error(actuals, predictions)
        per_fold.append({
            "fold": fold,
            "train_rows": cut,
            "holdout_rows": len(actuals),
            "mape": fold_mape,
            "points_excluded": excluded,
        })
        all_actuals.extend(actuals)
        all_predictions.extend(predictions)

    mape, scored, excluded = mean_absolute_percentage_error(all_actuals, all_predictions)
    return BacktestResult(
        mape=mape,
        mae=mean_absolute_error(all_actuals, all_predictions),
        points_scored=scored,
        points_excluded=excluded,
        folds=folds,
        holdout=holdout,
        insufficient_data=(
            "Every holdout actual was zero; MAPE is undefined for this metric."
            if mape is None else None
        ),
        per_fold=per_fold,
    )


def select_best_model(
    df: Any,
    candidates: dict[str, Callable[[], Forecaster]],
    holdout: int = _DEFAULT_HOLDOUT,
    folds: int = 1,
) -> tuple[str | None, BacktestResult | None, dict[str, BacktestResult]]:
    """Backtest every candidate and return the lowest-MAPE one.

    Selection is on measured holdout error alone — no tie-break on model
    complexity, recency, or preference. A candidate whose backtest is
    unusable cannot win, because "we could not measure it" is never
    evidence that it is best.

    Args:
        df:         Prepared frame, as for :func:`backtest`.
        candidates: ``{name: factory}``. Evaluated in insertion order, and
                    ties resolve to the first — so callers that care about
                    a default should list it first.
        holdout:    Observations withheld per fold.
        folds:      Rolling origins per candidate.

    Returns:
        ``(winning_name, winning_result, all_results)``. The first two are
        ``None`` when no candidate produced a usable measurement; the third
        is always populated so the caller can report *why* each failed.
    """
    results: dict[str, BacktestResult] = {}
    best_name: str | None = None
    best_result: BacktestResult | None = None

    for name, factory in candidates.items():
        result = backtest(df, factory, holdout=holdout, folds=folds)
        results[name] = result
        if not result.usable:
            logger.info(
                "select_best_model | candidate=%s not usable | %s",
                name, result.failed or result.insufficient_data,
            )
            continue
        if best_result is None or result.mape < best_result.mape:
            best_name, best_result = name, result

    if best_name is not None:
        logger.info(
            "select_best_model | winner=%s | holdout_mape=%.4f%% | candidates=%d",
            best_name, best_result.mape, len(candidates),
        )
    else:
        logger.warning(
            "select_best_model | no candidate produced a usable backtest | candidates=%d",
            len(candidates),
        )

    return best_name, best_result, results


class SeasonalNaiveForecaster:
    """A seasonal-naive baseline: tomorrow looks like the same day last cycle.

    Present for two reasons. It is the honest floor any real forecaster must
    beat — a model that cannot outperform "last week's value" is not earning
    its complexity — and it is a fully-functional :class:`Forecaster` that
    needs no Prophet, which makes the harness itself testable.

    Args:
        period: Seasonal cycle length in observations.

    Raises:
        ValueError: If ``period`` < 1.
    """

    def __init__(self, period: int = 7) -> None:
        if period < 1:
            raise ValueError("period must be >= 1.")
        self._period = period
        self._tail: list[float] = []

    def train(self, df: Any) -> None:
        values = [float(v) for v in list(df["y"])]
        if not values:
            raise ValueError("SeasonalNaiveForecaster requires at least one observation.")
        self._tail = values[-self._period:]

    def predict(self, periods: int = 1) -> Any:
        if not self._tail:
            raise RuntimeError("SeasonalNaiveForecaster.predict called before train.")
        import pandas as pd  # imported lazily (CODE-6) — only needed to shape the return

        yhat = [self._tail[i % len(self._tail)] for i in range(periods)]
        return pd.DataFrame({"yhat": yhat})

    def __repr__(self) -> str:
        return f"SeasonalNaiveForecaster(period={self._period})"
