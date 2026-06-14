"""
aeam/security/rate_limiter.py

Redis-based rate limiting for the AEAM system.

Tracks request counts per key in Redis with a sliding fixed-window TTL.
Returns True when the request is within the allowed limit, False when
the limit has been exceeded.

No Redis pipeline is used — operations are performed sequentially to
match project test expectations.

Dependencies:
- RedisClient (aeam/integrations/redis_client.py)
"""

from __future__ import annotations

import logging
from typing import Any

from aeam.integrations.redis_client import RedisClient

logger = logging.getLogger(__name__)


class RateLimiter:
    """
    Fixed-window Redis-based rate limiter.

    Tracks the number of requests made against a given ``key`` within a
    rolling time window. Each call to :meth:`allow` increments the counter
    for that key and returns ``True`` if the count is within ``limit``, or
    ``False`` if the limit has been exceeded.

    The TTL for each key is (re)set on every call to ensure the window
    remains anchored correctly. No Redis pipeline is used.

    Args:
        redis_client: Connected :class:`~aeam.integrations.redis_client.RedisClient`.

    Raises:
        ValueError: If ``redis_client`` is None.

    Example::

        limiter = RateLimiter(redis_client=redis_client)

        allowed = limiter.allow(
            key="rate:api:user_123",
            limit=100,
            window_seconds=60,
        )
        if not allowed:
            raise PermissionError("Rate limit exceeded.")
    """

    def __init__(self, redis_client: RedisClient) -> None:
        """
        Initialise the RateLimiter with an injected Redis client.

        Args:
            redis_client: Active RedisClient instance. Must not be None.

        Raises:
            ValueError: If ``redis_client`` is None.
        """
        if redis_client is None:
            raise ValueError("redis_client must not be None.")
        self._redis: RedisClient = redis_client

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def allow(
        self,
        key: str,
        limit: int,
        window_seconds: int = 60,
    ) -> bool:
        """
        Determine whether a request identified by ``key`` is within the
        rate limit.

        Steps:
        1. ``GET`` the current count for ``key`` from Redis.
        2. If the key exists, increment the count in memory.
        3. If the incremented count exceeds ``limit``, log a denial and
           return ``False`` without writing back to Redis (the existing
           TTL continues to govern the window).
        4. Otherwise, persist the new count with ``SETEX`` using
           ``window_seconds`` as the TTL, then log an allow and return
           ``True``.

        If the key does not yet exist, the count is initialised to 1
        and stored with the full ``window_seconds`` TTL.

        Args:
            key:            Redis key that identifies the rate-limit bucket
                            (e.g. ``"rate:api:user_123"`` or
                            ``"rate:action:execute"``).
            limit:          Maximum number of requests allowed within
                            ``window_seconds``. Must be >= 1.
            window_seconds: Duration of the rate-limit window in seconds.
                            Defaults to ``60``.

        Returns:
            ``True``  — request is within the limit and has been counted.
            ``False`` — request exceeds the limit and is blocked.

        Note:
            On Redis connectivity errors the method defaults to ``True``
            (allow) so that a transient cache failure does not block all
            traffic. The error is logged at ERROR level.

        Example::

            limiter = RateLimiter(redis_client=redis_client)

            for _ in range(5):
                assert limiter.allow("rate:test", limit=3, window_seconds=60)
                # True, True, True, False, False
        """
        if not key or not key.strip():
            logger.warning("RateLimiter.allow | empty key received | defaulting to allow.")
            return True

        if limit < 1:
            logger.warning(
                "RateLimiter.allow | limit=%d < 1 | key=%s | defaulting to deny.",
                limit, key,
            )
            return False

        try:
            # Step 1: get current count.
            raw: Any = self._redis.get(key)

            if raw is not None:
                # Step 2: key exists — parse and increment.
                current_count: int = int(raw)
                new_count: int = current_count + 1

                # Step 3: deny if over limit.
                if new_count > limit:
                    logger.warning(
                        "RateLimiter.allow | DENIED | key=%s | count=%d | limit=%d | "
                        "window=%ds",
                        key, new_count, limit, window_seconds,
                    )
                    return False

                # Step 4: within limit — persist updated count with TTL.
                self._redis.setex(key, window_seconds, new_count)

                logger.info(
                    "RateLimiter.allow | ALLOWED | key=%s | count=%d/%d | window=%ds",
                    key, new_count, limit, window_seconds,
                )
                return True

            else:
                # Key does not exist — initialise at count 1.
                self._redis.setex(key, window_seconds, 1)

                logger.info(
                    "RateLimiter.allow | ALLOWED (new window) | key=%s | count=1/%d | "
                    "window=%ds",
                    key, limit, window_seconds,
                )
                return True

        except Exception as exc:  # noqa: BLE001
            logger.error(
                "RateLimiter.allow | Redis error for key=%s: %s | defaulting to allow.",
                key, exc,
            )
            return True

    def __repr__(self) -> str:
        return "RateLimiter(backend=redis)"