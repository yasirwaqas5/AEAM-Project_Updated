"""
aeam/core/event_models.py

Defines immutable event objects used across the AEAM modular monolith.

Events are the core data unit flowing through the system — produced by detectors,
consumed by investigators, and stored for auditing. All models are frozen (immutable)
to ensure events are never mutated after creation, making them safe to pass across
module boundaries and async contexts.
"""

import hashlib
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field, field_validator,ConfigDict


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_SEVERITIES: frozenset[str] = frozenset({"CRITICAL", "HIGH", "MEDIUM", "LOW"})


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class Event(BaseModel):
    """
    Immutable representation of a detected anomaly or system event.

    Attributes:
        event_id:           Unique identifier for this event (e.g. UUID4 string).
        event_type:         Category of event (e.g. "ANOMALY", "THRESHOLD_BREACH").
        metric:             The metric or signal that triggered the event
                            (e.g. "cpu_utilization", "error_rate").
        current_value:      The observed value of the metric at detection time.
        expected_value:     The baseline or predicted value, if available.
        detection_methods:  Ordered list of methods that flagged this event
                            (e.g. ["zscore", "isolation_forest"]).
        severity:           Severity level. Must be one of:
                            "CRITICAL", "HIGH", "MEDIUM", "LOW".
        timestamp:          UTC datetime when the event was detected.
        metadata:           Arbitrary key-value pairs for context
                            (e.g. host, region, trace_id). Values must be
                            JSON-serialisable primitives.
    """

    model_config = ConfigDict(frozen=True)

    event_id: str = Field(
        ...,
        description="Unique identifier for this event instance.",
    )
    event_type: str = Field(
        ...,
        description="High-level category of the event.",
    )
    metric: str = Field(
        ...,
        description="Name of the metric or signal that triggered this event.",
    )
    current_value: float = Field(
        ...,
        description="Observed metric value at the time of detection.",
    )
    expected_value: float | None = Field(
        default=None,
        description="Baseline or predicted metric value, if known.",
    )
    detection_methods: list[str] = Field(
        ...,
        min_length=1,
        description="Ordered list of detection algorithms that flagged this event.",
    )
    severity: str = Field(
        ...,
        description="Severity level: CRITICAL | HIGH | MEDIUM | LOW.",
    )
    timestamp: datetime = Field(
        ...,
        description="UTC datetime when the event was detected.",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Arbitrary context key-value pairs (must be JSON-serialisable).",
    )

    # ------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------

    @field_validator("severity")
    @classmethod
    def validate_severity(cls, v: str) -> str:
        """Ensure severity is one of the accepted levels."""
        normalised = v.upper()
        if normalised not in VALID_SEVERITIES:
            raise ValueError(
                f"severity must be one of {sorted(VALID_SEVERITIES)}. Got: '{v}'"
            )
        return normalised

    @field_validator("timestamp")
    @classmethod
    def ensure_utc(cls, v: datetime) -> datetime:
        """
        Ensure the timestamp is timezone-aware.
        Naive datetimes are assumed to be UTC and made aware.
        """
        if v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v

    @field_validator("event_id", "event_type", "metric")
    @classmethod
    def non_empty_string(cls, v: str, info: Any) -> str:
        """Reject empty or whitespace-only strings for key identity fields."""
        if not v or not v.strip():
            raise ValueError(f"'{info.field_name}' must not be empty or whitespace.")
        return v.strip()

    # ------------------------------------------------------------------
    # Methods
    # ------------------------------------------------------------------

    def dedup_key(self, window_minutes: int) -> str:
        """
        Generate a stable deduplication key for this event within a time window.

        Two events with the same ``event_type``, ``metric``, ``severity``, and
        falling within the same ``window_minutes``-sized bucket will produce an
        identical key, allowing upstream consumers to collapse duplicates.

        Args:
            window_minutes: Size of the bucketing window in minutes. Must be >= 1.

        Returns:
            A lowercase hex SHA-256 digest string (64 characters).

        Raises:
            ValueError: If ``window_minutes`` is less than 1.

        Example:
            >>> key = event.dedup_key(window_minutes=5)
            >>> cache.set_if_absent(key, event, ttl=300)
        """
        if window_minutes < 1:
            raise ValueError(f"window_minutes must be >= 1. Got: {window_minutes}")

        # Bucket the timestamp into fixed-size windows.
        epoch_minutes = int(self.timestamp.timestamp()) // 60
        window_bucket = epoch_minutes // window_minutes

        raw = "|".join(
            [
                self.event_type,
                self.metric,
                self.severity,
                str(window_bucket),
            ]
        )
        return hashlib.sha256(raw.encode()).hexdigest()