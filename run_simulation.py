import asyncio
from aeam.main import create_app
from aeam.agents.monitor.monitor_agent import MonitorAgent
from aeam.agents.kpi.rule_engine import RuleEngine
from aeam.agents.kpi.statistical_detector import StatisticalDetector
from aeam.pipelines.structured_data_pipeline import StructuredDataPipeline


class DummyForecastAgent:
    """Harmless placeholder that never reports a forecast deviation."""
    def analyze(self, metric_name: str, actual_value: float) -> dict:
        return {"is_deviation": False}


async def run():
    app = create_app()

    async with app.router.lifespan_context(app):
        container = app.state.container

        rule_engine = RuleEngine()
        statistical_detector = StatisticalDetector(window_size=7)
        pipeline = StructuredDataPipeline()
        forecast_agent = DummyForecastAgent()   # <-- safe dummy

        monitor = MonitorAgent(
            event_bus=container.event_bus,
            queue=container.queue,
            deduplicator=container.deduplicator,
            rule_engine=rule_engine,
            statistical_detector=statistical_detector,
            forecast_agent=forecast_agent,
            pipeline=pipeline,
            settings=container.settings,
        )

        # Simulate a KPI observation (sales dropped from 200 to 100).
        history = [200, 198, 195, 199, 202, 197, 200]   # last 7 periods
        print("Feeding KPI observation to MonitorAgent…")
        event = monitor.process_kpi(
            metric_name="sales",
            current=100.0,
            previous=200.0,
            history=history,
        )

        if event:
            # NOTE: process_kpi() has ALREADY published this event to the
            # EventBus (see MonitorAgent.process_kpi step 9) and the
            # Orchestrator has already run a full investigation for it by the
            # time it returns.
            #
            # This script used to call container.event_bus.publish(event) again
            # here. That second publish bypassed EventDeduplicator entirely
            # (dedup runs INSIDE process_kpi, before its own publish), so one
            # simulated anomaly produced two full investigations, two incident
            # rows, and two sets of REAL external side effects — duplicate
            # Slack posts, duplicate Jira tickets, duplicate emails
            # (ActionAgent's idempotency is keyed on incident_id, which
            # differs between the two investigations, so it did not suppress
            # them) — plus double LLM spend. The duplicates also skewed every
            # aggregate computed from the incidents table, including F2
            # calibration fits and F4 graph edges.
            print(f"Multi-signal anomaly confirmed! Event ID: {event.event_id}")
            print("Investigation already completed synchronously via EventBus.")
        else:
            print("No anomaly confirmed (insufficient signals).")


if __name__ == "__main__":
    asyncio.run(run())