import asyncio
from aeam.main import create_app
from aeam.core.event_models import Event


async def run():
    app = create_app()

    async with app.router.lifespan_context(app):
        container = app.state.container

        event = Event(
            event_id="1",
            event_type="TEST",
            metric="sales",
            severity="HIGH",
            current_value=100,
            expected_value=200,
            detection_methods=["rule"],
            timestamp="2025-01-01T00:00:00Z",
        )

        container.event_bus.publish(event)


if __name__ == "__main__":
    asyncio.run(run())