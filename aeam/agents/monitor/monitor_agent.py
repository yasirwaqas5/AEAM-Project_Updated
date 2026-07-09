"""
aeam/agents/monitor/monitor_agent.py

Monitor Agent — deterministic KPI detection and event creation for AEAM.

The MonitorAgent runs a continuous polling loop, applying deterministic
detection (rule-based and statistical) to KPI observations. When any signals
are detected (1 or more), an immutable Event is created, deduplicated, pushed
to the priority queue, and published via the EventBus.

Constraints:
- No LLM calls.
- Forecasting only via injected ForecastAgent.
- No orchestrator logic.
- No direct database access — metric persistence (Phase 5) is delegated to
  an injected LongTermMemory, the same indirection ForecastAgent already
  uses to read that same data back for training.
- No external API calls.
- All dependencies are injected; no globals.
"""

from __future__ import annotations

import logging
import time
import uuid
from aeam.monitoring.logging_config import get_logger
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

from aeam.agents.kpi.rule_engine import RuleEngine
from aeam.agents.kpi.statistical_detector import StatisticalDetector
from aeam.config.settings import Settings
from aeam.core.deduplication import EventDeduplicator
from aeam.core.event_bus import EventBus
from aeam.core.event_models import Event
from aeam.core.priority_queue import EventPriorityQueue
from aeam.monitoring.metrics import agent_execution_time, end_timer, start_timer
from aeam.pipelines.structured_data_pipeline import StructuredDataPipeline

# Type hint only – actual import will be resolved at runtime
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from aeam.agents.forecast.forecast_agent import ForecastAgent

logger = get_logger(__name__, agent="monitor")


# ---------------------------------------------------------------------------
# KPI row-source protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class KPIRowSource(Protocol):
    """
    Structural protocol for a tabular KPI feed used by :meth:`MonitorAgent._run_cycle`.

    ``aeam.connectors.sheets.SheetsConnector`` already satisfies this protocol
    without any change — this exists so ``MonitorAgent`` depends on an
    abstract data source (mirroring ``ForecastAgent``'s ``HistoricalDataSource``
    pattern) instead of importing a concrete connector directly.
    """

    def fetch_rows(self, sheet_name: str) -> list[dict[str, Any]]:
        """
        Return data rows as a list of dicts keyed by column header.

        Implementations must degrade to an empty list on any failure or when
        disabled — never raise.
        """
        ...


@runtime_checkable
class MetricsSink(Protocol):
    """
    Structural protocol for the metric-persistence half of LongTermMemory.

    ``aeam.memory.long_term.LongTermMemory`` already satisfies this without
    any change — this exists so ``MonitorAgent`` depends on the same narrow
    abstraction ``ForecastAgent`` uses (``HistoricalDataSource``), not the
    concrete DB-backed class.
    """

    def store_metrics(self, metrics: list[dict[str, Any]]) -> None:
        """Persist metric observation dicts (``metric``, ``value``, ``timestamp``)."""
        ...


