
"""
aeam/tests/test_phase5_forecast.py

Phase 5 test suite for AEAM Forecasting Layer.

Validates:
- ForecastDataPipeline
- ForecastModel lifecycle
- ForecastAgent behaviour
- deviation detection
- model persistence
- insufficient data handling
"""

import os
import tempfile
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from aeam.agents.forecast.forecast_model import ForecastModel
from aeam.agents.forecast.forecast_agent import ForecastAgent
from aeam.pipelines.forecast_data_pipeline import ForecastDataPipeline
from aeam.config.settings import Settings


# -------------------------------------------------------------------
# Dummy LongTermMemory
# -------------------------------------------------------------------

class DummyLTM:
    """Mock LongTermMemory returning synthetic metric history."""

    def __init__(self, rows: int = 60):
        self.rows = rows

    def get_metric_history(self, metric_name: str, limit=None):
        now = datetime.now(timezone.utc)

        data = []
        for i in range(self.rows):
            data.append({
                "timestamp": now - timedelta(days=self.rows - i),
                "value": 100 + i
            })

        return data


# -------------------------------------------------------------------
# ForecastDataPipeline Tests
# -------------------------------------------------------------------

def test_pipeline_prepare_basic():
    pipeline = ForecastDataPipeline()

    df = pd.DataFrame({
        "ds": pd.date_range("2024-01-01", periods=10),
        "y": [1, 2, 3, 4, None, 6, 7, 8, 1000, 10]
    })

    result = pipeline.prepare(df)

    assert "day_of_week" in result.columns
    assert "month" in result.columns
    assert "is_weekend" in result.columns
    assert result["y"].isna().sum() == 0


def test_pipeline_outlier_winsorization():
    pipeline = ForecastDataPipeline()

    df = pd.DataFrame({
        "ds": pd.date_range("2024-01-01", periods=20),
        "y": [1]*19 + [10000]
    })

    result = pipeline.prepare(df)

    assert result["y"].max() < 10000


# -------------------------------------------------------------------
# ForecastModel Tests
# -------------------------------------------------------------------

def build_training_df(rows=40):
    return pd.DataFrame({
        "ds": pd.date_range("2023-01-01", periods=rows),
        "y": [100 + i for i in range(rows)]
    })


def test_forecast_model_train_predict():
    df = build_training_df()

    model = ForecastModel()
    model.train(df)

    forecast = model.predict(periods=3)

    assert len(forecast) == 3
    assert "yhat" in forecast.columns
    assert "yhat_lower" in forecast.columns
    assert "yhat_upper" in forecast.columns


def test_predict_before_train_fails():
    model = ForecastModel()

    with pytest.raises(RuntimeError):
        model.predict()


def test_deviation_detection():
    df = build_training_df()

    model = ForecastModel()
    model.train(df)

    forecast = model.predict(periods=1).iloc[0]

    result = model.detect_deviation(
        actual=forecast["yhat"] * 2,
        forecast_row=forecast
    )

    assert result["is_deviation"] is True
    assert result["deviation_percent"] > 20


# -------------------------------------------------------------------
# Model Persistence Tests
# -------------------------------------------------------------------

def test_model_save_and_load():
    df = build_training_df()

    model = ForecastModel()
    model.train(df)

    with tempfile.TemporaryDirectory() as tmpdir:

        path = os.path.join(tmpdir, "model.pkl")

        model.save_model(path)

        new_model = ForecastModel()
        new_model.load_model(path)

        forecast = new_model.predict(periods=1)

        assert "yhat" in forecast.columns


# -------------------------------------------------------------------
# ForecastAgent Tests
# -------------------------------------------------------------------

def build_settings():
    return Settings(
        DATABASE_URL="sqlite:///test.db",
        REDIS_URL="redis://localhost:6379/0",
        VECTOR_DB_URL="http://localhost:6333",
        ENVIRONMENT="development",
    )


def test_forecast_agent_analyze_success():
    ltm = DummyLTM(rows=60)

    agent = ForecastAgent(
        long_term_memory=ltm,
        data_pipeline=ForecastDataPipeline(),
        settings=build_settings(),
        model_dir=tempfile.mkdtemp(),
    )

    result = agent.analyze("sales", 200)

    assert "predicted" in result
    assert "is_deviation" in result


def test_forecast_agent_insufficient_data():
    ltm = DummyLTM(rows=10)

    agent = ForecastAgent(
        long_term_memory=ltm,
        data_pipeline=ForecastDataPipeline(),
        settings=build_settings(),
        model_dir=tempfile.mkdtemp(),
    )

    result = agent.analyze("sales", 200)

    assert result["insufficient_data"] is True


# -------------------------------------------------------------------
# Retraining Logic Test
# -------------------------------------------------------------------

def test_model_retraining_if_old():

    ltm = DummyLTM(rows=60)

    model_dir = tempfile.mkdtemp()

    agent = ForecastAgent(
        long_term_memory=ltm,
        data_pipeline=ForecastDataPipeline(),
        settings=build_settings(),
        model_dir=model_dir,
    )

    model = agent.load_or_train("sales")

    # simulate stale model
    model.last_trained = datetime.now(timezone.utc) - timedelta(days=10)

    model.save_model(os.path.join(model_dir, "prophet_metric_sales_v1.pkl"))

    new_model = agent.load_or_train("sales")

    assert new_model.last_trained > model.last_trained
