"""
aeam/api/trigger.py

Manual event injection API for the AEAM system.

Allows operators and the UI to inject anomaly events directly into the
AEAM pipeline via the EventBus. The flow is strictly:

    UI → POST /api/v1/trigger → EventBus.publish(event) → Orchestrator

The Orchestrator is a registered EventBus subscriber and picks up the
event automatically. This endpoint never calls agents, the Orchestrator,
or any external service directly.

Rules enforced:
- Input validated with Pydantic before event construction.
- Event published via request.app.state.container.event_bus.
- No orchestrator calls.
- No agent calls.
- No external API calls.
- No business logic.
- Public endpoint — no authentication required.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from aeam.core.event_models import Event, VALID_SEVERITIES

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/trigger", tags=["Trigger"])


# ---------------------------------------------------------------------------
# Request schema
# ---------------------------------------------------------------------------

class TriggerRequest(BaseModel):
    """
    Validated input for manual event injection.

    Attributes:
        event_type: Category of the anomaly event
                    (e.g. ``"KPI_ANOMALY"``, ``"THRESHOLD_BREACH"``).
        metric:     Name of the affected metric
                    (e.g. ``"sales_daily"``, ``"cpu_usage"``).
        value:      Observed metric value at the time of detection.
        severity:   Severity level. One of:
                    ``"CRITICAL"``, ``"HIGH"``, ``"MEDIUM"``, ``"LOW"``.
    """

    event_type: str = Field(
        ...,
        min_length=1,
        description="Anomaly event category (e.g. 'KPI_ANOMALY').",
    )
    metric: str = Field(
        ...,
        min_length=1,
        description="Name of the affected metric.",
    )
    value: float = Field(
        ...,
        description="Observed metric value.",
    )
    severity: str = Field(
        ...,
        description="Severity level: CRITICAL | HIGH | MEDIUM | LOW.",
    )

    @field_validator("severity")
    @classmethod
    def validate_severity(cls, v: str) -> str:
        """Normalise and validate severity against accepted values."""
        normalised = v.strip().upper()
        if normalised not in VALID_SEVERITIES:
            raise ValueError(
                f"severity must be one of {sorted(VALID_SEVERITIES)}. "
                f"Got: '{v}'."
            )
        return normalised

    @field_validator("event_type", "metric")
    @classmethod
    def validate_non_blank(cls, v: str) -> str:
        """Reject whitespace-only strings."""
        if not v.strip():
            raise ValueError("Field must not be blank.")
        return v.strip()


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.post(
    "/",
    summary="Manually trigger an anomaly event",
    response_description="Confirmation that the event was published.",
    status_code=202,
)
def trigger_event(body: TriggerRequest, request: Request) -> JSONResponse:
    """
    Inject a manually constructed anomaly event into the AEAM pipeline.

    Validates the request body, constructs an :class:`~aeam.core.event_models.Event`,
    and publishes it to the EventBus. The Orchestrator — registered as a
    subscriber — picks it up and begins an investigation cycle.

    Flow::

        POST /api/v1/trigger
            → TriggerRequest validated
            → Event constructed
            → event_bus.publish(event)
            → Orchestrator.handle_event(event)  [via EventBus, not directly]

    Args:
        body:    Validated :class:`TriggerRequest` parsed from the JSON body.
        request: Incoming FastAPI request. Used to access the EventBus via
                 ``request.app.state.container.event_bus``.

    Returns:
        ``202 Accepted`` — Event accepted and published::

            {
                "status":    "accepted",
                "event_id":  "uuid-...",
                "event_type": "KPI_ANOMALY",
                "metric":    "sales_daily",
                "severity":  "HIGH"
            }

        ``422 Unprocessable Entity`` — Pydantic validation failure
        (invalid severity, blank fields, etc.) — handled automatically
        by FastAPI.

        ``500`` — Internal error if EventBus publish fails.

    Note:
        Returns ``202`` (Accepted) rather than ``200`` because event
        processing is asynchronous — the Orchestrator investigation
        runs after this response is returned.
    """
    event_id = str(uuid.uuid4())

    event = Event(
        event_id=event_id,
        event_type=body.event_type,
        metric=body.metric,
        current_value=body.value,
        expected_value=None,
        detection_methods=["manual_trigger"],
        severity=body.severity,
        timestamp=datetime.now(tz=timezone.utc),
        metadata={"source": "api_trigger"},
    )

    try:
        event_bus = request.app.state.container.event_bus
        event_bus.publish(event)

        logger.info(
            "trigger_event | published | event_id=%s | event_type=%s | "
            "metric=%s | severity=%s",
            event_id, body.event_type, body.metric, body.severity,
        )

        return JSONResponse(
            status_code=202,
            content={
                "status":     "accepted",
                "event_id":   event_id,
                "event_type": body.event_type,
                "metric":     body.metric,
                "severity":   body.severity,
            },
        )

    except Exception as exc:  # noqa: BLE001
        logger.error(
            "trigger_event | EventBus publish failed | event_id=%s | error=%s",
            event_id, exc,
        )
        return JSONResponse(
            status_code=500,
            content={"detail": f"Failed to publish event: {exc}"},
        )