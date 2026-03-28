"""Pydantic schemas for agent task request/response models."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class AgentTaskCreate(BaseModel):
    """Request to create a new agent task.

    The ``agent_type`` is determined by the URL path parameter
    (``/agents/{agent_type}/tasks``). If ``agent_type`` is also
    present in the request body it is ignored in favour of the path.
    """

    agent_type: str | None = Field(
        default=None,
        description="Ignored — agent type is taken from the URL path parameter. "
        "Accepted for backwards-compatibility but not required.",
    )
    input_data: dict[str, Any] = Field(
        default_factory=dict,
        description="Input data for the agent",
    )
    patient_id: uuid.UUID | None = Field(
        default=None, description="Patient UUID"
    )
    organization_id: uuid.UUID | None = Field(
        default=None, description="Organization UUID"
    )


class AgentTaskUpdate(BaseModel):
    """Request to update a task's mutable fields."""

    input_data: dict[str, Any] | None = Field(
        default=None, description="Updated input data"
    )
    patient_id: uuid.UUID | None = Field(
        default=None, description="Updated patient UUID"
    )
    organization_id: uuid.UUID | None = Field(
        default=None, description="Updated organization UUID"
    )


class AgentTaskResponse(BaseModel):
    """Response for an agent task."""

    id: uuid.UUID
    task_id: uuid.UUID = Field(default=None, description="Alias for id (task identifier)")
    agent_type: str
    status: str
    input_data: dict[str, Any] | None = None
    output_data: dict[str, Any] | None = None
    error_message: str | None = None
    confidence_score: float | None = None
    workflow_execution_id: uuid.UUID | None = None
    patient_id: uuid.UUID | None = None
    organization_id: uuid.UUID | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True, "arbitrary_types_allowed": True}


class AgentTaskList(BaseModel):
    """Paginated list of agent tasks."""

    items: list[AgentTaskResponse]
    total: int
    limit: int
    offset: int


class AgentStatsResponse(BaseModel):
    """Aggregate statistics for an agent type."""

    agent_type: str
    total_tasks: int = 0
    pending: int = 0
    running: int = 0
    completed: int = 0
    failed: int = 0
    in_review: int = 0
    cancelled: int = 0
    avg_confidence: float | None = None
