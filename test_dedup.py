from aeam.core.deduplication import EventDeduplicator
from aeam.core.event_models import Event
from aeam.config.settings import Settings
import redis
from datetime import datetime, timezone

settings = Settings()
redis_client = redis.Redis.from_url(str(settings.REDIS_URL))

dedup = EventDeduplicator(redis_client)

event = Event(
    event_id="1",
    event_type="ANOMALY",
    metric="cpu",
    current_value=95.0,
    expected_value=50.0,
    detection_methods=["zscore"],
    severity="HIGH",
    timestamp=datetime.now(timezone.utc),
    metadata={}
)

print("First:", dedup.is_duplicate(event, window_minutes=1))
print("Second:", dedup.is_duplicate(event, window_minutes=1))