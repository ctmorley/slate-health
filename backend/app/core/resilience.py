"""Circuit breaker and retry-with-backoff resilience patterns.

These utilities protect against cascading failures when calling external
services (FHIR servers, payer APIs, OIG/NPPES endpoints, etc.).
"""

from __future__ import annotations

import asyncio
import enum
import random
import time
from typing import Any, Tuple, Type


class CircuitBreakerOpenError(Exception):
    """Raised when a call is attempted while the circuit breaker is open."""

    def __init__(self, breaker_name: str, remaining_seconds: float) -> None:
        self.breaker_name = breaker_name
        self.remaining_seconds = remaining_seconds
        super().__init__(
            f"Circuit breaker '{breaker_name}' is OPEN. "
            f"Retry after {remaining_seconds:.1f}s."
        )


class CircuitState(enum.Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Async-compatible circuit breaker.

    Usage::

        cb = CircuitBreaker(name="fhir")

        async with cb:
            result = await call_fhir_server()

    The circuit opens after *failure_threshold* consecutive failures.  After
    *recovery_timeout* seconds it transitions to HALF_OPEN and permits up to
    *half_open_max_calls* trial requests.  A successful trial resets the
    breaker to CLOSED; a failed trial reopens it.
    """

    def __init__(
        self,
        name: str = "default",
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        half_open_max_calls: int = 1,
    ) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_max_calls = half_open_max_calls

        self._state = CircuitState.CLOSED
        self._failure_count: int = 0
        self._last_failure_time: float = 0.0
        self._half_open_calls: int = 0
        self._lock = asyncio.Lock()

    # ── Read-only properties ─────────────────────────────────────────

    @property
    def state(self) -> CircuitState:
        """Return the current circuit state, accounting for recovery timeout."""
        if (
            self._state is CircuitState.OPEN
            and time.monotonic() - self._last_failure_time >= self.recovery_timeout
        ):
            return CircuitState.HALF_OPEN
        return self._state

    @property
    def failure_count(self) -> int:
        return self._failure_count

    # ── Async context manager interface ──────────────────────────────

    async def __aenter__(self) -> "CircuitBreaker":
        async with self._lock:
            current = self.state

            if current is CircuitState.OPEN:
                remaining = self.recovery_timeout - (
                    time.monotonic() - self._last_failure_time
                )
                raise CircuitBreakerOpenError(self.name, max(remaining, 0.0))

            if current is CircuitState.HALF_OPEN:
                if self._half_open_calls >= self.half_open_max_calls:
                    raise CircuitBreakerOpenError(self.name, 0.0)
                self._half_open_calls += 1

            # Transition internal state if we detected recovery timeout
            if self._state is CircuitState.OPEN and current is CircuitState.HALF_OPEN:
                self._state = CircuitState.HALF_OPEN
                self._half_open_calls = 1  # count this call

        return self

    async def __aexit__(
        self,
        exc_type: Type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> bool:
        async with self._lock:
            if exc_type is None:
                # Success path
                self._reset()
            elif self._is_transient_failure(exc_type, exc_val):
                # Only count transient/upstream failures toward breaker threshold.
                # Client errors (validation, 4xx) should NOT trip the breaker.
                self._record_failure()
            else:
                # Non-transient error (e.g. validation, client mistake).
                # Do NOT increment the failure counter — the upstream service
                # is healthy; the request was simply invalid.
                pass
        # Never suppress the exception
        return False

    @staticmethod
    def _is_transient_failure(
        exc_type: Type[BaseException] | None,
        exc_val: BaseException | None,
    ) -> bool:
        """Determine whether an exception represents a transient upstream failure.

        Returns True for connection errors, timeouts, and server-side (5xx)
        errors — i.e. problems with the remote service itself.

        Returns False for client-side validation errors, 4xx responses, and
        other non-transient issues that should not count toward the circuit
        breaker threshold.
        """
        if exc_type is None:
            return False

        # Import locally to avoid hard dependency at module level
        try:
            import httpx as _httpx
            if issubclass(exc_type, (_httpx.ConnectError, _httpx.TimeoutException)):
                return True
        except ImportError:
            pass

        # Check for our own clearinghouse exception hierarchy
        try:
            from app.core.clearinghouse.base import (
                ClearinghouseConnectionError,
                ClearinghouseValidationError,
                ClearinghouseError,
            )
            # Connection errors are always transient
            if issubclass(exc_type, ClearinghouseConnectionError):
                return True
            # Validation errors are never transient
            if issubclass(exc_type, ClearinghouseValidationError):
                return False
            # Generic clearinghouse errors: check if the message hints at a
            # server-side issue (5xx, retryable) vs client issue (4xx).
            if issubclass(exc_type, ClearinghouseError) and exc_val is not None:
                msg = str(exc_val)
                # Retryable errors explicitly mention retryable or 5xx codes
                if "retryable" in msg.lower() or "50" in msg[:30]:
                    return True
                # Client-side 4xx errors are not transient
                return False
        except ImportError:
            pass

        # Fallback: OSError / ConnectionError subclasses are transient
        if issubclass(exc_type, (ConnectionError, OSError, TimeoutError)):
            return True

        # Default: treat unknown exceptions as transient to be safe
        return True

    # ── Internal helpers ─────────────────────────────────────────────

    def reset(self) -> None:
        """Reset breaker to healthy closed state (public API for testing)."""
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._half_open_calls = 0

    # Keep private alias for internal use
    _reset = reset

    def _record_failure(self) -> None:
        """Record a failure and potentially open the breaker."""
        self._failure_count += 1
        self._last_failure_time = time.monotonic()

        if self._state is CircuitState.HALF_OPEN:
            # Trial call failed — reopen immediately
            self._state = CircuitState.OPEN
            self._half_open_calls = 0
        elif self._failure_count >= self.failure_threshold:
            self._state = CircuitState.OPEN
            self._half_open_calls = 0

    async def call(self, func: Any, *args: Any, **kwargs: Any) -> Any:
        """Convenience method: execute *func* under circuit breaker protection."""
        async with self:
            return await func(*args, **kwargs)


class RetryWithBackoff:
    """Retry an async callable with exponential backoff and jitter.

    Usage::

        retry = RetryWithBackoff(max_retries=3, retryable_exceptions=(httpx.TimeoutException,))
        result = await retry.execute(some_async_function, arg1, kwarg1=val)
    """

    def __init__(
        self,
        max_retries: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 30.0,
        retryable_exceptions: Tuple[Type[BaseException], ...] = (Exception,),
    ) -> None:
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.retryable_exceptions = retryable_exceptions

    async def execute(self, func: Any, *args: Any, **kwargs: Any) -> Any:
        """Call *func* with retries on retryable exceptions.

        After exhausting all retries the last exception is re-raised.
        """
        last_exc: BaseException | None = None

        for attempt in range(self.max_retries + 1):
            try:
                return await func(*args, **kwargs)
            except self.retryable_exceptions as exc:
                last_exc = exc
                if attempt == self.max_retries:
                    raise
                delay = self._compute_delay(attempt)
                await asyncio.sleep(delay)

        # Should never reach here, but satisfy type checker
        raise last_exc  # type: ignore[misc]

    async def call(self, func: Any, *args: Any, **kwargs: Any) -> Any:
        """Alias for :meth:`execute` for a more concise API."""
        return await self.execute(func, *args, **kwargs)

    def _compute_delay(self, attempt: int) -> float:
        """Exponential backoff with full jitter (AWS-style)."""
        exp_delay = self.base_delay * (2 ** attempt)
        capped = min(exp_delay, self.max_delay)
        # Full jitter: uniform random between 0 and capped delay
        return random.uniform(0, capped)


# ── Convenience: pre-configured retry for external HTTP calls ─────


#: Standard retry policy for external HTTP calls (OIDC, SAML, OIG, NPPES,
#: etc.).  Retries on connection errors and timeouts with exponential
#: backoff (1 s → 2 s → 4 s, max 3 retries).
HTTP_RETRY = RetryWithBackoff(
    max_retries=3,
    base_delay=1.0,
    max_delay=10.0,
    retryable_exceptions=(Exception,),  # narrowed at call site
)


async def resilient_http_get(
    url: str,
    *,
    timeout: float = 10.0,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
    max_retries: int = 3,
) -> "httpx.Response":
    """GET with automatic retry on transient errors.

    Retries on ``httpx.ConnectError``, ``httpx.TimeoutException``, and
    ``httpx.HTTPStatusError`` for 5xx responses.
    """
    import httpx as _httpx

    retry = RetryWithBackoff(
        max_retries=max_retries,
        base_delay=1.0,
        max_delay=10.0,
        retryable_exceptions=(
            _httpx.ConnectError,
            _httpx.TimeoutException,
        ),
    )

    async def _do_get() -> _httpx.Response:
        async with _httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url, headers=headers, params=params)
            # Retry on 5xx server errors
            if resp.status_code >= 500:
                raise _httpx.ConnectError(
                    f"Server error {resp.status_code} from {url}"
                )
            return resp

    return await retry.execute(_do_get)


async def resilient_http_post(
    url: str,
    *,
    data: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
    timeout: float = 10.0,
    headers: dict[str, str] | None = None,
    max_retries: int = 3,
) -> "httpx.Response":
    """POST with automatic retry on transient errors."""
    import httpx as _httpx

    retry = RetryWithBackoff(
        max_retries=max_retries,
        base_delay=1.0,
        max_delay=10.0,
        retryable_exceptions=(
            _httpx.ConnectError,
            _httpx.TimeoutException,
        ),
    )

    async def _do_post() -> _httpx.Response:
        async with _httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                url, data=data, json=json_body, headers=headers,
            )
            if resp.status_code >= 500:
                raise _httpx.ConnectError(
                    f"Server error {resp.status_code} from {url}"
                )
            return resp

    return await retry.execute(_do_post)
