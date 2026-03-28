"""Pydantic schemas for eligibility agent requests and responses."""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, Field


class EligibilityRequest(BaseModel):
    """Input data for an eligibility verification task."""

    subscriber_id: str = Field(description="Insurance subscriber/member ID")
    subscriber_first_name: str = Field(description="Subscriber first name")
    subscriber_last_name: str = Field(description="Subscriber last name")
    subscriber_dob: str | None = Field(default=None, description="DOB in YYYYMMDD format")
    payer_id: str | None = Field(default=None, description="Payer identifier code")
    payer_name: str | None = Field(default=None, description="Payer name")
    provider_npi: str | None = Field(default=None, description="Provider NPI")
    provider_first_name: str | None = Field(default=None, description="Provider first name")
    provider_last_name: str | None = Field(default=None, description="Provider last name")
    date_of_service: str | None = Field(default=None, description="Date of service YYYYMMDD")
    service_type_code: str = Field(default="30", description="X12 service type code")
    patient_id: uuid.UUID | None = Field(default=None, description="Patient UUID")
    organization_id: uuid.UUID | None = Field(default=None, description="Organization UUID")

    # Test control flags — used for deterministic E2E testing of specific paths.
    # These do NOT affect production behavior when absent (default=False).
    force_low_confidence: bool = Field(
        default=False,
        description="Test flag: force confidence to 0.3 to trigger HITL review",
    )
    force_clearinghouse_error: bool = Field(
        default=False,
        description="Test flag: simulate clearinghouse connection failure",
    )


class EligibilityCoverageDetail(BaseModel):
    """Coverage details from eligibility response."""

    active: bool = False
    effective_date: str | None = None
    termination_date: str | None = None
    plan_name: str | None = None
    group_number: str | None = None
    coverage_type: str | None = None


class EligibilityResult(BaseModel):
    """Output from an eligibility verification."""

    coverage_active: bool = False
    coverage_details: dict[str, Any] = Field(default_factory=dict)
    benefits: list[dict[str, Any]] = Field(default_factory=list)
    subscriber: dict[str, Any] = Field(default_factory=dict)
    payer: dict[str, Any] = Field(default_factory=dict)
    confidence: float = 0.0
    needs_review: bool = False
    review_reason: str = ""
    transaction_id: str = ""
