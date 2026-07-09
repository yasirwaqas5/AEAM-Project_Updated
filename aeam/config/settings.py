"""
aeam/config/settings.py

Centralized configuration for the AEAM modular monolith.

This module defines all application-level settings using Pydantic's BaseSettings,
which automatically loads values from environment variables or a .env file.

No secrets are hardcoded. Required fields will raise a ValidationError at startup
if not provided via the environment.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, field_validator


class Settings(BaseSettings):
    """
    Application settings loaded from environment variables.

    Required environment variables:
        - DATABASE_URL
        - REDIS_URL
        - VECTOR_DB_URL
        - ENVIRONMENT

    Optional environment variables (with defaults):
        - MONITOR_INTERVAL_SECONDS (default: 300)
        - MAX_INVESTIGATION_DEPTH (default: 5)
        - LLM_ENABLED (default: False)
        - USE_MOCK_LLM (default: True)

        # --- Forecast configuration (Phase 5) ---
        - FORECAST_WINDOW_DAYS (default: 7)
        - FORECAST_MIN_HISTORY_DAYS (default: 30)
        - FORECAST_RETRAIN_DAYS (default: 7)
        - FORECAST_DEVIATION_THRESHOLD_PERCENT (default: 20.0)
        - FORECAST_CONFIDENCE_INTERVAL (default: 0.95)
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="forbid",
    )

    # --- Required settings ---

    DATABASE_URL: str = Field(
        ...,
        description="Connection string for relational database (e.g. PostgreSQL or SQLite).",
    )

    REDIS_URL: str = Field(
        ...,
        description="Connection string for Redis instance.",
    )

    VECTOR_DB_URL: str = Field(
        ...,
        description="Connection string or endpoint for vector database.",
    )

    ENVIRONMENT: str = Field(
        ...,
        description="Deployment environment: development, staging, production, or test.",
    )

    # --- Optional settings with defaults ---

    MONITOR_INTERVAL_SECONDS: int = Field(
        default=300,
        ge=1,
        description="Polling interval (seconds) for monitor loop.",
    )

    MAX_INVESTIGATION_DEPTH: int = Field(
        default=5,
        ge=1,
        description="Maximum recursive depth for investigation chain.",
    )

    LLM_ENABLED: bool = Field(
        default=False,
        description="Feature flag to enable or disable LLM-powered components.",
    )

    ENABLE_MONITOR_AGENT: bool = Field(
        default=False,
        description="Feature flag to enable or disable the MonitorAgent.",
    )

    LLM_PROVIDER: str = Field(
        default="gemini",
        description="Which LLM backend to use: 'gemini', 'openai', etc.",
    )

    LLM_API_KEY: str = Field(
        default="",
        description="API key for the LLM provider (loaded from .env).",
    )

    USE_MOCK_LLM: bool = Field(
        default=True,
        description="When True, LLM calls return mock responses for tests/offline use.",
    )

    # --- Forecast configuration (Phase 5) ---

    FORECAST_WINDOW_DAYS: int = Field(
        default=7,
        ge=1,
        description="Forecast horizon (number of future periods).",
    )

    FORECAST_MIN_HISTORY_DAYS: int = Field(
        default=30,
        ge=7,
        description="Minimum historical window required to train forecast model.",
    )

    FORECAST_RETRAIN_DAYS: int = Field(
        default=7,
        ge=1,
        description="Days after which the forecast model must retrain.",
    )

    FORECAST_DEVIATION_THRESHOLD_PERCENT: float = Field(
        default=20.0,
        ge=1.0,
        description="Deviation threshold percentage beyond forecast confidence interval.",
    )

    FORECAST_CONFIDENCE_INTERVAL: float = Field(
        default=0.95,
        gt=0.5,
        lt=1.0,
        description="Confidence interval width for Prophet forecasts.",
    )

    # --- Google Sheets configuration ---

    GOOGLE_SHEETS_SA_CREDENTIALS: str = Field(
        default="",
        description="Minified one-line Google service account JSON credentials.",
    )

    SHEET_ID: str = Field(
        default="",
        description="Google Sheets spreadsheet ID.",
    )

    SHEET_RANGE: str = Field(
        default="Sheet1!A2:C10",
        description="Google Sheets range for KPI data.",
    )

    # --- Slack configuration ---

    SLACK_BOT_TOKEN: str = Field(
        default="",
        description="Slack Bot User OAuth token.",
    )

    SLACK_CHANNEL: str = Field(
        default="#aeam-alerts",
        description="Default Slack channel for alerts.",
    )

    # --- Jira configuration ---

    JIRA_URL: str = Field(
        default="",
        description="Jira Cloud instance URL",
    )

    JIRA_API_TOKEN: str = Field(
        default="",
        description="Jira API token",
    )

    JIRA_USER_EMAIL: str = Field(
        default="",
        description="Jira user email for authentication",
    )

    JIRA_PROJECT_KEY: str = Field(
        default="", 
        description="Jira project key (e.g., 'OPS')"
    )

    JIRA_ISSUE_TYPE: str = Field(
        default="",
        description="Jira issue type name or ID (e.g., '10004' for Task)"
    )

    # --- Validators ---

    @field_validator("ENVIRONMENT")
    @classmethod
    def validate_environment(cls, v: str) -> str:
        allowed = {"development", "staging", "production", "test"}
        value = v.lower()
        if value not in allowed:
            raise ValueError(f"ENVIRONMENT must be one of {allowed}. Got: '{v}'")
        return value