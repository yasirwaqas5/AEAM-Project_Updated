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
import threading
import time
from datetime import datetime, timezone
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

worker_heartbeat_timestamp_seconds: Gauge = Gauge(
    "worker_heartbeat_timestamp_seconds",
    "Unix timestamp of the last heartbeat recorded by a background worker",
    ["worker"],
)
"""
Gauge holding the Unix timestamp of a background worker's most recent
heartbeat (Phase E7, OBS-3/4). Mirrors :class:`HeartbeatTracker`'s
in-process state for Prometheus scraping -- ``GET /health`` reads
:class:`HeartbeatTracker` directly (no Prometheus client-library
internals), this Gauge is the ``/metrics`` view of the same fact.

Labels:
    worker: Worker name, e.g. ``"monitor"``, ``"ingestion"``.

Usage::

    heartbeat_tracker.record("monitor")   # updates both the tracker and this gauge
"""

llm_calls_total: Counter = Counter(
    "llm_calls_total",
    "Total LLM calls, by provider and outcome",
    ["provider", "status"],
)
"""
Counter incremented once per :meth:`~aeam.services.llm_service.LLMService.generate`
invocation (Phase E8, AI-6).

Labels:
    provider: The configured LLM provider (e.g. ``"groq"``), or ``"mock"``
              when the call was served by the mock path (``USE_MOCK_LLM``
              or ``LLM_ENABLED=false``) -- so an operator can tell from
              ``/metrics`` alone whether AEAM is making real calls.
    status:   ``"success"``, ``"failure"``, or ``"mock"``.

Usage::

    llm_calls_total.labels(provider="groq", status="success").inc()
"""

llm_call_duration_seconds: Histogram = Histogram(
    "llm_call_duration_seconds",
    "Wall-clock duration of a real LLM provider call",
    ["provider"],
)
"""
Histogram recording the duration of each REAL (non-mock) LLM call (Phase
E8, AI-3/AI-6). Never observed for mock calls -- those are near-zero and
would only distort the distribution.

Usage::

    t = start_timer()
    response = client.chat.completions.create(...)
    end_timer(llm_call_duration_seconds.labels(provider="groq"), t)
"""

llm_tokens_total: Counter = Counter(
    "llm_tokens_total",
    "Total LLM tokens consumed, by provider and kind",
    ["provider", "kind"],
)
"""
Counter incremented by the token counts a provider's response reports
(Phase E8, AI-6). Best-effort: a provider/SDK version that omits usage
data simply never increments this counter for that call -- never a
fabricated estimate.

Labels:
    provider: The LLM provider (e.g. ``"groq"``).
    kind:     ``"prompt"`` or ``"completion"``.

Usage::

    llm_tokens_total.labels(provider="groq", kind="prompt").inc(142)
"""

llm_cost_usd_total: Counter = Counter(
    "llm_cost_usd_total",
    "Estimated cumulative LLM spend in USD, by provider",
    ["provider"],
)
"""
Counter accumulating estimated USD cost per provider (Phase E8, AI-6),
computed from actual token usage times the operator-configured
``Settings.LLM_COST_PER_1K_{PROMPT,COMPLETION}_TOKENS_USD`` rates. Reports
0 when a rate is left at its honest zero default -- this metric's
semantics are "informational, operator-priced," never an invoiced total.

Usage::

    llm_cost_usd_total.labels(provider="groq").inc(0.0032)
"""


class HeartbeatTracker:
    """
    Thread-safe last-seen tracker for autonomous background workers
    (Phase E7, OBS-3/4).

    A worker (:class:`~aeam.agents.monitor.monitor_agent.MonitorAgent`,
    :class:`~aeam.ingestion.worker.IngestionWorker`) calls :meth:`record`
    once per loop iteration -- whether or not that iteration's cycle
    succeeded, since the heartbeat's job is to prove the THREAD is alive,
    not that its last cycle was error-free (cycle-level failures are
    already logged and metered separately). ``GET /health`` and the
    console StatusBar read :meth:`age_seconds` to distinguish a live
    worker from a silently-dead one -- the exact "a dead thread is
    discovered, not detected" audit gap this phase closes.

    A single module-level instance (:data:`heartbeat_tracker`) is shared
    by every caller, matching the existing pattern for the module-level
    Prometheus metric singletons above.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_seen: dict[str, float] = {}

    def record(self, worker: str) -> None:
        """Record 'now' as ``worker``'s most recent heartbeat."""
        now = time.time()
        with self._lock:
            self._last_seen[worker] = now
        worker_heartbeat_timestamp_seconds.labels(worker=worker).set(now)

    def age_seconds(self, worker: str) -> float | None:
        """Seconds since ``worker``'s last heartbeat, or ``None`` if it has never reported."""
        with self._lock:
            last = self._last_seen.get(worker)
        return (time.time() - last) if last is not None else None

    def last_seen_iso(self, worker: str) -> str | None:
        """UTC ISO-8601 timestamp of ``worker``'s last heartbeat, or ``None``."""
        with self._lock:
            last = self._last_seen.get(worker)
        if last is None:
            return None
        return datetime.fromtimestamp(last, tz=timezone.utc).isoformat()

    def snapshot(self) -> dict[str, float]:
        """Return a shallow copy of every worker's last-seen Unix timestamp."""
        with self._lock:
            return dict(self._last_seen)


heartbeat_tracker: HeartbeatTracker = HeartbeatTracker()
"""Module-level shared :class:`HeartbeatTracker` (Phase E7). Import this,
never construct a second instance -- one tracker, one source of truth."""


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