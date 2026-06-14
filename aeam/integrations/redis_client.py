"""
aeam/integrations/redis_client.py

Redis wrapper for the AEAM modular monolith.

Provides a thin, typed interface over the ``redis-py`` client. All public
methods handle connection and command errors, log minimally, and re-raise so
that callers retain full control over retry and fallback policy.

This module contains no business logic, no agent references, and no
application-level semantics. It is a pure infrastructure adapter.
"""

import logging
from typing import Any

import redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisError, TimeoutError as RedisTimeoutError

logger = logging.getLogger(__name__)


class RedisClient:
    """
    Typed wrapper around a ``redis.Redis`` connection.

    Exposes only the Redis commands used by AEAM, providing consistent error
    handling and logging across all call sites. The underlying ``redis.Redis``
    client (and its connection pool) is created once at construction time and
    reused for all operations.

    Args:
        redis_url:      Redis connection URL
                        (e.g. ``"redis://localhost:6379/0"``).
        socket_timeout: Seconds to wait for a response from the Redis server
                        before raising :class:`redis.exceptions.TimeoutError`.
                        Defaults to ``5``.
        socket_connect_timeout:
                        Seconds to wait when establishing a new connection.
                        Defaults to ``5``.
        decode_responses:
                        When ``True`` (default), all values returned from Redis
                        are decoded from bytes to :class:`str`. Set to ``False``
                        if raw bytes are required.

    Raises:
        ValueError: If ``redis_url`` is empty or whitespace-only.

    Example::

        client = RedisClient(redis_url="redis://localhost:6379/0")
        client.setex("dedup:cpu:123", ttl=300, value="1")
        if client.exists("dedup:cpu:123"):
            print("duplicate detected")
    """

    def __init__(
        self,
        redis_url: str,
        socket_timeout: int = 5,
        socket_connect_timeout: int = 5,
        decode_responses: bool = True,
    ) -> None:
        """
        Initialise the Redis client from a connection URL.

        Args:
            redis_url:              Redis connection URL. Must not be empty.
            socket_timeout:         Response timeout in seconds. Must be >= 1.
            socket_connect_timeout: Connection timeout in seconds. Must be >= 1.
            decode_responses:       Decode byte responses to strings when True.

        Raises:
            ValueError: If ``redis_url`` is empty or whitespace-only.
        """
        if not redis_url or not redis_url.strip():
            raise ValueError("redis_url must be a non-empty string.")

        self._client: redis.Redis = redis.Redis.from_url(  # type: ignore[type-arg]
            redis_url,
            socket_timeout=socket_timeout,
            socket_connect_timeout=socket_connect_timeout,
            decode_responses=decode_responses,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, key: str) -> str | None:
        """
        Retrieve the string value associated with ``key``.

        Args:
            key: The Redis key to look up. Must be non-empty.

        Returns:
            The stored string value, or ``None`` if the key does not exist
            or has expired.

        Raises:
            ValueError:          If ``key`` is empty or whitespace-only.
            RedisConnectionError: If the client cannot reach the Redis server.
            RedisError:          On any other Redis command failure.

        Example::

            value = client.get("session:abc-123")
            if value is not None:
                process(value)
        """
        self._validate_key(key)
        try:
            return self._client.get(key)  # type: ignore[return-value]
        except (RedisConnectionError, RedisTimeoutError) as exc:
            logger.error("get() connection error | key=%r | error=%s", key, exc)
            raise
        except RedisError as exc:
            logger.error("get() failed | key=%r | error=%s", key, exc)
            raise

    def setex(self, key: str, ttl: int, value: str) -> None:
        """
        Store ``value`` under ``key`` with an expiry of ``ttl`` seconds.

        If ``key`` already exists its value and TTL are overwritten.

        Args:
            key:   The Redis key to write. Must be non-empty.
            ttl:   Time-to-live in seconds. Must be >= 1.
            value: The string value to store.

        Raises:
            ValueError:           If ``key`` is empty, ``ttl`` < 1, or
                                  ``value`` is not a string.
            RedisConnectionError: On connection or timeout failure.
            RedisError:           On any other Redis command failure.

        Example::

            client.setex("dedup:cpu:bucket_42", ttl=300, value="1")
        """
        self._validate_key(key)
        if ttl < 1:
            raise ValueError(f"ttl must be >= 1 second. Got: {ttl}.")
        if not isinstance(value, str):
            raise ValueError(
                f"value must be a str. Got: {type(value).__name__!r}."
            )

        try:
            self._client.setex(name=key, time=ttl, value=value)
        except (RedisConnectionError, RedisTimeoutError) as exc:
            logger.error(
                "setex() connection error | key=%r | ttl=%s | error=%s",
                key, ttl, exc,
            )
            raise
        except RedisError as exc:
            logger.error("setex() failed | key=%r | ttl=%s | error=%s", key, ttl, exc)
            raise

    def exists(self, key: str) -> bool:
        """
        Return whether ``key`` is present in Redis (and has not expired).

        Args:
            key: The Redis key to check. Must be non-empty.

        Returns:
            ``True`` if the key exists, ``False`` otherwise.

        Raises:
            ValueError:           If ``key`` is empty or whitespace-only.
            RedisConnectionError: On connection or timeout failure.
            RedisError:           On any other Redis command failure.

        Example::

            if client.exists("lock:investigation:INC-42"):
                skip_reinvestigation()
        """
        self._validate_key(key)
        try:
            # redis-py returns the count of keys found (0 or 1 for a single key).
            return bool(self._client.exists(key))
        except (RedisConnectionError, RedisTimeoutError) as exc:
            logger.error("exists() connection error | key=%r | error=%s", key, exc)
            raise
        except RedisError as exc:
            logger.error("exists() failed | key=%r | error=%s", key, exc)
            raise

    def incr(self, key: str) -> int:
        """
        Atomically increment the integer value stored at ``key`` by 1.

        If ``key`` does not exist, it is created with value ``0`` before
        incrementing, resulting in a final value of ``1``. The key does not
        automatically gain a TTL — use :meth:`expire` afterward if needed.

        Args:
            key: The Redis key to increment. Must be non-empty.

        Returns:
            The new integer value of ``key`` after incrementing.

        Raises:
            ValueError:           If ``key`` is empty or whitespace-only.
            RedisConnectionError: On connection or timeout failure.
            RedisError:           On any other Redis command failure (e.g. if
                                  the stored value is not an integer).

        Example::

            count = client.incr("alert:rate:service_a")
            client.expire("alert:rate:service_a", ttl=60)
        """
        self._validate_key(key)
        try:
            return int(self._client.incr(key))
        except (RedisConnectionError, RedisTimeoutError) as exc:
            logger.error("incr() connection error | key=%r | error=%s", key, exc)
            raise
        except RedisError as exc:
            logger.error("incr() failed | key=%r | error=%s", key, exc)
            raise

    def expire(self, key: str, ttl: int) -> bool:
        """
        Set a TTL (expiry) on an existing ``key``.

        If the key does not exist, this is a no-op and ``False`` is returned.
        Typically called after :meth:`incr` to attach a sliding window expiry
        to a counter key.

        Args:
            key: The Redis key to set an expiry on. Must be non-empty.
            ttl: Expiry duration in seconds. Must be >= 1.

        Returns:
            ``True`` if the expiry was applied successfully, ``False`` if
            the key did not exist.

        Raises:
            ValueError:           If ``key`` is empty or ``ttl`` < 1.
            RedisConnectionError: On connection or timeout failure.
            RedisError:           On any other Redis command failure.

        Example::

            client.incr("alert:count:web-01")
            client.expire("alert:count:web-01", ttl=60)
        """
        self._validate_key(key)
        if ttl < 1:
            raise ValueError(f"ttl must be >= 1 second. Got: {ttl}.")

        try:
            return bool(self._client.expire(key, ttl))
        except (RedisConnectionError, RedisTimeoutError) as exc:
            logger.error(
                "expire() connection error | key=%r | ttl=%s | error=%s",
                key, ttl, exc,
            )
            raise
        except RedisError as exc:
            logger.error("expire() failed | key=%r | ttl=%s | error=%s", key, ttl, exc)
            raise

    # ------------------------------------------------------------------
    # Health check
    # ------------------------------------------------------------------

    def ping(self) -> bool:
        """
        Send a ``PING`` to the Redis server to verify connectivity.

        Returns:
            ``True`` if the server responds with ``PONG``, ``False`` otherwise.

        This method does **not** raise on failure — it is intended for health
        checks and startup probes where a boolean result is more useful than
        an exception.
        """
        try:
            return bool(self._client.ping())
        except RedisError as exc:
            logger.warning("ping() failed | error=%s", exc)
            return False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_key(key: str) -> None:
        """
        Raise :class:`ValueError` if ``key`` is empty or whitespace-only.

        Args:
            key: The Redis key string to validate.

        Raises:
            ValueError: If the key is empty or blank.
        """
        if not key or not key.strip():
            raise ValueError("Redis key must be a non-empty string.")

    def close(self) -> None:
        """
        Close the underlying connection pool.

        Should be called during application shutdown to release Redis
        connections cleanly. Idempotent — safe to call multiple times.
        """
        try:
            self._client.close()
            logger.info("RedisClient connection pool closed.")
        except RedisError as exc:
            logger.warning("close() encountered an error: %s", exc)

    def __repr__(self) -> str:
        pool = self._client.connection_pool
        return f"RedisClient(pool={pool!r})"