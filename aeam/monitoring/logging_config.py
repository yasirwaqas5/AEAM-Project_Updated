"""
aeam/monitoring/logging_config.py

Structured JSON logging configuration for the AEAM system.

Configures structlog to emit one JSON object per log line, enriched with
timestamp, log level, and AEAM-specific context fields (incident_id, agent,
action). Integrates with the standard ``logging`` module so that third-party
libraries (SQLAlchemy, uvicorn, prophet) emit JSON-compatible output too.

Call :func:`configure_logging` once at application startup (e.g. in
``aeam/main.py``) before any log statements are made. Then use
:func:`get_logger` throughout the codebase in place of
``logging.getLogger()``.

Dependencies:
- structlog: pip install structlog
"""

from __future__ import annotations

import logging
import logging.config
import sys
from typing import Any

import structlog

# ============================================================
# Constants
# ============================================================

# AEAM context fields that are always bound to the logger when provided.
AEAM_CONTEXT_FIELDS: tuple[str, ...] = (
    "incident_id",
    "agent",
    "action",
)


# ============================================================
# Configuration
# ============================================================

def configure_logging(log_level: str = "INFO") -> None:
    """
    Configure structlog and the stdlib ``logging`` module for JSON output.

    Must be called once at application startup before any log statements.
    Subsequent calls are idempotent (structlog skips re-configuration if
    already configured).

    The pipeline adds, in order:

    1. ``merge_contextvars``   — merges thread-local / async context variables.
    2. ``add_log_level``       — adds ``"level"`` field.
    3. ``add_logger_name``     — adds ``"logger"`` field from the logger name.
    4. ``TimeStamper``         — adds ``"timestamp"`` in ISO 8601 UTC format.
    5. ``StackInfoRenderer``   — renders stack info if present.
    6. ``format_exc_info``     — formats exception tracebacks as strings.
    7. ``JSONRenderer``        — serialises the event dict to a JSON string.

    All stdlib ``logging`` records are routed through structlog's foreign
    pre-chain so that third-party library logs are also JSON-formatted.

    Args:
        log_level: Minimum log level to emit. One of ``"DEBUG"``,
                   ``"INFO"``, ``"WARNING"``, ``"ERROR"``, ``"CRITICAL"``.
                   Defaults to ``"INFO"``.

    Example::

        # In aeam/main.py, before anything else:
        from aeam.monitoring.logging_config import configure_logging
        configure_logging(log_level="INFO")
    """
    level: int = getattr(logging, log_level.upper(), logging.INFO)

    # Shared processors applied to both structlog and stdlib records.
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    # Configure stdlib logging to route through structlog.
    logging.config.dictConfig({
        "version":                  1,
        "disable_existing_loggers": False,
        "formatters": {
            "json": {
                "()": structlog.stdlib.ProcessorFormatter,
                "processors": [
                    structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                    structlog.processors.JSONRenderer(),
                ],
                "foreign_pre_chain": shared_processors,
            }
        },
        "handlers": {
            "console": {
                "class":     "logging.StreamHandler",
                "stream":    "ext://sys.stdout",
                "formatter": "json",
            }
        },
        "root": {
            "handlers": ["console"],
            "level":    log_level.upper(),
        },
        "loggers": {
            # Suppress overly verbose third-party loggers.
            "uvicorn.access":  {"level": "WARNING", "propagate": True},
            "sqlalchemy.engine": {"level": "WARNING", "propagate": True},
            "prophet":         {"level": "WARNING", "propagate": True},
            "cmdstanpy":       {"level": "WARNING", "propagate": True},
        },
    })

    structlog.configure(
        processors=shared_processors + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


# ============================================================
# Public API
# ============================================================

def get_logger(name: str, **initial_context: Any) -> structlog.BoundLogger:
    """
    Return a structlog bound logger for ``name``.

    The returned logger emits JSON lines via the pipeline configured by
    :func:`configure_logging`. Optional ``initial_context`` keyword
    arguments are permanently bound to every log record emitted by the
    returned logger (e.g. ``agent="rag"``).

    AEAM context fields that can be bound:
    - ``incident_id`` — unique identifier for the active incident.
    - ``agent``       — name of the emitting agent (e.g. ``"rag"``,
      ``"forecast"``, ``"orchestrator"``).
    - ``action``      — specific operation being performed.

    Additional arbitrary fields may also be bound.

    Args:
        name:             Logger name, conventionally the module's
                          ``__name__`` (e.g. ``"aeam.agents.rag.rag_agent"``).
        **initial_context: Key-value pairs permanently bound to the logger.

    Returns:
        A :class:`structlog.BoundLogger` with ``initial_context`` pre-bound.

    Example::

        from aeam.monitoring.logging_config import get_logger

        logger = get_logger(__name__, agent="orchestrator")
        logger.info("Investigation started", incident_id="INC-42", depth=1)
        # → {"level": "info", "logger": "aeam.agents.orchestrator...",
        #    "agent": "orchestrator", "incident_id": "INC-42",
        #    "depth": 1, "event": "Investigation started",
        #    "timestamp": "2024-01-15T14:32:00.123456Z"}

        # Bind additional context mid-session.
        bound = logger.bind(incident_id="INC-42", action="rag_investigate")
        bound.warning("Low confidence", confidence=0.45)
    """
    log = structlog.get_logger(name)
    if initial_context:
        log = log.bind(**initial_context)
    return log