"""Scheduling agent Temporal workflow.

Orchestrates the scheduling lifecycle:
  validate input → run scheduling agent → persist scheduling request → write result to DB

Uses the generic agent workflow activities for common steps and adds
scheduling-specific handling for FHIR appointment creation, waitlist
management, and scheduling request DB persistence.
"""

from __future__ import annotations

import logging
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


# ── Scheduling-specific Activities ─────────────────────────────────


@activity.defn
async def validate_scheduling_input(args: dict[str, Any]) -> dict[str, Any]:
    """Validate scheduling-specific input.

    Ensures the request has either a natural language text or structured
    scheduling parameters.
    """
    restore_correlation_id(args)
    input_data = args.get("input_data", {})
    task_id = args.get("task_id", "")

    has_text = bool(input_data.get("request_text"))
    has_structured = bool(
        input_data.get("specialty")
        or input_data.get("provider_npi")
        or input_data.get("provider_name")
    )

    if not has_text and not has_structured:
        return asdict(ActivityResult(
            success=False,
            error="Scheduling request requires either 'request_text' or "
                  "structured parameters (specialty, provider_npi, provider_name)",
        ))

    return asdict(ActivityResult(
        success=True,
        data={
            "task_id": task_id,
            "agent_type": "scheduling",
            "input_data": input_data,
            "has_text": has_text,
            "has_structured": has_structured,
        },
    ))


@activity.defn
async def run_scheduling_agent_activity(args: dict[str, Any]) -> dict[str, Any]:
    """Execute the scheduling agent graph.

    Runs the full LangGraph scheduling workflow: parse_intent → query_availability →
    match_slots → create_appointment.
    """
    restore_correlation_id(args)
    input_data = args.get("input_data", {})
    task_id = args.get("task_id", "")

    safe_heartbeat("running_scheduling_agent")

    from app.agents.scheduling.graph import run_scheduling_agent
    from app.core.engine.llm_provider import LLMProvider, MockLLMBackend

    llm_provider = LLMProvider(primary=MockLLMBackend())

    try:
        state = await run_scheduling_agent(
            input_data=input_data,
            llm_provider=llm_provider,
            task_id=task_id,
        )

        output = state.get("decision", {})
        output["confidence"] = state.get("confidence", 0.0)
        output["needs_review"] = state.get("needs_review", False)
        output["review_reason"] = state.get("review_reason", "")

        # Carry forward scheduling-specific data for persistence
        appointment_result = state.get("appointment_result", {})
        parsed_intent = state.get("parsed_intent", {})
        waitlist_result = state.get("waitlist_result")
        audit_trail = [
            {
                "timestamp": e.get("timestamp", "") if isinstance(e, dict) else getattr(e, "timestamp", ""),
                "node": e.get("node", "") if isinstance(e, dict) else getattr(e, "node", ""),
                "action": e.get("action", "") if isinstance(e, dict) else getattr(e, "action", ""),
                "details": e.get("details", {}) if isinstance(e, dict) else getattr(e, "details", {}),
            }
            for e in state.get("audit_trail", [])
        ]

        return asdict(ActivityResult(
            success=True,
            data={
                "task_id": task_id,
                "agent_type": "scheduling",
                "output": output,
                "confidence": state.get("confidence", 0.0),
                "needs_review": state.get("needs_review", False),
                "review_reason": state.get("review_reason", ""),
                "appointment_result": appointment_result,
                "parsed_intent": parsed_intent,
                "waitlist_result": waitlist_result,
                "audit_trail": audit_trail,
            },
        ))
    except Exception as exc:
        return asdict(ActivityResult(
            success=False,
            error=f"Scheduling agent execution failed: {exc}",
        ))


