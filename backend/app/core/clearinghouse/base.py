"""Abstract clearinghouse interface for EDI transaction submission.

All clearinghouse implementations (Availity, Claim.MD, etc.) implement
this interface so the agent layer can work with any clearinghouse through
a uniform API.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class TransactionType(str, Enum):
    """Supported EDI transaction types."""

    ELIGIBILITY_270 = "270"
    ELIGIBILITY_271 = "271"
    CLAIM_837P = "837P"
    CLAIM_837I = "837I"
    CLAIM_STATUS_276 = "276"
    CLAIM_STATUS_277 = "277"
    REMITTANCE_835 = "835"
    PRIOR_AUTH_278 = "278"


class TransactionStatus(str, Enum):
    """Status of a submitted transaction."""

    PENDING = "pending"
    SUBMITTED = "submitted"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    ERROR = "error"
    COMPLETED = "completed"


@dataclass
class TransactionRequest:
    """A clearinghouse transaction submission."""

    transaction_type: TransactionType
    payload: str
    sender_id: str = ""
    receiver_id: str = ""
    control_number: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class TransactionResponse:
    """Response from a clearinghouse transaction."""

    transaction_id: str
    transaction_type: TransactionType
    status: TransactionStatus
    raw_response: str = ""
    parsed_response: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    submitted_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = field(default_factory=dict)


class ClearinghouseError(Exception):
    """Base exception for clearinghouse operations."""

    def __init__(self, message: str, transaction_id: str = "", errors: list[str] | None = None):
        super().__init__(message)
        self.transaction_id = transaction_id
        self.errors = errors or []


class ClearinghouseConnectionError(ClearinghouseError):
    """Raised when the clearinghouse cannot be reached."""
    pass


class ClearinghouseValidationError(ClearinghouseError):
    """Raised when the transaction payload fails validation."""
    pass


class BaseClearinghouse(ABC):
    """Abstract interface for clearinghouse integrations.

    Implementations must provide:
    - submit_transaction(): Send an EDI transaction
    - check_status(): Query the status of a previously submitted transaction
    - parse_response(): Parse a raw EDI response into structured data
    - validate_transaction(): Validate a transaction before submission
    """

    def __init__(
        self,
        *,
        api_endpoint: str,
        credentials: dict[str, Any] | None = None,
        timeout: float = 30.0,
        max_retries: int = 3,
    ):
        self.api_endpoint = api_endpoint.rstrip("/")
        self.credentials = credentials or {}
        self.timeout = timeout
        self.max_retries = max_retries

    @property
    @abstractmethod
    def name(self) -> str:
        """Return the clearinghouse name (e.g. 'availity', 'claim_md')."""
        ...

    @property
    @abstractmethod
    def supported_transactions(self) -> list[TransactionType]:
        """Return the list of supported transaction types."""
        ...

    @abstractmethod
    async def submit_transaction(self, request: TransactionRequest) -> TransactionResponse:
        """Submit an EDI transaction to the clearinghouse.

        Args:
            request: The transaction to submit.

        Returns:
            TransactionResponse with status and any immediate response data.

        Raises:
            ClearinghouseError: If submission fails.
            ClearinghouseConnectionError: If the clearinghouse cannot be reached.
            ClearinghouseValidationError: If the transaction fails validation.
        """
        ...

    @abstractmethod
    async def check_status(self, transaction_id: str) -> TransactionResponse:
        """Check the status of a previously submitted transaction.

        Args:
            transaction_id: The clearinghouse-assigned transaction ID.

        Returns:
            TransactionResponse with current status.

        Raises:
            ClearinghouseError: If status check fails.
        """
        ...

    @abstractmethod
    async def parse_response(
        self, raw_response: str, transaction_type: TransactionType
    ) -> dict[str, Any]:
        """Parse a raw EDI response into structured data.

        Args:
            raw_response: Raw EDI response string.
            transaction_type: The type of transaction (for parser selection).

        Returns:
            Parsed response as a dictionary.
        """
        ...

    def validate_transaction(self, request: TransactionRequest) -> list[str]:
        """Validate a transaction request before submission.

        Args:
            request: The transaction to validate.

        Returns:
            List of validation error messages (empty if valid).
        """
        errors: list[str] = []

        if not request.payload:
            errors.append("Transaction payload is empty")

        if request.transaction_type not in self.supported_transactions:
            errors.append(
                f"Transaction type {request.transaction_type.value} "
                f"is not supported by {self.name}. "
                f"Supported: {[t.value for t in self.supported_transactions]}"
            )

        return errors

    def _ensure_supported(self, transaction_type: TransactionType) -> None:
        """Raise if the transaction type is not supported."""
        if transaction_type not in self.supported_transactions:
            raise ClearinghouseValidationError(
                f"Transaction type {transaction_type.value} not supported by {self.name}",
                errors=[f"Unsupported: {transaction_type.value}"],
            )
