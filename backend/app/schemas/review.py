"""Pydantic schemas for HITL review request/response models."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class ReviewResponse(BaseModel):
    """Response for a HITL review."""

    id: uuid.UUID
    task_id: uuid.UUID
    reviewer_id: uuid.UUID | None = None
    status: str
    reason: str
    agent_decision: dict[str, Any] | None = None
    confidence_score: float | None = None
    reviewer_notes: str | None = None
    decided_at: datetime | None = None
    created_at: datetime
    updated_at: datetime | None = None
    # Denormalised from the linked AgentTask for display convenience
    agent_type: str | None = None
    patient_id: uuid.UUID | None = None

    model_config = {"from_attributes": True}


class ReviewList(BaseModel):
    """Paginated list of reviews."""

    items: list[ReviewResponse]
    total: int
    limit: int
    offset: int


class ReviewActionRequest(BaseModel):
    """Request to approve, reject, or escalate a review."""

    notes: str | None = Field(
        default=None, description="Optional reviewer notes"
    )
