"""Workflow service — start, query, cancel workflows via Temporal client.

Provides the business logic layer for managing Temporal workflow executions.
This service is used by the API routes to interact with the Temporal server
and track workflow state in the database.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.client import Client as TemporalClient, WorkflowHandle

from app.models.agent_task import AgentTask
from app.models.workflow import WorkflowExecution
from app.workflows.base import (
    DEFAULT_TASK_QUEUE,
    WorkflowInput,
    WorkflowResult,
    WorkflowStatus,
)
from app.core.logging_config import get_correlation_id
from app.workflows.eligibility import EligibilityWorkflow, run_eligibility_workflow
from app.workflows.scheduling import SchedulingWorkflow, run_scheduling_workflow
from app.workflows.claims import ClaimsWorkflow, run_claims_workflow
from app.workflows.prior_auth import PriorAuthWorkflow, run_prior_auth_workflow
from app.workflows.credentialing import CredentialingWorkflow, run_credentialing_workflow
from app.workflows.compliance import ComplianceWorkflow, run_compliance_workflow
from app.workflows.agent_workflow import GenericAgentWorkflow

logger = logging.getLogger(__name__)


# Mapping from agent_type to Temporal workflow class
_WORKFLOW_TYPES: dict[str, type] = {
    "eligibility": EligibilityWorkflow,
    "scheduling": SchedulingWorkflow,
    "claims": ClaimsWorkflow,
    "prior_auth": PriorAuthWorkflow,
    "credentialing": CredentialingWorkflow,
    "compliance": ComplianceWorkflow,
}

# Fallback inline runners for when Temporal is not available
_INLINE_RUNNERS = {
    "eligibility": run_eligibility_workflow,
    "scheduling": run_scheduling_workflow,
    "claims": run_claims_workflow,
    "prior_auth": run_prior_auth_workflow,
    "credentialing": run_credentialing_workflow,
    "compliance": run_compliance_workflow,
}


class WorkflowService:
    """Service for managing Temporal workflow executions.

    Provides methods to start, query, cancel, and list workflows.
    Tracks workflow state in the database for query and reporting,
    and dispatches execution to the Temporal server when a client
    is provided.

    Background completion tasks are tracked in ``_background_tasks`` so
    they are not garbage-collected prematurely and can be cleaned up on
    shutdown via :meth:`shutdown`.

    Args:
        session: SQLAlchemy async session for DB operations.
        temporal_client: Optional pre-connected Temporal client.
            When provided, workflows are dispatched to Temporal.
            When ``None``, known agent types fall back to inline
            execution (useful for testing).
    """

    def __init__(
        self,
        session: AsyncSession,
        temporal_client: TemporalClient | None = None,
    ):
        self._session = session
        self._temporal_client = temporal_client
        self._background_tasks: set[asyncio.Task] = set()

    async def shutdown(self) -> None:
        """Cancel and await all background completion tasks.

        Should be called during application shutdown to avoid
        unawaited-coroutine warnings and ensure clean teardown.
        """
        for task in self._background_tasks:
            task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        self._background_tasks.clear()

    async def start_workflow(
        self,
        *,
        agent_type: str,
        task_id: str,
        input_data: dict[str, Any] | None = None,
        patient_context: dict[str, Any] | None = None,
        payer_context: dict[str, Any] | None = None,
        organization_id: str | None = None,
        clearinghouse_config: dict[str, Any] | None = None,
        task_queue: str | None = None,
    ) -> WorkflowExecution:
        """Start a new workflow for the given agent type.

        Creates a WorkflowExecution record, then dispatches the workflow
        to Temporal (or runs inline as a fallback).

        Returns:
            The created WorkflowExecution record.
        """
        workflow_id = f"{agent_type}-{task_id}-{uuid.uuid4().hex[:8]}"
        run_id = uuid.uuid4().hex
        queue = task_queue or DEFAULT_TASK_QUEUE

        # Create the DB record
        execution = WorkflowExecution(
            workflow_id=workflow_id,
            run_id=run_id,
            agent_type=agent_type,
            status="running",
            task_queue=queue,
            input_data={
                "task_id": task_id,
                "agent_type": agent_type,
                "input_data": input_data or {},
                "patient_context": patient_context or {},
                "payer_context": payer_context or {},
                "organization_id": organization_id,
            },
        )
        self._session.add(execution)
        await self._session.flush()

        # Link the task to this workflow execution
        result = await self._session.execute(
            select(AgentTask).where(AgentTask.id == task_id)
        )
        task = result.scalar_one_or_none()
        if task is not None:
            task.workflow_execution_id = execution.id
            task.status = "running"

        await self._session.flush()

        # Build the workflow input with correlation ID for cross-component tracing
        workflow_input = WorkflowInput(
            task_id=task_id,
            agent_type=agent_type,
            input_data=input_data or {},
            patient_context=patient_context or {},
            payer_context=payer_context or {},
            organization_id=organization_id,
            clearinghouse_config=clearinghouse_config,
            correlation_id=get_correlation_id(),
        )

        try:
            if self._temporal_client is not None:
                # Dispatch to Temporal
                await self._start_temporal_workflow(
                    execution, task, workflow_input, queue,
                )
            else:
                # Fallback: inline execution
                await self._run_inline(execution, task, workflow_input)
        except Exception as exc:
            logger.error("Workflow %s failed: %s", workflow_id, exc)
            execution.status = "failed"
            execution.error_message = str(exc)
            if task is not None:
                task.status = "failed"
                task.error_message = str(exc)

        await self._session.flush()
        return execution

    async def _start_temporal_workflow(
        self,
        execution: WorkflowExecution,
        task: AgentTask | None,
        workflow_input: WorkflowInput,
        task_queue: str,
    ) -> None:
        """Dispatch the workflow to Temporal and start a background awaiter.

        The workflow handle is started and the run_id is stored.  A
        background task is spawned to await the Temporal result; when the
        workflow completes (or fails) the awaiter updates the DB and
        broadcasts a real-time WebSocket event — no polling required.
        """
        workflow_cls = _WORKFLOW_TYPES.get(workflow_input.agent_type, GenericAgentWorkflow)

        handle = await self._temporal_client.start_workflow(
            workflow_cls.run,
            workflow_input,
            id=execution.workflow_id,
            task_queue=task_queue,
        )

        # Store the Temporal run_id — execution stays in "running" status
        execution.run_id = handle.result_run_id

        # Spawn a tracked background task that awaits the workflow result
        # and broadcasts a WebSocket event immediately on completion.
        # The task is registered in _background_tasks so it is not
        # garbage-collected prematurely and can be cleaned up on shutdown.
        bg_task = asyncio.create_task(
            _await_temporal_completion(
                temporal_client=self._temporal_client,
                workflow_id=execution.workflow_id,
                task_id=workflow_input.task_id,
                agent_type=workflow_input.agent_type,
            ),
            name=f"temporal-awaiter-{execution.workflow_id}",
        )
        self._background_tasks.add(bg_task)
        bg_task.add_done_callback(self._background_tasks.discard)

    async def refresh_workflow_status(self, workflow_id: str) -> WorkflowExecution | None:
        """Poll Temporal for a workflow's current result and update the DB.

        If the workflow has completed (or failed), the local
        ``WorkflowExecution`` and linked ``AgentTask`` records are updated.
        If it is still running the records are left unchanged.

        Returns the (possibly updated) ``WorkflowExecution``, or ``None``
        if the workflow_id is unknown.
        """
        execution = await self.get_workflow(workflow_id)
        if execution is None:
            return None

        # Already in a terminal state locally — nothing to refresh
        if execution.status in ("completed", "failed", "cancelled"):
            return execution

        if self._temporal_client is None:
            return execution

        try:
            handle = self._temporal_client.get_workflow_handle(workflow_id)
            desc = await handle.describe()
            status_name = desc.status.name if desc.status else ""

            if status_name in ("COMPLETED",):
                raw_result = await handle.result()
                # Temporal may return a dict (serialized) or WorkflowResult
                if isinstance(raw_result, dict):
                    wf_result = WorkflowResult(
                        task_id=raw_result.get("task_id", ""),
                        agent_type=raw_result.get("agent_type", ""),
                        status=raw_result.get("status", "completed"),
                        output_data=raw_result.get("output_data", {}),
                        confidence=raw_result.get("confidence", 0.0),
                        needs_review=raw_result.get("needs_review", False),
                        review_reason=raw_result.get("review_reason", ""),
                        error=raw_result.get("error"),
                        clearinghouse_transaction_id=raw_result.get("clearinghouse_transaction_id"),
                    )
                else:
                    wf_result = raw_result
                task = await self._get_linked_task(execution)
                await self._complete_workflow(execution, task, wf_result)
            elif status_name in ("FAILED", "TERMINATED", "TIMED_OUT"):
                execution.status = "failed"
                execution.error_message = f"Temporal status: {status_name}"
                task = await self._get_linked_task(execution)
                if task is not None and task.status not in ("completed", "failed"):
                    task.status = "failed"
                    task.error_message = execution.error_message
            elif status_name in ("CANCELED",):
                execution.status = "cancelled"
                task = await self._get_linked_task(execution)
                if task is not None and task.status not in ("completed", "failed"):
                    task.status = "cancelled"

            await self._session.flush()
        except (ConnectionError, OSError) as exc:
            # Transient network errors — log warning but preserve current
            # DB state (don't mutate).  The caller can retry later.
            logger.warning(
                "Transient error refreshing workflow %s from Temporal: %s",
                workflow_id, exc,
            )
        except Exception as exc:
            # Unexpected error — record it on the execution so the
            # inconsistency is visible rather than silently swallowed.
            logger.error(
                "Failed to refresh workflow %s from Temporal: %s",
                workflow_id, exc,
            )
            execution.error_message = f"Refresh error: {exc}"
            await self._session.flush()

        return execution

    async def _get_linked_task(self, execution: WorkflowExecution) -> AgentTask | None:
        """Retrieve the AgentTask linked to a WorkflowExecution."""
        task_id = (execution.input_data or {}).get("task_id")
        if not task_id:
            return None
        result = await self._session.execute(
            select(AgentTask).where(AgentTask.id == task_id)
        )
        return result.scalar_one_or_none()

    async def _run_inline(
        self,
        execution: WorkflowExecution,
        task: AgentTask | None,
        workflow_input: WorkflowInput,
    ) -> None:
        """Run the workflow inline (fallback when no Temporal client).

        Commits the current session before executing the workflow so that
        activities opening independent DB sessions can see the task and
        workflow records created earlier in the request.
        """
        runner = _INLINE_RUNNERS.get(workflow_input.agent_type)
        if runner is not None:
            # Commit task/execution records so that activities using
            # independent sessions (e.g. write_eligibility_result) can
            # read them.  Without this, the task may not be visible and
            # the activity would fail with "AgentTask not found".
            await self._session.commit()

            wf_result = await runner(workflow_input)
            await self._complete_workflow(execution, task, wf_result)
        else:
            # No runner available — mark as pending for Temporal
            execution.status = "pending"
            logger.info(
                "No inline runner for agent_type '%s'; "
                "workflow %s queued for Temporal",
                workflow_input.agent_type, execution.workflow_id,
            )

    async def _complete_workflow(
        self,
        execution: WorkflowExecution,
        task: AgentTask | None,
        result: WorkflowResult,
    ) -> None:
        """Update the workflow and task records with the workflow result.

        When the result indicates the task needs HITL review (low confidence
        or ambiguous response), creates a HITLReview record via the
        EscalationManager and sets the task status to 'review'.
        """
        if result.status == WorkflowStatus.COMPLETED.value:
            execution.status = "completed"
            execution.output_data = result.output_data

            if task is not None:
                task.output_data = result.output_data
                task.confidence_score = result.confidence
                if result.needs_review:
                    task.status = "review"
                    # Create HITL review record.  When `needs_review` is
                    # explicitly True (e.g. PA denials requiring appeal),
                    # the review must be created deterministically — not
                    # only when confidence falls below threshold.
                    try:
                        from app.core.hitl.escalation import EscalationManager
                        escalation_mgr = EscalationManager(self._session)

                        # First try the standard threshold-based path
                        review = await escalation_mgr.evaluate_and_escalate(
                            task_id=str(task.id),
                            agent_type=task.agent_type,
                            confidence=result.confidence,
                            agent_decision=result.output_data,
                            has_error=False,
                        )

                        # If threshold-based evaluation didn't create a
                        # review (confidence was above threshold) but the
                        # workflow explicitly flagged needs_review=True,
                        # force-create the review record.
                        if review is None:
                            review_reason = (
                                result.review_reason
                                or f"Workflow flagged needs_review=True for {task.agent_type}"
                            )
                            await escalation_mgr.create_review(
                                task_id=str(task.id),
                                reason=review_reason,
                                agent_decision=result.output_data,
                                confidence_score=result.confidence,
                            )
                    except Exception as exc:
                        logger.error(
                            "Failed to create HITL review for task %s: %s",
                            task.id, exc,
                        )
                else:
                    task.status = "completed"

                # Broadcast WebSocket status update
                try:
                    from app.api.websocket import broadcast_task_update
                    await broadcast_task_update(
                        task_id=str(task.id),
                        agent_type=task.agent_type,
                        task_status=task.status,
                        confidence=result.confidence,
                    )
                except Exception as exc:
                    logger.warning("WebSocket broadcast failed: %s", exc)

        elif result.status == WorkflowStatus.FAILED.value:
            execution.status = "failed"
            execution.error_message = result.error

            if task is not None:
                task.status = "failed"
                task.error_message = result.error

                # Broadcast failure status update
                try:
                    from app.api.websocket import broadcast_task_update
                    await broadcast_task_update(
                        task_id=str(task.id),
                        agent_type=task.agent_type,
                        task_status="failed",
                        error=result.error,
                    )
                except Exception as exc:
                    logger.warning("WebSocket broadcast failed: %s", exc)
        else:
            execution.status = result.status
            execution.output_data = result.output_data

    async def get_workflow(self, workflow_id: str) -> WorkflowExecution | None:
        """Get a workflow execution by its workflow_id."""
        result = await self._session.execute(
            select(WorkflowExecution).where(
                WorkflowExecution.workflow_id == workflow_id
            )
        )
        return result.scalar_one_or_none()

    async def get_workflow_by_id(self, execution_id: str) -> WorkflowExecution | None:
        """Get a workflow execution by its database ID."""
        result = await self._session.execute(
            select(WorkflowExecution).where(WorkflowExecution.id == execution_id)
        )
        return result.scalar_one_or_none()

    async def cancel_workflow(self, workflow_id: str) -> WorkflowExecution | None:
        """Cancel a running workflow.

        If a Temporal client is available, sends a cancellation request
        to Temporal in addition to updating the local DB state.
        """
        execution = await self.get_workflow(workflow_id)
        if execution is None:
            return None

        if execution.status in ("completed", "failed", "cancelled"):
            logger.warning(
                "Cannot cancel workflow %s: already in terminal state '%s'",
                workflow_id, execution.status,
            )
            return execution

        # Cancel via Temporal if a client is available.
        # Only update local DB state if the Temporal cancellation succeeds
        # (or if no Temporal client is configured), to keep DB and Temporal
        # state consistent.
        if self._temporal_client is not None:
            try:
                handle = self._temporal_client.get_workflow_handle(workflow_id)
                await handle.cancel()
            except Exception as exc:
                logger.warning(
                    "Temporal cancel for %s failed: %s — "
                    "local state NOT updated to preserve consistency",
                    workflow_id, exc,
                )
                execution.error_message = f"Cancel failed: {exc}"
                await self._session.flush()
                return execution

        execution.status = "cancelled"

        # Also cancel the associated task
        if execution.input_data and execution.input_data.get("task_id"):
            task_id = execution.input_data["task_id"]
            result = await self._session.execute(
                select(AgentTask).where(AgentTask.id == task_id)
            )
            task = result.scalar_one_or_none()
            if task is not None and task.status not in ("completed", "failed"):
                task.status = "cancelled"

        await self._session.flush()
        return execution

    async def list_workflows(
        self,
        *,
        agent_type: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[WorkflowExecution], int]:
        """List workflow executions with optional filters."""
        query = select(WorkflowExecution)
        count_query = select(func.count()).select_from(WorkflowExecution)

        if agent_type:
            query = query.where(WorkflowExecution.agent_type == agent_type)
            count_query = count_query.where(WorkflowExecution.agent_type == agent_type)

        if status:
            query = query.where(WorkflowExecution.status == status)
            count_query = count_query.where(WorkflowExecution.status == status)

        query = query.order_by(WorkflowExecution.created_at.desc())
        query = query.limit(limit).offset(offset)

        result = await self._session.execute(query)
        executions = list(result.scalars().all())

        count_result = await self._session.execute(count_query)
        total = count_result.scalar() or 0

        return executions, total

    async def get_workflow_history(self, workflow_id: str) -> list[dict[str, Any]] | None:
        """Get the event history for a workflow execution.

        If a Temporal client is available, fetches the real workflow
        history from Temporal.  Otherwise, returns a synthetic history
        based on the DB record.

        Returns None if the workflow_id is not found in the database.
        """
        # Check that the workflow exists in our DB first
        execution = await self.get_workflow(workflow_id)
        if execution is None:
            return None

        if self._temporal_client is not None:
            return await self._get_temporal_history(workflow_id)

        return await self._get_synthetic_history(workflow_id)

    async def _get_temporal_history(self, workflow_id: str) -> list[dict[str, Any]]:
        """Fetch real workflow event history from Temporal.

        Falls back to synthetic history only on transient network errors.
        Other exceptions are propagated so callers can handle them.
        """
        try:
            handle = self._temporal_client.get_workflow_handle(workflow_id)
            events: list[dict[str, Any]] = []
            event_id = 0
            async for event in handle.fetch_history_events():
                event_id += 1
                event_type_str = event.event_type.name if hasattr(event.event_type, "name") else str(event.event_type)
                details = self._extract_temporal_event_details(event, event_type_str)
                events.append({
                    "event_id": event_id,
                    "event_type": event_type_str,
                    "timestamp": event.event_time.isoformat() if event.event_time else "",
                    "details": details,
                })
            return events
        except (ConnectionError, OSError) as exc:
            # Transient network issue — degrade gracefully to synthetic history
            logger.warning(
                "Transient error fetching Temporal history for %s: %s — "
                "falling back to synthetic history",
                workflow_id, exc,
            )
            return await self._get_synthetic_history(workflow_id)

    @staticmethod
    def _extract_temporal_event_details(event: Any, event_type: str) -> dict[str, Any]:
        """Extract meaningful details from a Temporal history event.

        Different event types carry attributes under different field names.
        We inspect the well-known attribute fields and surface the useful
        subset so the frontend can render expandable detail panels.
        """
        details: dict[str, Any] = {}

        # Map event type prefixes to the attribute field names used by the
        # Temporal Python SDK history event objects.
        _attr_field_map = {
            "WorkflowExecutionStarted": "workflow_execution_started_event_attributes",
            "WorkflowExecutionCompleted": "workflow_execution_completed_event_attributes",
            "WorkflowExecutionFailed": "workflow_execution_failed_event_attributes",
            "WorkflowExecutionTimedOut": "workflow_execution_timed_out_event_attributes",
            "WorkflowExecutionCanceled": "workflow_execution_canceled_event_attributes",
            "ActivityTaskScheduled": "activity_task_scheduled_event_attributes",
            "ActivityTaskStarted": "activity_task_started_event_attributes",
            "ActivityTaskCompleted": "activity_task_completed_event_attributes",
            "ActivityTaskFailed": "activity_task_failed_event_attributes",
            "ActivityTaskTimedOut": "activity_task_timed_out_event_attributes",
            "TimerStarted": "timer_started_event_attributes",
            "TimerFired": "timer_fired_event_attributes",
        }

        # Try to locate the attributes container for this event type
        attrs = None
        for prefix, field in _attr_field_map.items():
            if event_type.startswith(prefix) or event_type == prefix:
                attrs = getattr(event, field, None)
                break

        # If we didn't match a known prefix, try a generic scan
        if attrs is None:
            for field in _attr_field_map.values():
                attrs = getattr(event, field, None)
                if attrs is not None:
                    break

        if attrs is None:
            return details

        # --- Extract fields depending on the event type ---

        # Activity scheduled: surface activity type/name and task queue
        if "Scheduled" in event_type and "Activity" in event_type:
            if hasattr(attrs, "activity_type") and attrs.activity_type:
                act_name = getattr(attrs.activity_type, "name", None) or str(attrs.activity_type)
                details["activity_type"] = act_name
            if hasattr(attrs, "task_queue") and attrs.task_queue:
                tq_name = getattr(attrs.task_queue, "name", None) or str(attrs.task_queue)
                details["task_queue"] = tq_name
            if hasattr(attrs, "input") and attrs.input:
                try:
                    details["input_summary"] = str(attrs.input)[:500]
                except Exception:
                    pass

        # Activity started: attempt number
        elif "Started" in event_type and "Activity" in event_type:
            if hasattr(attrs, "attempt"):
                details["attempt"] = attrs.attempt

        # Activity completed: result preview
        elif "Completed" in event_type and "Activity" in event_type:
            if hasattr(attrs, "result") and attrs.result:
                try:
                    details["result_summary"] = str(attrs.result)[:500]
                except Exception:
                    pass

        # Activity failed: failure info
        elif "Failed" in event_type and "Activity" in event_type:
            if hasattr(attrs, "failure") and attrs.failure:
                failure = attrs.failure
                details["failure_message"] = getattr(failure, "message", str(failure))[:500]
                if hasattr(failure, "activity_type"):
                    details["activity_type"] = str(failure.activity_type)

        # Workflow started: workflow type, task queue
        elif "WorkflowExecutionStarted" in event_type:
            if hasattr(attrs, "workflow_type") and attrs.workflow_type:
                wf_name = getattr(attrs.workflow_type, "name", None) or str(attrs.workflow_type)
                details["workflow_type"] = wf_name
            if hasattr(attrs, "task_queue") and attrs.task_queue:
                tq_name = getattr(attrs.task_queue, "name", None) or str(attrs.task_queue)
                details["task_queue"] = tq_name

        # Workflow completed: result preview
        elif "WorkflowExecutionCompleted" in event_type:
            if hasattr(attrs, "result") and attrs.result:
                try:
                    details["result_summary"] = str(attrs.result)[:500]
                except Exception:
                    pass

        # Workflow failed: failure message
        elif "WorkflowExecutionFailed" in event_type:
            if hasattr(attrs, "failure") and attrs.failure:
                failure = attrs.failure
                details["failure_message"] = getattr(failure, "message", str(failure))[:500]

        # Timer events: timer ID and duration
        elif "Timer" in event_type:
            if hasattr(attrs, "timer_id"):
                details["timer_id"] = str(attrs.timer_id)
            if hasattr(attrs, "start_to_fire_timeout"):
                details["duration"] = str(attrs.start_to_fire_timeout)

        return details

    async def _get_synthetic_history(self, workflow_id: str) -> list[dict[str, Any]]:
        """Generate a synthetic event history from the DB record."""
        execution = await self.get_workflow(workflow_id)
        if execution is None:
            return []

        events: list[dict[str, Any]] = []

        # Start event
        events.append({
            "event_id": 1,
            "event_type": "WorkflowExecutionStarted",
            "timestamp": execution.created_at.isoformat() if execution.created_at else "",
            "details": {
                "workflow_id": execution.workflow_id,
                "agent_type": execution.agent_type,
                "task_queue": execution.task_queue,
                "input": execution.input_data,
            },
        })

        # Completion or failure event
        if execution.status == "completed":
            events.append({
                "event_id": 2,
                "event_type": "WorkflowExecutionCompleted",
                "timestamp": (execution.updated_at or execution.created_at).isoformat() if execution.created_at else "",
                "details": {
                    "output": execution.output_data,
                },
            })
        elif execution.status == "failed":
            events.append({
                "event_id": 2,
                "event_type": "WorkflowExecutionFailed",
                "timestamp": (execution.updated_at or execution.created_at).isoformat() if execution.created_at else "",
                "details": {
                    "error": execution.error_message,
                },
            })
        elif execution.status == "cancelled":
            events.append({
                "event_id": 2,
                "event_type": "WorkflowExecutionCancelled",
                "timestamp": (execution.updated_at or execution.created_at).isoformat() if execution.created_at else "",
                "details": {},
            })

        return events


# ── Background Temporal completion awaiter ──────────────────────────


async def _await_temporal_completion(
    *,
    temporal_client: TemporalClient,
    workflow_id: str,
    task_id: str,
    agent_type: str,
) -> None:
    """Await a Temporal workflow result and update DB + broadcast immediately.

    Runs as a background ``asyncio`` task spawned by
    :meth:`WorkflowService._start_temporal_workflow`.  When the workflow
    finishes (success or failure) this function opens a *new* DB session,
    persists the outcome, and broadcasts a WebSocket event — providing
    true real-time push semantics without requiring the client to poll.
    """
    try:
        handle = temporal_client.get_workflow_handle(workflow_id)
        raw_result = await handle.result()  # blocks until workflow ends

        if isinstance(raw_result, dict):
            wf_result = WorkflowResult(
                task_id=raw_result.get("task_id", ""),
                agent_type=raw_result.get("agent_type", ""),
                status=raw_result.get("status", "completed"),
                output_data=raw_result.get("output_data", {}),
                confidence=raw_result.get("confidence", 0.0),
                needs_review=raw_result.get("needs_review", False),
                review_reason=raw_result.get("review_reason", ""),
                error=raw_result.get("error"),
                clearinghouse_transaction_id=raw_result.get("clearinghouse_transaction_id"),
            )
        else:
            wf_result = raw_result

        # Open independent session for DB writes with proper pool tuning
        from app.dependencies import create_disposable_engine
        engine = create_disposable_engine()
        try:
            factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
            async with factory() as session:
                svc = WorkflowService(session, temporal_client=None)
                execution = await svc.get_workflow(workflow_id)
                if execution is None:
                    return
                # Only update if still running (avoid overwriting manual changes)
                if execution.status not in ("completed", "failed", "cancelled"):
                    task = await svc._get_linked_task(execution)
                    await svc._complete_workflow(execution, task, wf_result)
                    await session.commit()
        finally:
            await engine.dispose()

    except asyncio.CancelledError:
        return
    except Exception as exc:
        logger.warning(
            "Background completion awaiter for workflow %s failed: %s",
            workflow_id, exc,
        )
        # Best-effort: broadcast a generic update so clients know to re-fetch
        try:
            from app.api.websocket import broadcast_task_update
            await broadcast_task_update(
                task_id=task_id,
                agent_type=agent_type,
                task_status="failed" if "fail" in str(exc).lower() else "unknown",
                error=str(exc),
            )
        except Exception:
            pass
