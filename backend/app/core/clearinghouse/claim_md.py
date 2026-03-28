"""Claim.MD clearinghouse client — eligibility, claims, status via Claim.MD API.

Implements the BaseClearinghouse interface for the Claim.MD REST API.
Claim.MD provides a unified API for eligibility checks, claims submission,
status tracking, and remittance retrieval.

All submit and status calls are protected by a shared ``CircuitBreaker``
to prevent cascading failures when Claim.MD is degraded or unreachable.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx

from app.core.ingestion.x12_client import parse_271, parse_277, parse_835
from app.core.resilience import CircuitBreaker, CircuitBreakerOpenError

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

logger = logging.getLogger(__name__)

# Module-level circuit breaker shared across all ClaimMDClient instances.
_claim_md_breaker = CircuitBreaker(
    name="claim_md",
    failure_threshold=5,
    recovery_timeout=30.0,
)


class ClaimMDClient(BaseClearinghouse):
    """Claim.MD clearinghouse REST API client.

    Supports eligibility (270/271), professional claims (837P),
    institutional claims (837I), claim status (276/277), remittance (835),
    and prior authorization (278).
    """

    @property
    def name(self) -> str:
        return "claim_md"

    @property
    def supported_transactions(self) -> list[TransactionType]:
        return [
            TransactionType.ELIGIBILITY_270,
            TransactionType.CLAIM_837P,
            TransactionType.CLAIM_837I,
            TransactionType.CLAIM_STATUS_276,
            TransactionType.REMITTANCE_835,
            TransactionType.PRIOR_AUTH_278,
        ]

    def _get_auth_headers(self) -> dict[str, str]:
        """Build authentication headers for Claim.MD API requests."""
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        api_key = self.credentials.get("api_key", "")
        if api_key:
            headers["X-ClaimMD-API-Key"] = api_key
        account_key = self.credentials.get("account_key", "")
        if account_key:
            headers["X-ClaimMD-Account-Key"] = account_key
        return headers

    def _get_endpoint_path(self, transaction_type: TransactionType) -> str:
        """Map transaction type to Claim.MD API endpoint path."""
        paths = {
            TransactionType.ELIGIBILITY_270: "/api/v2/eligibility",
            TransactionType.CLAIM_837P: "/api/v2/claims/professional",
            TransactionType.CLAIM_837I: "/api/v2/claims/institutional",
            TransactionType.CLAIM_STATUS_276: "/api/v2/claims/status",
            TransactionType.REMITTANCE_835: "/api/v2/remittance",
            TransactionType.PRIOR_AUTH_278: "/api/v2/authorizations",
        }
        return paths.get(transaction_type, "/api/v2/transactions")

    async def submit_transaction(self, request: TransactionRequest) -> TransactionResponse:
        """Submit a transaction to Claim.MD API.

        Claim.MD uses a unified API for all transaction types with
        type-specific endpoints.
        """
        self._ensure_supported(request.transaction_type)

        validation_errors = self.validate_transaction(request)
        if validation_errors:
            raise ClearinghouseValidationError(
                f"Transaction validation failed: {'; '.join(validation_errors)}",
                errors=validation_errors,
            )

        endpoint = f"{self.api_endpoint}{self._get_endpoint_path(request.transaction_type)}"
        headers = self._get_auth_headers()

        body = {
            "type": request.transaction_type.value,
            "control_number": request.control_number or str(uuid.uuid4())[:9],
            "sender_id": request.sender_id,
            "receiver_id": request.receiver_id,
            "edi_data": request.payload,
            "options": request.metadata,
        }

        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                async with _claim_md_breaker:
                    async with httpx.AsyncClient(timeout=self.timeout) as client:
                        resp = await client.post(endpoint, json=body, headers=headers)

                    # ── Non-transient client errors: handle OUTSIDE breaker ──
                    if resp.status_code == 400:
                        error_data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
                        _validation_error = ClearinghouseValidationError(
                            f"Claim.MD validation error: {resp.text}",
                            errors=error_data.get("errors", [resp.text]),
                        )
                    elif resp.status_code >= 400 and resp.status_code < 500 and resp.status_code != 429:
                        _validation_error = ClearinghouseValidationError(
                            f"Claim.MD client error {resp.status_code}: {resp.text}",
                            errors=[resp.text],
                        )
                    else:
                        _validation_error = None

                    # ── Transient server errors: raise INSIDE breaker ──
                    if resp.status_code == 429 or resp.status_code >= 500:
                        raise ClearinghouseError(
                            f"Claim.MD retryable error {resp.status_code}: {resp.text}"
                        )

                # Outside breaker context — raise non-transient errors
                if _validation_error is not None:
                    raise _validation_error

                data = resp.json()
                transaction_id = data.get("transaction_id", data.get("id", str(uuid.uuid4())))

                # Claim.MD returns real-time eligibility; claims are async
                status = TransactionStatus.SUBMITTED
                if request.transaction_type == TransactionType.ELIGIBILITY_270:
                    status = TransactionStatus.COMPLETED

                return TransactionResponse(
                    transaction_id=str(transaction_id),
                    transaction_type=request.transaction_type,
                    status=status,
                    raw_response=resp.text,
                    parsed_response=data,
                    submitted_at=datetime.now(timezone.utc),
                    metadata={"attempt": attempt},
                )

            except CircuitBreakerOpenError:
                raise ClearinghouseConnectionError(
                    "Claim.MD circuit breaker is OPEN — too many consecutive failures. "
                    "Calls are temporarily blocked to allow recovery.",
                )
            except ClearinghouseValidationError:
                # Non-transient client error — do not retry
                raise
            except ClearinghouseError as exc:
                last_error = exc
                logger.warning(
                    "Claim.MD retryable HTTP error on attempt %d/%d: %s",
                    attempt, self.max_retries, exc,
                )
                continue
            except (httpx.ConnectError, httpx.TimeoutException) as exc:
                last_error = exc
                logger.warning(
                    "Claim.MD connection attempt %d/%d failed: %s",
                    attempt, self.max_retries, exc,
                )
                continue

        raise ClearinghouseConnectionError(
            f"Failed to connect to Claim.MD after {self.max_retries} attempts: {last_error}",
        )

    async def check_status(self, transaction_id: str) -> TransactionResponse:
        """Check the status of a previously submitted Claim.MD transaction.

        Protected by the same circuit breaker as submit_transaction.
        Retries on transient connection/timeout errors with backoff.
        """
        endpoint = f"{self.api_endpoint}/api/v2/transactions/{transaction_id}"
        headers = self._get_auth_headers()

        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                async with _claim_md_breaker:
                    async with httpx.AsyncClient(timeout=self.timeout) as client:
                        resp = await client.get(endpoint, headers=headers)

                    # Non-transient client errors — do not trip breaker
                    if resp.status_code == 404:
                        _client_error = ClearinghouseError(
                            f"Transaction {transaction_id} not found",
                            transaction_id=transaction_id,
                        )
                    elif resp.status_code >= 400 and resp.status_code < 500 and resp.status_code != 429:
                        _client_error = ClearinghouseError(
                            f"Claim.MD status check error {resp.status_code}: {resp.text}",
                            transaction_id=transaction_id,
                        )
                    else:
                        _client_error = None

                    # Transient server errors — trip breaker
                    if resp.status_code == 429 or resp.status_code >= 500:
                        raise ClearinghouseError(
                            f"Claim.MD retryable status-check error {resp.status_code}: {resp.text}",
                            transaction_id=transaction_id,
                        )

                # Outside breaker context — raise non-transient errors
                if _client_error is not None:
                    raise _client_error

                data = resp.json()
                raw_status = data.get("status", "pending").lower()
                status_map = {
                    "pending": TransactionStatus.PENDING,
                    "submitted": TransactionStatus.SUBMITTED,
                    "accepted": TransactionStatus.ACCEPTED,
                    "rejected": TransactionStatus.REJECTED,
                    "completed": TransactionStatus.COMPLETED,
                    "error": TransactionStatus.ERROR,
                    "processing": TransactionStatus.SUBMITTED,
                }
                status = status_map.get(raw_status, TransactionStatus.PENDING)

                try:
                    tx_type = TransactionType(data.get("type", "270"))
                except ValueError:
                    raise ClearinghouseError(
                        f"Unknown transaction type in Claim.MD status response: "
                        f"{data.get('type')}",
                        transaction_id=transaction_id,
                    )

                return TransactionResponse(
                    transaction_id=transaction_id,
                    transaction_type=tx_type,
                    status=status,
                    raw_response=resp.text,
                    parsed_response=data,
                    errors=data.get("errors", []),
                )

            except CircuitBreakerOpenError:
                raise ClearinghouseConnectionError(
                    "Claim.MD circuit breaker is OPEN — status checks temporarily blocked.",
                    transaction_id=transaction_id,
                )
            except ClearinghouseError as exc:
                if "retryable" in str(exc).lower():
                    last_error = exc
                    logger.warning(
                        "Claim.MD status check retryable error on attempt %d/%d: %s",
                        attempt, self.max_retries, exc,
                    )
                    continue
                raise
            except (httpx.ConnectError, httpx.TimeoutException) as exc:
                last_error = exc
                logger.warning(
                    "Claim.MD status check attempt %d/%d failed: %s",
                    attempt, self.max_retries, exc,
                )
                continue

        raise ClearinghouseConnectionError(
            f"Failed to check Claim.MD status after {self.max_retries} attempts: {last_error}",
            transaction_id=transaction_id,
        )

    async def parse_response(
        self, raw_response: str, transaction_type: TransactionType
    ) -> dict[str, Any]:
        """Parse a raw EDI response using the X12 parsers."""
        parsers = {
            TransactionType.ELIGIBILITY_271: parse_271,
            TransactionType.CLAIM_STATUS_277: parse_277,
            TransactionType.REMITTANCE_835: parse_835,
        }
        parser = parsers.get(transaction_type)
        if parser is None:
            return {"raw": raw_response, "transaction_type": transaction_type.value}
        return parser(raw_response)

    def validate_transaction(self, request: TransactionRequest) -> list[str]:
        """Claim.MD-specific validation."""
        errors = super().validate_transaction(request)

        if not self.credentials.get("api_key"):
            errors.append("Claim.MD API key is required")
        if not self.credentials.get("account_key"):
            errors.append("Claim.MD account key is required")

        return errors
