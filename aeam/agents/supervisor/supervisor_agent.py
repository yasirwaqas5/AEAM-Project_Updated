"""
aeam/agents/supervisor/supervisor_agent.py

The Supervisor Agent (Phase F6 — Agent Mesh Formalization).

AEAM has run an agent mesh since Phase 2, but no member of it ever looked at
the mesh *as a whole*. Individual agents report their own metrics, ``/health``
checks two background threads, and D3/E11 summarise investigation quality —
but nothing answered "is the mesh healthy, and is any agent behaving
oddly?", so that judgement lived in an operator's head across four
dashboards. This agent makes it an accountable, inspectable artifact.

**It observes. It has no coordination authority whatsoever.**
--------------------------------------------------------------
ARCH-1 gives AEAM exactly one coordinator, the Orchestrator, and this phase
does not touch it. The Supervisor is a monitor, not a second orchestrator,
and that is enforced structurally rather than by convention:

* it imports no ``Orchestrator``, ``ActionAgent``, ``PlanningAgent``,
  ``EventBus``, ``RuleEngine``, or LLM client — so it cannot reach anything
  that dispatches, executes, or decides;
* it has no ``handle_event``, ``execute``, ``dispatch``, ``coordinate``,
  ``restart``, or ``plan`` method — the absence is the enforcement, exactly
  as it is on ``LearningAgent`` (F2) and ``PolicyAgent`` (F3);
* its only inputs are read-only telemetry providers and its only output is
  a report. It writes nothing: no incident, no plan, no finding, no
  approval, no agent state.

It may observe, summarise, detect behaviour anomalies, and **recommend**
escalation. Recommending is where its authority ends — an operator acts on
a recommendation through the existing gated surfaces (AGENT-5). An advisory
agent whose recommendation restarts a worker is not advisory; it is a
supervisor with a confirmation dialog.

Evidence, never invention (OBS-4)
---------------------------------
Every figure comes from telemetry that already existed before this phase:

* **E7 heartbeats** — :class:`~aeam.monitoring.metrics.HeartbeatTracker`,
  the same tracker ``/health`` reads;
* **E11 metrics** — the existing ``agent_execution_time`` /
  ``action_success_total`` / ``action_failure_total`` collectors, read from
  the objects themselves rather than by re-scraping ``/metrics``;
* **roster participation** — the E1 ``agent_roster``, i.e. the agents this
  process actually constructed;
* **investigation quality** — the D3/E11 observability summary, passed in
  already-computed.

No new monitoring infrastructure is introduced, no new table, no new
collector, and no polling loop. The report is computed on read.

Every issue the Supervisor raises discloses its **affected agent**, the
**observed metrics** behind it, and a plain **reason**. A health score is
published only when at least one component is genuinely computable, and it
states its own formula and which components fed it; when nothing is
computable it says so instead of publishing a number
(``available: false`` + ``reason``).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from aeam.monitoring.metrics import (
    action_failure_total,
    action_success_total,
    agent_execution_time,
    end_timer,
    heartbeat_tracker,
    start_timer,
)
from aeam.monitoring.tracing import investigation_span

logger = logging.getLogger(__name__)

#: The name this agent registers under in the E1 ``agent_roster``, the E7
#: heartbeat tracker, and its ``agent_execution_time`` label.
AGENT_NAME: str = "supervisor"

#: Roster members whose heartbeat proves a LIVE THREAD. A stale heartbeat
#: here means the thread died or wedged, which is a real fault — the same
#: reading ``/health`` already applies to these two and only these two.
_THREAD_WORKERS: frozenset[str] = frozenset({"monitor", "ingestion"})

#: Roster members that are request-scoped: they run when the Orchestrator
#: calls them, so heartbeat AGE means "time since last use", not liveness.
#: An old heartbeat on these is not a fault and must never be reported as
#: one — a quiet platform is not a broken platform.
_REQUEST_SCOPED: frozenset[str] = frozenset({"planning", "supervisor", "report", "rag", "kpi"})

#: Hardening — roster name → the ``agent_execution_time`` label(s) the
#: platform ACTUALLY records for that member.
#:
#: The roster (Phase E1) names agents; the E11 histogram is labelled by
#: STAGE. For most members the two coincide, but three did not, and the
#: Supervisor's exact-label lookup therefore reported them as never having
#: executed *immediately after they executed* — verified at runtime: a live
#: investigation recorded ``agent="action:jira"``, ``"action:slack"``,
#: ``"action:email"`` and ``"kpi_analysis"``, while the Supervisor answered
#: ``observed=false`` for roster members ``action`` and ``kpi`` and scored
#: ``agent_activity`` at 4/8 instead of 6/8.
#:
#: That is a false negative presented as observed fact, which is precisely
#: what this agent's "evidence, never invention" contract forbids. Resolving
#: the alias is not invention: it reads the SAME existing series, and the
#: label it resolved to is disclosed in the response so the reading stays
#: traceable to its source.
_EXECUTION_LABEL_ALIASES: dict[str, tuple[str, ...]] = {
    # KPIAgent's investigation pass is timed under its stage name.
    "kpi": ("kpi_analysis",),
    # The Orchestrator times its own stages, not itself. `decision` is the
    # one stage no other component records and which runs once per
    # investigation depth, so it is the Orchestrator's own execution count.
    "orchestrator": ("decision",),
}

#: Roster name → label PREFIX, for members recorded as one series per
#: sub-type. ActionAgent is labelled ``action:<registry_type>``
#: (``action:jira``, ``action:slack``, ``action:email``), so its execution
#: count is the sum across those series.
_EXECUTION_LABEL_PREFIXES: dict[str, str] = {
    "action": "action:",
}

#: The observability-summary keys that describe one roster agent's
#: participation across investigations, so a "this agent contributed to
#: nothing" observation cites a measured rate rather than an impression.
_PARTICIPATION_METRIC: dict[str, str] = {
    "memory": "memory_hit_rate",
    "policy": "policy_hit_rate",
    "rag": "retrieval_success_rate",
    "cross_dataset": "cross_dataset_usage_rate",
    "adaptive": "adaptive_detection_usage_rate",
}

#: Issue severities. Ordered, so a report can be sorted worst-first without
#: a second vocabulary.
_SEVERITY_ORDER: dict[str, int] = {"critical": 0, "warning": 1, "info": 2}


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _counter_value(collector: Any, label_key: str, label_value: str) -> float | None:
    """
    One labelled counter/histogram-count value, read from the live collector.

    Reads the metric OBJECT rather than re-scraping ``/metrics``: the
    collector is already in this process, and parsing the exposition format
    back out of a text endpoint would be a second, lossier path to the same
    number.

    Returns ``None`` when the series does not exist — which is meaningfully
    different from zero. "This agent has never been invoked" and "this agent
    has been invoked zero times since the metric appeared" are the same
    thing here, but "this metric is not instrumented at all" is not, and the
    caller needs to be able to tell.
    """
    try:
        for metric in collector.collect():
            for sample in metric.samples:
                if sample.labels.get(label_key) != label_value:
                    continue
                if sample.name.endswith(("_count", "_total")):
                    return float(sample.value)
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("supervisor | metric read failed | %s=%s | %s", label_key, label_value, exc)
        return None


def _counter_prefix_total(collector: Any, label_key: str, prefix: str) -> float | None:
    """Sum every ``_count``/``_total`` series whose label starts with ``prefix``.

    Hardening: ActionAgent is timed as one series per registry type
    (``action:jira``, ``action:slack``, ``action:email``), so its roster-level
    execution count is the sum across them. Returns ``None`` when no matching
    series exists, preserving the "not instrumented" vs "measured zero"
    distinction the exact-match reader above is careful about.
    """
    try:
        total = 0.0
        seen = False
        for metric in collector.collect():
            for sample in metric.samples:
                label = sample.labels.get(label_key)
                if not isinstance(label, str) or not label.startswith(prefix):
                    continue
                if sample.name.endswith(("_count", "_total")):
                    total += float(sample.value)
                    seen = True
        return total if seen else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("supervisor | prefix metric read failed | %s* | %s", prefix, exc)
        return None


def _resolve_executions(name: str) -> tuple[float | None, str]:
    """This roster member's execution count and the label it was read from.

    Resolution order, so the common case stays a plain exact-label read:

    1. the roster name itself (``rag``, ``report``, ``planning``,
       ``supervisor``, ``forecast`` all record under their own name);
    2. a declared prefix (``action`` → ``action:*``, summed);
    3. a declared alias (``kpi`` → ``kpi_analysis``, ``orchestrator`` →
       ``decision``).

    The returned label is what the caller discloses, so a reader can always
    go and check the series the number came from.
    """
    exact = _counter_value(agent_execution_time, "agent", name)
    if exact is not None:
        return exact, name

    prefix = _EXECUTION_LABEL_PREFIXES.get(name)
    if prefix:
        total = _counter_prefix_total(agent_execution_time, "agent", prefix)
        if total is not None:
            return total, f"{prefix}*"

    for alias in _EXECUTION_LABEL_ALIASES.get(name, ()):
        value = _counter_value(agent_execution_time, "agent", alias)
        if value is not None:
            return value, alias

    # Nothing recorded anywhere. Report the label a reader should look for,
    # which for an aliased member is the alias rather than the roster name.
    candidates = (
        [f"{prefix}*"] if prefix else list(_EXECUTION_LABEL_ALIASES.get(name, ())) or [name]
    )
    return None, candidates[0]


def _collector_total(collector: Any) -> float | None:
    """The summed value of every ``_total``/``_count`` series on a collector."""
    try:
        total = 0.0
        seen = False
        for metric in collector.collect():
            for sample in metric.samples:
                if sample.name.endswith(("_count", "_total")):
                    total += float(sample.value)
                    seen = True
        return total if seen else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("supervisor | collector total read failed | %s", exc)
        return None


class SupervisorAgent:
    """
    Advisory, observation-only oversight of the agent mesh.

    Args:
        settings:              Application settings. Only
                               ``HEARTBEAT_STALE_SECONDS`` is read, so the
                               Supervisor and ``/health`` can never disagree
                               about what "stale" means.
        roster_provider:       Zero-argument callable returning the E1
                               ``agent_roster`` — the agents this process
                               actually constructed. A callable rather than a
                               list so the report reflects the roster at read
                               time.
        observability_provider: Optional zero-argument callable returning the
                               already-computed D3/E11 observability summary,
                               or ``None``. The Supervisor never computes
                               that summary itself (ENG-6: one observability
                               engine) and never reads the incidents table.
        tracker:               Optional :class:`HeartbeatTracker` override for
                               tests. Defaults to the shared E7 singleton —
                               the same instance ``/health`` reads.

    Raises:
        ValueError: If ``roster_provider`` is not callable.
    """

    def __init__(
        self,
        settings: Any,
        roster_provider: Callable[[], list[str]],
        observability_provider: Callable[[], dict[str, Any] | None] | None = None,
        tracker: Any | None = None,
    ) -> None:
        if not callable(roster_provider):
            raise ValueError("roster_provider must be a zero-argument callable.")
        self._settings = settings
        self._roster_provider = roster_provider
        self._observability_provider = observability_provider
        self._tracker = tracker or heartbeat_tracker

    # ------------------------------------------------------------------
    # Public API — the ONLY public method that produces anything
    # ------------------------------------------------------------------

    def observe(self) -> dict[str, Any]:
        """
        Build the mesh-health and behaviour-anomaly report.

        Reads telemetry, computes, and returns. Nothing is dispatched,
        executed, persisted, or coordinated — this method's entire effect on
        the platform is its own heartbeat and its own execution-time metric,
        which is the minimum required for the Supervisor to be as observable
        as the agents it observes.

        Returns:
            A dict of the same shape every time::

                {
                    "generated_at": str,
                    "roster": [str, ...],
                    "roster_source": str,
                    "agents": [ {...}, ... ],
                    "issues": [ {...}, ... ],
                    "mesh_health": {...},
                    "recommended_escalations": [ {...}, ... ],
                    "advisory_contract": {...},
                }

            ``issues`` is sorted worst-first. Every entry carries the
            affected ``agent``, an ``evidence`` block of the observed
            metrics, and a plain ``reason``. ``mesh_health`` publishes a
            score only when at least one component is computable, and
            discloses its formula and components either way.
        """
        started = start_timer()
        try:
            with investigation_span(f"agent.{AGENT_NAME}"):
                return self._observe_unsafe()
        finally:
            end_timer(agent_execution_time.labels(agent=AGENT_NAME), started)
            self._tracker.record(AGENT_NAME)

    # ------------------------------------------------------------------
    # Internal — observation only
    # ------------------------------------------------------------------

    def _observe_unsafe(self) -> dict[str, Any]:
        roster = self._roster()
        observability = self._observability()
        stale_after = float(getattr(self._settings, "HEARTBEAT_STALE_SECONDS", 120) or 120)

        agents = [self._agent_view(name, observability, stale_after) for name in roster]
        issues = self._detect_issues(agents, observability)
        issues.sort(key=lambda i: (_SEVERITY_ORDER.get(i["severity"], 9), i["agent"]))

        return {
            "generated_at": _now_iso(),
            "roster": roster,
            "roster_source": "container.agent_roster (Phase E1) — agents this process constructed",
            "agents": agents,
            "issues": issues,
            "mesh_health": self._mesh_health(agents, observability, issues),
            "recommended_escalations": [
                {
                    "agent": issue["agent"],
                    "severity": issue["severity"],
                    "recommendation": issue["recommended_escalation"],
                    "reason": issue["reason"],
                }
                for issue in issues
                if issue["severity"] in ("critical", "warning")
            ],
            "advisory_contract": {
                "observes_only": True,
                "coordinates": False,
                "executes_actions": False,
                "modifies_incidents": False,
                "modifies_plans": False,
                "restarts_agents": False,
                "note": (
                    "The Supervisor reports and recommends. The Orchestrator remains "
                    "the single coordinator (ARCH-1); acting on a recommendation is an "
                    "operator decision taken through the existing gated surfaces."
                ),
            },
        }

    def _roster(self) -> list[str]:
        """The E1 roster, read at report time. Never fabricated: a provider
        failure yields an empty roster and a stated reason downstream, not a
        guessed agent list."""
        try:
            return sorted(str(name) for name in (self._roster_provider() or []))
        except Exception as exc:  # noqa: BLE001
            logger.warning("supervisor | roster read failed | %s", exc)
            return []

    def _observability(self) -> dict[str, Any] | None:
        if self._observability_provider is None:
            return None
        try:
            summary = self._observability_provider()
        except Exception as exc:  # noqa: BLE001
            logger.warning("supervisor | observability read failed | %s", exc)
            return None
        return summary if isinstance(summary, dict) else None

    def _agent_view(
        self, name: str, observability: dict[str, Any] | None, stale_after: float
    ) -> dict[str, Any]:
        """One roster member's observed state, with every source named."""
        age = self._tracker.age_seconds(name)
        last_seen = self._tracker.last_seen_iso(name)
        thread_worker = name in _THREAD_WORKERS

        # A thread worker's heartbeat proves liveness, so an old one is
        # STALE — a real fault. A request-scoped agent only beats when the
        # Orchestrator calls it, so an old heartbeat means the platform has
        # been quiet: reported as IDLE and never counted against health.
        if age is None:
            heartbeat_state = "never_reported"
        elif age <= stale_after:
            heartbeat_state = "live"
        else:
            heartbeat_state = "stale" if thread_worker else "idle"

        executions, execution_label = _resolve_executions(name)

        return {
            "agent": name,
            "registered": True,
            "scope": "thread_worker" if thread_worker else (
                "request_scoped" if name in _REQUEST_SCOPED else "invoked"
            ),
            "heartbeat": {
                "instrumented": age is not None,
                "state": heartbeat_state,
                "age_seconds": round(age, 3) if age is not None else None,
                "last_seen": last_seen,
                "stale_after_seconds": stale_after if thread_worker else None,
                "source": "HeartbeatTracker (Phase E7)",
                "note": (
                    "Heartbeat age proves thread liveness for this agent."
                    if thread_worker
                    else "This agent is request-scoped; heartbeat age is time since "
                         "last invocation, not a liveness signal."
                ),
            },
            "executions": {
                "observed": executions is not None,
                "count": executions,
                # The resolved label is disclosed so the figure stays traceable
                # to the exact series it was read from (see
                # _EXECUTION_LABEL_ALIASES / _EXECUTION_LABEL_PREFIXES).
                "source": (
                    f"agent_execution_time_seconds_count{{agent={execution_label}}} (Phase E11)"
                ),
                "reason": None if executions is not None
                else (
                    f"No agent_execution_time series is recorded under "
                    f"agent={execution_label}."
                ),
            },
            "participation": self._participation(name, observability),
        }

    @staticmethod
    def _participation(name: str, observability: dict[str, Any] | None) -> dict[str, Any]:
        """
        This agent's measured contribution across investigations.

        Read straight out of the D3/E11 summary — the Supervisor does not
        recompute a rate, so its numbers and the Analytics page's are the
        same numbers (ENG-6).
        """
        key = _PARTICIPATION_METRIC.get(name)
        if key is None:
            return {
                "measured": False,
                "reason": (
                    f"No cross-investigation participation metric exists for "
                    f"agent={name!r}; its contribution is not measured per incident."
                ),
            }
        if observability is None:
            return {
                "measured": False,
                "metric": key,
                "reason": "No observability summary was available to this report.",
            }
        entry = observability.get(key)
        if not isinstance(entry, dict) or not entry.get("available"):
            return {
                "measured": False,
                "metric": key,
                "reason": (
                    (entry or {}).get("reason")
                    if isinstance(entry, dict)
                    else f"{key} is not present in the observability summary."
                ),
            }
        return {
            "measured": True,
            "metric": key,
            "rate": entry.get("rate"),
            "consulted": entry.get("consulted"),
            "total_investigations": observability.get("total_investigations"),
            "source": "observability summary (Phase D3/E11)",
        }

    # ------------------------------------------------------------------
    # Behaviour anomalies — each one grounded in an observed number
    # ------------------------------------------------------------------

    def _detect_issues(
        self, agents: list[dict[str, Any]], observability: dict[str, Any] | None
    ) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        if not agents:
            issues.append({
                "agent": "mesh",
                "kind": "empty_roster",
                "severity": "critical",
                "reason": (
                    "No agents are registered in the roster. Either the process "
                    "constructed none, or the roster could not be read."
                ),
                "evidence": {"roster_size": 0, "source": "container.agent_roster"},
                "recommended_escalation": (
                    "Check startup logs for agent construction failures. The Supervisor "
                    "cannot distinguish 'none constructed' from 'roster unreadable'."
                ),
            })
            return issues

        total_investigations = (observability or {}).get("total_investigations")

        for view in agents:
            name = view["agent"]
            heartbeat = view["heartbeat"]

            # 1. A thread worker whose heartbeat went stale: the thread died
            #    or wedged. The same reading /health applies, so the two
            #    surfaces never contradict each other.
            if heartbeat["state"] == "stale":
                issues.append({
                    "agent": name,
                    "kind": "stale_heartbeat",
                    "severity": "critical",
                    "reason": (
                        f"{name} last reported {heartbeat['age_seconds']}s ago, beyond the "
                        f"{heartbeat['stale_after_seconds']}s staleness threshold. Its thread "
                        "has stopped updating its heartbeat — it died or is wedged."
                    ),
                    "evidence": {
                        "heartbeat_age_seconds": heartbeat["age_seconds"],
                        "stale_after_seconds": heartbeat["stale_after_seconds"],
                        "last_seen": heartbeat["last_seen"],
                        "source": heartbeat["source"],
                    },
                    "recommended_escalation": (
                        f"Escalate to an operator to inspect the {name} thread. The "
                        "Supervisor does not restart agents."
                    ),
                })

            # 2. A thread worker registered but never heard from at all.
            elif view["scope"] == "thread_worker" and not heartbeat["instrumented"]:
                issues.append({
                    "agent": name,
                    "kind": "no_heartbeat",
                    "severity": "warning",
                    "reason": (
                        f"{name} is on the roster but has never recorded a heartbeat. It may "
                        "still be starting, or it may have failed before its first loop."
                    ),
                    "evidence": {
                        "heartbeat_age_seconds": None,
                        "last_seen": None,
                        "source": heartbeat["source"],
                    },
                    "recommended_escalation": (
                        f"Re-check shortly. If {name} still has no heartbeat, escalate for "
                        "an operator to inspect startup."
                    ),
                })

            # 3. A roster member that has never executed while the platform
            #    HAS processed investigations. Only reported when there is a
            #    measured baseline to compare against — with zero
            #    investigations, silence is correct behaviour, not an anomaly.
            if (
                view["executions"]["observed"]
                and view["executions"]["count"] == 0
                and isinstance(total_investigations, int)
                and total_investigations > 0
                and view["scope"] != "thread_worker"
            ):
                issues.append({
                    "agent": name,
                    "kind": "silent_agent",
                    "severity": "warning",
                    "reason": (
                        f"{name} is registered but has executed zero times across "
                        f"{total_investigations} recorded investigation(s). It is wired but "
                        "is not being reached."
                    ),
                    "evidence": {
                        "executions": 0,
                        "total_investigations": total_investigations,
                        "source": view["executions"]["source"],
                    },
                    "recommended_escalation": (
                        f"Escalate for review of whether {name} should be participating; the "
                        "Supervisor cannot tell a misconfiguration from a deliberate opt-out."
                    ),
                })

            # 4. A measured participation rate of zero: the agent runs but
            #    contributes nothing. Distinct from (3) — it IS being reached.
            participation = view["participation"]
            if (
                participation.get("measured")
                and participation.get("rate") == 0
                and isinstance(participation.get("consulted"), int)
                and participation["consulted"] > 0
            ):
                issues.append({
                    "agent": name,
                    "kind": "zero_participation",
                    "severity": "info",
                    "reason": (
                        f"{name} was consulted {participation['consulted']} time(s) and "
                        "contributed evidence in none of them. This may be correct (nothing "
                        "to find) or may indicate a retrieval or configuration problem."
                    ),
                    "evidence": {
                        "metric": participation["metric"],
                        "rate": participation["rate"],
                        "consulted": participation["consulted"],
                        "total_investigations": participation.get("total_investigations"),
                        "source": participation["source"],
                    },
                    "recommended_escalation": (
                        f"Informational. Review {name}'s configuration if the rate stays at "
                        "zero as investigation volume grows."
                    ),
                })

        # 5. Action outcomes: more failures than successes is a behaviour
        #    anomaly of the executing edge of the mesh, measured from the
        #    existing E11 counters.
        successes = _collector_total(action_success_total)
        failures = _collector_total(action_failure_total)
        if successes is not None and failures is not None and (successes + failures) > 0:
            failure_rate = failures / (successes + failures)
            if failure_rate > 0.5:
                issues.append({
                    "agent": "action",
                    "kind": "action_failure_rate",
                    "severity": "critical" if failure_rate >= 0.9 else "warning",
                    "reason": (
                        f"{failures:.0f} of {successes + failures:.0f} action dispatches failed "
                        f"({failure_rate:.0%}). The mesh is producing plans it cannot execute."
                    ),
                    "evidence": {
                        "action_success_total": successes,
                        "action_failure_total": failures,
                        "failure_rate": round(failure_rate, 4),
                        "source": "action_success_total / action_failure_total (Phase E11)",
                    },
                    "recommended_escalation": (
                        "Escalate to an operator to check integration credentials and "
                        "connectivity. The Supervisor does not retry or execute actions."
                    ),
                })

        return issues

    # ------------------------------------------------------------------
    # Mesh health — computed from observable components, or withheld
    # ------------------------------------------------------------------

    def _mesh_health(
        self,
        agents: list[dict[str, Any]],
        observability: dict[str, Any] | None,
        issues: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """
        The mesh-health score, or an honest statement that none is computable.

        Three components, each included ONLY when its inputs are genuinely
        observable, and each reported with the evidence behind it:

        * ``heartbeat_health`` — the fraction of thread workers whose
          heartbeat is live. Omitted when the roster has no thread workers
          (a Sheets-less, monitor-disabled deployment is not unhealthy for
          having nothing to heartbeat).
        * ``agent_activity`` — the fraction of roster agents with at least
          one recorded execution. Omitted when the platform has processed no
          investigations, because silence is then correct.
        * ``investigation_health`` — D3/E11's own ``overall_ai_health``,
          reused rather than recomputed.

        The score is the unweighted mean of the components present. Equal
        weighting is stated rather than tuned: any weighting would be an
        assertion about relative importance that the evidence does not
        support (PHIL-1).

        With no component computable, ``available`` is ``false`` and the
        reason says which inputs were missing — never a placeholder number.
        """
        components: dict[str, dict[str, Any]] = {}

        thread_workers = [a for a in agents if a["scope"] == "thread_worker"]
        if thread_workers:
            live = sum(1 for a in thread_workers if a["heartbeat"]["state"] == "live")
            components["heartbeat_health"] = {
                "value": round(live / len(thread_workers), 4),
                "live": live,
                "total": len(thread_workers),
                "evidence": "HeartbeatTracker age vs HEARTBEAT_STALE_SECONDS (Phase E7)",
            }

        total_investigations = (observability or {}).get("total_investigations")
        if isinstance(total_investigations, int) and total_investigations > 0 and agents:
            active = sum(
                1 for a in agents
                if a["executions"]["observed"] and (a["executions"]["count"] or 0) > 0
            )
            components["agent_activity"] = {
                "value": round(active / len(agents), 4),
                "active": active,
                "total": len(agents),
                "total_investigations": total_investigations,
                "evidence": "agent_execution_time_seconds_count per roster agent (Phase E11)",
            }

        health_entry = (observability or {}).get("overall_ai_health")
        if isinstance(health_entry, dict) and health_entry.get("available"):
            score = health_entry.get("score")
            if isinstance(score, (int, float)):
                components["investigation_health"] = {
                    "value": round(float(score), 4),
                    "evidence": "overall_ai_health from the observability summary (Phase D3/E11)",
                }

        if not components:
            return {
                "available": False,
                "score": None,
                "components": {},
                "formula": None,
                "issue_counts": self._issue_counts(issues),
                "reason": (
                    "No health component is computable: the roster contains no thread "
                    "workers to heartbeat, no investigations have been recorded, and no "
                    "observability summary was available. A score is withheld rather "
                    "than estimated."
                ),
            }

        values = [c["value"] for c in components.values()]
        return {
            "available": True,
            "score": round(sum(values) / len(values), 4),
            "components": components,
            "formula": (
                "unweighted mean of the "
                f"{len(components)} computable component(s): "
                f"{', '.join(sorted(components))}. Components with unobservable inputs "
                "are omitted, never defaulted to zero."
            ),
            "issue_counts": self._issue_counts(issues),
            "reason": None,
        }

    @staticmethod
    def _issue_counts(issues: list[dict[str, Any]]) -> dict[str, int]:
        counts = {"critical": 0, "warning": 0, "info": 0}
        for issue in issues:
            severity = issue.get("severity")
            if severity in counts:
                counts[severity] += 1
        return counts

    # ------------------------------------------------------------------
    # Roster identity
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        """The roster/heartbeat/metric name — see :data:`AGENT_NAME`."""
        return AGENT_NAME

    def __repr__(self) -> str:
        return "SupervisorAgent(observes_only=True)"
