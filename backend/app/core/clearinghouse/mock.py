"""Mock clearinghouse for testing and local development.

Provides a deterministic clearinghouse implementation that returns
configurable responses without making any external API calls. Used as
the default fallback when no real clearinghouse credentials are configured,
ensuring the eligibility pipeline can complete end-to-end in dev/test
environments.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from app.core.clearinghouse.base import (
    BaseClearinghouse,
    TransactionRequest,
    TransactionResponse,
    TransactionStatus,
    TransactionType,
)

logger = logging.getLogger(__name__)


# Default mock 271 eligibility response
_DEFAULT_ELIGIBILITY_RESPONSE: dict[str, Any] = {
    "coverage": {
        "active": True,
        "effective_date": "2024-01-01",
        "termination_date": "",
        "plan_name": "Mock PPO Plan",
        "plan_number": "MOCK-PPO-001",
        "group_number": "GRP-12345",
    },
    "subscriber": {
        "id": "MOCK-SUB-001",
        "first_name": "Jane",
        "last_name": "Doe",
        "date_of_birth": "1990-01-01",
    },
    "payer": {
        "id": "MOCK-PAYER",
        "name": "Mock Insurance Co",
    },
    "benefits": [
        {
            "eligibility_code": "1",
            "coverage_level": "IND",
            "service_type": "30",
            "description": "Health Benefit Plan Coverage - Active",
            "copay": 25.00,
            "coinsurance": 0.20,
            "deductible": 500.00,
            "deductible_remaining": 350.00,
        }
    ],
    "errors": [],
}


class MockClearinghouse(BaseClearinghouse):
    """Mock clearinghouse for testing and development.

    Returns deterministic, configurable responses for all transaction types.
    No external API calls are made.
    """

    def __init__(
        self,
        *,
        api_endpoint: str = "http://mock-clearinghouse",
        credentials: dict[str, Any] | None = None,
        timeout: float = 30.0,
        max_retries: int = 3,
        eligibility_response: dict[str, Any] | None = None,
        default_status: TransactionStatus = TransactionStatus.COMPLETED,
    ):
        super().__init__(
            api_endpoint=api_endpoint,
            credentials=credentials,
            timeout=timeout,
            max_retries=max_retries,
        )
        self._eligibility_response = eligibility_response or _DEFAULT_ELIGIBILITY_RESPONSE
        self._default_status = default_status
        self._submitted: dict[str, TransactionResponse] = {}

    @property
    def name(self) -> str:
        return "mock"

    @property
    def supported_transactions(self) -> list[TransactionType]:
        return list(TransactionType)

    async def submit_transaction(self, request: TransactionRequest) -> TransactionResponse:
        """Return a deterministic mock response for the transaction."""
        self._ensure_supported(request.transaction_type)

        transaction_id = f"MOCK-{uuid.uuid4().hex[:12].upper()}"

        if request.transaction_type == TransactionType.ELIGIBILITY_270:
            parsed = self._eligibility_response
            raw = "MOCK~271~RESPONSE~ACTIVE"
        else:
            parsed = {"status": "accepted", "transaction_type": request.transaction_type.value}
            raw = f"MOCK~{request.transaction_type.value}~RESPONSE"

        response = TransactionResponse(
            transaction_id=transaction_id,
            transaction_type=request.transaction_type,
            status=self._default_status,
            raw_response=raw,
            parsed_response=parsed,
            submitted_at=datetime.now(timezone.utc),
        )

        self._submitted[transaction_id] = response
        logger.info(
            "MockClearinghouse: submitted %s transaction %s",
            request.transaction_type.value,
            transaction_id,
        )
        return response

    async def check_status(self, transaction_id: str) -> TransactionResponse:
        """Return the status of a previously submitted mock transaction."""
        if transaction_id in self._submitted:
            return self._submitted[transaction_id]

        return TransactionResponse(
            transaction_id=transaction_id,
            transaction_type=TransactionType.ELIGIBILITY_270,
            status=TransactionStatus.COMPLETED,
            raw_response="MOCK~STATUS~COMPLETED",
            parsed_response={"status": "completed"},
        )

    async def parse_response(
        self, raw_response: str, transaction_type: TransactionType
    ) -> dict[str, Any]:
        """Parse a mock response string."""
        if transaction_type in (TransactionType.ELIGIBILITY_270, TransactionType.ELIGIBILITY_271):
            return self._eligibility_response
        return {"status": "parsed", "transaction_type": transaction_type.value}
