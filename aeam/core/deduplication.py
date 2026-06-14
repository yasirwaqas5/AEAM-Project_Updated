"""
aeam/core/deduplication.py

Window-based event deduplication logic for the AEAM modular monolith.

Events are considered duplicates if another event with the same type, metric,
and time-window bucket has already been seen within the configured window.
Redis is used as the backing store, with keys expiring automatically after
the window duration (TTL = window_minutes * 60 seconds).

The Redis client is dependency-injected, keeping this module decoupled from
any specific Redis initialisation or connection pooling strategy.
"""

from datetime import timezone

import redis

from aeam.core.event_models import Event


class EventDeduplicator:
    """
    Window-based event deduplicator backed by Redis.

    Each unique ``(event_type, metric, time_window_bucket)`` combination is
    stored as a Redis key with a TTL equal to the window duration. A second
    event arriving within the same window and sharing the same type and metric
    is classified as a duplicate and suppressed.

    The ``Event`` object is never mutated by this class.

    Args:
        redis_client: A connected :class:`redis.Redis` instance. The caller is
                      responsible for managing connection lifecycle and pooling.

    Example::

        import redis
        from aeam.core.deduplication import EventDeduplicator

        client = redis.Redis.from_url("redis://localhost:6379/0")
        deduplicator = EventDeduplicator(redis_client=client)

        if not deduplicator.is_duplicate(event, window_minutes=30):
            process(event)
    """

    def __init__(self, redis_client: redis.Redis) -> None:  # type: ignore[type-arg]
        """
        Initialise the deduplicator with an injected Redis client.

        Args:
            redis_client: Active :class:`redis.Redis` connection. Must support
                          ``exists`` and ``set`` commands with ``ex`` (TTL) option.
        """
        self._redis: redis.Redis = redis_client  # type: ignore[type-arg]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_duplicate(self, event: Event, window_minutes: int = 60) -> bool:
        """
        Determine whether an event is a duplicate within the given time window.

        A deduplication key is derived from the event's ``event_type``,
        ``metric``, and a rounded time-window bucket computed from the event's
        ``timestamp``. If that key already exists in Redis the event is a
        duplicate. Otherwise the key is written with a TTL of
        ``window_minutes * 60`` seconds, and the event is treated as novel.

        The ``event`` object is not modified at any point.

        Args:
            event:          The :class:`~aeam.core.event_models.Event` to check.
            window_minutes: Width of the deduplication window in minutes.
                            Must be >= 1. Defaults to ``60``.

        Returns:
            ``True``  — a matching key was found in Redis; this event is a duplicate.
            ``False`` — no matching key existed; the event has been registered and
                        will suppress identical events for the remainder of the window.

        Raises:
            ValueError:    If ``window_minutes`` is less than 1.
            redis.RedisError: If the Redis operation fails (connection error,
                              timeout, etc.). Callers should handle this to avoid
                              dropping events silently.
        """
        if window_minutes < 1:
            raise ValueError(
                f"window_minutes must be >= 1. Got: {window_minutes}"
            )

        key = self._build_dedup_key(event, window_minutes)
        ttl_seconds = window_minutes * 60

        # SET key "" EX ttl NX  →  sets only if key does not exist.
        # Returns True  if the key was newly created  (not a duplicate).
        # Returns None/False if the key already existed (duplicate).
        was_set: bool | None = self._redis.set(
            name=key,
            value="1",
            ex=ttl_seconds,
            nx=True,  # Only set if Not eXists
        )

        return was_set is None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_dedup_key(event: Event, window_minutes: int) -> str:
        """
        Construct a Redis deduplication key for the given event and window.

        The key encodes the event type, metric, and a time-bucket index derived
        by dividing the event's UTC epoch-minutes by ``window_minutes``. Two
        events with the same type and metric whose timestamps fall within the
        same bucket will produce an identical key.

        Key format::

            aeam:dedup:{event_type}:{metric}:{window_bucket}

        Args:
            event:          The event to build a key for.
            window_minutes: Window size used to bucket the timestamp.

        Returns:
            A colon-delimited string suitable for use as a Redis key.
        """
        # Ensure we work in UTC regardless of the timestamp's tzinfo.
        ts_utc = event.timestamp.astimezone(timezone.utc)
        epoch_minutes = int(ts_utc.timestamp()) // 60
        rounded_time_window = epoch_minutes // window_minutes

        return (
            f"aeam:dedup"
            f":{event.event_type}"
            f":{event.metric}"
            f":{rounded_time_window}"
        )

    def __repr__(self) -> str:
        return f"EventDeduplicator(redis={self._redis!r})"