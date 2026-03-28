"""Agent service — orchestrates task creation, workflow start, and status tracking.

Provides the business logic layer for managing agent tasks across all 6 agent
types. Used by the API routes to create tasks, start workflows, and query status.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import String as db_text, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit.logger import AuditLogger
from app.models.agent_task import AGENT_TYPES, AgentTask
from app.services.workflow_service import WorkflowService

logger = logging.getLogger(__name__)


class AgentService:
    """Service for managing agent tasks and orchestrating workflows.

    Handles the full task lifecycle: create → start workflow → track status.
    """

    def __init__(
        self,
        session: AsyncSession,
        workflow_service: WorkflowService | None = None,
    ) -> None:
        self._session = session
        self._workflow_service = workflow_service or WorkflowService(session)
        self._audit_logger = AuditLogger(session)

    async def create_task(
        self,
        *,
        agent_type: str,
        input_data: dict[str, Any] | None = None,
        patient_id: uuid.UUID | None = None,
        organization_id: uuid.UUID | None = None,
        actor_id: uuid.UUID | None = None,
    ) -> AgentTask:
        """Create a new agent task and start its workflow.

        Creates the AgentTask record, then dispatches it to the workflow
        service for execution (Temporal or inline fallback).

        Returns:
            The created AgentTask with workflow_execution_id populated.
        """
        if agent_type not in AGENT_TYPES:
            raise ValueError(
                f"Invalid agent_type '{agent_type}'. "
                f"Must be one of: {', '.join(AGENT_TYPES)}"
            )

        task = AgentTask(
            agent_type=agent_type,
            status="pending",
            input_data=input_data or {},
            patient_id=patient_id,
            organization_id=organization_id,
        )
        self._session.add(task)
        await self._session.flush()

        # Audit: task created
        await self._audit_logger.log(
            action="agent_task_created",
            actor_id=actor_id,
            actor_type="user" if actor_id else "system",
            resource_type="agent_task",
            resource_id=str(task.id),
            details={
                "agent_type": agent_type,
                "patient_id": str(patient_id) if patient_id else None,
            },
        )

        # Look up the organization's clearinghouse config so that
        # downstream workflows use the real clearinghouse rather than
        # falling back to a mock.  When no org or config is found the
        # workflow receives None and must fail fast in production mode
        # (see individual workflow activities).
        clearinghouse_cfg: dict[str, Any] | None = None
        if organization_id:
            try:
                from app.core.payer.registry import PayerRegistry
                registry = PayerRegistry(self._session)
                ch_config = await registry.get_clearinghouse_config(
                    str(organization_id),
                )
                if ch_config is not None:
                    clearinghouse_cfg = {
                        "clearinghouse_name": ch_config.clearinghouse_name,
                        "api_endpoint": ch_config.api_endpoint,
                        "credentials": ch_config.credentials,
                    }
            except Exception as exc:
                logger.warning(
                    "Failed to look up clearinghouse config for org %s: %s",
                    organization_id, exc,
                )

        # Start the workflow
        try:
            execution = await self._workflow_service.start_workflow(
                agent_type=agent_type,
                task_id=str(task.id),
                input_data=input_data,
                patient_context=self._build_patient_context(input_data),
                payer_context=self._build_payer_context(input_data),
                organization_id=str(organization_id) if organization_id else None,
                clearinghouse_config=clearinghouse_cfg,
            )
            task.workflow_execution_id = execution.id

            # Audit: workflow started
            await self._audit_logger.log(
                action="agent_workflow_started",
                actor_type="system",
                resource_type="agent_task",
                resource_id=str(task.id),
                details={
                    "agent_type": agent_type,
                    "workflow_id": execution.workflow_id,
                },
            )

            # Broadcast the running status via WebSocket
            try:
                from app.api.websocket import broadcast_task_update
                await broadcast_task_update(
                    task_id=str(task.id),
                    agent_type=agent_type,
                    task_status=task.status,
                )
            except Exception:
                pass  # Non-critical
        except Exception as exc:
            logger.error("Failed to start workflow for task %s: %s", task.id, exc)
            task.status = "failed"
            task.error_message = f"Workflow start failed: {exc}"

        await self._session.flush()
        return task

    async def get_task(self, task_id: str) -> AgentTask | None:
        """Get a task by its ID."""
        result = await self._session.execute(
            select(AgentTask).where(AgentTask.id == task_id)
        )
        return result.scalar_one_or_none()

    async def get_task_with_refresh(self, task_id: str) -> AgentTask | None:
        """Get a task, refreshing workflow status if still running."""
        task = await self.get_task(task_id)
        if task is None:
            return None

        if task.status == "running" and task.workflow_execution_id:
            from app.models.workflow import WorkflowExecution

            wf_result = await self._session.execute(
                select(WorkflowExecution).where(
                    WorkflowExecution.id == task.workflow_execution_id
                )
            )
            wf = wf_result.scalar_one_or_none()
            if wf:
                await self._workflow_service.refresh_workflow_status(wf.workflow_id)

        return task

    async def list_tasks(
        self,
        *,
        agent_type: str | None = None,
        status: str | None = None,
        patient_id: str | None = None,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        search: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[AgentTask], int]:
        """List agent tasks with optional filters and pagination.

        ``search`` performs a case-insensitive LIKE match against the
        task ID (cast to text) and patient_id (cast to text) so that the
        frontend search bar works across the full dataset rather than
        just the current page.
        """
        query = select(AgentTask)
        count_query = select(func.count()).select_from(AgentTask)

        if agent_type:
            query = query.where(AgentTask.agent_type == agent_type)
            count_query = count_query.where(AgentTask.agent_type == agent_type)

        if status:
            query = query.where(AgentTask.status == status)
            count_query = count_query.where(AgentTask.status == status)

        if patient_id:
            query = query.where(AgentTask.patient_id == patient_id)
            count_query = count_query.where(AgentTask.patient_id == patient_id)

        if start_date is not None:
            query = query.where(AgentTask.created_at >= start_date)
            count_query = count_query.where(AgentTask.created_at >= start_date)

        if end_date is not None:
            query = query.where(AgentTask.created_at <= end_date)
            count_query = count_query.where(AgentTask.created_at <= end_date)

        if search:
            like_pattern = f"%{search}%"
            search_filter = or_(
                AgentTask.id.cast(db_text).ilike(like_pattern),
                AgentTask.patient_id.cast(db_text).ilike(like_pattern),
            )
            query = query.where(search_filter)
            count_query = count_query.where(search_filter)

        query = query.order_by(AgentTask.created_at.desc())
        query = query.limit(limit).offset(offset)

        result = await self._session.execute(query)
        tasks = list(result.scalars().all())

        count_result = await self._session.execute(count_query)
        total = count_result.scalar() or 0

        return tasks, total

    async def update_task(
        self,
        task_id: str,
        *,
        update_data: dict[str, Any],
        actor_id: uuid.UUID | None = None,
    ) -> AgentTask | None:
        """Update a task's mutable fields.

        Only tasks in 'pending' or 'failed' status can be updated.
        """
        task = await self.get_task(task_id)
        if task is None:
            return None

        if task.status not in ("pending", "failed"):
            return task  # Return unchanged; caller checks status

        if "input_data" in update_data and update_data["input_data"] is not None:
            task.input_data = update_data["input_data"]
        if "patient_id" in update_data and update_data["patient_id"] is not None:
            task.patient_id = update_data["patient_id"]
        if "organization_id" in update_data and update_data["organization_id"] is not None:
            task.organization_id = update_data["organization_id"]

        await self._session.flush()

        await self._audit_logger.log(
            action="agent_task_updated",
            actor_id=actor_id,
            actor_type="user" if actor_id else "system",
            resource_type="agent_task",
            resource_id=str(task.id),
            details={"updated_fields": list(update_data.keys())},
        )

        return task

    async def delete_task(
        self,
        task_id: str,
        *,
        actor_id: uuid.UUID | None = None,
    ) -> bool:
        """Delete a task. Only tasks in terminal states can be deleted.

        Returns True if deleted, False if not found or not deletable.
        """
        task = await self.get_task(task_id)
        if task is None:
            return False

        if task.status not in ("completed", "failed", "cancelled"):
            return False

        await self._audit_logger.log(
            action="agent_task_deleted",
            actor_id=actor_id,
            actor_type="user" if actor_id else "system",
            resource_type="agent_task",
            resource_id=str(task.id),
        )

        await self._session.delete(task)
        await self._session.flush()
        return True

    async def cancel_task(
        self,
        task_id: str,
        *,
        actor_id: uuid.UUID | None = None,
    ) -> AgentTask | None:
        """Cancel a running task."""
        task = await self.get_task(task_id)
        if task is None:
            return None

        if task.status in ("completed", "failed", "cancelled"):
            return task

        # Cancel via workflow service if linked
        if task.workflow_execution_id:
            from app.models.workflow import WorkflowExecution

            wf_result = await self._session.execute(
                select(WorkflowExecution).where(
                    WorkflowExecution.id == task.workflow_execution_id
                )
            )
            wf = wf_result.scalar_one_or_none()
            if wf:
                await self._workflow_service.cancel_workflow(wf.workflow_id)

        task.status = "cancelled"
        await self._session.flush()

        await self._audit_logger.log(
            action="agent_task_cancelled",
            actor_id=actor_id,
            actor_type="user" if actor_id else "system",
            resource_type="agent_task",
            resource_id=str(task.id),
        )

        # Broadcast cancellation status update
        try:
            from app.api.websocket import broadcast_task_update
            await broadcast_task_update(
                task_id=str(task.id),
                agent_type=task.agent_type,
                task_status="cancelled",
            )
        except Exception as exc:
            logger.warning("WebSocket broadcast failed: %s", exc)

        return task

    async def get_agent_stats(self, agent_type: str) -> dict[str, Any]:
        """Get aggregate statistics for an agent type."""
        base = select(AgentTask).where(AgentTask.agent_type == agent_type)

        total_result = await self._session.execute(
            select(func.count()).select_from(AgentTask).where(
                AgentTask.agent_type == agent_type
            )
        )
        total = total_result.scalar() or 0

        stats: dict[str, int] = {}
        for status_val in ("pending", "running", "completed", "failed", "review", "cancelled"):
            count_result = await self._session.execute(
                select(func.count()).select_from(AgentTask).where(
                    AgentTask.agent_type == agent_type,
                    AgentTask.status == status_val,
                )
            )
            stats[status_val] = count_result.scalar() or 0

        # Average confidence for completed tasks
        avg_result = await self._session.execute(
            select(func.avg(AgentTask.confidence_score)).where(
                AgentTask.agent_type == agent_type,
                AgentTask.status == "completed",
                AgentTask.confidence_score.isnot(None),
            )
        )
        avg_confidence = avg_result.scalar()

        return {
            "agent_type": agent_type,
            "total_tasks": total,
            "pending": stats.get("pending", 0),
            "running": stats.get("running", 0),
            "completed": stats.get("completed", 0),
            "failed": stats.get("failed", 0),
            "in_review": stats.get("review", 0),
            "cancelled": stats.get("cancelled", 0),
            "avg_confidence": round(avg_confidence, 3) if avg_confidence is not None else None,
        }

    def _build_patient_context(self, input_data: dict[str, Any] | None) -> dict[str, Any]:
        """Extract patient context from input data."""
        if not input_data:
            return {}
        return {
            k: v
            for k, v in input_data.items()
            if k
            in (
                "patient_id",
                "subscriber_id",
                "subscriber_first_name",
                "subscriber_last_name",
                "subscriber_dob",
            )
            and v
        }

    def _build_payer_context(self, input_data: dict[str, Any] | None) -> dict[str, Any]:
        """Extract payer context from input data."""
        if not input_data:
            return {}
        return {
            k: v
            for k, v in input_data.items()
            if k in ("payer_id", "payer_name", "payer_id_code") and v
        }