@activity.defn
async def write_scheduling_result(args: dict[str, Any]) -> dict[str, Any]:
    """Persist the scheduling result to the database.

    Creates/updates:
    - AgentTask with output data and status
    - SchedulingRequest record with appointment/waitlist details
    - Audit log entries from the agent trail
    """
    restore_correlation_id(args)
    task_id = args.get("task_id", "")
    output = args.get("output", {})
    input_data = args.get("input_data", {})
    appointment_result = args.get("appointment_result", {})
    parsed_intent = args.get("parsed_intent", {})
    waitlist_result = args.get("waitlist_result")
    audit_trail = args.get("audit_trail", [])

    safe_heartbeat("writing_scheduling_result")

    from app.workflows.eligibility import _get_activity_session_factory
    from sqlalchemy import select
    from app.models.agent_task import AgentTask

    factory, engine_to_dispose = _get_activity_session_factory()
    try:
        async with factory() as session:
            # 1. Update AgentTask
            result = await session.execute(
                select(AgentTask).where(AgentTask.id == task_id)
            )
            task = result.scalar_one_or_none()
            if task is not None:
                task.output_data = output
                task.status = "completed"
                task.confidence_score = output.get("confidence", 0.0)
            else:
                logger.warning("AgentTask %s not found; skipping DB update", task_id)

            # 2. Upsert SchedulingRequest record (idempotent — keyed on task_id)
            from app.models.scheduling import SchedulingRequest
            from datetime import datetime, timezone

            patient_id = None
            if task is not None:
                patient_id = task.patient_id

            existing_sched_result = await session.execute(
                select(SchedulingRequest).where(SchedulingRequest.task_id == task_id)
            )
            sched_req = existing_sched_result.scalar_one_or_none()

            new_status = "booked" if appointment_result.get("success") else (
                "waitlisted" if waitlist_result else "pending"
            )

            if sched_req is None:
                sched_req = SchedulingRequest(
                    task_id=task_id,
                    patient_id=patient_id,
                    request_text=input_data.get("request_text"),
                    parsed_intent=parsed_intent or {},
                    provider_npi=parsed_intent.get("provider_npi") or input_data.get("provider_npi"),
                    specialty=parsed_intent.get("specialty") or input_data.get("specialty"),
                    status=new_status,
                    appointment_fhir_id=appointment_result.get("fhir_id") if appointment_result.get("success") else None,
                    appointment_details=appointment_result if appointment_result.get("success") else (
                        waitlist_result if waitlist_result else {}
                    ),
                )
                session.add(sched_req)
            else:
                # Update mutable fields on retry/replay
                sched_req.status = new_status
                sched_req.parsed_intent = parsed_intent or sched_req.parsed_intent
                if appointment_result.get("success"):
                    sched_req.appointment_fhir_id = appointment_result.get("fhir_id")
                    sched_req.appointment_details = appointment_result
                elif waitlist_result:
                    sched_req.appointment_details = waitlist_result

            # Parse dates if available
            date_start = parsed_intent.get("preferred_date_start") or input_data.get("preferred_date_start")
            if date_start:
                try:
                    sched_req.preferred_date_start = datetime.strptime(
                        date_start, "%Y-%m-%d"
                    ).replace(tzinfo=timezone.utc)
                except (ValueError, TypeError):
                    pass

            date_end = parsed_intent.get("preferred_date_end") or input_data.get("preferred_date_end")
            if date_end:
                try:
                    sched_req.preferred_date_end = datetime.strptime(
                        date_end, "%Y-%m-%d"
                    ).replace(tzinfo=timezone.utc)
                except (ValueError, TypeError):
                    pass

            # 3. Write audit log entries
            if audit_trail:
                from app.core.audit.logger import AuditLogger
                audit_logger = AuditLogger(session)
                for entry in audit_trail:
                    try:
                        await audit_logger.log(
                            action=f"agent:scheduling:{entry.get('action', 'unknown')}",
                            actor_type="agent",
                            resource_type="agent_task",
                            resource_id=str(task_id),
                            details={
                                "node": entry.get("node", ""),
                                **entry.get("details", {}),
                            },
                        )
                    except Exception as exc:
                        logger.warning("Failed to write audit entry: %s", exc)

            await session.commit()
    except Exception as exc:
        logger.error("Failed to persist scheduling result to DB: %s", exc)
        raise
    finally:
        if engine_to_dispose is not None:
            await engine_to_dispose.dispose()

    return asdict(ActivityResult(
        success=True,
        data={"task_id": task_id, "output": output},
    ))


# ── Workflow Definition ──────────────────────────────────────────────


