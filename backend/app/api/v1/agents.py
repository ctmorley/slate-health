"""Agent task API routes — CRUD for agent tasks with filters and pagination."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.jwt import TokenPayload
from app.core.auth.middleware import require_role
from app.dependencies import get_db, get_temporal_client
from app.models.agent_task import AGENT_TYPES, AgentTask
from app.schemas.agent import (
    AgentStatsResponse,
    AgentTaskCreate,
    AgentTaskList,
    AgentTaskResponse,
    AgentTaskUpdate,
)
from app.schemas.claims import ClaimsRequest
from app.schemas.compliance import ComplianceRequest
from app.schemas.credentialing import CredentialingRequest
from app.schemas.eligibility import EligibilityRequest
from app.schemas.prior_auth import PriorAuthRequest as PriorAuthRequestSchema
from app.schemas.scheduling import SchedulingRequest
from app.services.agent_service import AgentService
from app.services.workflow_service import WorkflowService

router = APIRouter(prefix="/agents", tags=["agents"])


async def _get_agent_service(
    session: AsyncSession = Depends(get_db),
    temporal_client: object | None = Depends(get_temporal_client),
) -> AgentService:
    workflow_service = WorkflowService(session, temporal_client=temporal_client)
    return AgentService(session, workflow_service=workflow_service)


def _task_to_response(task: AgentTask) -> AgentTaskResponse:
    """Convert an ORM AgentTask to a Pydantic response, avoiding lazy loads."""
    from sqlalchemy import inspect as sa_inspect

    # Use the instance dict to avoid triggering lazy loads on expired attrs
    d = sa_inspect(task).dict
    task_uuid = d.get("id", task.id)
    return AgentTaskResponse(
        id=task_uuid,
        task_id=task_uuid,
        agent_type=d.get("agent_type", ""),
        status=d.get("status", "pending"),
        input_data=d.get("input_data"),
        output_data=d.get("output_data"),
        error_message=d.get("error_message"),
        confidence_score=d.get("confidence_score"),
        workflow_execution_id=d.get("workflow_execution_id"),
        patient_id=d.get("patient_id"),
        organization_id=d.get("organization_id"),
        created_at=d.get("created_at"),
        updated_at=d.get("updated_at"),
    )


@router.post(
    "/{agent_type}/tasks",
    response_model=AgentTaskResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_task(
    agent_type: str,
    body: AgentTaskCreate,
    current_user: TokenPayload = Depends(require_role("viewer")),
    service: AgentService = Depends(_get_agent_service),
) -> AgentTaskResponse:
    """Submit a new agent task.

    For eligibility tasks, input_data is validated against the
    EligibilityRequest schema to ensure required fields (subscriber_id,
    subscriber_first_name, subscriber_last_name) are present.
    """
    if agent_type not in AGENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid agent_type '{agent_type}'. Must be one of: {', '.join(AGENT_TYPES)}",
        )

    input_data = body.input_data
    patient_id = body.patient_id
    organization_id = body.organization_id

    # Validate agent-specific input early
    if agent_type == "eligibility":
        try:
            elig_req = EligibilityRequest(**input_data)
            # Use validated/normalized data, and extract patient_id/org_id if provided
            input_data = elig_req.model_dump(exclude_none=True, exclude={"patient_id", "organization_id"})
            if elig_req.patient_id and patient_id is None:
                patient_id = elig_req.patient_id
            if elig_req.organization_id and organization_id is None:
                organization_id = elig_req.organization_id
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Invalid eligibility input: {e}",
            )
    elif agent_type == "scheduling":
        try:
            sched_req = SchedulingRequest(**input_data)
            input_data = sched_req.model_dump(exclude_none=True, exclude={"patient_id", "organization_id"})
            if sched_req.patient_id and patient_id is None:
                patient_id = sched_req.patient_id
            if sched_req.organization_id and organization_id is None:
                organization_id = sched_req.organization_id
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Invalid scheduling input: {e}",
            )
    elif agent_type == "claims":
        try:
            claims_req = ClaimsRequest(**input_data)
            input_data = claims_req.model_dump(mode="json", exclude_none=True, exclude={"patient_id", "organization_id"})
            if claims_req.patient_id and patient_id is None:
                patient_id = claims_req.patient_id
            if claims_req.organization_id and organization_id is None:
                organization_id = claims_req.organization_id
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Invalid claims input: {e}",
            )
    elif agent_type == "prior_auth":
        try:
            pa_req = PriorAuthRequestSchema(**input_data)
            input_data = pa_req.model_dump(exclude_none=True)
            if pa_req.patient_id and patient_id is None:
                patient_id = pa_req.patient_id
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Invalid prior_auth input: {e}",
            )
    elif agent_type == "credentialing":
        try:
            cred_req = CredentialingRequest(**input_data)
            input_data = cred_req.model_dump(exclude_none=True, exclude={"patient_id", "organization_id"})
            if cred_req.patient_id and patient_id is None:
                patient_id = cred_req.patient_id
            if cred_req.organization_id and organization_id is None:
                organization_id = cred_req.organization_id
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Invalid credentialing input: {e}",
            )
    elif agent_type == "compliance":
        try:
            comp_req = ComplianceRequest(**input_data)
            input_data = comp_req.model_dump(exclude_none=True, exclude={"patient_id"})
            if comp_req.patient_id and patient_id is None:
                patient_id = comp_req.patient_id
            # organization_id is already validated as UUID by the schema
            if organization_id is None:
                organization_id = uuid.UUID(comp_req.organization_id)
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Invalid compliance input: {e}",
            )

    try:
        task = await service.create_task(
            agent_type=agent_type,
            input_data=input_data,
            patient_id=patient_id,
            organization_id=organization_id,
            actor_id=current_user.user_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    return _task_to_response(task)


@router.get("/{agent_type}/tasks", response_model=AgentTaskList)
async def list_tasks(
    agent_type: str,
    status_filter: str | None = None,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
    search: str | None = None,
    limit: int = 50,
    offset: int = 0,
    current_user: TokenPayload = Depends(require_role("viewer")),
    service: AgentService = Depends(_get_agent_service),
) -> AgentTaskList:
    """List tasks for an agent type with optional filters and pagination."""
    if agent_type not in AGENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid agent_type '{agent_type}'.",
        )

    effective_limit = min(limit, 100)
    tasks, total = await service.list_tasks(
        agent_type=agent_type,
        status=status_filter,
        start_date=start_date,
        end_date=end_date,
        search=search,
        limit=effective_limit,
        offset=offset,
    )

    return AgentTaskList(
        items=[_task_to_response(t) for t in tasks],
        total=total,
        limit=effective_limit,
        offset=offset,
    )


@router.get("/{agent_type}/tasks/{task_id}", response_model=AgentTaskResponse)
async def get_task(
    agent_type: str,
    task_id: uuid.UUID,
    current_user: TokenPayload = Depends(require_role("viewer")),
    service: AgentService = Depends(_get_agent_service),
) -> AgentTaskResponse:
    """Get a single task by ID."""
    task = await service.get_task_with_refresh(str(task_id))
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task '{task_id}' not found",
        )
    if task.agent_type != agent_type:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task '{task_id}' is not of type '{agent_type}'",
        )
    return _task_to_response(task)


@router.put("/{agent_type}/tasks/{task_id}", response_model=AgentTaskResponse)
async def update_task(
    agent_type: str,
    task_id: uuid.UUID,
    body: AgentTaskUpdate,
    current_user: TokenPayload = Depends(require_role("reviewer")),
    service: AgentService = Depends(_get_agent_service),
) -> AgentTaskResponse:
    """Update a task's mutable fields (input_data, patient_id, organization_id).

    Only tasks in 'pending' or 'failed' status can be updated.
    """
    task = await service.update_task(
        str(task_id),
        update_data=body.model_dump(exclude_none=True),
        actor_id=current_user.user_id,
    )
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task '{task_id}' not found",
        )
    if task.agent_type != agent_type:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task '{task_id}' is not of type '{agent_type}'",
        )
    return _task_to_response(task)


@router.delete(
    "/{agent_type}/tasks/{task_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_task(
    agent_type: str,
    task_id: uuid.UUID,
    current_user: TokenPayload = Depends(require_role("admin")),
    service: AgentService = Depends(_get_agent_service),
) -> None:
    """Delete a task. Only tasks in terminal states can be deleted."""
    success = await service.delete_task(
        str(task_id),
        actor_id=current_user.user_id,
    )
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task '{task_id}' not found or cannot be deleted",
        )


@router.post("/{agent_type}/tasks/{task_id}/cancel", response_model=AgentTaskResponse)
async def cancel_task(
    agent_type: str,
    task_id: uuid.UUID,
    current_user: TokenPayload = Depends(require_role("reviewer")),
    service: AgentService = Depends(_get_agent_service),
) -> AgentTaskResponse:
    """Cancel a running task."""
    task = await service.cancel_task(
        str(task_id),
        actor_id=current_user.user_id,
    )
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task '{task_id}' not found",
        )
    return _task_to_response(task)


@router.get("/{agent_type}/stats", response_model=AgentStatsResponse)
async def get_agent_stats(
    agent_type: str,
    current_user: TokenPayload = Depends(require_role("viewer")),
    service: AgentService = Depends(_get_agent_service),
) -> AgentStatsResponse:
    """Get aggregate statistics for an agent type."""
    if agent_type not in AGENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid agent_type '{agent_type}'.",
        )
    stats = await service.get_agent_stats(agent_type)
    return AgentStatsResponse(**stats)
