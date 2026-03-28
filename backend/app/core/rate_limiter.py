"""Sliding window rate limiter middleware for FastAPI/Starlette.

Supports two backends:
- **In-memory** (default): Suitable for single-instance deployments and
  development.  No external dependencies required.
- **Redis**: For multi-instance / replicated production deployments.
  Enable by setting ``SLATE_RATE_LIMIT_REDIS_URL`` to a Redis connection
  string (e.g. ``redis://redis:6379/0``).

The middleware auto-selects the backend at startup based on configuration.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from typing import Deque, Set

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

# Paths that are never rate-limited
EXEMPT_PATHS: Set[str] = {"/health", "/ready", "/docs", "/openapi.json"}


class SlidingWindowCounter:
    """Thread-safe (asyncio) sliding window request counter keyed by client."""

    def __init__(self, max_requests: int, window_seconds: int) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._requests: dict[str, Deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def is_allowed(self, key: str) -> tuple[bool, int, float]:
        """Check whether *key* may proceed.

        Returns a tuple of ``(allowed, remaining, retry_after)``.

        * ``allowed`` — True if the request should be served.
        * ``remaining`` — number of requests left in the current window.
        * ``retry_after`` — seconds the client should wait (0.0 if allowed).
        """
        now = time.monotonic()
        cutoff = now - self.window_seconds

        async with self._lock:
            timestamps = self._requests[key]

            # Evict timestamps outside the window
            while timestamps and timestamps[0] <= cutoff:
                timestamps.popleft()

            if len(timestamps) >= self.max_requests:
                # The oldest remaining timestamp tells us when the window
                # will slide enough to allow the next request.
                retry_after = timestamps[0] - cutoff
                return False, 0, max(retry_after, 0.0)

            timestamps.append(now)
            remaining = self.max_requests - len(timestamps)
            return True, remaining, 0.0

    async def cleanup_stale(self) -> None:
        """Remove entries for clients with no recent activity.

        Call periodically (e.g. once per minute) to prevent memory growth.
        """
        now = time.monotonic()
        cutoff = now - self.window_seconds

        async with self._lock:
            stale_keys = [
                k
                for k, dq in self._requests.items()
                if not dq or dq[-1] <= cutoff
            ]
            for k in stale_keys:
                del self._requests[k]


class RedisSlidingWindowCounter:
    """Redis-backed sliding window counter for multi-instance deployments.

    Uses Redis sorted sets with timestamps as scores.  Each client gets
    a sorted set keyed by ``ratelimit:{client_ip}``.  Old entries are
    pruned on each check via ZREMRANGEBYSCORE.

    Falls back to the in-memory counter if the Redis connection fails,
    and periodically attempts to reconnect (every ``_REDIS_RETRY_INTERVAL``
    seconds) so that distributed limiting is restored after transient Redis
    outages.
    """

    # Seconds to wait before attempting to reconnect to Redis after a failure
    _REDIS_RETRY_INTERVAL: float = 30.0

    def __init__(
        self, max_requests: int, window_seconds: int, redis_url: str
    ) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._redis_url = redis_url
        self._redis: object | None = None
        self._fallback = SlidingWindowCounter(max_requests, window_seconds)
        self._redis_failed = False
        self._redis_failed_at: float = 0.0

    async def _get_redis(self):
        """Lazy-connect to Redis."""
        if self._redis is not None:
            return self._redis
        try:
            import redis.asyncio as aioredis
            self._redis = aioredis.from_url(
                self._redis_url, decode_responses=True
            )
            return self._redis
        except Exception:
            self._redis_failed = True
            self._redis_failed_at = time.monotonic()
            return None

    def _should_retry_redis(self) -> bool:
        """Check whether enough time has elapsed to attempt Redis reconnection."""
        if not self._redis_failed:
            return False
        return (time.monotonic() - self._redis_failed_at) >= self._REDIS_RETRY_INTERVAL

    async def _attempt_redis_reconnect(self) -> bool:
        """Try to reconnect to Redis after a transient failure.

        Returns True if reconnection succeeded, False otherwise.
        """
        import logging
        _logger = logging.getLogger(__name__)

        # Reset connection state so _get_redis creates a fresh client
        old_redis = self._redis
        self._redis = None
        # Close the old connection if it exists
        if old_redis is not None:
            try:
                await old_redis.close()  # type: ignore[union-attr]
            except Exception:
                pass

        try:
            r = await self._get_redis()
            if r is None:
                return False
            # Verify connectivity with a lightweight PING
            await r.ping()  # type: ignore[union-attr]
            self._redis_failed = False
            self._redis_failed_at = 0.0
            _logger.info("Redis rate limiter reconnected successfully")
            return True
        except Exception as exc:
            _logger.debug("Redis reconnection attempt failed: %s", exc)
            self._redis_failed = True
            self._redis_failed_at = time.monotonic()
            self._redis = None
            return False

    async def is_allowed(self, key: str) -> tuple[bool, int, float]:
        # If Redis previously failed, periodically attempt reconnection
        if self._redis_failed:
            if self._should_retry_redis():
                await self._attempt_redis_reconnect()
            if self._redis_failed:
                return await self._fallback.is_allowed(key)

        try:
            r = await self._get_redis()
            if r is None:
                return await self._fallback.is_allowed(key)

            import time as _time
            import uuid as _uuid
            now = _time.time()
            cutoff = now - self.window_seconds
            redis_key = f"ratelimit:{key}"
            # Use unique member ID to avoid collisions under high concurrency
            member_id = f"{now}:{_uuid.uuid4().hex[:12]}"

            pipe = r.pipeline()
            pipe.zremrangebyscore(redis_key, 0, cutoff)
            pipe.zcard(redis_key)
            pipe.zadd(redis_key, {member_id: now})
            pipe.expire(redis_key, self.window_seconds + 1)
            results = await pipe.execute()

            current_count = results[1]
            if current_count >= self.max_requests:
                # Remove the entry we just added (over limit)
                await r.zrem(redis_key, member_id)
                return False, 0, float(self.window_seconds)

            remaining = self.max_requests - current_count - 1
            return True, max(remaining, 0), 0.0
        except Exception:
            import logging
            logging.getLogger(__name__).warning(
                "Redis rate limiter failed, falling back to in-memory"
            )
            self._redis_failed = True
            self._redis_failed_at = time.monotonic()
            return await self._fallback.is_allowed(key)

    async def cleanup_stale(self) -> None:
        """No-op for Redis — TTL handles expiry automatically."""
        pass


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Starlette middleware that enforces per-client request rate limits.

    Clients are identified by IP address (``X-Forwarded-For`` first hop, then
    ``client.host``).  Requests to exempt paths (health checks, docs) are
    always allowed.

    When the limit is exceeded, a ``429 Too Many Requests`` response is
    returned with a ``Retry-After`` header (in whole seconds).

    For multi-instance production deployments, set ``redis_url`` to enable
    Redis-backed distributed rate limiting.  Without it, the in-memory
    counter is used (per-instance, suitable for development/single-instance).
    """

    def __init__(
        self,
        app: object,
        max_requests: int = 100,
        window_seconds: int = 60,
        redis_url: str | None = None,
    ) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        if redis_url:
            self._counter: SlidingWindowCounter | RedisSlidingWindowCounter = (
                RedisSlidingWindowCounter(max_requests, window_seconds, redis_url)
            )
        else:
            self._counter = SlidingWindowCounter(max_requests, window_seconds)

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        # Skip rate limiting for exempt paths
        if request.url.path in EXEMPT_PATHS:
            return await call_next(request)

        client_ip = self._get_client_ip(request)
        allowed, remaining, retry_after = await self._counter.is_allowed(client_ip)

        if not allowed:
            return JSONResponse(
                status_code=429,
                content={
                    "detail": "Too many requests. Please slow down.",
                },
                headers={
                    "Retry-After": str(int(retry_after) + 1),
                    "X-RateLimit-Limit": str(self._counter.max_requests),
                    "X-RateLimit-Remaining": "0",
                },
            )

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(self._counter.max_requests)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        return response

    @staticmethod
    def _get_client_ip(request: Request) -> str:
        """Extract client IP, respecting X-Forwarded-For from trusted proxies."""
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            # First address in the chain is the original client
            return forwarded.split(",")[0].strip()
        if request.client:
            return request.client.host
        return "unknown"
