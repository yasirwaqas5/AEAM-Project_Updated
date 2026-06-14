
"""
aeam/agents/forecast/forecast_agent.py

Forecast Agent for time-series prediction and deviation detection in AEAM.

Manages per-metric Prophet model lifecycle: loads an existing model from
``models/forecasting/`` if it is fresh (< 7 days old), otherwise fetches
historical data from LongTermMemory, preprocesses it, trains a new model,
and saves it. Exposes a single ``analyze`` method that returns a deviation
analysis dict for consumption by the Orchestrator.

Architecture constraints (all enforced):
- Does NOT create Event objects.
- Does NOT call LLM.
- Does NOT call Action Agent.
- Does NOT retrain in the background.
- Models stored under ``models/forecasting/``.
- All dependencies injected.
- Training is explicit only.
"""

from __future__ import annotations

import logging
from aeam.monitoring.logging_config import get_logger
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import pandas as pd

from aeam.agents.forecast.forecast_model import ForecastModel
from aeam.config.settings import Settings
from aeam.pipelines.forecast_data_pipeline import ForecastDataPipeline

logger = get_logger(__name__, agent="forecast")

# Minimum rows of historical data required before training.
_MIN_TRAINING_ROWS: int = 30

# Model staleness threshold — retrain if older than this many days.
_MODEL_MAX_AGE_DAYS: int = 7

# Root directory for persisted models (architecture constraint).
_MODEL_DIR: str = "models/forecasting"


