"""Unit tests for check_status retry logic in clearinghouse clients.

Tests cover:
- check_status retries on ConnectError and succeeds on subsequent attempt
- check_status retries on TimeoutException and succeeds on subsequent attempt
- check_status raises ClearinghouseConnectionError after exhausting retries
- check_status does NOT retry on 404 (ClearinghouseError)
- CircuitBreakerOpenError is raised immediately without retries
"""

from __future__ import annotations

import json

import httpx
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from app.core.clearinghouse.availity import AvailityClient, _availity_breaker
from app.core.clearinghouse.claim_md import ClaimMDClient, _claim_md_breaker
from app.core.clearinghouse.base import (
    ClearinghouseConnectionError,
    ClearinghouseError,
    TransactionStatus,
)
from app.core.resilience import CircuitBreakerOpenError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _success_response(
    status_code: int = 200,
    *,
    tx_type_key: str = "transactionType",
    tx_type_val: str = "270",
) -> httpx.Response:
    """Build a fake successful status-check response."""
    body = {
        "status": "completed",
        tx_type_key: tx_type_val,
        "errors": [],
    }
    return httpx.Response(
        status_code=status_code,
        json=body,
        request=httpx.Request("GET", "https://example.com"),
    )


def _error_response(status_code: int, text: str = "error") -> httpx.Response:
    """Build a fake error response."""
    return httpx.Response(
        status_code=status_code,
        text=text,
        request=httpx.Request("GET", "https://example.com"),
    )


def _mock_client(side_effects: list):
    """Return a patched httpx.AsyncClient whose .get() uses *side_effects*."""
    mock_instance = AsyncMock()
    mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
    mock_instance.__aexit__ = AsyncMock(return_value=False)
    mock_instance.get = AsyncMock(side_effect=side_effects)
    return mock_instance


def _reset_breakers():
    """Force-reset both circuit breakers so tests are independent."""
    _availity_breaker._failure_count = 0
    _availity_breaker._state = "closed"
    _claim_md_breaker._failure_count = 0
    _claim_md_breaker._state = "closed"


# ---------------------------------------------------------------------------
# AvailityClient tests
# ---------------------------------------------------------------------------

class TestAvailityCheckStatusRetry:

    def setup_method(self):
        _reset_breakers()
        self.client = AvailityClient(
            api_endpoint="https://mock-availity.example.com",
            credentials={"api_key": "test-key"},
            max_retries=3,
        )

    @pytest.mark.asyncio
    async def test_retries_on_connect_error_then_succeeds(self):
        """check_status should retry on ConnectError and succeed on 2nd attempt."""
        effects = [
            httpx.ConnectError("Connection refused"),
            _success_response(),
        ]
        with patch("httpx.AsyncClient", return_value=_mock_client(effects)):
            result = await self.client.check_status("txn-001")

        assert result.transaction_id == "txn-001"
        assert result.status == TransactionStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_retries_on_timeout_then_succeeds(self):
        """check_status should retry on TimeoutException and succeed on 2nd attempt."""
        effects = [
            httpx.TimeoutException("read timed out"),
            _success_response(),
        ]
        with patch("httpx.AsyncClient", return_value=_mock_client(effects)):
            result = await self.client.check_status("txn-002")

        assert result.transaction_id == "txn-002"
        assert result.status == TransactionStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_raises_connection_error_after_exhausting_retries(self):
        """check_status should raise ClearinghouseConnectionError after all retries."""
        effects = [
            httpx.ConnectError("fail 1"),
            httpx.TimeoutException("fail 2"),
            httpx.ConnectError("fail 3"),
        ]
        with patch("httpx.AsyncClient", return_value=_mock_client(effects)):
            with pytest.raises(ClearinghouseConnectionError, match="after 3 attempts"):
                await self.client.check_status("txn-003")

    @pytest.mark.asyncio
    async def test_does_not_retry_on_404(self):
        """check_status should raise ClearinghouseError immediately on 404."""
        mock = _mock_client([_error_response(404, "not found")])
        with patch("httpx.AsyncClient", return_value=mock):
            with pytest.raises(ClearinghouseError, match="not found"):
                await self.client.check_status("txn-004")
        # .get() should have been called exactly once — no retry
        assert mock.get.call_count == 1

    @pytest.mark.asyncio
    async def test_circuit_breaker_open_raises_immediately(self):
        """CircuitBreakerOpenError should surface as ClearinghouseConnectionError."""
        with patch(
            "app.core.clearinghouse.availity._availity_breaker",
        ) as mock_breaker:
            mock_breaker.__aenter__ = AsyncMock(
                side_effect=CircuitBreakerOpenError("availity", remaining_seconds=30.0),
            )
            mock_breaker.__aexit__ = AsyncMock(return_value=False)
            with pytest.raises(ClearinghouseConnectionError, match="circuit breaker is OPEN"):
                await self.client.check_status("txn-005")