@workflow.defn
class SchedulingWorkflow:
    """Temporal workflow for scheduling agent execution.

    Orchestrates: validate → run agent → persist scheduling request → write result.
    """

    @workflow.run
    async def run(self, workflow_input: WorkflowInput) -> WorkflowResult:
        task_id = workflow_input.task_id
        agent_type = workflow_input.agent_type
        correlation_id = workflow_input.correlation_id or ""

        # Step 1: Validate input
        validation = await workflow.execute_activity(
            validate_scheduling_input,
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

        # Step 2: Run scheduling agent
        agent_result = await workflow.execute_activity(
            run_scheduling_agent_activity,
            {
                "task_id": task_id,
                "input_data": workflow_input.input_data,
                "correlation_id": correlation_id,
            },
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

        agent_data = agent_result.get("data", {})
        output = agent_data.get("output", {})
        confidence = agent_data.get("confidence", 0.0)
        needs_review = agent_data.get("needs_review", False)
        review_reason = agent_data.get("review_reason", "")

        # Step 3: Write result (includes SchedulingRequest + audit)
        await workflow.execute_activity(
            write_scheduling_result,
            {
                "task_id": task_id,
                "output": output,
                "input_data": workflow_input.input_data,
                "appointment_result": agent_data.get("appointment_result", {}),
                "parsed_intent": agent_data.get("parsed_intent", {}),
                "waitlist_result": agent_data.get("waitlist_result"),
                "audit_trail": agent_data.get("audit_trail", []),
                "correlation_id": correlation_id,
            },
            start_to_close_timeout=DB_ACTIVITY_TIMEOUT,
            heartbeat_timeout=DB_HEARTBEAT_TIMEOUT,
            retry_policy=DB_RETRY_POLICY.to_retry_policy(),
        )

        return WorkflowResult(
            task_id=task_id,
            agent_type=agent_type,
            status=WorkflowStatus.COMPLETED.value,
            output_data=output,
            confidence=confidence,
            needs_review=needs_review,
            review_reason=review_reason,
        )


# ── Inline runner (fallback when Temporal is unavailable) ──────────


async def run_scheduling_workflow(workflow_input: WorkflowInput) -> WorkflowResult:
    """Run the scheduling workflow inline (no Temporal).

    Used as a fallback for testing or when Temporal is not available.
    Executes the full scheduling pipeline including DB persistence.
    """
    task_id = workflow_input.task_id
    agent_type = workflow_input.agent_type

    from app.agents.scheduling.graph import run_scheduling_agent
    from app.core.engine.llm_provider import LLMProvider, MockLLMBackend

    llm_provider = LLMProvider(primary=MockLLMBackend())

    try:
        state = await run_scheduling_agent(
            input_data=workflow_input.input_data,
            llm_provider=llm_provider,
            task_id=task_id,
        )

        output = state.get("decision", {})
        confidence = state.get("confidence", 0.0)
        needs_review = state.get("needs_review", False)
        review_reason = state.get("review_reason", "")

        # Persist scheduling request and audit trail (inline)
        try:
            audit_trail = []
            for entry in state.get("audit_trail", []):
                if isinstance(entry, dict):
                    audit_trail.append(entry)
                else:
                    audit_trail.append({
                        "timestamp": getattr(entry, "timestamp", ""),
                        "node": getattr(entry, "node", ""),
                        "action": getattr(entry, "action", ""),
                        "details": getattr(entry, "details", {}),
                    })

            await write_scheduling_result({
                "task_id": task_id,
                "output": output,
                "input_data": workflow_input.input_data,
                "appointment_result": state.get("appointment_result", {}),
                "parsed_intent": state.get("parsed_intent", {}),
                "waitlist_result": state.get("waitlist_result"),
                "audit_trail": audit_trail,
            })
        except Exception as exc:
            logger.error("Inline scheduling result persistence failed: %s", exc)
            return WorkflowResult(
                task_id=task_id,
                agent_type=agent_type,
                status=WorkflowStatus.FAILED.value,
                error=f"Persistence failed: {exc}",
                output_data=output,
                confidence=confidence,
                needs_review=needs_review,
                review_reason=review_reason,
            )

        return WorkflowResult(
            task_id=task_id,
            agent_type=agent_type,
            status=WorkflowStatus.COMPLETED.value,
            output_data=output,
            confidence=confidence,
            needs_review=needs_review,
            review_reason=review_reason,
        )
    except Exception as exc:
        return WorkflowResult(
            task_id=task_id,
            agent_type=agent_type,
            status=WorkflowStatus.FAILED.value,
            error=str(exc),
        )