class MonitorAgent:
    """
    Deterministic Monitor Agent for KPI anomaly detection.

    Orchestrates the detection pipeline for a single KPI observation:
    1. Clean and validate the data via :class:`~aeam.pipelines.structured_data_pipeline.StructuredDataPipeline`.
    2. Apply rule-based thresholds via :class:`~aeam.agents.kpi.rule_engine.RuleEngine`.
    3. Apply statistical detection via :class:`~aeam.agents.kpi.statistical_detector.StatisticalDetector`.
    4. (Phase 5) Apply forecast deviation detection via injected ForecastAgent.
    5. If any signals fire (1 or more), create an immutable :class:`~aeam.core.event_models.Event`.
    6. Deduplicate via :class:`~aeam.core.deduplication.EventDeduplicator`.
    7. Push to :class:`~aeam.core.priority_queue.EventPriorityQueue`.
    8. Publish via :class:`~aeam.core.event_bus.EventBus`.

    The agent contains no LLM calls, no forecasting logic of its own, no
    orchestrator logic, no database writes, and no external API calls.

    Args:
        event_bus:            Internal event dispatcher.
        queue:                Priority queue for confirmed events.
        deduplicator:         Window-based duplicate filter.
        rule_engine:          Threshold-based rule evaluator.
        statistical_detector: Statistical anomaly detector.
        forecast_agent:       Forecast agent for deviation detection (Phase 5).
        pipeline:             Data cleaning and summarization pipeline.
        settings:             Application configuration (provides
                              ``MONITOR_INTERVAL_SECONDS`` and
                              ``MAX_INVESTIGATION_DEPTH``).
        kpi_source:           Optional :class:`KPIRowSource` (e.g. a
                              ``SheetsConnector``) polled once per cycle by
                              :meth:`_run_cycle`. When ``None`` (default),
                              the cycle is a safe no-op tick — matching the
                              agent's prior placeholder behaviour and keeping
                              every existing caller (tests, ``run_simulation.py``)
                              unaffected.
        long_term_memory:     Optional :class:`MetricsSink` (e.g. a
                              ``LongTermMemory`` instance). When provided,
                              :meth:`_run_cycle` persists each cycle's latest
                              observation per metric via ``store_metrics()``,
                              so :class:`ForecastAgent` has real training
                              history to read back. ``None`` (default) skips
                              persistence — unchanged prior behaviour.

    Example::

        agent = MonitorAgent(
            event_bus=bus,
            queue=queue,
            deduplicator=deduplicator,
            rule_engine=rule_engine,
            statistical_detector=StatisticalDetector(window_size=7),
            forecast_agent=forecast_agent,
            pipeline=StructuredDataPipeline(),
            settings=settings,
        )
        agent.start()   # blocks — run in a dedicated thread
    """

    def __init__(
        self,
        event_bus: EventBus,
        queue: EventPriorityQueue,
        deduplicator: EventDeduplicator,
        rule_engine: RuleEngine,
        statistical_detector: StatisticalDetector,
        forecast_agent: 'ForecastAgent',  # injected Phase 5 dependency
        pipeline: StructuredDataPipeline,
        settings: Settings,
        kpi_source: KPIRowSource | None = None,
        long_term_memory: MetricsSink | None = None,
    ) -> None:
        self._bus = event_bus
        self._queue = queue
        self._deduplicator = deduplicator
        self._rule_engine = rule_engine
        self._detector = statistical_detector
        self._forecast = forecast_agent
        self._pipeline = pipeline
        self._settings = settings
        self._kpi_source = kpi_source
        self._ltm = long_term_memory
        # Worksheet tab name derived from "SHEET_RANGE" (e.g. "Sheet1!A2:C10"
        # -> "Sheet1"), matching the range operators already configured for
        # the live KPI feed. Falls back to "Sheet1" if unset.
        sheet_range = getattr(settings, "SHEET_RANGE", "") or ""
        self._kpi_sheet_name = sheet_range.split("!")[0] if "!" in sheet_range else "Sheet1"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """
        Begin the continuous monitoring loop.

        Sleeps for ``MONITOR_INTERVAL_SECONDS`` between cycles. This method
        blocks indefinitely; run it in a dedicated thread or process.

        Override :meth:`_run_cycle` to inject KPI observations in subclasses
        or during testing.

        The loop catches and logs all unhandled exceptions within a cycle so
        that a single bad cycle never kills the monitoring thread.

        Example::

            import threading
            t = threading.Thread(target=agent.start, daemon=True)
            t.start()
        """
        logger.info(
            "MonitorAgent starting | interval=%ss",
            self._settings.MONITOR_INTERVAL_SECONDS,
        )
        while True:
            try:
                self._run_cycle()
            except Exception as exc:  # noqa: BLE001
                logger.error("MonitorAgent cycle error: %s", exc, exc_info=True)
            time.sleep(self._settings.MONITOR_INTERVAL_SECONDS)

    def process_kpi(
        self,
        metric_name: str,
        current: float,
        previous: float,
        history: list[float],
    ) -> Event | None:
        """
        Run the full detection pipeline for a single KPI observation.

        Steps:
        1. Clean missing values in ``history`` via the pipeline.
        2. Apply rule-based detection (:class:`RuleEngine`).
        3. Apply statistical detection (:class:`StatisticalDetector`).
        4. Apply forecast deviation detection (:class:`ForecastAgent`).
        5. Collect triggered signals.
        6. If any signals fire (1 or more) → anomaly detected → create event.
        7. Check deduplication; discard if duplicate.
        8. Push to priority queue and publish via EventBus.

        Args:
            metric_name: Metric domain identifier (e.g. ``"sales"``).
            current:     Current observed value.
            previous:    Prior period value (used by rule engine).
            history:     Time-ordered list of historical observations.
                         May contain ``float('nan')`` for missing values.

        Returns:
            The created :class:`~aeam.core.event_models.Event` if an anomaly
            was detected and not deduplicated, ``None`` otherwise.

        Raises:
            ValueError: If ``metric_name`` is empty or whitespace-only.
        """
        if not metric_name or not metric_name.strip():
            raise ValueError("metric_name must be a non-empty string.")

        # Step 1: clean history.
        clean_history = self._pipeline.clean_missing(history)

        # Step 2: rule-based detection.
        rule_result = self._rule_engine.evaluate(
            metric_name=metric_name,
            current=current,
            previous=previous,
        )

        # Step 3: statistical detection.
        stat_result = self._detector.detect(
            current=current,
            history=clean_history,
        )

        # Step 4: collect base signals (rule + statistical).
        signals = self._collect_signals(
            current=current,
            rule_result=rule_result,
            stat_result=stat_result,
        )

        # Step 5: forecast detection (Phase 5).
        forecast_result: dict[str, Any] | None = None
        t = start_timer()
        try:
            forecast_result = self._forecast.analyze(
                metric_name=metric_name,
                actual_value=current,
            )
            if forecast_result.get("is_deviation"):
                signals.append("FORECAST")
        except Exception as exc:
            # Forecast failure should not break the pipeline; log and continue.
            logger.error("ForecastAgent.analyze failed: %s", exc, exc_info=True)
        finally:
            end_timer(agent_execution_time.labels(agent="forecast"), t)

        logger.debug(
            "process_kpi | metric=%s | current=%.4f | signals=%s",
            metric_name, current, signals,
        )

        # Step 6: signal evaluation - any signals trigger event creation.
        if len(signals) == 0:
            logger.debug(
                "process_kpi | metric=%s | no signals fired",
                metric_name,
            )
            return None

        logger.info(
            "Anomaly detected | metric=%s | signals=%s | current=%.4f",
            metric_name, signals, current,
        )

        # Step 7: create immutable event.
        event = self.create_event(
            metric_name=metric_name,
            current=current,
            detection_methods=signals,
            rule_details=rule_result,
            stat_details=stat_result,
            forecast_details=forecast_result,
        )

        # Step 8: deduplication.
        if self._deduplicator.is_duplicate(event):
            logger.info(
                "Duplicate event suppressed | metric=%s | event_id=%s",
                metric_name, event.event_id,
            )
            return None

        # Step 9: push and publish.
        self._queue.push(event)
        logger.info(
            "Event queued | event_id=%s | severity=%s | queue_depth=%d",
            event.event_id, event.severity, self._queue.size(),
        )

        try:
            self._bus.publish(event)
        except Exception as exc:  # noqa: BLE001
            # EventBus raises HandlerError aggregating all handler failures.
            # We log but do not abort — the event is already queued.
            logger.error(
                "EventBus publish error | event_id=%s | error=%s",
                event.event_id, exc,
            )

        return event

    def create_event(
        self,
        metric_name: str,
        current: float,
        detection_methods: list[str],
        rule_details: dict[str, Any],
        stat_details: dict[str, Any],
        forecast_details: dict[str, Any] | None = None,
    ) -> Event:
        """
        Construct an immutable :class:`~aeam.core.event_models.Event`.

        Severity is derived from the number of detection signals:
        - ``>= 2`` signals → ``"HIGH"``
        - ``1`` signal    → ``"MEDIUM"``
        - ``0`` signals   → ``"LOW"`` (edge case; callers should not reach here)

        Args:
            metric_name:       The KPI domain this event relates to.
            current:           The anomalous observed value.
            detection_methods: List of signal names that fired (e.g.
                               ``["rule:sales.daily_drop_percent", "statistical:z_score"]``).
            rule_details:      Raw result dict from :class:`RuleEngine`.
            stat_details:      Raw result dict from :class:`StatisticalDetector`.
            forecast_details:  Raw result dict from :class:`ForecastAgent.analyze`
                               (Phase 5), when the forecast step ran successfully.
                               ``None`` if the forecast step raised or was skipped.

        Returns:
            A frozen, immutable :class:`~aeam.core.event_models.Event`.
        """
        severity = self._derive_severity(len(detection_methods))

        metadata: dict[str, Any] = {
            "rule": rule_details,
            "statistical": stat_details,
        }
        if forecast_details is not None:
            metadata["forecast"] = forecast_details

        return Event(
            event_id=str(uuid.uuid4()),
            event_type="KPI_ANOMALY",
            metric=metric_name,
            current_value=current,
            expected_value=stat_details.get("moving_avg"),
            detection_methods=detection_methods,
            severity=severity,
            timestamp=datetime.now(tz=timezone.utc),
            metadata=metadata,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _run_cycle(self) -> None:
        """
        Execute a single monitoring cycle.

        When no ``kpi_source`` was injected, this remains a no-op tick
        (unchanged prior behaviour). When one is configured, pulls the
        latest rows for every metric domain known to the :class:`RuleEngine`
        (``rule_engine.loaded_domains`` — e.g. ``"sales"``, ``"complaints"``,
        ``"inventory"``) and feeds each through :meth:`process_kpi`.

        A metric domain with fewer than two data points, or a data source
        returning no rows (disabled connector, transient failure), is
        silently skipped — never raises, so a single bad cycle never kills
        the monitoring thread (enforced by the caller, :meth:`start`).
        """
        if self._kpi_source is None:
            logger.debug("MonitorAgent cycle tick — no KPI data source configured.")
            return

        rows = self._kpi_source.fetch_rows(self._kpi_sheet_name)
        if not rows:
            logger.debug(
                "MonitorAgent cycle tick — no rows from KPI source | sheet=%s",
                self._kpi_sheet_name,
            )
            return

        for metric_name in self._rule_engine.loaded_domains:
            series = self._extract_series(rows, metric_name)
            if len(series) < 2:
                continue

            current = series[-1]
            history = series[:-1]
            previous = history[-1]

            if self._ltm is not None:
                try:
                    self._ltm.store_metrics([{
                        "metric": metric_name,
                        "value": current,
                        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
                    }])
                except Exception as exc:  # noqa: BLE001
                    logger.error(
                        "MonitorAgent cycle | metric=%s | store_metrics failed: %s",
                        metric_name, exc, exc_info=True,
                    )

            try:
                self.process_kpi(
                    metric_name=metric_name,
                    current=current,
                    previous=previous,
                    history=history,
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "MonitorAgent cycle | metric=%s | process_kpi failed: %s",
                    metric_name, exc, exc_info=True,
                )

    @staticmethod
    def _extract_series(rows: list[dict[str, Any]], metric_name: str) -> list[float]:
        """
        Extract a chronological float series for ``metric_name`` from ``rows``.

        Matches a row's column header to ``metric_name`` case-insensitively.
        Cells that are blank or non-numeric are dropped (not interpolated —
        :meth:`process_kpi` already runs ``history`` through
        :meth:`StructuredDataPipeline.clean_missing`).

        Args:
            rows:        Row dicts as returned by :class:`KPIRowSource.fetch_rows`,
                         in sheet order (assumed chronological, oldest first).
            metric_name: Metric domain to extract (e.g. ``"sales"``).

        Returns:
            List of floats in chronological order. Empty if no matching
            column or no parseable values.
        """
        target = metric_name.strip().lower()
        values: list[float] = []
        for row in rows:
            raw = next(
                (v for k, v in row.items() if k.strip().lower() == target),
                None,
            )
            if raw is None or str(raw).strip() == "":
                continue
            try:
                values.append(float(raw))
            except (TypeError, ValueError):
                continue
        return values

    @staticmethod
    def _collect_signals(
        current: float,
        rule_result: dict[str, Any],
        stat_result: dict[str, Any],
    ) -> list[str]:
        """
        Collect the names of all detection signals that fired.

        A signal is considered fired when:
        - Rule engine: ``rule_result["rule_triggered"] is True``.
          Signal name: ``f"rule:{rule_result['rule_name']}"``.
        - Statistical: ``stat_result["statistical_anomaly"] is True``.
          Sub-signals are derived from z-score and percentile bounds,
          evaluated against the actual current value.

        Args:
            current:     The actual current observed value.
            rule_result: Output dict from :meth:`RuleEngine.evaluate`.
            stat_result: Output dict from :meth:`StatisticalDetector.detect`.

        Returns:
            List of descriptive signal name strings. Empty list if nothing fired.
        """
        signals: list[str] = []

        # Rule-based signal.
        if rule_result.get("rule_triggered"):
            rule_name = rule_result.get("rule_name")
            if rule_name:
                signals.append(f"rule:{rule_name}")
            else:
                signals.append("rule:unknown")

        # Statistical sub-signals — report each contributing condition.
        if stat_result.get("statistical_anomaly"):
            z = stat_result.get("z_score", 0.0)
            p_low = stat_result.get("percentile_low")
            p_high = stat_result.get("percentile_high")

            # Z-score condition
            if abs(z) > StatisticalDetector.Z_SCORE_THRESHOLD:
                signals.append(f"statistical:z_score({z:.2f})")

            # Percentile bounds condition — evaluated against actual current value
            if p_low is not None and p_high is not None:
                if current < p_low:
                    signals.append("statistical:below_p5")
                elif current > p_high:
                    signals.append("statistical:above_p95")
                # If we're here but no bounds breach, the anomaly must have come
                # from z-score alone (already added above) - no generic signal needed

        return signals

    @staticmethod
    def _derive_severity(signal_count: int) -> str:
        """
        Map the number of confirmed signals to a severity string.

        Args:
            signal_count: Number of independent detection signals that fired.

        Returns:
            - ``"HIGH"``   if ``signal_count >= 2``.
            - ``"MEDIUM"`` if ``signal_count == 1``.
            - ``"LOW"``    if ``signal_count == 0``.
        """
        if signal_count >= 2:
            return "HIGH"
        if signal_count == 1:
            return "MEDIUM"
        return "LOW"

    def __repr__(self) -> str:
        return (
            f"MonitorAgent("
            f"interval={self._settings.MONITOR_INTERVAL_SECONDS}s, "
            f"queue_depth={self._queue.size()})"
        )
