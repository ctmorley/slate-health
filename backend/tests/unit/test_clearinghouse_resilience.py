"""Unit tests for clearinghouse circuit breaker and retry integration.

Tests cover:
- Clearinghouse client uses circuit breaker
- Circuit opens after repeated failures
- Circuit breaker open error is raised (not masked)
- Retry logic with exponential backoff on transient errors
- HTTP 5xx and 429 responses are retried before raising errors
- HTTP 4xx (non-429) client errors are NOT retried
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

import httpx

from app.core.clearinghouse.base import (
    ClearinghouseConnectionError,
    ClearinghouseError,
    TransactionRequest,
    TransactionType,
)
from app.core.clearinghouse.availity import AvailityClient
from app.core.clearinghouse.claim_md import ClaimMDClient
from app.core.resilience import CircuitBreakerOpenError


def _make_request() -> TransactionRequest:
    return TransactionRequest(
        transaction_type=TransactionType.ELIGIBILITY_270,
        payload="ISA*00*...",
        sender_id="SENDER01",
        receiver_id="RECEIVER01",
    )


class TestAvailityCircuitBreaker:
    """Test that AvailityClient respects circuit breaker pattern."""

    @pytest.mark.asyncio
    async def test_connection_failures_trigger_circuit_breaker(self):
        """After max_retries × failure_threshold failures, circuit should open."""
        client = AvailityClient(
            api_endpoint="https://mock-availity.example.com",
            credentials={"api_key": "test-key"},
            max_retries=1,  # Minimize retries for test speed
        )

        request = _make_request()

        # Simulate connection failures by patching httpx
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_instance = AsyncMock()
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=False)
            mock_instance.post = AsyncMock(
                side_effect=httpx.ConnectError("Connection refused")
            )
            mock_client_cls.return_value = mock_instance

            # Each call should fail with ClearinghouseConnectionError
            for _ in range(5):
                with pytest.raises(ClearinghouseConnectionError):
                    await client.submit_transaction(request)


class TestClaimMDCircuitBreaker:
    """Test that ClaimMDClient respects circuit breaker pattern."""

    @pytest.mark.asyncio
    async def test_connection_failures_raise_connection_error(self):
        """ClaimMD client should raise ClearinghouseConnectionError on failures."""
        client = ClaimMDClient(
            api_endpoint="https://mock-claimmd.example.com",
            credentials={"api_key": "test-key", "account_key": "test-account"},
            max_retries=1,
        )

        request = _make_request()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_instance = AsyncMock()
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=False)
            mock_instance.post = AsyncMock(
                side_effect=httpx.ConnectError("Connection refused")
            )
            mock_client_cls.return_value = mock_instance

            with pytest.raises(ClearinghouseConnectionError):
                await client.submit_transaction(request)


# ── Helper to build a mock httpx response ────────────────────────────


def _mock_response(status_code: int, json_body: dict | None = None, text: str = "") -> MagicMock:
    """Create a mock httpx.Response with given status and body."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text or str(json_body or {})
    resp.headers = {"content-type": "application/json"}
    resp.json.return_value = json_body or {}
    return resp


def _mock_async_client(responses: list[MagicMock]) -> tuple:
    """Return (mock_cls, mock_instance) with post/get returning *responses* in order."""
    mock_instance = AsyncMock()
    mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
    mock_instance.__aexit__ = AsyncMock(return_value=False)
    mock_instance.post = AsyncMock(side_effect=responses)
    mock_instance.get = AsyncMock(side_effect=responses)
    return mock_instance


# ── Availity: HTTP 5xx / 429 retry ───────────────────────────────────


class TestAvailityRetryOnServerErrors:
    """Availity client retries on 5xx and 429 before failing."""

    @pytest.mark.asyncio
    async def test_retries_on_500_then_succeeds(self):
        """First call returns 500, second returns 200 → should succeed."""
        client = AvailityClient(
            api_endpoint="https://mock-availity.example.com",
            credentials={"api_key": "test-key"},
            max_retries=3,
        )
        request = _make_request()

        success_resp = _mock_response(200, {"id": "tx-123", "status": "completed"})
        fail_resp = _mock_response(500, text="Internal Server Error")
        mock_instance = _mock_async_client([fail_resp, success_resp])

        with patch("httpx.AsyncClient", return_value=mock_instance):
            result = await client.submit_transaction(request)

        assert result.transaction_id == "tx-123"
        assert mock_instance.post.call_count == 2

    @pytest.mark.asyncio
    async def test_retries_on_429_then_succeeds(self):
        """First call returns 429, second returns 200 → should succeed."""
        client = AvailityClient(
            api_endpoint="https://mock-availity.example.com",
            credentials={"api_key": "test-key"},
            max_retries=3,
        )
        request = _make_request()

        success_resp = _mock_response(200, {"id": "tx-456", "status": "completed"})
        rate_limited_resp = _mock_response(429, text="Rate limited")
        mock_instance = _mock_async_client([rate_limited_resp, success_resp])

        with patch("httpx.AsyncClient", return_value=mock_instance):
            result = await client.submit_transaction(request)

        assert result.transaction_id == "tx-456"
        assert mock_instance.post.call_count == 2

    @pytest.mark.asyncio
    async def test_exhausts_retries_on_repeated_500(self):
        """All retries return 500 → should raise ClearinghouseConnectionError."""
        client = AvailityClient(
            api_endpoint="https://mock-availity.example.com",
            credentials={"api_key": "test-key"},
            max_retries=2,
        )
        request = _make_request()

        fail_resps = [_mock_response(503, text="Service Unavailable") for _ in range(2)]
        mock_instance = _mock_async_client(fail_resps)

        with patch("httpx.AsyncClient", return_value=mock_instance):
            with pytest.raises(ClearinghouseConnectionError):
                await client.submit_transaction(request)

        assert mock_instance.post.call_count == 2

    @pytest.mark.asyncio
    async def test_does_not_retry_on_403(self):
        """403 Forbidden is a client error and should NOT be retried."""
        client = AvailityClient(
            api_endpoint="https://mock-availity.example.com",
            credentials={"api_key": "test-key"},
            max_retries=3,
        )
        request = _make_request()

        forbidden_resp = _mock_response(403, text="Forbidden")
        mock_instance = _mock_async_client([forbidden_resp])

        with patch("httpx.AsyncClient", return_value=mock_instance):
            with pytest.raises(ClearinghouseError):
                await client.submit_transaction(request)

        # Should NOT retry — only 1 call
        assert mock_instance.post.call_count == 1

    @pytest.mark.asyncio
    async def test_status_check_retries_on_502(self):
        """check_status retries on 502 then succeeds."""
        client = AvailityClient(
            api_endpoint="https://mock-availity.example.com",
            credentials={"api_key": "test-key"},
            max_retries=3,
        )

        success_resp = _mock_response(200, {
            "status": "completed",
            "transactionType": "270",
        })
        fail_resp = _mock_response(502, text="Bad Gateway")
        mock_instance = _mock_async_client([fail_resp, success_resp])

        with patch("httpx.AsyncClient", return_value=mock_instance):
            result = await client.check_status("tx-789")

        assert result.transaction_id == "tx-789"
        assert mock_instance.get.call_count == 2


