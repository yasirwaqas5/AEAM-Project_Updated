"""
aeam/monitoring/metrics.py

Prometheus metrics definitions for the AEAM system.

Exposes counters, histograms, and gauges for incident lifecycle tracking,
agent execution timing, and action outcomes. All metrics are module-level
singletons registered with the default Prometheus registry at import time.

Helper functions :func:`start_timer` and :func:`end_timer` provide a
simple API for recording durations without requiring callers to manage
``time.time()`` directly.

Dependencies:
- prometheus-client: pip install prometheus-client
"""

from __future__ import annotations

import logging
import time
from typing import Any

from prometheus_client import Counter, Gauge, Histogram

logger = logging.getLogger(__name__)

# ============================================================
# Metric definitions
# ============================================================

incidents_total: Counter = Counter(
    "incidents_total",
    "Total incidents processed",
    ["event_type", "severity"],
)
"""
Counter incremented once per processed incident.

Labels:
    event_type: The anomaly event type (e.g. ``"KPI_ANOMALY"``).
    severity:   Incident severity level (e.g. ``"CRITICAL"``, ``"HIGH"``).

Usage::

    incidents_total.labels(event_type="KPI_ANOMALY", severity="HIGH").inc()
"""

investigation_duration: Histogram = Histogram(
    "investigation_duration_seconds",
    "Time taken for investigation",
)
"""
Histogram recording the wall-clock duration of a full investigation cycle
from ``handle_event()`` to ``finalize_incident()``.

Usage::

    t = start_timer()
    # ... investigation ...
    end_timer(investigation_duration, t)
"""

active_incidents: Gauge = Gauge(
    "active_incidents",
    "Number of active incidents",
)
"""
Gauge tracking the number of incidents currently being investigated.
Incremented when an investigation starts; decremented when it finalises.

Usage::

    active_incidents.inc()   # investigation starts
    active_incidents.dec()   # investigation ends
"""

agent_execution_time: Histogram = Histogram(
    "agent_execution_time_seconds",
    "Execution time per agent",
    ["agent"],
)
"""
Histogram recording per-agent execution duration.

Labels:
    agent: Agent name (e.g. ``"rag"``, ``"forecast"``, ``"report"``).

Usage::

    t = start_timer()
    result = rag_agent.investigate(event, memory)
    end_timer(agent_execution_time.labels(agent="rag"), t)
"""

action_success_total: Counter = Counter(
    "action_success_total",
    "Successful actions",
    ["action_type"],
)
"""
Counter incremented on each successfully completed action.

Labels:
    action_type: Registry key of the action (e.g. ``"jira"``, ``"slack"``).

Usage::

    action_success_total.labels(action_type="jira").inc()
"""

action_failure_total: Counter = Counter(
    "action_failure_total",
    "Failed actions",
    ["action_type"],
)
"""
Counter incremented on each failed action (after all retries exhausted).

Labels:
    action_type: Registry key of the action (e.g. ``"jira"``, ``"slack"``).

Usage::

    action_failure_total.labels(action_type="jira").inc()
"""


# ============================================================
# Helper functions
# ============================================================

def start_timer() -> float:
    """
    Record the current wall-clock time as a timer start point.

    Returns:
        Current time as a float (seconds since the Unix epoch),
        suitable for passing to :func:`end_timer`.

    Example::

        t = start_timer()
        do_work()
        end_timer(investigation_duration, t)
    """
    return time.time()


def end_timer(metric: Histogram | Any, started_at: float) -> float:
    """
    Observe the elapsed time since ``started_at`` on ``metric``.

    Calculates ``elapsed = time.time() - started_at`` and calls
    ``metric.observe(elapsed)``. Safe to call on any Prometheus
    ``Histogram`` or pre-labelled histogram child
    (e.g. ``agent_execution_time.labels(agent="rag")``).

    Args:
        metric:     A :class:`prometheus_client.Histogram` instance or
                    a labelled child returned by ``.labels(...)``.
        started_at: Float timestamp returned by :func:`start_timer`.

    Returns:
        Elapsed time in seconds (float).

    Raises:
        AttributeError: If ``metric`` does not expose an ``observe``
                        method.

    Example::

        t = start_timer()
        result = forecast_agent.analyze("sales", 42_000.0)
        elapsed = end_timer(agent_execution_time.labels(agent="forecast"), t)
        logger.debug("Forecast took %.3fs", elapsed)
    """
    elapsed: float = time.time() - started_at
    metric.observe(elapsed)
    logger.debug("end_timer | elapsed=%.4fs", elapsed)
    return elapsed