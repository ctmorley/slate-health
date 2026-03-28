"""Pydantic schemas for scheduling agent requests and responses."""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, Field


class SchedulingRequest(BaseModel):
    """Input data for a scheduling task.

    Supports both natural language text and structured input.
    """

    request_text: str | None = Field(
        default=None,
        description="Natural language scheduling request (e.g., 'annual checkup with Dr. Smith next Tuesday')",
    )
    patient_id: uuid.UUID | None = Field(default=None, description="Patient UUID")
    patient_first_name: str | None = Field(default=None, description="Patient first name")
    patient_last_name: str | None = Field(default=None, description="Patient last name")
    provider_npi: str | None = Field(default=None, description="Preferred provider NPI")
    provider_name: str | None = Field(default=None, description="Preferred provider name")
    specialty: str | None = Field(default=None, description="Medical specialty needed")
    preferred_date_start: str | None = Field(default=None, description="Earliest date YYYY-MM-DD")
    preferred_date_end: str | None = Field(default=None, description="Latest date YYYY-MM-DD")
    preferred_time_of_day: str = Field(default="any", description="morning/afternoon/evening/any")
    urgency: str = Field(default="routine", description="routine/urgent/emergency")
    visit_type: str = Field(default="follow_up", description="Visit type")
    duration_minutes: int = Field(default=30, description="Expected duration in minutes")
    notes: str = Field(default="", description="Additional notes")
    payer_id: str | None = Field(default=None, description="Payer identifier")
    payer_name: str | None = Field(default=None, description="Payer name")
    organization_id: uuid.UUID | None = Field(default=None, description="Organization UUID")


class SchedulingSlot(BaseModel):
    """An available appointment slot."""

    slot_id: str
    fhir_id: str = ""
    start: str
    end: str
    status: str = "free"
    provider_npi: str = ""
    provider_name: str = ""
    specialty: str = ""
    location: str = ""
    duration_minutes: int = 30


class SchedulingResult(BaseModel):
    """Output from a scheduling task."""

    appointment_id: str | None = None
    fhir_appointment_id: str | None = None
    slot: SchedulingSlot | None = None
    alternatives: list[SchedulingSlot] = Field(default_factory=list)
    parsed_intent: dict[str, Any] = Field(default_factory=dict)
    waitlist_id: str | None = None
    waitlist_position: int | None = None
    status: str = "pending"
    confidence: float = 0.0
    needs_review: bool = False
    review_reason: str = ""
