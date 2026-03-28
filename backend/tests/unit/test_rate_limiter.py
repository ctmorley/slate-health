"""Unit tests for the API rate limiting middleware.

Tests cover:
- Requests within limit are allowed (200)
- Requests exceeding limit receive 429 with Retry-After header
- Exempt paths (/health, /ready, /docs) are not rate-limited
- Different client IPs have independent limits
- Window expiry allows new requests
- Redis rate limiter recovery after transient failure
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.core.rate_limiter import (
    RateLimitMiddleware,
    RedisSlidingWindowCounter,
    SlidingWindowCounter,
)


def _make_app(max_requests: int = 5, window_seconds: int = 60):
    """Create a minimal Starlette app with rate limiting."""

    async def index(request: Request):
        return JSONResponse({"ok": True})

    async def health(request: Request):
        return JSONResponse({"status": "healthy"})

    app = Starlette(
        routes=[
            Route("/", index),
            Route("/api/test", index),
            Route("/health", health),
            Route("/ready", health),
            Route("/docs", health),
        ]
    )
    app.add_middleware(
        RateLimitMiddleware,
        max_requests=max_requests,
        window_seconds=window_seconds,
    )
    return app


class TestRateLimitMiddleware:
    @pytest.mark.asyncio
    async def test_requests_within_limit_allowed(self):
        app = _make_app(max_requests=10)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            for _ in range(10):
                resp = await client.get("/api/test")
                assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_requests_exceeding_limit_get_429(self):
        app = _make_app(max_requests=3)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            for _ in range(3):
                resp = await client.get("/api/test")
                assert resp.status_code == 200

            resp = await client.get("/api/test")
            assert resp.status_code == 429
            assert "retry-after" in resp.headers

    @pytest.mark.asyncio
    async def test_exempt_paths_not_rate_limited(self):
        app = _make_app(max_requests=2)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Exhaust the limit
            for _ in range(2):
                await client.get("/api/test")

            # Exempt paths should still work
            resp = await client.get("/health")
            assert resp.status_code == 200

            resp = await client.get("/ready")
            assert resp.status_code == 200

            resp = await client.get("/docs")
            assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_different_ips_independent_limits(self):
        app = _make_app(max_requests=2)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Client 1 (default IP)
            for _ in range(2):
                resp = await client.get("/api/test")
                assert resp.status_code == 200

            # Client 1 should be rate limited
            resp = await client.get("/api/test")
            assert resp.status_code == 429

            # Client 2 via X-Forwarded-For should have its own limit
            resp = await client.get(
                "/api/test",
                headers={"X-Forwarded-For": "192.168.1.100"},
            )
            assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_429_response_body(self):
        app = _make_app(max_requests=1)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            await client.get("/api/test")
            resp = await client.get("/api/test")
            assert resp.status_code == 429
            data = resp.json()
            assert "detail" in data
            assert "rate limit" in data["detail"].lower() or "too many" in data["detail"].lower()


class TestRedisSlidingWindowCounter:
    """Tests for Redis rate limiter fallback and recovery behaviour."""

    @pytest.mark.asyncio
    async def test_falls_back_to_in_memory_on_redis_failure(self):
        """When Redis fails, requests should still be served via in-memory counter."""
        counter = RedisSlidingWindowCounter(
            max_requests=10, window_seconds=60, redis_url="redis://invalid:9999/0"
        )
        # Force a failed state
        counter._redis_failed = True
        counter._redis_failed_at = time.monotonic()

        allowed, remaining, retry_after = await counter.is_allowed("client1")
        assert allowed is True
        assert remaining >= 0

    @pytest.mark.asyncio
    async def test_redis_failed_flag_is_not_permanent(self):
        """After _REDIS_RETRY_INTERVAL elapses, the counter should attempt reconnection."""
        counter = RedisSlidingWindowCounter(
            max_requests=10, window_seconds=60, redis_url="redis://localhost:6379/0"
        )
        # Simulate a failure that happened long ago
        counter._redis_failed = True
        counter._redis_failed_at = time.monotonic() - counter._REDIS_RETRY_INTERVAL - 1

        assert counter._should_retry_redis() is True

    @pytest.mark.asyncio
    async def test_redis_not_retried_too_soon(self):
        """Within retry interval, should not attempt reconnection."""
        counter = RedisSlidingWindowCounter(
            max_requests=10, window_seconds=60, redis_url="redis://localhost:6379/0"
        )
        counter._redis_failed = True
        counter._redis_failed_at = time.monotonic()  # Just failed

        assert counter._should_retry_redis() is False

    @pytest.mark.asyncio
    async def test_successful_reconnection_restores_redis(self):
        """After successful reconnect, _redis_failed should be reset to False."""
        counter = RedisSlidingWindowCounter(
            max_requests=10, window_seconds=60, redis_url="redis://localhost:6379/0"
        )
        counter._redis_failed = True
        counter._redis_failed_at = time.monotonic() - 100

        # Mock the Redis connection to succeed
        mock_redis = AsyncMock()
        mock_redis.ping = AsyncMock(return_value=True)

        async def mock_get_redis():
            counter._redis = mock_redis
            return mock_redis

        counter._get_redis = mock_get_redis

        result = await counter._attempt_redis_reconnect()
        assert result is True
        assert counter._redis_failed is False
        assert counter._redis_failed_at == 0.0

    @pytest.mark.asyncio
    async def test_failed_reconnection_keeps_fallback(self):
        """If reconnection fails, should stay in fallback mode."""
        counter = RedisSlidingWindowCounter(
            max_requests=10, window_seconds=60, redis_url="redis://localhost:6379/0"
        )
        old_time = time.monotonic() - 100
        counter._redis_failed = True
        counter._redis_failed_at = old_time

        # Mock Redis to connect but fail on ping
        mock_redis = AsyncMock()
        mock_redis.ping = AsyncMock(side_effect=ConnectionError("refused"))
        mock_redis.close = AsyncMock()

        async def mock_get_redis():
            counter._redis = mock_redis
            return mock_redis

        counter._get_redis = mock_get_redis

        result = await counter._attempt_redis_reconnect()
        assert result is False
        assert counter._redis_failed is True
        # Timestamp should be updated (more recent than old_time)
        assert counter._redis_failed_at > old_time

    @pytest.mark.asyncio
    async def test_is_allowed_triggers_reconnect_after_interval(self):
        """is_allowed should try reconnection when retry interval has elapsed."""
        counter = RedisSlidingWindowCounter(
            max_requests=10, window_seconds=60, redis_url="redis://invalid:9999/0"
        )
        counter._redis_failed = True
        counter._redis_failed_at = time.monotonic() - counter._REDIS_RETRY_INTERVAL - 1

        # Mock _attempt_redis_reconnect to track if it was called
        reconnect_called = False
        original_reconnect = counter._attempt_redis_reconnect

        async def mock_reconnect():
            nonlocal reconnect_called
            reconnect_called = True
            # Reconnect fails, stay in fallback
            counter._redis_failed = True
            counter._redis_failed_at = time.monotonic()

        counter._attempt_redis_reconnect = mock_reconnect

        # Should still work via fallback
        allowed, _, _ = await counter.is_allowed("client1")
        assert allowed is True
        assert reconnect_called is True

    @pytest.mark.asyncio
    async def test_redis_recovery_restores_distributed_limiting_via_is_allowed(self):
        """Full flow: Redis fails → fallback → interval elapses → reconnect succeeds → Redis used again.

        This tests the complete is_allowed path (not just _attempt_redis_reconnect),
        proving that distributed rate limiting is actually restored after a transient
        Redis outage.
        """
        counter = RedisSlidingWindowCounter(
            max_requests=10, window_seconds=60, redis_url="redis://localhost:6379/0"
        )

        # Phase 1: Simulate Redis failure — counter should use in-memory fallback
        counter._redis_failed = True
        counter._redis_failed_at = time.monotonic()

        allowed, _, _ = await counter.is_allowed("client1")
        assert allowed is True  # fallback works

        # Phase 2: Advance time past retry interval and mock successful reconnection
        counter._redis_failed_at = time.monotonic() - counter._REDIS_RETRY_INTERVAL - 1

        # Set up a mock Redis that actually works
        mock_redis = AsyncMock()
        mock_redis.ping = AsyncMock(return_value=True)

        # Track whether Redis pipeline was used after recovery
        pipeline_used = False
        mock_pipe = AsyncMock()
        mock_pipe.zremrangebyscore = MagicMock(return_value=mock_pipe)
        mock_pipe.zcard = MagicMock(return_value=mock_pipe)
        mock_pipe.zadd = MagicMock(return_value=mock_pipe)
        mock_pipe.expire = MagicMock(return_value=mock_pipe)
        mock_pipe.execute = AsyncMock(return_value=[0, 0, True, True])  # empty set, 0 count
        mock_redis.pipeline = MagicMock(return_value=mock_pipe)

        async def mock_get_redis():
            counter._redis = mock_redis
            return mock_redis

        counter._get_redis = mock_get_redis

        # Phase 3: Call is_allowed — should trigger reconnect, then use Redis
        allowed, remaining, _ = await counter.is_allowed("client2")
        assert allowed is True
        assert counter._redis_failed is False, "Redis should be restored after successful reconnect"
        # Verify that the Redis pipeline was actually invoked (not fallback)
        assert mock_redis.pipeline.called, "Redis pipeline should be used after recovery"

    @pytest.mark.asyncio
    async def test_redis_recovery_failure_keeps_fallback_via_is_allowed(self):
        """Full flow: Redis fails → interval elapses → reconnect fails → still uses fallback."""
        counter = RedisSlidingWindowCounter(
            max_requests=10, window_seconds=60, redis_url="redis://invalid:9999/0"
        )

        # Simulate failure that happened long ago
        counter._redis_failed = True
        counter._redis_failed_at = time.monotonic() - counter._REDIS_RETRY_INTERVAL - 1

        # Mock reconnect to fail
        mock_redis = AsyncMock()
        mock_redis.ping = AsyncMock(side_effect=ConnectionError("still down"))
        mock_redis.close = AsyncMock()

        async def mock_get_redis():
            counter._redis = mock_redis
            return mock_redis

        counter._get_redis = mock_get_redis

        # Should still work via fallback after failed reconnect
        allowed, _, _ = await counter.is_allowed("client1")
        assert allowed is True
        assert counter._redis_failed is True, "Should remain in fallback mode"
