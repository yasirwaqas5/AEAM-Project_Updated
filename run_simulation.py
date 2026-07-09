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
            print(f"Multi‑signal anomaly confirmed! Event ID: {event.event_id}")
            container.event_bus.publish(event)
        else:
            print("No anomaly confirmed (insufficient signals).")


if __name__ == "__main__":
    asyncio.run(run())