# ---------------------------------------------------------------------------
# ClaimMDClient tests
# ---------------------------------------------------------------------------

class TestClaimMDCheckStatusRetry:

    def setup_method(self):
        _reset_breakers()
        self.client = ClaimMDClient(
            api_endpoint="https://mock-claimmd.example.com",
            credentials={"api_key": "test-key", "account_key": "test-acct"},
            max_retries=3,
        )

    @pytest.mark.asyncio
    async def test_retries_on_connect_error_then_succeeds(self):
        """check_status should retry on ConnectError and succeed on 2nd attempt."""
        effects = [
            httpx.ConnectError("Connection refused"),
            _success_response(tx_type_key="type"),
        ]
        with patch("httpx.AsyncClient", return_value=_mock_client(effects)):
            result = await self.client.check_status("txn-101")

        assert result.transaction_id == "txn-101"
        assert result.status == TransactionStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_retries_on_timeout_then_succeeds(self):
        """check_status should retry on TimeoutException and succeed on 2nd attempt."""
        effects = [
            httpx.TimeoutException("read timed out"),
            _success_response(tx_type_key="type"),
        ]
        with patch("httpx.AsyncClient", return_value=_mock_client(effects)):
            result = await self.client.check_status("txn-102")

        assert result.transaction_id == "txn-102"
        assert result.status == TransactionStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_raises_connection_error_after_exhausting_retries(self):
        """check_status should raise ClearinghouseConnectionError after all retries."""
        effects = [
            httpx.ConnectError("fail 1"),
            httpx.TimeoutException("fail 2"),
            httpx.ConnectError("fail 3"),
        ]
        with patch("httpx.AsyncClient", return_value=_mock_client(effects)):
            with pytest.raises(ClearinghouseConnectionError, match="after 3 attempts"):
                await self.client.check_status("txn-103")

    @pytest.mark.asyncio
    async def test_does_not_retry_on_404(self):
        """check_status should raise ClearinghouseError immediately on 404."""
        mock = _mock_client([_error_response(404, "not found")])
        with patch("httpx.AsyncClient", return_value=mock):
            with pytest.raises(ClearinghouseError, match="not found"):
                await self.client.check_status("txn-104")
        assert mock.get.call_count == 1

    @pytest.mark.asyncio
    async def test_circuit_breaker_open_raises_immediately(self):
        """CircuitBreakerOpenError should surface as ClearinghouseConnectionError."""
        with patch(
            "app.core.clearinghouse.claim_md._claim_md_breaker",
        ) as mock_breaker:
            mock_breaker.__aenter__ = AsyncMock(
                side_effect=CircuitBreakerOpenError("claim_md", remaining_seconds=30.0),
            )
            mock_breaker.__aexit__ = AsyncMock(return_value=False)
            with pytest.raises(ClearinghouseConnectionError, match="circuit breaker is OPEN"):
                await self.client.check_status("txn-105")
