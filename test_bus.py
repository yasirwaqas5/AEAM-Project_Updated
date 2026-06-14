from aeam.core.event_bus import EventBus
from aeam.core.event_models import Event
from datetime import datetime, timezone

bus = EventBus()

def handler(event):
    print("Handler triggered:", event.event_type)

bus.register_handler("TEST", handler)

event = Event(
    event_id="1",
    event_type="TEST",
    metric="cpu",
    current_value=90.0,
    expected_value=50.0,
    detection_methods=["zscore"],
    severity="HIGH",
    timestamp=datetime.now(timezone.utc),
    metadata={}
)

bus.publish(event)