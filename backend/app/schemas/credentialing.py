"""Pydantic schemas for credentialing request/response models."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator


class CredentialingRequest(BaseModel):
    """Request to create a new credentialing task."""

    provider_npi: str = Field(
        ..., description="10-digit NPI number of the provider to credential"
    )
    target_organization: str = Field(
        default="", description="Target organization for credentialing"
    )
    target_payer_id: str = Field(
        default="", description="Target payer ID for enrollment"
    )
    credentialing_type: str = Field(
        default="initial",
        description="Type of credentialing: initial, renewal, or hospital_privileges",
    )
    state: str = Field(
        default="", description="Two-letter state code for license verification"
    )
    patient_id: uuid.UUID | None = Field(
        default=None, description="Patient UUID (not typically used for credentialing)"
    )
    organization_id: uuid.UUID | None = Field(
        default=None, description="Organization UUID"
    )

    @field_validator("provider_npi")
    @classmethod
    def validate_npi(cls, v: str) -> str:
        """Ensure NPI is a valid 10-digit number."""
        if not v or len(v) != 10 or not v.isdigit():
            raise ValueError(
                f"provider_npi must be exactly 10 digits, got: '{v}'"
            )
        return v


class CredentialingResponse(BaseModel):
    """Response for a credentialing result."""

    task_id: uuid.UUID
    status: str = Field(description="Application status: initiated, submitted, under_review, approved, denied")
    provider_npi: str = Field(default="")
    provider_name: str = Field(default="")
    target_organization: str = Field(default="")
    documents_complete: bool = Field(default=False)
    missing_documents: list[str] = Field(default_factory=list)
    sanctions_clear: bool = Field(default=True)
    license_verified: bool = Field(default=False)
    tracking_number: str = Field(default="")
    confidence: float = Field(default=0.0)
    needs_review: bool = Field(default=False)
    review_reason: str = Field(default="")
    submitted_date: date | None = None
    approved_date: date | None = None
    expiration_date: date | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True, "arbitrary_types_allowed": True}
