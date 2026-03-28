"""Availity clearinghouse client — eligibility (270/271), claims (837), status (276/277).

Implements the BaseClearinghouse interface for the Availity REST API.
Availity provides real-time eligibility checks, claims submission,
and claim status inquiries via their REST-based API.

All submit and status calls are protected by a shared ``CircuitBreaker``
to prevent cascading failures when Availity is degraded or unreachable.
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

# Module-level circuit breaker shared across all AvailityClient instances.
# Opens after 5 consecutive failures, recovers after 30 s.
_availity_breaker = CircuitBreaker(
    name="availity",
    failure_threshold=5,
    recovery_timeout=30.0,
)


class AvailityClient(BaseClearinghouse):
    """Availity clearinghouse REST API client.

    Supports eligibility verification (270/271), professional claims (837P),
    institutional claims (837I), claim status (276/277), and remittance (835).
    """

    @property
    def name(self) -> str:
        return "availity"

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
        """Build authentication headers for Availity API requests."""
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        api_key = self.credentials.get("api_key", "")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        customer_id = self.credentials.get("customer_id", "")
        if customer_id:
            headers["X-Availity-Customer-ID"] = customer_id
        return headers

    def _get_endpoint_path(self, transaction_type: TransactionType) -> str:
        """Map transaction type to Availity API endpoint path."""
        paths = {
            TransactionType.ELIGIBILITY_270: "/availity/v1/coverages",
            TransactionType.CLAIM_837P: "/availity/v1/claim-submissions",
            TransactionType.CLAIM_837I: "/availity/v1/claim-submissions",
            TransactionType.CLAIM_STATUS_276: "/availity/v1/claim-statuses",
            TransactionType.REMITTANCE_835: "/availity/v1/remittance-advices",
            TransactionType.PRIOR_AUTH_278: "/availity/v1/authorizations",
        }
        return paths.get(transaction_type, "/availity/v1/transactions")

    async def submit_transaction(self, request: TransactionRequest) -> TransactionResponse:
        """Submit a transaction to Availity REST API.

        For eligibility (270), Availity supports real-time response.
        For claims (837), Availity returns an acknowledgment with a tracking ID.

        Protected by a circuit breaker: after 5 consecutive connection
        failures the breaker opens and immediately rejects calls for 30 s
        to avoid overwhelming a degraded Availity endpoint.
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
            "transactionType": request.transaction_type.value,
            "controlNumber": request.control_number or str(uuid.uuid4())[:9],
            "senderId": request.sender_id,
            "receiverId": request.receiver_id,
            "payload": request.payload,
            "metadata": request.metadata,
        }

        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                async with _availity_breaker:
                    async with httpx.AsyncClient(timeout=self.timeout) as client:
                        resp = await client.post(endpoint, json=body, headers=headers)

                    # ── Non-transient client errors: raise OUTSIDE breaker ──
                    # These are client mistakes, not upstream failures, so we
                    # must not let them increment the breaker failure count.
                    if resp.status_code == 422:
                        error_data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
                        # Store response to raise after exiting breaker context
                        _validation_error = ClearinghouseValidationError(
                            f"Availity validation error: {resp.text}",
                            errors=error_data.get("errors", [resp.text]),
                        )
                        # Breaker __aexit__ will see no exception → counts as success
                    elif resp.status_code >= 400 and resp.status_code < 500 and resp.status_code != 429:
                        _validation_error = ClearinghouseValidationError(
                            f"Availity client error {resp.status_code}: {resp.text}",
                            errors=[resp.text],
                        )
                    else:
                        _validation_error = None

                    # ── Transient server errors: raise INSIDE breaker ──
                    # 429 and 5xx are upstream problems that should trip breaker.
                    if resp.status_code == 429 or resp.status_code >= 500:
                        raise ClearinghouseError(
                            f"Availity retryable error {resp.status_code}: {resp.text}"
                        )

                # Now outside breaker context — raise non-transient errors
                if _validation_error is not None:
                    raise _validation_error

                data = resp.json()
                transaction_id = data.get("id", data.get("transactionId", str(uuid.uuid4())))

                status = TransactionStatus.SUBMITTED
                if request.transaction_type == TransactionType.ELIGIBILITY_270:
                    # Eligibility is real-time — response is immediate
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
                    "Availity circuit breaker is OPEN — too many consecutive failures. "
                    "Calls are temporarily blocked to allow recovery.",
                )
            except ClearinghouseValidationError:
                # Non-transient client error — do not retry
                raise
            except ClearinghouseError as exc:
                # Transient server error — retry
                last_error = exc
                logger.warning(
                    "Availity retryable HTTP error on attempt %d/%d: %s",
                    attempt, self.max_retries, exc,
                )
                continue
            except (httpx.ConnectError, httpx.TimeoutException) as exc:
                last_error = exc
                logger.warning(
                    "Availity connection attempt %d/%d failed: %s",
                    attempt, self.max_retries, exc,
                )
                continue

        raise ClearinghouseConnectionError(
            f"Failed to connect to Availity after {self.max_retries} attempts: {last_error}",
        )

    async def check_status(self, transaction_id: str) -> TransactionResponse:
        """Check the status of a previously submitted Availity transaction.

        Protected by the same circuit breaker as submit_transaction.
        Retries on transient connection/timeout errors with backoff.
        """
        endpoint = f"{self.api_endpoint}/availity/v1/transactions/{transaction_id}/status"
        headers = self._get_auth_headers()

        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                async with _availity_breaker:
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
                            f"Availity status check error {resp.status_code}: {resp.text}",
                            transaction_id=transaction_id,
                        )
                    else:
                        _client_error = None

                    # Transient server errors — trip breaker
                    if resp.status_code == 429 or resp.status_code >= 500:
                        raise ClearinghouseError(
                            f"Availity retryable status-check error {resp.status_code}: {resp.text}",
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
                }
                status = status_map.get(raw_status, TransactionStatus.PENDING)

                try:
                    tx_type = TransactionType(data.get("transactionType", "270"))
                except ValueError:
                    raise ClearinghouseError(
                        f"Unknown transaction type in Availity status response: "
                        f"{data.get('transactionType')}",
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
                    "Availity circuit breaker is OPEN — status checks temporarily blocked.",
                    transaction_id=transaction_id,
                )
            except ClearinghouseError as exc:
                if "retryable" in str(exc).lower():
                    last_error = exc
                    logger.warning(
                        "Availity status check retryable error on attempt %d/%d: %s",
                        attempt, self.max_retries, exc,
                    )
                    continue
                raise
            except (httpx.ConnectError, httpx.TimeoutException) as exc:
                last_error = exc
                logger.warning(
                    "Availity status check attempt %d/%d failed: %s",
                    attempt, self.max_retries, exc,
                )
                continue

        raise ClearinghouseConnectionError(
            f"Failed to check Availity status after {self.max_retries} attempts: {last_error}",
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
        """Availity-specific transaction validation."""
        errors = super().validate_transaction(request)

        if not self.credentials.get("api_key"):
            errors.append("Availity API key is required")

        if request.transaction_type in (
            TransactionType.ELIGIBILITY_270,
            TransactionType.CLAIM_837P,
            TransactionType.CLAIM_837I,
        ):
            if not request.sender_id:
                errors.append("Sender ID is required")
            if not request.receiver_id:
                errors.append("Receiver ID is required")

        return errors
