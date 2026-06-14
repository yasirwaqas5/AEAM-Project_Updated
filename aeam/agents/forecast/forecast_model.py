"""
aeam/agents/forecast/forecast_model.py

Prophet-based forecasting model lifecycle for the AEAM forecasting agent.

Encapsulates training, prediction, deviation detection, and model
persistence for a single Prophet model instance. Training is always
explicit — predict() raises if called before train(). No background
retraining, no LLM usage, no Orchestrator logic.

Architecture constraints:
- Models stored under models/forecasting/ (caller supplies path).
- train() must be called explicitly before predict().
- No auto background retrain.
- No LLM calls.
- No Orchestrator references.
- All dependencies injected where applicable.
"""

from __future__ import annotations

import logging
from aeam.monitoring.logging_config import get_logger
import pickle
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from prophet import Prophet

logger = get_logger(__name__, agent="forecast")

# Deviation threshold above which a forecast breach is considered significant.
_DEVIATION_THRESHOLD_PCT: float = 20.0


class ForecastModel:
    """
    Encapsulates the full lifecycle of a Prophet forecasting model.

    Responsibilities:
    - Explicit training via :meth:`train`.
    - Future-period prediction via :meth:`predict`.
    - Deviation detection between an actual observation and a forecast row
      via :meth:`detect_deviation`.
    - Local persistence via :meth:`save_model` / :meth:`load_model`.

    Training is always explicit. :meth:`predict` raises :class:`RuntimeError`
    if called before :meth:`train` or :meth:`load_model`. There is no
    automatic or background retraining.

    Args:
        interval_width: Confidence interval width used by Prophet.
                        Must be in (0, 1). Defaults to ``0.95``.

    Raises:
        ValueError: If ``interval_width`` is outside (0, 1).

    Example::

        model = ForecastModel(interval_width=0.95)
        model.train(prepared_df)
        forecast = model.predict(periods=7)
        result = model.detect_deviation(actual=42_000.0, forecast_row=forecast.iloc[0])
    """

    def __init__(self, interval_width: float = 0.95) -> None:
        """
        Initialise an untrained ForecastModel.

        Args:
            interval_width: Prophet confidence interval width. Default ``0.95``.

        Raises:
            ValueError: If ``interval_width`` is not in (0, 1).
        """
        if not (0 < interval_width < 1):
            raise ValueError(
                f"interval_width must be in (0, 1). Got: {interval_width}."
            )

        self.interval_width: float = interval_width
        self.model: Prophet | None = None
        self.last_trained: datetime | None = None

    # ------------------------------------------------------------------
    # Training
    # ------------------------------------------------------------------

    def train(self, df: pd.DataFrame) -> None:
        """
        Initialise and fit a new Prophet model on ``df``.

        A fresh :class:`prophet.Prophet` instance is created each time
        ``train`` is called — there is no incremental fitting. After fitting,
        :attr:`last_trained` is set to the current UTC datetime.

        The DataFrame must have been preprocessed by
        :class:`~aeam.pipelines.forecast_data_pipeline.ForecastDataPipeline`
        (i.e. ``ds`` is timezone-naive ``datetime64``, ``y`` is ``float64``,
        sorted ascending).

        Args:
            df: Training DataFrame with at minimum columns ``ds``
                (timezone-naive datetime64) and ``y`` (float64).

        Raises:
            ValueError: If ``df`` is missing ``ds`` or ``y`` columns, or is
                        empty after dropping NaN rows.

        Note:
            This method must be called explicitly before :meth:`predict`.
            Re-calling ``train`` replaces the previous model entirely.
        """
        self._validate_train_df(df)

        logger.info(
            "ForecastModel.train | rows=%d | interval_width=%.2f",
            len(df), self.interval_width,
        )

        self.model = Prophet(
            yearly_seasonality=True,
            weekly_seasonality=True,
            daily_seasonality=False,
            interval_width=self.interval_width,
        )

        # Suppress Prophet's verbose Stan output.
        import logging as _logging
        _logging.getLogger("prophet").setLevel(_logging.WARNING)
        _logging.getLogger("cmdstanpy").setLevel(_logging.WARNING)

        self.model.fit(df[["ds", "y"]])
        self.last_trained = datetime.now(tz=timezone.utc)

        logger.info(
            "ForecastModel.train complete | last_trained=%s",
            self.last_trained.isoformat(),
        )

    # ------------------------------------------------------------------
    # Prediction
    # ------------------------------------------------------------------

    def predict(self, periods: int = 1) -> pd.DataFrame:
        """
        Generate a forecast for ``periods`` future time steps.

        Uses the fitted model's last training date as the starting point for
        the future DataFrame. The frequency is inferred as daily (``"D"``).

        Args:
            periods: Number of future periods to forecast. Must be >= 1.

        Returns:
            DataFrame with columns:

            - ``ds``          — future datetime timestamps.
            - ``yhat``        — point forecast.
            - ``yhat_lower``  — lower bound of the confidence interval.
            - ``yhat_upper``  — upper bound of the confidence interval.

        Raises:
            RuntimeError: If called before :meth:`train` or :meth:`load_model`.
            ValueError:   If ``periods`` < 1.

        Example::

            forecast = model.predict(periods=7)
            next_day = forecast.iloc[0]
            print(next_day["yhat"], next_day["yhat_lower"], next_day["yhat_upper"])
        """
        self._require_trained("predict")

        if periods < 1:
            raise ValueError(f"periods must be >= 1. Got: {periods}.")

        logger.info("ForecastModel.predict | periods=%d", periods)

        future = self.model.make_future_dataframe(  # type: ignore[union-attr]
            periods=periods,
            freq="D",
            include_history=False,
        )

        raw_forecast = self.model.predict(future)  # type: ignore[union-attr]

        result = raw_forecast[["ds", "yhat", "yhat_lower", "yhat_upper"]].copy()
        result = result.reset_index(drop=True)

        logger.debug(
            "ForecastModel.predict | returned %d rows", len(result)
        )

        return result

    # ------------------------------------------------------------------
    # Deviation detection
    # ------------------------------------------------------------------

    def detect_deviation(
        self,
        actual: float,
        forecast_row: pd.Series,
    ) -> dict[str, Any]:
        """
        Determine whether ``actual`` deviates significantly from the forecast.

        A deviation is flagged when:
        1. ``actual`` falls outside the forecast confidence interval
           ``[yhat_lower, yhat_upper]``, **and**
        2. The percentage deviation from ``yhat`` exceeds
           :data:`_DEVIATION_THRESHOLD_PCT` (20%).

        The ``yhat`` value is used as the reference for percentage deviation
        rather than the interval bounds, to ensure the magnitude of the
        deviation is meaningful and not just a boundary breach.

        Args:
            actual:       The observed real-world value to evaluate.
            forecast_row: A single row from the DataFrame returned by
                          :meth:`predict`. Must contain ``yhat``,
                          ``yhat_lower``, and ``yhat_upper``.

        Returns:
            When a significant deviation is detected::

                {
                    "is_deviation":      True,
                    "deviation_percent": float,   # positive value
                    "direction":         "above" | "below",
                }

            When no significant deviation is detected::

                {"is_deviation": False}

        Raises:
            KeyError: If ``forecast_row`` is missing ``yhat``, ``yhat_lower``,
                      or ``yhat_upper``.

        Example::

            result = model.detect_deviation(
                actual=38_000.0,
                forecast_row=forecast.iloc[0],
            )
            # {"is_deviation": True, "deviation_percent": 23.5, "direction": "below"}
        """
        required = {"yhat", "yhat_lower", "yhat_upper"}
        missing = required - set(forecast_row.index)
        if missing:
            raise KeyError(
                f"forecast_row is missing required fields: {sorted(missing)}."
            )

        yhat: float = float(forecast_row["yhat"])
        yhat_lower: float = float(forecast_row["yhat_lower"])
        yhat_upper: float = float(forecast_row["yhat_upper"])

        # Check 1: is actual outside the confidence interval?
        outside_interval = actual < yhat_lower or actual > yhat_upper

        if not outside_interval:
            return {"is_deviation": False}

        # Check 2: is the percentage deviation above the threshold?
        if yhat == 0.0:
            # Avoid division by zero; clamp to 100% if actual differs.
            deviation_pct = 100.0 if actual != 0.0 else 0.0
        else:
            deviation_pct = abs((actual - yhat) / abs(yhat)) * 100.0

        if deviation_pct <= _DEVIATION_THRESHOLD_PCT:
            return {"is_deviation": False}

        direction = "above" if actual > yhat else "below"

        logger.debug(
            "detect_deviation | actual=%.4f | yhat=%.4f | "
            "deviation_pct=%.2f%% | direction=%s",
            actual, yhat, deviation_pct, direction,
        )

        return {
            "is_deviation":      True,
            "deviation_percent": round(deviation_pct, 4),
            "direction":         direction,
        }

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save_model(self, path: str) -> None:
        """
        Serialize and save the fitted model to ``path`` using pickle.

        The parent directory is created if it does not exist.
        Models should be saved under ``models/forecasting/`` per architecture
        constraints.

        Args:
            path: File path for the serialised model
                  (e.g. ``"models/forecasting/sales_prophet.pkl"``).

        Raises:
            RuntimeError: If called before :meth:`train` or :meth:`load_model`.
            OSError:      If the file cannot be written.

        Example::

            model.save_model("models/forecasting/sales_prophet.pkl")
        """
        self._require_trained("save_model")

        save_path = Path(path)
        save_path.parent.mkdir(parents=True, exist_ok=True)

        payload = {
            "model": self.model,
            "last_trained": self.last_trained,
            "interval_width": self.interval_width,
        }

        with save_path.open("wb") as fh:
            pickle.dump(payload, fh, protocol=pickle.HIGHEST_PROTOCOL)

        logger.info("ForecastModel.save_model | saved to %s", save_path)

    def load_model(self, path: str) -> None:
        """
        Load a previously saved model from ``path``.

        Restores :attr:`model`, :attr:`last_trained`, and
        :attr:`interval_width` from the pickle payload written by
        :meth:`save_model`.

        Args:
            path: File path of the serialised model pickle.

        Raises:
            FileNotFoundError: If ``path`` does not exist.
            ValueError:        If the pickle payload is missing expected keys.
            OSError:           If the file cannot be read.

        Example::

            model = ForecastModel()
            model.load_model("models/forecasting/sales_prophet.pkl")
            forecast = model.predict(periods=7)
        """
        load_path = Path(path)
        if not load_path.exists():
            raise FileNotFoundError(
                f"Model file not found: '{load_path}'. "
                "Train and save the model before loading."
            )

        with load_path.open("rb") as fh:
            payload: dict[str, Any] = pickle.load(fh)  # noqa: S301

        required_keys = {"model", "last_trained", "interval_width"}
        missing = required_keys - set(payload.keys())
        if missing:
            raise ValueError(
                f"Model pickle at '{load_path}' is missing keys: {sorted(missing)}."
            )

        self.model = payload["model"]
        self.last_trained = payload["last_trained"]
        self.interval_width = payload["interval_width"]

        logger.info(
            "ForecastModel.load_model | loaded from %s | last_trained=%s",
            load_path,
            self.last_trained.isoformat() if self.last_trained else "unknown",
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _require_trained(self, method_name: str) -> None:
        """
        Raise :class:`RuntimeError` if the model has not been trained or loaded.

        Args:
            method_name: Calling method name, used in the error message.

        Raises:
            RuntimeError: If :attr:`model` is ``None``.
        """
        if self.model is None:
            raise RuntimeError(
                f"ForecastModel.{method_name}() called before training. "
                "Call train(df) or load_model(path) first."
            )

    @staticmethod
    def _validate_train_df(df: pd.DataFrame) -> None:
        """
        Raise :class:`ValueError` if ``df`` is unsuitable for training.

        Checks:
        - ``ds`` and ``y`` columns must be present.
        - DataFrame must not be empty.

        Args:
            df: Training DataFrame to inspect.

        Raises:
            ValueError: On schema or empty-frame violations.
        """
        missing = [col for col in ("ds", "y") if col not in df.columns]
        if missing:
            raise ValueError(
                f"Training DataFrame is missing required columns: {missing}."
            )
        if df.empty:
            raise ValueError(
                "Training DataFrame must not be empty."
            )
        if df[["ds", "y"]].dropna().empty:
            raise ValueError(
                "Training DataFrame has no valid (ds, y) rows after dropping NaNs."
            )

    def __repr__(self) -> str:
        trained_str = (
            self.last_trained.isoformat()
            if self.last_trained
            else "untrained"
        )
        return (
            f"ForecastModel("
            f"interval_width={self.interval_width}, "
            f"last_trained={trained_str!r})"
        )
