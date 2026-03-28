"""Pydantic schemas for prior authorization request/response models."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator


class PriorAuthRequest(BaseModel):
    """Request to create a new prior authorization task."""

    procedure_code: str = Field(
        ..., description="CPT procedure code requiring authorization"
    )
    procedure_description: str = Field(
        default="", description="Human-readable description of the procedure"
    )
    diagnosis_codes: list[str] = Field(
        default_factory=list,
        description="ICD-10 diagnosis codes supporting medical necessity",
    )
    subscriber_id: str = Field(
        ..., description="Insurance subscriber/member ID"
    )
    subscriber_first_name: str = Field(
        ..., description="Subscriber first name"
    )
    subscriber_last_name: str = Field(
        ..., description="Subscriber last name"
    )
    subscriber_dob: str = Field(
        default="", description="Subscriber date of birth (YYYYMMDD)"
    )
    payer_id: str = Field(
        ..., description="Payer identifier (required for PA requirement determination)"
    )
    payer_name: str = Field(
        default="", description="Payer organization name"
    )
    provider_npi: str = Field(
        default="", description="Requesting provider NPI"
    )
    provider_name: str = Field(
        default="", description="Requesting provider name"
    )
    patient_id: str = Field(
        ..., description="Internal patient ID (must be a valid UUID) for FHIR lookups"
    )

    @field_validator("patient_id")
    @classmethod
    def validate_patient_id_uuid(cls, v: str) -> str:
        """Ensure patient_id is a valid UUID string."""
        if not v:
            raise ValueError("patient_id is required and cannot be empty")
        try:
            uuid.UUID(v)
        except (ValueError, AttributeError):
            raise ValueError(
                f"patient_id must be a valid UUID, got: '{v}'"
            )
        return v
    date_of_service: str = Field(
        default="", description="Planned date of service (YYYYMMDD)"
    )
    place_of_service: str = Field(
        default="11", description="CMS place of service code"
    )
    submission_channel: str = Field(
        default="",
        description="Preferred submission channel: clearinghouse, payer_api (or api), portal, manual",
    )
    fhir_base_url: str = Field(
        default="", description="FHIR server URL for clinical data retrieval"
    )
    payer_policy_reference: str = Field(
        default="", description="Payer policy reference for appeal citations"
    )
    related_claim_task_id: str | None = Field(default=None, description="Linked claims task ID for cross-agent tracing")


class PriorAuthResponse(BaseModel):
    """Response for a prior authorization result."""

    task_id: uuid.UUID
    status: str = Field(description="PA status: pending, approved, denied, pended, cancelled")
    pa_required: bool = Field(default=True, description="Whether PA was determined to be required")
    procedure_code: str = Field(default="")
    authorization_number: str = Field(default="", description="Auth number if approved")
    determination: str = Field(default="", description="Payer determination")
    submission_channel: str = Field(default="")
    confidence: float = Field(default=0.0)
    needs_review: bool = Field(default=False)
    review_reason: str = Field(default="")
    clinical_summary: dict[str, Any] = Field(default_factory=dict)
    appeal_letter: str | None = Field(default=None, description="Generated appeal letter if denied")
    effective_date: date | None = Field(default=None)
    expiration_date: date | None = Field(default=None)
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True, "arbitrary_types_allowed": True}


class PriorAuthAppealResponse(BaseModel):
    """Response for a prior authorization appeal."""

    id: uuid.UUID
    prior_auth_id: uuid.UUID
    appeal_level: int = 1
    status: str = Field(default="draft")
    appeal_letter: str | None = None
    clinical_evidence: dict[str, Any] | None = None
    outcome: str | None = None
    outcome_details: dict[str, Any] | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True, "arbitrary_types_allowed": True}
