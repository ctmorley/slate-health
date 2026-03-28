"""Pydantic schemas for claims agent requests and responses."""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, Field


class ClaimsRequest(BaseModel):
    """Input data for a claims task."""

    subscriber_id: str = Field(description="Insurance subscriber/member ID")
    subscriber_first_name: str = Field(description="Subscriber first name")
    subscriber_last_name: str = Field(description="Subscriber last name")
    subscriber_dob: str | None = Field(default=None, description="DOB YYYYMMDD")
    subscriber_gender: str = Field(default="U", description="M/F/U")
    payer_id: str | None = Field(default=None, description="Payer identifier")
    payer_name: str | None = Field(default=None, description="Payer name")
    billing_provider_npi: str | None = Field(default=None, description="Billing provider NPI")
    billing_provider_name: str | None = Field(default=None, description="Billing provider name")
    billing_provider_tax_id: str | None = Field(default=None, description="Tax ID")
    diagnosis_codes: list[str] = Field(description="ICD-10 diagnosis codes")
    procedure_codes: list[str] = Field(default_factory=list, description="CPT procedure codes")
    service_lines: list[dict[str, str]] | None = Field(
        default=None,
        description="Service lines with procedure_code, charge, units, modifier",
    )
    total_charge: str = Field(default="0.00", description="Total claim charge amount")
    date_of_service: str | None = Field(default=None, description="Date of service YYYYMMDD")
    place_of_service: str = Field(default="11", description="Place of service code")
    claim_type: str = Field(default="837P", description="837P (Professional) or 837I (Institutional)")
    patient_id: uuid.UUID | None = Field(default=None, description="Patient UUID")
    encounter_id: uuid.UUID | None = Field(default=None, description="Encounter UUID")
    organization_id: uuid.UUID | None = Field(default=None, description="Organization UUID")
    eligibility_task_id: str | None = Field(default=None, description="Linked eligibility task ID for cross-agent tracing")


class CodeValidationResult(BaseModel):
    """Result of ICD-10 or CPT code validation."""

    code: str
    valid: bool
    description: str = ""
    error: str | None = None
    warning: str | None = None


class DenialAnalysis(BaseModel):
    """Analysis of a claim denial with appeal recommendation."""

    denial_code: str
    denial_reason: str = ""
    category: str
    category_description: str = ""
    appeal_recommendation: dict[str, Any] = Field(default_factory=dict)
    claim_id: str = ""


class ClaimsResult(BaseModel):
    """Output from a claims task."""

    claim_id: str = ""
    claim_type: str = "837P"
    submission_status: str = "pending"
    dx_validation: dict[str, Any] = Field(default_factory=dict)
    cpt_validation: dict[str, Any] = Field(default_factory=dict)
    payment_info: dict[str, Any] | None = None
    denial_analyses: list[DenialAnalysis] = Field(default_factory=list)
    total_charge: str = "0.00"
    total_paid: str = "0.00"
    patient_responsibility: str = "0.00"
    confidence: float = 0.0
    needs_review: bool = False
    review_reason: str = ""
