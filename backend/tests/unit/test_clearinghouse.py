"""Unit tests for clearinghouse clients and factory.

Tests cover:
- BaseClearinghouse validation
- AvailityClient: submission, status check, response parsing, validation,
  outbound request URL/headers/body assertions
- ClaimMDClient: same coverage as Availity with Claim.MD-specific assertions
- Factory: correct client selection, error handling, registration
- Unknown transaction type handling in check_status
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch, call

import httpx
import pytest

from app.core.clearinghouse.availity import AvailityClient
from app.core.clearinghouse.base import (
    BaseClearinghouse,
    ClearinghouseConnectionError,
    ClearinghouseError,
    ClearinghouseValidationError,
    TransactionRequest,
    TransactionResponse,
    TransactionStatus,
    TransactionType,
)
from app.core.clearinghouse.claim_md import ClaimMDClient
from app.core.clearinghouse.factory import (
    get_clearinghouse,
    get_clearinghouse_from_config,
    list_clearinghouses,
)


# ── Helpers ─────────────────────────────────────────────────────────────


def _make_request(
    tx_type: TransactionType = TransactionType.ELIGIBILITY_270,
    payload: str = "ISA*00*...",
    sender_id: str = "SENDER01",
    receiver_id: str = "RECEIVER01",
) -> TransactionRequest:
    return TransactionRequest(
        transaction_type=tx_type,
        payload=payload,
        sender_id=sender_id,
        receiver_id=receiver_id,
        control_number="000000001",
    )


def _mock_httpx_response(
    status_code: int = 200,
    json_data: dict | None = None,
    text: str = "",
) -> httpx.Response:
    """Create a mock httpx.Response."""
    content = json.dumps(json_data).encode() if json_data else text.encode()
    headers = {"content-type": "application/json"} if json_data else {}
    return httpx.Response(
        status_code=status_code,
        content=content,
        headers=headers,
        request=httpx.Request("POST", "http://test"),
    )


# ── BaseClearinghouse Tests ─────────────────────────────────────────────


class TestTransactionTypes:
    def test_all_transaction_types_have_values(self):
        assert TransactionType.ELIGIBILITY_270.value == "270"
        assert TransactionType.CLAIM_837P.value == "837P"
        assert TransactionType.CLAIM_STATUS_276.value == "276"
        assert TransactionType.REMITTANCE_835.value == "835"
        assert TransactionType.PRIOR_AUTH_278.value == "278"

    def test_transaction_statuses(self):
        assert TransactionStatus.PENDING.value == "pending"
        assert TransactionStatus.COMPLETED.value == "completed"
        assert TransactionStatus.REJECTED.value == "rejected"


class TestTransactionRequest:
    def test_create_request(self):
        req = _make_request()
        assert req.transaction_type == TransactionType.ELIGIBILITY_270
        assert req.payload == "ISA*00*..."
        assert req.sender_id == "SENDER01"

    def test_default_metadata(self):
        req = TransactionRequest(
            transaction_type=TransactionType.ELIGIBILITY_270,
            payload="test",
        )
        assert req.metadata == {}


class TestTransactionResponse:
    def test_create_response(self):
        resp = TransactionResponse(
            transaction_id="TX-001",
            transaction_type=TransactionType.ELIGIBILITY_270,
            status=TransactionStatus.COMPLETED,
        )
        assert resp.transaction_id == "TX-001"
        assert resp.status == TransactionStatus.COMPLETED
        assert resp.errors == []


class TestClearinghouseErrors:
    def test_base_error(self):
        err = ClearinghouseError("test error", transaction_id="TX-001", errors=["e1"])
        assert str(err) == "test error"
        assert err.transaction_id == "TX-001"
        assert err.errors == ["e1"]

    def test_connection_error_is_clearinghouse_error(self):
        err = ClearinghouseConnectionError("timeout")
        assert isinstance(err, ClearinghouseError)

    def test_validation_error_is_clearinghouse_error(self):
        err = ClearinghouseValidationError("bad input")
        assert isinstance(err, ClearinghouseError)


# ── AvailityClient Tests ───────────────────────────────────────────────


class TestAvailityClient:
    def setup_method(self):
        self.client = AvailityClient(
            api_endpoint="https://api.availity.com",
            credentials={"api_key": "test-key", "customer_id": "CUST01"},
            timeout=10.0,
            max_retries=2,
        )

    def test_name(self):
        assert self.client.name == "availity"

    def test_supported_transactions(self):
        supported = self.client.supported_transactions
        assert TransactionType.ELIGIBILITY_270 in supported
        assert TransactionType.CLAIM_837P in supported
        assert TransactionType.CLAIM_STATUS_276 in supported

    def test_auth_headers_with_api_key(self):
        headers = self.client._get_auth_headers()
        assert headers["Authorization"] == "Bearer test-key"
        assert headers["X-Availity-Customer-ID"] == "CUST01"

    def test_auth_headers_without_credentials(self):
        client = AvailityClient(
            api_endpoint="https://test.com",
            credentials={},
        )
        headers = client._get_auth_headers()
        assert "Authorization" not in headers

    def test_endpoint_paths(self):
        assert "/coverages" in self.client._get_endpoint_path(TransactionType.ELIGIBILITY_270)
        assert "/claim-submissions" in self.client._get_endpoint_path(TransactionType.CLAIM_837P)
        assert "/claim-statuses" in self.client._get_endpoint_path(TransactionType.CLAIM_STATUS_276)

    def test_validate_missing_api_key(self):
        client = AvailityClient(api_endpoint="https://test.com", credentials={})
        req = _make_request()
        errors = client.validate_transaction(req)
        assert any("API key" in e for e in errors)

    def test_validate_missing_sender_id(self):
        req = _make_request(sender_id="")
        errors = self.client.validate_transaction(req)
        assert any("Sender ID" in e for e in errors)

    def test_validate_empty_payload(self):
        req = _make_request(payload="")
        errors = self.client.validate_transaction(req)
        assert any("empty" in e.lower() for e in errors)

    def test_validate_unsupported_type(self):
        # 271 is a response type, not submittable
        req = _make_request(tx_type=TransactionType.ELIGIBILITY_271)
        errors = self.client.validate_transaction(req)
        assert any("not supported" in e.lower() for e in errors)

    @pytest.mark.asyncio
    async def test_submit_eligibility_success(self):
        mock_resp = _mock_httpx_response(200, {"id": "TX-001", "status": "completed"})

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp):
            resp = await self.client.submit_transaction(_make_request())

        assert resp.transaction_id == "TX-001"
        assert resp.status == TransactionStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_submit_eligibility_outbound_request(self):
        """Verify outbound URL, headers, and body sent to Availity."""
        mock_resp = _mock_httpx_response(200, {"id": "TX-OUT"})

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp) as mock_post:
            req = _make_request()
            await self.client.submit_transaction(req)

            # Verify the outbound call
            mock_post.assert_called_once()
            call_args = mock_post.call_args

            # URL should be base endpoint + /availity/v1/coverages
            url = call_args.args[0] if call_args.args else call_args.kwargs.get("url", "")
            assert url == "https://api.availity.com/availity/v1/coverages"

            # Headers should include auth
            headers = call_args.kwargs.get("headers", {})
            assert headers["Authorization"] == "Bearer test-key"
            assert headers["X-Availity-Customer-ID"] == "CUST01"
            assert headers["Content-Type"] == "application/json"

            # Body should include transaction details
            body = call_args.kwargs.get("json", {})
            assert body["transactionType"] == "270"
            assert body["senderId"] == "SENDER01"
            assert body["receiverId"] == "RECEIVER01"
            assert body["controlNumber"] == "000000001"
            assert body["payload"] == "ISA*00*..."

    @pytest.mark.asyncio
    async def test_submit_claims_outbound_url(self):
        """Verify 837P submission goes to the claim-submissions endpoint."""
        mock_resp = _mock_httpx_response(200, {"id": "TX-CLM"})

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp) as mock_post:
            req = _make_request(tx_type=TransactionType.CLAIM_837P)
            await self.client.submit_transaction(req)

            url = mock_post.call_args.args[0] if mock_post.call_args.args else ""
            assert "/claim-submissions" in url

    @pytest.mark.asyncio
    async def test_submit_claims_returns_submitted(self):
        mock_resp = _mock_httpx_response(200, {"id": "TX-002"})

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp):
            req = _make_request(tx_type=TransactionType.CLAIM_837P)
            resp = await self.client.submit_transaction(req)

        assert resp.status == TransactionStatus.SUBMITTED

    @pytest.mark.asyncio
    async def test_submit_validation_error_422(self):
        mock_resp = _mock_httpx_response(422, {"errors": ["Invalid segment"]})

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp):
            with pytest.raises(ClearinghouseValidationError):
                await self.client.submit_transaction(_make_request())

    @pytest.mark.asyncio
    async def test_submit_server_error(self):
        mock_resp = _mock_httpx_response(500, text="Internal Server Error")

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp):
            with pytest.raises(ClearinghouseError):
                await self.client.submit_transaction(_make_request())

    @pytest.mark.asyncio
    async def test_submit_connection_error_retries(self):
        with patch(
            "httpx.AsyncClient.post",
            new_callable=AsyncMock,
            side_effect=httpx.ConnectError("refused"),
        ):
            with pytest.raises(ClearinghouseConnectionError) as exc_info:
                await self.client.submit_transaction(_make_request())
            assert "2 attempts" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_submit_timeout_retries(self):
        with patch(
            "httpx.AsyncClient.post",
            new_callable=AsyncMock,
            side_effect=httpx.TimeoutException("timeout"),
        ):
            with pytest.raises(ClearinghouseConnectionError):
                await self.client.submit_transaction(_make_request())

    @pytest.mark.asyncio
    async def test_submit_unsupported_type_raises(self):
        req = _make_request(tx_type=TransactionType.ELIGIBILITY_271)
        with pytest.raises(ClearinghouseValidationError):
            await self.client.submit_transaction(req)

    @pytest.mark.asyncio
    async def test_check_status_success(self):
        mock_resp = _mock_httpx_response(200, {
            "status": "completed",
            "transactionType": "270",
        })

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_resp):
            resp = await self.client.check_status("TX-001")

        assert resp.transaction_id == "TX-001"
        assert resp.status == TransactionStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_check_status_outbound_request(self):
        """Verify outbound GET URL and headers for status check."""
        mock_resp = _mock_httpx_response(200, {
            "status": "pending",
            "transactionType": "270",
        })

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_resp) as mock_get:
            await self.client.check_status("TX-STATUS-001")

            mock_get.assert_called_once()
            call_args = mock_get.call_args

            url = call_args.args[0] if call_args.args else ""
            assert url == "https://api.availity.com/availity/v1/transactions/TX-STATUS-001/status"

            headers = call_args.kwargs.get("headers", {})
            assert headers["Authorization"] == "Bearer test-key"

    @pytest.mark.asyncio
    async def test_check_status_not_found(self):
        mock_resp = _mock_httpx_response(404)

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_resp):
            with pytest.raises(ClearinghouseError, match="not found"):
                await self.client.check_status("TX-999")

    @pytest.mark.asyncio
    async def test_check_status_connection_error(self):
        with patch(
            "httpx.AsyncClient.get",
            new_callable=AsyncMock,
            side_effect=httpx.ConnectError("refused"),
        ):
            with pytest.raises(ClearinghouseConnectionError):
                await self.client.check_status("TX-001")

    @pytest.mark.asyncio
    async def test_check_status_unknown_transaction_type(self):
        """Unknown transactionType in response raises ClearinghouseError."""
        mock_resp = _mock_httpx_response(200, {
            "status": "completed",
            "transactionType": "INVALID_TYPE",
        })

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_resp):
            with pytest.raises(ClearinghouseError, match="Unknown transaction type"):
                await self.client.check_status("TX-BAD-TYPE")

    @pytest.mark.asyncio
    async def test_parse_271_response(self):
        # Use a minimal 271 response
        raw_271 = (
            "ST*271*0001~"
            "NM1*PR*2*Test Payer*****PI*PAYER01~"
            "NM1*IL*1*DOE*JANE****MI*MEM001~"
            "EB*1**30~"
            "SE*4*0001~"
        )
        result = await self.client.parse_response(raw_271, TransactionType.ELIGIBILITY_271)
        assert result["transaction_type"] == "271"
        assert result["coverage"]["active"] is True

    @pytest.mark.asyncio
    async def test_parse_unknown_type_returns_raw(self):
        result = await self.client.parse_response("raw data", TransactionType.ELIGIBILITY_270)
        assert result["raw"] == "raw data"


# ── ClaimMDClient Tests ────────────────────────────────────────────────


class TestClaimMDClient:
    def setup_method(self):
        self.client = ClaimMDClient(
            api_endpoint="https://api.claim.md",
            credentials={"api_key": "cmd-key", "account_key": "acct-key"},
            timeout=10.0,
            max_retries=2,
        )

    def test_name(self):
        assert self.client.name == "claim_md"

    def test_supported_transactions(self):
        supported = self.client.supported_transactions
        assert TransactionType.ELIGIBILITY_270 in supported
        assert TransactionType.CLAIM_837P in supported
        assert TransactionType.PRIOR_AUTH_278 in supported

    def test_auth_headers(self):
        headers = self.client._get_auth_headers()
        assert headers["X-ClaimMD-API-Key"] == "cmd-key"
        assert headers["X-ClaimMD-Account-Key"] == "acct-key"

    def test_endpoint_paths(self):
        assert "/eligibility" in self.client._get_endpoint_path(TransactionType.ELIGIBILITY_270)
        assert "/claims/professional" in self.client._get_endpoint_path(TransactionType.CLAIM_837P)
        assert "/claims/institutional" in self.client._get_endpoint_path(TransactionType.CLAIM_837I)

    def test_validate_missing_api_key(self):
        client = ClaimMDClient(api_endpoint="https://test.com", credentials={})
        errors = client.validate_transaction(_make_request())
        assert any("API key" in e for e in errors)

    def test_validate_missing_account_key(self):
        client = ClaimMDClient(
            api_endpoint="https://test.com",
            credentials={"api_key": "key"},
        )
        errors = client.validate_transaction(_make_request())
        assert any("account key" in e.lower() for e in errors)

    @pytest.mark.asyncio
    async def test_submit_eligibility_success(self):
        mock_resp = _mock_httpx_response(200, {"transaction_id": "CMD-001"})

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp):
            resp = await self.client.submit_transaction(_make_request())

        assert resp.transaction_id == "CMD-001"
        assert resp.status == TransactionStatus.COMPLETED  # eligibility is real-time

    @pytest.mark.asyncio
    async def test_submit_eligibility_outbound_request(self):
        """Verify outbound URL, headers, and body sent to Claim.MD."""
        mock_resp = _mock_httpx_response(200, {"transaction_id": "CMD-OUT"})

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp) as mock_post:
            req = _make_request()
            await self.client.submit_transaction(req)

            mock_post.assert_called_once()
            call_args = mock_post.call_args

            # URL should be base endpoint + /api/v2/eligibility
            url = call_args.args[0] if call_args.args else call_args.kwargs.get("url", "")
            assert url == "https://api.claim.md/api/v2/eligibility"

            # Headers should include Claim.MD auth
            headers = call_args.kwargs.get("headers", {})
            assert headers["X-ClaimMD-API-Key"] == "cmd-key"
            assert headers["X-ClaimMD-Account-Key"] == "acct-key"
            assert headers["Content-Type"] == "application/json"

            # Body should include Claim.MD-specific fields
            body = call_args.kwargs.get("json", {})
            assert body["type"] == "270"
            assert body["sender_id"] == "SENDER01"
            assert body["receiver_id"] == "RECEIVER01"
            assert body["control_number"] == "000000001"
            assert body["edi_data"] == "ISA*00*..."

    @pytest.mark.asyncio
    async def test_submit_claims_outbound_url(self):
        """Verify 837P submission goes to professional claims endpoint."""
        mock_resp = _mock_httpx_response(200, {"transaction_id": "CMD-CLM"})

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp) as mock_post:
            req = _make_request(tx_type=TransactionType.CLAIM_837P)
            await self.client.submit_transaction(req)

            url = mock_post.call_args.args[0] if mock_post.call_args.args else ""
            assert "/claims/professional" in url

    @pytest.mark.asyncio
    async def test_submit_institutional_claims_outbound_url(self):
        """Verify 837I submission goes to institutional claims endpoint."""
        mock_resp = _mock_httpx_response(200, {"transaction_id": "CMD-837I"})

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp) as mock_post:
            req = _make_request(tx_type=TransactionType.CLAIM_837I)
            await self.client.submit_transaction(req)

            url = mock_post.call_args.args[0] if mock_post.call_args.args else ""
            assert "/claims/institutional" in url

    @pytest.mark.asyncio
    async def test_submit_claims_returns_submitted(self):
        mock_resp = _mock_httpx_response(200, {"transaction_id": "CMD-002"})

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp):
            req = _make_request(tx_type=TransactionType.CLAIM_837P)
            resp = await self.client.submit_transaction(req)

        assert resp.status == TransactionStatus.SUBMITTED

    @pytest.mark.asyncio
    async def test_submit_validation_error_400(self):
        mock_resp = _mock_httpx_response(400, {"errors": ["Bad data"]})

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp):
            with pytest.raises(ClearinghouseValidationError):
                await self.client.submit_transaction(_make_request())

    @pytest.mark.asyncio
    async def test_submit_connection_error_retries(self):
        with patch(
            "httpx.AsyncClient.post",
            new_callable=AsyncMock,
            side_effect=httpx.ConnectError("refused"),
        ):
            with pytest.raises(ClearinghouseConnectionError):
                await self.client.submit_transaction(_make_request())

    @pytest.mark.asyncio
    async def test_check_status_success(self):
        mock_resp = _mock_httpx_response(200, {
            "status": "accepted",
            "type": "837P",
        })

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_resp):
            resp = await self.client.check_status("CMD-001")

        assert resp.status == TransactionStatus.ACCEPTED

    @pytest.mark.asyncio
    async def test_check_status_outbound_request(self):
        """Verify outbound GET URL and headers for Claim.MD status check."""
        mock_resp = _mock_httpx_response(200, {
            "status": "pending",
            "type": "270",
        })

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_resp) as mock_get:
            await self.client.check_status("CMD-STATUS-001")

            mock_get.assert_called_once()
            call_args = mock_get.call_args

            url = call_args.args[0] if call_args.args else ""
            assert url == "https://api.claim.md/api/v2/transactions/CMD-STATUS-001"

            headers = call_args.kwargs.get("headers", {})
            assert headers["X-ClaimMD-API-Key"] == "cmd-key"
            assert headers["X-ClaimMD-Account-Key"] == "acct-key"

    @pytest.mark.asyncio
    async def test_check_status_processing_maps_to_submitted(self):
        mock_resp = _mock_httpx_response(200, {
            "status": "processing",
            "type": "837P",
        })

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_resp):
            resp = await self.client.check_status("CMD-001")

        assert resp.status == TransactionStatus.SUBMITTED

    @pytest.mark.asyncio
    async def test_check_status_not_found(self):
        mock_resp = _mock_httpx_response(404)

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_resp):
            with pytest.raises(ClearinghouseError, match="not found"):
                await self.client.check_status("CMD-999")

    @pytest.mark.asyncio
    async def test_check_status_unknown_transaction_type(self):
        """Unknown type in response raises ClearinghouseError."""
        mock_resp = _mock_httpx_response(200, {
            "status": "completed",
            "type": "BOGUS_TYPE",
        })

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_resp):
            with pytest.raises(ClearinghouseError, match="Unknown transaction type"):
                await self.client.check_status("CMD-BAD-TYPE")

    @pytest.mark.asyncio
    async def test_parse_277_response(self):
        raw_277 = (
            "ST*277*0001~"
            "NM1*PR*2*Test Payer****~"
            "TRN*1*CLM001~"
            "STC*A1:20*20240101~"
            "SE*4*0001~"
        )
        result = await self.client.parse_response(raw_277, TransactionType.CLAIM_STATUS_277)
        assert result["transaction_type"] == "277"
        assert len(result["claims"]) == 1


# ── Factory Tests ──────────────────────────────────────────────────────


class TestClearinghouseFactory:
    def test_get_availity(self):
        client = get_clearinghouse(
            clearinghouse_name="availity",
            api_endpoint="https://api.availity.com",
            credentials={"api_key": "key"},
        )
        assert isinstance(client, AvailityClient)
        assert client.name == "availity"

    def test_get_claim_md(self):
        client = get_clearinghouse(
            clearinghouse_name="claim_md",
            api_endpoint="https://api.claim.md",
        )
        assert isinstance(client, ClaimMDClient)

    def test_get_claimmd_alias(self):
        client = get_clearinghouse(
            clearinghouse_name="claimmd",
            api_endpoint="https://api.claim.md",
        )
        assert isinstance(client, ClaimMDClient)

    def test_case_insensitive(self):
        client = get_clearinghouse(
            clearinghouse_name="Availity",
            api_endpoint="https://test.com",
        )
        assert isinstance(client, AvailityClient)

    def test_unknown_clearinghouse_raises(self):
        with pytest.raises(ClearinghouseError, match="Unknown clearinghouse"):
            get_clearinghouse(
                clearinghouse_name="nonexistent",
                api_endpoint="https://test.com",
            )

    def test_get_from_config(self):
        config = {
            "clearinghouse_name": "availity",
            "api_endpoint": "https://api.availity.com",
            "credentials": {"api_key": "key"},
        }
        client = get_clearinghouse_from_config(config)
        assert isinstance(client, AvailityClient)

    def test_list_clearinghouses(self):
        result = list_clearinghouses()
        assert "availity" in result
        assert "claim_md" in result

    def test_factory_passes_credentials(self):
        creds = {"api_key": "my-key", "customer_id": "CUST"}
        client = get_clearinghouse(
            clearinghouse_name="availity",
            api_endpoint="https://test.com",
            credentials=creds,
        )
        assert client.credentials == creds

    def test_factory_passes_timeout(self):
        client = get_clearinghouse(
            clearinghouse_name="availity",
            api_endpoint="https://test.com",
            timeout=60.0,
        )
        assert client.timeout == 60.0

    def test_factory_passes_max_retries(self):
        client = get_clearinghouse(
            clearinghouse_name="claim_md",
            api_endpoint="https://test.com",
            max_retries=5,
        )
        assert client.max_retries == 5
