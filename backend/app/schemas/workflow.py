"""Pydantic schemas for workflow execution request/response models."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class WorkflowExecutionResponse(BaseModel):
    """Response for a workflow execution."""

    id: uuid.UUID
    workflow_id: str
    run_id: str | None = None
    agent_type: str
    status: str
    task_queue: str | None = None
    input_data: dict[str, Any] | None = None
    output_data: dict[str, Any] | None = None
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class WorkflowExecutionList(BaseModel):
    """Paginated list of workflow executions."""

    items: list[WorkflowExecutionResponse]
    total: int
    limit: int
    offset: int


class WorkflowStartRequest(BaseModel):
    """Request to start a new workflow."""

    agent_type: str = Field(description="Agent type for this workflow")
    task_id: str = Field(description="Associated agent task ID")
    input_data: dict[str, Any] = Field(default_factory=dict)
    patient_context: dict[str, Any] = Field(default_factory=dict)
    payer_context: dict[str, Any] = Field(default_factory=dict)
    organization_id: str | None = None
    clearinghouse_config: dict[str, Any] | None = None
    task_queue: str | None = None


class WorkflowCancelResponse(BaseModel):
    """Response from cancelling a workflow."""

    workflow_id: str
    status: str
    message: str


class WorkflowHistoryEvent(BaseModel):
    """A single event in a workflow's execution history."""

    event_id: int
    event_type: str
    timestamp: str
    details: dict[str, Any] = Field(default_factory=dict)


class WorkflowHistoryResponse(BaseModel):
    """Workflow event history."""

    workflow_id: str
    events: list[WorkflowHistoryEvent]
