"""Pydantic schemas for compliance request/response models."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

# Supported measure sets
SUPPORTED_MEASURE_SETS = frozenset({"HEDIS", "MIPS", "CMS_STARS"})


class ComplianceRequest(BaseModel):
    """Request to create a new compliance evaluation task."""

    organization_id: str = Field(
        ..., description="Organization UUID to evaluate"
    )
    measure_set: str = Field(
        default="HEDIS",
        description="Quality measure set: HEDIS, MIPS, or CMS_STARS",
    )
    reporting_period_start: str = Field(
        ..., description="Start of reporting period (YYYY-MM-DD)"
    )
    reporting_period_end: str = Field(
        ..., description="End of reporting period (YYYY-MM-DD)"
    )
    measure_ids: list[str] | None = Field(
        default=None,
        description="Optional list of specific measure IDs to evaluate",
    )
    patient_id: uuid.UUID | None = Field(
        default=None, description="Patient UUID (not typically used for compliance)"
    )

    @field_validator("organization_id")
    @classmethod
    def validate_organization_id(cls, v: str) -> str:
        """Ensure organization_id is a valid UUID string."""
        if not v:
            raise ValueError("organization_id is required and cannot be empty")
        try:
            uuid.UUID(v)
        except (ValueError, AttributeError):
            raise ValueError(
                f"organization_id must be a valid UUID, got: '{v}'"
            )
        return v

    @field_validator("measure_set")
    @classmethod
    def validate_measure_set(cls, v: str) -> str:
        """Ensure measure_set is one of the supported values."""
        normalized = v.upper()
        if normalized not in SUPPORTED_MEASURE_SETS:
            raise ValueError(
                f"measure_set must be one of: {', '.join(sorted(SUPPORTED_MEASURE_SETS))}. "
                f"Got: '{v}'"
            )
        return normalized

    @field_validator("reporting_period_start", "reporting_period_end")
    @classmethod
    def validate_date_format(cls, v: str) -> str:
        """Ensure dates are in YYYY-MM-DD format."""
        try:
            date.fromisoformat(v)
        except (ValueError, TypeError):
            raise ValueError(
                f"Date must be in YYYY-MM-DD format, got: '{v}'"
            )
        return v


class ComplianceResponse(BaseModel):
    """Response for a compliance evaluation result."""

    task_id: uuid.UUID
    status: str = Field(description="Report status: pending, completed, failed")
    measure_set: str = Field(default="HEDIS")
    reporting_period: str = Field(default="")
    overall_score: float | None = Field(default=None, description="Overall compliance score (0.0-1.0)")
    total_measures: int = Field(default=0)
    measures_met: int = Field(default=0)
    measures_not_met: int = Field(default=0)
    measure_scores: dict[str, Any] = Field(default_factory=dict)
    total_gaps: int = Field(default=0)
    gap_details: list[dict[str, Any]] = Field(default_factory=list)
    recommendations: list[dict[str, Any]] = Field(default_factory=list)
    confidence: float = Field(default=0.0)
    needs_review: bool = Field(default=False)
    review_reason: str = Field(default="")
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True, "arbitrary_types_allowed": True}
