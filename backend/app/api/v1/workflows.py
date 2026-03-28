"""Workflow execution API routes — list, detail, and event history."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.jwt import TokenPayload
from app.core.auth.middleware import require_role
from app.dependencies import get_db, get_temporal_client
from app.schemas.workflow import (
    WorkflowExecutionList,
    WorkflowExecutionResponse,
    WorkflowHistoryEvent,
    WorkflowHistoryResponse,
)
from app.models.workflow import WorkflowExecution
from app.services.workflow_service import WorkflowService

router = APIRouter(prefix="/workflows", tags=["workflows"])


def _wf_to_response(wf: WorkflowExecution) -> WorkflowExecutionResponse:
    """Convert ORM WorkflowExecution to Pydantic response."""
    from sqlalchemy import inspect as sa_inspect

    d = sa_inspect(wf).dict
    return WorkflowExecutionResponse(
        id=d.get("id", wf.id),
        workflow_id=d.get("workflow_id", ""),
        run_id=d.get("run_id"),
        agent_type=d.get("agent_type", ""),
        status=d.get("status", "pending"),
        task_queue=d.get("task_queue"),
        input_data=d.get("input_data"),
        output_data=d.get("output_data"),
        error_message=d.get("error_message"),
        created_at=d.get("created_at"),
        updated_at=d.get("updated_at"),
    )


async def _get_workflow_service(
    session: AsyncSession = Depends(get_db),
    temporal_client: object | None = Depends(get_temporal_client),
) -> WorkflowService:
    return WorkflowService(session, temporal_client=temporal_client)


@router.get("", response_model=WorkflowExecutionList)
async def list_workflows(
    agent_type: str | None = None,
    status_filter: str | None = None,
    limit: int = 50,
    offset: int = 0,
    current_user: TokenPayload = Depends(require_role("viewer")),
    service: WorkflowService = Depends(_get_workflow_service),
) -> WorkflowExecutionList:
    """List workflow executions with optional filters."""
    effective_limit = min(limit, 100)
    executions, total = await service.list_workflows(
        agent_type=agent_type,
        status=status_filter,
        limit=effective_limit,
        offset=offset,
    )
    return WorkflowExecutionList(
        items=[_wf_to_response(e) for e in executions],
        total=total,
        limit=effective_limit,
        offset=offset,
    )


@router.get("/{workflow_id}", response_model=WorkflowExecutionResponse)
async def get_workflow(
    workflow_id: str,
    current_user: TokenPayload = Depends(require_role("viewer")),
    service: WorkflowService = Depends(_get_workflow_service),
) -> WorkflowExecutionResponse:
    """Get a workflow execution by its workflow_id."""
    execution = await service.get_workflow(workflow_id)
    if execution is None:
        # Try by DB id
        execution = await service.get_workflow_by_id(workflow_id)
    if execution is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workflow '{workflow_id}' not found",
        )
    return _wf_to_response(execution)


@router.get("/{workflow_id}/history", response_model=WorkflowHistoryResponse)
async def get_workflow_history(
    workflow_id: str,
    current_user: TokenPayload = Depends(require_role("viewer")),
    service: WorkflowService = Depends(_get_workflow_service),
) -> WorkflowHistoryResponse:
    """Get the event history for a workflow execution.

    Returns 404 if the workflow_id is not found.
    """
    events = await service.get_workflow_history(workflow_id)
    if events is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workflow '{workflow_id}' not found",
        )
    return WorkflowHistoryResponse(
        workflow_id=workflow_id,
        events=[WorkflowHistoryEvent(**e) for e in events],
    )