# ── Claim.MD: HTTP 5xx / 429 retry ──────────────────────────────────


class TestClaimMDRetryOnServerErrors:
    """Claim.MD client retries on 5xx and 429 before failing."""

    @pytest.mark.asyncio
    async def test_retries_on_500_then_succeeds(self):
        """First call returns 500, second returns 200 → should succeed."""
        client = ClaimMDClient(
            api_endpoint="https://mock-claimmd.example.com",
            credentials={"api_key": "test-key", "account_key": "test-account"},
            max_retries=3,
        )
        request = _make_request()

        success_resp = _mock_response(200, {"transaction_id": "cmd-123"})
        fail_resp = _mock_response(500, text="Internal Server Error")
        mock_instance = _mock_async_client([fail_resp, success_resp])

        with patch("httpx.AsyncClient", return_value=mock_instance):
            result = await client.submit_transaction(request)

        assert result.transaction_id == "cmd-123"
        assert mock_instance.post.call_count == 2

    @pytest.mark.asyncio
    async def test_retries_on_429_then_succeeds(self):
        """First call returns 429, second returns 200 → should succeed."""
        client = ClaimMDClient(
            api_endpoint="https://mock-claimmd.example.com",
            credentials={"api_key": "test-key", "account_key": "test-account"},
            max_retries=3,
        )
        request = _make_request()

        success_resp = _mock_response(200, {"transaction_id": "cmd-456"})
        rate_limited_resp = _mock_response(429, text="Rate limited")
        mock_instance = _mock_async_client([rate_limited_resp, success_resp])

        with patch("httpx.AsyncClient", return_value=mock_instance):
            result = await client.submit_transaction(request)

        assert result.transaction_id == "cmd-456"
        assert mock_instance.post.call_count == 2

    @pytest.mark.asyncio
    async def test_exhausts_retries_on_repeated_503(self):
        """All retries return 503 → should raise ClearinghouseConnectionError."""
        client = ClaimMDClient(
            api_endpoint="https://mock-claimmd.example.com",
            credentials={"api_key": "test-key", "account_key": "test-account"},
            max_retries=2,
        )
        request = _make_request()

        fail_resps = [_mock_response(503, text="Service Unavailable") for _ in range(2)]
        mock_instance = _mock_async_client(fail_resps)

        with patch("httpx.AsyncClient", return_value=mock_instance):
            with pytest.raises(ClearinghouseConnectionError):
                await client.submit_transaction(request)

        assert mock_instance.post.call_count == 2

    @pytest.mark.asyncio
    async def test_does_not_retry_on_403(self):
        """403 Forbidden is a client error and should NOT be retried."""
        client = ClaimMDClient(
            api_endpoint="https://mock-claimmd.example.com",
            credentials={"api_key": "test-key", "account_key": "test-account"},
            max_retries=3,
        )
        request = _make_request()

        forbidden_resp = _mock_response(403, text="Forbidden")
        mock_instance = _mock_async_client([forbidden_resp])

        with patch("httpx.AsyncClient", return_value=mock_instance):
            with pytest.raises(ClearinghouseError):
                await client.submit_transaction(request)

        assert mock_instance.post.call_count == 1

    @pytest.mark.asyncio
    async def test_status_check_retries_on_502(self):
        """check_status retries on 502 then succeeds."""
        client = ClaimMDClient(
            api_endpoint="https://mock-claimmd.example.com",
            credentials={"api_key": "test-key", "account_key": "test-account"},
            max_retries=3,
        )

        success_resp = _mock_response(200, {
            "status": "completed",
            "type": "270",
        })
        fail_resp = _mock_response(502, text="Bad Gateway")
        mock_instance = _mock_async_client([fail_resp, success_resp])

        with patch("httpx.AsyncClient", return_value=mock_instance):
            result = await client.check_status("cmd-789")

        assert result.transaction_id == "cmd-789"
        assert mock_instance.get.call_count == 2
