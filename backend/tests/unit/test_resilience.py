"""Unit tests for the circuit breaker and retry-with-backoff resilience patterns.

Tests cover:
- CircuitBreaker state transitions (CLOSED → OPEN → HALF_OPEN → CLOSED)
- CircuitBreaker opens after failure_threshold consecutive failures
- CircuitBreaker recovery after recovery_timeout
- CircuitBreakerOpenError raised when circuit is open
- RetryWithBackoff retries on transient errors
- RetryWithBackoff respects max_retries
- RetryWithBackoff does not retry non-retryable exceptions
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from app.core.resilience import (
    CircuitBreaker,
    CircuitBreakerOpenError,
    CircuitState,
    RetryWithBackoff,
)


# ── CircuitBreaker Tests ──────────────────────────────────────────────


class TestCircuitBreaker:
    """Tests for the CircuitBreaker class."""

    def test_initial_state_is_closed(self):
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout=10.0)
        assert cb.state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_successful_calls_keep_circuit_closed(self):
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout=10.0)

        async def success():
            return "ok"

        for _ in range(10):
            result = await cb.call(success)
            assert result == "ok"

        assert cb.state == CircuitState.CLOSED
        assert cb.failure_count == 0

    @pytest.mark.asyncio
    async def test_circuit_opens_after_failure_threshold(self):
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout=10.0)

        async def failing():
            raise ConnectionError("connection refused")

        for _ in range(3):
            with pytest.raises(ConnectionError):
                await cb.call(failing)

        assert cb.state == CircuitState.OPEN
        assert cb.failure_count == 3

    @pytest.mark.asyncio
    async def test_open_circuit_raises_circuit_breaker_error(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=60.0)

        async def failing():
            raise ConnectionError("fail")

        with pytest.raises(ConnectionError):
            await cb.call(failing)

        assert cb.state == CircuitState.OPEN

        async def should_not_run():
            return "ok"

        with pytest.raises(CircuitBreakerOpenError):
            await cb.call(should_not_run)

    @pytest.mark.asyncio
    async def test_circuit_transitions_to_half_open_after_recovery(self):
        cb = CircuitBreaker(
            failure_threshold=1,
            recovery_timeout=0.1,  # 100ms
            half_open_max_calls=1,
        )

        async def failing():
            raise ConnectionError("fail")

        with pytest.raises(ConnectionError):
            await cb.call(failing)

        assert cb.state == CircuitState.OPEN

        await asyncio.sleep(0.15)

        # Next call should be allowed (half-open)
        async def success():
            return "recovered"

        result = await cb.call(success)
        assert result == "recovered"
        assert cb.state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_half_open_failure_reopens_circuit(self):
        cb = CircuitBreaker(
            failure_threshold=1,
            recovery_timeout=0.1,
            half_open_max_calls=1,
        )

        async def failing():
            raise ConnectionError("fail")

        with pytest.raises(ConnectionError):
            await cb.call(failing)

        await asyncio.sleep(0.15)

        # Half-open: trial call fails → circuit reopens
        with pytest.raises(ConnectionError):
            await cb.call(failing)

        assert cb.state == CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_success_resets_failure_count(self):
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout=10.0)

        async def failing():
            raise ConnectionError("fail")

        async def success():
            return "ok"

        # Two failures
        with pytest.raises(ConnectionError):
            await cb.call(failing)
        with pytest.raises(ConnectionError):
            await cb.call(failing)

        assert cb.failure_count == 2

        # One success resets
        await cb.call(success)
        assert cb.failure_count == 0
        assert cb.state == CircuitState.CLOSED


    @pytest.mark.asyncio
    async def test_non_transient_errors_do_not_trip_breaker(self):
        """Validation/client errors should NOT increment the failure counter.

        This verifies the fix for the evaluator finding that 4xx/validation
        errors were incorrectly tripping the circuit breaker.
        """
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout=10.0)

        # Import clearinghouse exceptions to test the classification
        from app.core.clearinghouse.base import (
            ClearinghouseValidationError,
        )

        # Simulate 10 validation errors (non-transient)
        for _ in range(10):
            with pytest.raises(ClearinghouseValidationError):
                async with cb:
                    raise ClearinghouseValidationError(
                        "Bad request", errors=["Invalid field"]
                    )

        # Breaker should still be CLOSED — validation errors are non-transient
        assert cb.state == CircuitState.CLOSED
        assert cb.failure_count == 0

    @pytest.mark.asyncio
    async def test_transient_errors_do_trip_breaker(self):
        """Connection errors and timeouts SHOULD increment the failure counter."""
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout=10.0)

        for _ in range(3):
            with pytest.raises(ConnectionError):
                async with cb:
                    raise ConnectionError("upstream unreachable")

        assert cb.state == CircuitState.OPEN
        assert cb.failure_count == 3

    @pytest.mark.asyncio
    async def test_mixed_errors_only_transient_trip_breaker(self):
        """Mix of transient and non-transient errors: only transient ones count."""
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout=10.0)

        from app.core.clearinghouse.base import ClearinghouseValidationError

        # 5 validation errors (non-transient) — should not count
        for _ in range(5):
            with pytest.raises(ClearinghouseValidationError):
                async with cb:
                    raise ClearinghouseValidationError("bad", errors=[])

        assert cb.failure_count == 0

        # 2 connection errors (transient) — should count
        for _ in range(2):
            with pytest.raises(ConnectionError):
                async with cb:
                    raise ConnectionError("timeout")

        assert cb.failure_count == 2
        assert cb.state == CircuitState.CLOSED  # threshold is 3

        # 1 more connection error — should trip breaker
        with pytest.raises(ConnectionError):
            async with cb:
                raise ConnectionError("timeout")

        assert cb.failure_count == 3
        assert cb.state == CircuitState.OPEN


# ── RetryWithBackoff Tests ────────────────────────────────────────────


class TestRetryWithBackoff:
    """Tests for the RetryWithBackoff class."""

    @pytest.mark.asyncio
    async def test_success_on_first_try(self):
        retry = RetryWithBackoff(max_retries=3, base_delay=0.01)
        mock_fn = AsyncMock(return_value="ok")
        result = await retry.call(mock_fn)
        assert result == "ok"
        assert mock_fn.call_count == 1

    @pytest.mark.asyncio
    async def test_retries_on_transient_error(self):
        retry = RetryWithBackoff(
            max_retries=3,
            base_delay=0.01,
            retryable_exceptions=(ConnectionError,),
        )
        mock_fn = AsyncMock(side_effect=[ConnectionError("fail"), ConnectionError("fail"), "ok"])
        result = await retry.call(mock_fn)
        assert result == "ok"
        assert mock_fn.call_count == 3

    @pytest.mark.asyncio
    async def test_raises_after_max_retries_exceeded(self):
        retry = RetryWithBackoff(
            max_retries=2,
            base_delay=0.01,
            retryable_exceptions=(ConnectionError,),
        )
        mock_fn = AsyncMock(side_effect=ConnectionError("persistent failure"))

        with pytest.raises(ConnectionError, match="persistent failure"):
            await retry.call(mock_fn)

        # 1 initial + 2 retries = 3 total attempts
        assert mock_fn.call_count == 3

    @pytest.mark.asyncio
    async def test_does_not_retry_non_retryable_exceptions(self):
        retry = RetryWithBackoff(
            max_retries=3,
            base_delay=0.01,
            retryable_exceptions=(ConnectionError,),
        )
        mock_fn = AsyncMock(side_effect=ValueError("bad input"))

        with pytest.raises(ValueError, match="bad input"):
            await retry.call(mock_fn)

        assert mock_fn.call_count == 1

    @pytest.mark.asyncio
    async def test_retry_with_arguments(self):
        retry = RetryWithBackoff(max_retries=1, base_delay=0.01)
        mock_fn = AsyncMock(return_value="result")
        result = await retry.call(mock_fn, "arg1", key="val")
        assert result == "result"
        mock_fn.assert_called_once_with("arg1", key="val")
