"""Generic agent workflow template — wraps any agent in Temporal execution.

Provides a reusable Temporal workflow that can execute any agent type by
dispatching to a common set of activities: validate → run agent → handle
result → write to DB.  Agent-specific behaviour is parameterised via the
``agent_type`` field on the ``WorkflowInput``.

For agents that need custom orchestration (like the Eligibility workflow
with its multi-step clearinghouse interaction), a specialised workflow
class can be used instead.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from dataclasses import asdict
from typing import Any

from temporalio import activity, workflow

with workflow.unsafe.imports_passed_through():
    from app.workflows.base import (
        AGENT_ACTIVITY_TIMEOUT,
        AGENT_RETRY_POLICY,
        DB_ACTIVITY_TIMEOUT,
        DB_HEARTBEAT_TIMEOUT,
        DB_RETRY_POLICY,
        ActivityResult,
        WorkflowInput,
        WorkflowResult,
        WorkflowStatus,
        safe_heartbeat,
        set_workflow_correlation_id,
        restore_correlation_id,
    )

logger = logging.getLogger(__name__)


# ── Generic Activities ──────────────────────────────────────────────────


@activity.defn
async def validate_agent_input(args: dict[str, Any]) -> dict[str, Any]:
    """Generic input validation for any agent type.

    Ensures the input contains an ``agent_type`` and at least one field in
    ``input_data``.  Specialised validators can be registered per agent
    type in the ``_VALIDATORS`` dict below.
    """
    restore_correlation_id(args)
    agent_type = args.get("agent_type", "")
    input_data = args.get("input_data", {})

    if not agent_type:
        return asdict(ActivityResult(
            success=False,
            error="agent_type is required",
        ))

    if not input_data:
        return asdict(ActivityResult(
            success=False,
            error=f"input_data is required for agent_type '{agent_type}'",
        ))

    # Delegate to type-specific validator if registered
    validator = _VALIDATORS.get(agent_type)
    if validator is not None:
        errors = validator(input_data)
        if errors:
            return asdict(ActivityResult(
                success=False,
                error=f"Validation errors: {'; '.join(errors)}",
            ))

    return asdict(ActivityResult(
        success=True,
        data={
            "agent_type": agent_type,
            "input_data": input_data,
            "task_id": args.get("task_id", ""),
        },
    ))


@activity.defn
async def execute_agent(args: dict[str, Any]) -> dict[str, Any]:
    """Execute the agent logic for the given agent type.

    In a full implementation this would invoke the LangGraph agent graph.
    Currently it delegates to agent runner functions registered in
    ``_AGENT_RUNNERS``.  If no runner is registered the activity returns
    a pending result so the task can be picked up later.
    """
    restore_correlation_id(args)
    agent_type = args.get("agent_type", "")
    input_data = args.get("input_data", {})
    task_id = args.get("task_id", "")

    runner = _AGENT_RUNNERS.get(agent_type)
    if runner is not None:
        try:
            result = await runner(input_data)
            return asdict(ActivityResult(
                success=True,
                data={
                    "task_id": task_id,
                    "agent_type": agent_type,
                    "output": result,
                },
            ))
        except (ValueError, KeyError, TypeError) as exc:
            # Non-retryable business/validation errors → return failure result
            return asdict(ActivityResult(
                success=False,
                error=f"Agent execution failed: {exc}",
            ))
        # All other exceptions (ConnectionError, IOError, etc.) propagate to
        # Temporal so the activity is retried per AGENT_RETRY_POLICY.

    # No runner registered — return a placeholder result
    return asdict(ActivityResult(
        success=True,
        data={
            "task_id": task_id,
            "agent_type": agent_type,
            "output": {"status": "pending", "message": f"No runner registered for {agent_type}"},
        },
    ))


@activity.defn
async def write_agent_result(args: dict[str, Any]) -> dict[str, Any]:
    """Persist the agent result to the database.

    Updates the associated ``AgentTask`` record with the output data.
    """
    restore_correlation_id(args)
    task_id = args.get("task_id", "")
    output = args.get("output", {})

    # Send a heartbeat before the DB operation
    safe_heartbeat("writing_agent_result")

    from app.workflows.eligibility import _get_activity_session_factory
    from sqlalchemy import select
    from app.models.agent_task import AgentTask

    factory, engine_to_dispose = _get_activity_session_factory()
    try:
        async with factory() as session:
            result = await session.execute(
                select(AgentTask).where(AgentTask.id == task_id)
            )
            task = result.scalar_one_or_none()
            if task is not None:
                task.output_data = output
                task.status = "completed"
                await session.commit()
            else:
                logger.warning("AgentTask %s not found; skipping DB update", task_id)
    except Exception as exc:
        logger.error("Failed to persist agent result to DB: %s", exc)
        raise  # Let Temporal retry via DB_RETRY_POLICY
    finally:
        if engine_to_dispose is not None:
            await engine_to_dispose.dispose()

    return asdict(ActivityResult(
        success=True,
        data={"task_id": task_id, "output": output},
    ))


# ── Registries ──────────────────────────────────────────────────────────


# Per-agent-type input validators: receive input_data dict, return list of error strings
_VALIDATORS: dict[str, Any] = {}

# Per-agent-type runner functions: receive input_data dict, return output dict
_AGENT_RUNNERS: dict[str, Any] = {}


def register_agent_validator(agent_type: str, fn: Any) -> None:
    """Register an input validator for *agent_type*."""
    _VALIDATORS[agent_type] = fn


def register_agent_runner(agent_type: str, fn: Any) -> None:
    """Register a runner function for *agent_type*."""
    _AGENT_RUNNERS[agent_type] = fn


# ── Workflow Definition ─────────────────────────────────────────────────


@workflow.defn
class GenericAgentWorkflow:
    """Generic Temporal workflow that can execute any agent type.

    The workflow dispatches three activities in sequence:
    1. ``validate_agent_input`` — validate input data
    2. ``execute_agent`` — run the agent logic
    3. ``write_agent_result`` — persist the result to DB

    This serves as the default workflow template.  Agents that need
    custom orchestration (multi-step clearinghouse interactions, long-running
    polling, etc.) should use a dedicated workflow class.
    """

    @workflow.run
    async def run(self, workflow_input: WorkflowInput) -> WorkflowResult:
        task_id = workflow_input.task_id
        agent_type = workflow_input.agent_type
        correlation_id = workflow_input.correlation_id or ""

        # Step 1: Validate
        validation = await workflow.execute_activity(
            validate_agent_input,
            {
                "task_id": task_id,
                "agent_type": agent_type,
                "input_data": workflow_input.input_data,
                "correlation_id": correlation_id,
            },
            start_to_close_timeout=AGENT_ACTIVITY_TIMEOUT,
            retry_policy=AGENT_RETRY_POLICY.to_retry_policy(),
        )
        if not validation.get("success"):
            return WorkflowResult(
                task_id=task_id,
                agent_type=agent_type,
                status=WorkflowStatus.FAILED.value,
                error=validation.get("error", "Validation failed"),
            )

        # Step 2: Execute agent
        agent_result = await workflow.execute_activity(
            execute_agent,
            {**validation.get("data", {}), "correlation_id": correlation_id},
            start_to_close_timeout=AGENT_ACTIVITY_TIMEOUT,
            retry_policy=AGENT_RETRY_POLICY.to_retry_policy(),
        )
        if not agent_result.get("success"):
            return WorkflowResult(
                task_id=task_id,
                agent_type=agent_type,
                status=WorkflowStatus.FAILED.value,
                error=agent_result.get("error", "Agent execution failed"),
            )

        output = agent_result.get("data", {}).get("output", {})

        # Step 3: Write result
        await workflow.execute_activity(
            write_agent_result,
            {"task_id": task_id, "output": output, "correlation_id": correlation_id},
            start_to_close_timeout=DB_ACTIVITY_TIMEOUT,
            heartbeat_timeout=DB_HEARTBEAT_TIMEOUT,
            retry_policy=DB_RETRY_POLICY.to_retry_policy(),
        )

        return WorkflowResult(
            task_id=task_id,
            agent_type=agent_type,
            status=WorkflowStatus.COMPLETED.value,
            output_data=output,
        )
