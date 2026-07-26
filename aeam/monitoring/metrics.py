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


retrieval_chunks_total: Counter = Counter(
    "retrieval_chunks_total",
    "Total evidence chunks returned by retrieval, by stage",
    ["stage"],
)
"""
Counter accumulating retrieval VOLUME (Phase E11) — the third cost axis
alongside LLM spend and action executions. Published through the SAME
Prometheus pipeline as every other metric (OBS-1: no second metrics store).

Labels:
    stage: Where the chunks were counted, e.g. ``"investigation"`` (chunks
           handed to an incident's RAG evidence stage).

Usage::

    retrieval_chunks_total.labels(stage="investigation").inc(5)
"""

action_executions_total: Counter = Counter(
    "action_executions_total",
    "Total action executions attributed to an investigation, by outcome",
    ["outcome"],
)
"""
Counter of action executions attributed to an investigation (Phase E11).

Distinct from ``action_success_total``/``action_failure_total``, which
:class:`~aeam.agents.action.action_agent.ActionAgent` increments per action
TYPE at the execution boundary. This counter is the cost-surface view: how
many actions an investigation caused, regardless of which integration ran
them, so the cost roll-up and the action-outcome metrics never have to be
reconciled against each other.

Labels:
    outcome: ``"executed"``, ``"skipped"``, or ``"withheld"`` (held back by
             the Phase E9 human-approval gate).
"""


class IncidentCostScope:
    """
    Per-incident cost accumulator (Phase E11, OBS-2 / EXPL-5).

    E8 gave the platform global LLM token/cost counters. A global counter
    cannot answer "what did *this* incident cost", which is exactly the
    question the cost surface must answer. This class is the missing
    per-incident attribution, and it is deliberately the thinnest thing that
    works: a thread-local accumulator opened by
    ``Orchestrator.handle_event`` and read once at finalization.

    Thread-local rather than global because ``handle_event`` is reentrant
    since Phase E2 — two concurrent investigations must accumulate into
    separate buckets, exactly as they already keep separate
    :class:`~aeam.agents.orchestrator.incident_context.IncidentContext`
    state. Every LLM call an investigation makes happens on that
    investigation's own thread (RAGAgent/policy extraction/query expansion
    are all called synchronously from the orchestrator's stack), so the
    thread is the correct attribution boundary.

    Recording outside any open scope is a silent no-op — a background
    worker's LLM call is not attributable to an incident, and inventing an
    attribution for it would be dishonest. Those calls are still counted by
    the global E8 counters, which is where they belong.
    """

    def __init__(self) -> None:
        self._local = threading.local()

    def start(self, incident_id: str) -> None:
        """Open a fresh accumulation scope for ``incident_id`` on this thread."""
        self._local.scope = {
            "incident_id": incident_id,
            "llm_calls": 0,
            "llm_prompt_tokens": 0,
            "llm_completion_tokens": 0,
            "llm_cost_usd": 0.0,
            "retrieval_chunks": 0,
            "actions_executed": 0,
            "actions_skipped": 0,
            "actions_withheld": 0,
        }

    def _scope(self) -> dict[str, Any] | None:
        return getattr(self._local, "scope", None)

    def record_llm(
        self,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cost_usd: float = 0.0,
    ) -> None:
        """
        Attribute one LLM call's usage to the open scope.

        Token counts are whatever the provider actually reported — this
        never estimates. A provider that omits usage data contributes a
        call with zero tokens, which the cost block discloses rather than
        filling in with a guess.
        """
        scope = self._scope()
        if scope is None:
            return
        scope["llm_calls"] += 1
        scope["llm_prompt_tokens"] += int(prompt_tokens or 0)
        scope["llm_completion_tokens"] += int(completion_tokens or 0)
        scope["llm_cost_usd"] += float(cost_usd or 0.0)

    def record_retrieval(self, chunk_count: int) -> None:
        """Attribute ``chunk_count`` retrieved evidence chunks to the open scope."""
        scope = self._scope()
        if scope is None:
            return
        count = max(0, int(chunk_count or 0))
        scope["retrieval_chunks"] += count
        if count:
            retrieval_chunks_total.labels(stage="investigation").inc(count)

    def record_action(self, outcome: str) -> None:
        """Attribute one action ``outcome`` (executed/skipped/withheld) to the open scope."""
        scope = self._scope()
        key = {
            "executed": "actions_executed",
            "skipped": "actions_skipped",
            "withheld": "actions_withheld",
        }.get(outcome)
        if key is None:
            return
        action_executions_total.labels(outcome=outcome).inc()
        if scope is None:
            return
        scope[key] += 1

    def snapshot(self) -> dict[str, Any] | None:
        """
        The open scope's accumulated totals, or ``None`` when no scope is
        open on this thread. Costs are rounded to 6 decimal places — enough
        precision for per-1K-token rates without implying invoice accuracy.
        """
        scope = self._scope()
        if scope is None:
            return None
        return {
            **scope,
            "llm_cost_usd": round(scope["llm_cost_usd"], 6),
            "llm_total_tokens": scope["llm_prompt_tokens"] + scope["llm_completion_tokens"],
        }

    def clear(self) -> None:
        """Close this thread's scope. Safe to call when none is open."""
        if hasattr(self._local, "scope"):
            del self._local.scope


incident_cost_scope: IncidentCostScope = IncidentCostScope()
"""Module-level shared :class:`IncidentCostScope` (Phase E11). Import this,
never construct a second instance — one accumulator, one attribution rule.
Mirrors the :data:`heartbeat_tracker` singleton pattern above."""


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