# ---------------------------------------------------------------------------
# LongTermMemory protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class HistoricalDataSource(Protocol):
    """
    Structural protocol for the LongTermMemory interface used by ForecastAgent.

    Only the method required for historical data retrieval is specified here.
    The full LongTermMemory class (Phase 1–3) is not imported to preserve
    strict modular boundaries.
    """

    def get_metric_history(
        self,
        metric_name: str,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """
        Return historical observations for ``metric_name`` as a list of dicts.

        Each dict must contain at minimum:
        - ``timestamp`` — datetime or ISO‑8601 string.
        - ``value``     — numeric value.

        Args:
            metric_name: Metric identifier to query.
            limit:       Maximum number of rows to return (most recent).

        Returns:
            List of metric records, ordered ascending by timestamp.
        """
        ...


# ---------------------------------------------------------------------------
# ForecastAgent
# ---------------------------------------------------------------------------


class ForecastAgent:
    """
    Time-series prediction and deviation detection agent.

    Manages per-metric Prophet model lifecycle and exposes a single
    :meth:`analyze` entry point. The agent:

    - Loads an existing model from ``models/forecasting/<metric_name>.pkl``
      when available and fresh (trained within the last 7 days).
    - Fetches historical data from :class:`HistoricalDataSource`, preprocesses
      it via :class:`~aeam.pipelines.forecast_data_pipeline.ForecastDataPipeline`,
      trains a new model, and saves it when the model is stale or absent.
    - Returns a structured analysis dict; never creates Events, calls an LLM,
      or calls an Action Agent.

    Args:
        long_term_memory: Data source satisfying :class:`HistoricalDataSource`.
        data_pipeline:    Preprocessing pipeline instance.
        settings:         Application configuration.
        model_dir:        Root directory for model files.
                          Defaults to ``"models/forecasting"``.

    Raises:
        ValueError: If ``long_term_memory`` or ``data_pipeline`` is None.

    Example::

        agent = ForecastAgent(
            long_term_memory=ltm,
            data_pipeline=ForecastDataPipeline(),
            settings=settings,
        )
        result = agent.analyze(metric_name="sales", actual_value=42_000.0)
    """

    def __init__(
        self,
        long_term_memory: HistoricalDataSource,
        data_pipeline: ForecastDataPipeline,
        settings: Settings,
        model_dir: str = _MODEL_DIR,
    ) -> None:
        """
        Initialise the ForecastAgent with injected dependencies.

        Args:
            long_term_memory: Historical data source (LongTermMemory).
            data_pipeline:    ForecastDataPipeline instance.
            settings:         Application Settings.
            model_dir:        Directory for saved model files.

        Raises:
            ValueError: If ``long_term_memory`` or ``data_pipeline`` is None.
        """
        if long_term_memory is None:
            raise ValueError("long_term_memory must not be None.")
        if data_pipeline is None:
            raise ValueError("data_pipeline must not be None.")

        self._ltm: HistoricalDataSource = long_term_memory
        self._pipeline: ForecastDataPipeline = data_pipeline
        self._settings: Settings = settings
        self._model_dir: Path = Path(model_dir)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load_or_train(self, metric_name: str) -> ForecastModel | dict[str, Any]:
        """
        Return a trained :class:`ForecastModel` for ``metric_name``.

        Decision logic:
        1. If a model file exists at ``models/forecasting/<metric_name>.pkl``
           **and** was trained within the last 7 days → load and return it.
        2. Otherwise → fetch historical data from LongTermMemory, preprocess,
           train a new model, save it, and return it.
        3. If fewer than ``_MIN_TRAINING_ROWS`` (30) valid rows are available
           after preprocessing → return ``{"insufficient_data": True}``
           without training.

        Args:
            metric_name: The metric identifier to load or train a model for.
                         Used as the model filename stem.

        Returns:
            A trained :class:`ForecastModel` instance, or
            ``{"insufficient_data": True}`` if not enough historical data
            exists to train.

        Raises:
            ValueError: If ``metric_name`` is empty or whitespace-only.
        """
        if not metric_name or not metric_name.strip():
            raise ValueError("metric_name must be a non-empty string.")

        model_path = self._model_path(metric_name)

        # Attempt to load a fresh existing model.
        if model_path.exists():
            model = ForecastModel()
            try:
                model.load_model(str(model_path))
                if self._is_model_fresh(model):
                    logger.info(
                        "load_or_train | loaded fresh model | metric=%s | "
                        "last_trained=%s",
                        metric_name,
                        model.last_trained.isoformat() if model.last_trained else "?",
                    )
                    return model
                else:
                    logger.info(
                        "load_or_train | model stale (>%d days) | metric=%s | retraining.",
                        _MODEL_MAX_AGE_DAYS, metric_name,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "load_or_train | failed to load model for %r: %s | retraining.",
                    metric_name, exc,
                )

        # Fetch and preprocess historical data.
        raw_df = self._fetch_historical(metric_name)

        if raw_df is None or raw_df.empty:
            logger.warning(
                "load_or_train | no historical data for metric=%s", metric_name
            )
            return {"insufficient_data": True}

        try:
            prepared_df = self._pipeline.prepare(raw_df)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "load_or_train | preprocessing failed for metric=%s: %s",
                metric_name, exc,
            )
            return {"insufficient_data": True}

        if len(prepared_df) < _MIN_TRAINING_ROWS:
            logger.warning(
                "load_or_train | insufficient data | metric=%s | rows=%d < %d",
                metric_name, len(prepared_df), _MIN_TRAINING_ROWS,
            )
            return {"insufficient_data": True}

        # Train, save, and return.
        model = ForecastModel(interval_width=0.95)
        model.train(prepared_df)

        self._model_dir.mkdir(parents=True, exist_ok=True)
        model.save_model(str(model_path))

        logger.info(
            "load_or_train | trained and saved | metric=%s | path=%s",
            metric_name, model_path,
        )

        return model

    def analyze(
        self,
        metric_name: str,
        actual_value: float,
    ) -> dict[str, Any]:
        """
        Forecast the next period for ``metric_name`` and compare to ``actual_value``.

        Steps:
        1. Call :meth:`load_or_train` to get a fresh model.
        2. If insufficient data → return early with ``insufficient_data`` flag.
        3. Predict the next 1 period.
        4. Call :meth:`~aeam.agents.forecast.forecast_model.ForecastModel.detect_deviation`
           with ``actual_value`` and the forecast row.
        5. Return the structured analysis dict.

        This method:
        - Does NOT create an Event.
        - Does NOT call an LLM.
        - Does NOT call an Action Agent.
        - Only returns analysis.

        Args:
            metric_name:   The metric to forecast and evaluate.
            actual_value:  The current real-world observation to compare against
                           the forecast.

        Returns:
            On success::

                {
                    "predicted":         float,         # yhat
                    "lower_bound":       float,         # yhat_lower
                    "upper_bound":       float,         # yhat_upper
                    "is_deviation":      bool,
                    "deviation_percent": float | None,  # None if no deviation
                }

            When insufficient data is available::

                {
                    "insufficient_data": True,
                    "predicted":         None,
                    "lower_bound":       None,
                    "upper_bound":       None,
                    "is_deviation":      False,
                    "deviation_percent": None,
                }

            On unexpected error::

                {
                    "error":             str,
                    "predicted":         None,
                    "lower_bound":       None,
                    "upper_bound":       None,
                    "is_deviation":      False,
                    "deviation_percent": None,
                }

        Raises:
            ValueError: If ``metric_name`` is empty or whitespace-only.
        """
        if not metric_name or not metric_name.strip():
            raise ValueError("metric_name must be a non-empty string.")

        logger.info(
            "ForecastAgent.analyze | metric=%s | actual=%.4f",
            metric_name, actual_value,
        )

        # Step 1: get model.
        result = self.load_or_train(metric_name)

        # Step 2: insufficient data guard.
        if isinstance(result, dict):
            logger.warning(
                "analyze | insufficient data for metric=%s", metric_name
            )
            return {
                "insufficient_data": True,
                "predicted":         None,
                "lower_bound":       None,
                "upper_bound":       None,
                "is_deviation":      False,
                "deviation_percent": None,
            }

        model: ForecastModel = result

        # Step 3 & 4: predict and detect deviation.
        try:
            forecast_df = model.predict(periods=1)
            forecast_row = forecast_df.iloc[0]

            deviation = model.detect_deviation(
                actual=actual_value,
                forecast_row=forecast_row,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "analyze | prediction/detection failed | metric=%s | error=%s",
                metric_name, exc,
            )
            return {
                "error":             str(exc),
                "predicted":         None,
                "lower_bound":       None,
                "upper_bound":       None,
                "is_deviation":      False,
                "deviation_percent": None,
            }

        # Step 5: assemble result.
        predicted = round(float(forecast_row["yhat"]), 4)
        lower = round(float(forecast_row["yhat_lower"]), 4)
        upper = round(float(forecast_row["yhat_upper"]), 4)
        is_dev: bool = bool(deviation.get("is_deviation", False))
        dev_pct: float | None = (
            round(float(deviation["deviation_percent"]), 4)
            if is_dev
            else None
        )

        logger.info(
            "analyze | metric=%s | predicted=%.4f | [%.4f, %.4f] | "
            "is_deviation=%s | deviation_pct=%s",
            metric_name, predicted, lower, upper, is_dev, dev_pct,
        )

        return {
            "predicted":         predicted,
            "lower_bound":       lower,
            "upper_bound":       upper,
            "is_deviation":      is_dev,
            "deviation_percent": dev_pct,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _model_path(self, metric_name: str) -> Path:
        """
        Construct the file path for a metric's saved model.

        Args:
            metric_name: Metric identifier. Sanitised to remove path separators.

        Returns:
            ``Path`` object under ``_model_dir``.
        """
        safe_name = metric_name.replace("/", "_").replace("\\", "_")
        filename = f"prophet_metric_{safe_name}_v1.pkl"
        return self._model_dir / filename

    def _fetch_historical(self, metric_name: str) -> pd.DataFrame | None:
        """
        Fetch historical metric data from LongTermMemory.

        Returns ``None`` on error so callers receive a clean ``insufficient_data``
        response rather than a traceback.

        Args:
            metric_name: Metric to retrieve history for.

        Returns:
            DataFrame with ``ds`` and ``y`` columns, or ``None`` on failure.
        """
        try:
            rows = self._ltm.get_metric_history(metric_name=metric_name)
            if not rows:
                logger.warning(
                    "_fetch_historical | no rows returned | metric=%s",
                    metric_name,
                )
                return None

            df = pd.DataFrame(rows)
            df = df.rename(columns={
                "timestamp": "ds",
                "value": "y"
            })
            return df
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "_fetch_historical | failed for metric=%s: %s", metric_name, exc
            )
            return None

    @staticmethod
    def _is_model_fresh(model: ForecastModel) -> bool:
        """
        Return ``True`` if the model was trained within the last 7 days.

        Args:
            model: The loaded :class:`ForecastModel` to check.

        Returns:
            ``True`` if fresh; ``False`` if stale or ``last_trained`` is None.
        """
        if model.last_trained is None:
            return False
        age = datetime.now(tz=timezone.utc) - model.last_trained
        return age < timedelta(days=_MODEL_MAX_AGE_DAYS)

    def __repr__(self) -> str:
        return (
            f"ForecastAgent("
            f"model_dir={str(self._model_dir)!r}, "
            f"min_rows={_MIN_TRAINING_ROWS}, "
            f"max_age_days={_MODEL_MAX_AGE_DAYS})"
        )
