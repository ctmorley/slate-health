"""Compliance & Reporting agent Temporal workflow.

Orchestrates the compliance evaluation lifecycle:
  validate input → run compliance agent → persist compliance report →
  write result to DB

The compliance workflow runs as a batch processing job for a reporting
period, evaluating all applicable measures at once.
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


# ── Compliance-specific Activities ────────────────────────────────


@activity.defn
async def validate_compliance_input(args: dict[str, Any]) -> dict[str, Any]:
    """Validate compliance-specific input.

    Ensures the request has organization_id, measure_set, and
    reporting period.
    """
    restore_correlation_id(args)
    input_data = args.get("input_data", {})
    task_id = args.get("task_id", "")

    errors = []
    if not input_data.get("organization_id"):
        errors.append("organization_id is required")
    if not input_data.get("measure_set"):
        errors.append("measure_set is required")
    if not input_data.get("reporting_period_start"):
        errors.append("reporting_period_start is required")
    if not input_data.get("reporting_period_end"):
        errors.append("reporting_period_end is required")

    if errors:
        return asdict(ActivityResult(
            success=False,
            error=f"Validation errors: {'; '.join(errors)}",
        ))

    return asdict(ActivityResult(
        success=True,
        data={
            "task_id": task_id,
            "agent_type": "compliance",
            "input_data": input_data,
        },
    ))


@activity.defn
async def run_compliance_agent_activity(args: dict[str, Any]) -> dict[str, Any]:
    """Execute the compliance agent graph.

    Runs the full LangGraph compliance workflow: identify measures →
    pull clinical data → evaluate measures → identify gaps → generate report.
    """
    restore_correlation_id(args)
    input_data = args.get("input_data", {})
    task_id = args.get("task_id", "")

    safe_heartbeat("running_compliance_agent")

    from app.agents.compliance.graph import run_compliance_agent
    from app.core.engine.llm_provider import LLMProvider, MockLLMBackend

    llm_provider = LLMProvider(primary=MockLLMBackend())

    try:
        state = await run_compliance_agent(
            input_data=input_data,
            llm_provider=llm_provider,
            task_id=task_id,
        )

        output = state.get("decision", {})
        output["confidence"] = state.get("confidence", 0.0)
        output["needs_review"] = state.get("needs_review", False)
        output["review_reason"] = state.get("review_reason", "")

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
                "agent_type": "compliance",
                "output": output,
                "confidence": state.get("confidence", 0.0),
                "needs_review": state.get("needs_review", False),
                "review_reason": state.get("review_reason", ""),
                "compliance_report": state.get("compliance_report", {}),
                "measure_results": state.get("measure_results", []),
                "gap_analysis": state.get("gap_analysis", {}),
                "audit_trail": audit_trail,
            },
        ))
    except Exception as exc:
        return asdict(ActivityResult(
            success=False,
            error=f"Compliance agent execution failed: {exc}",
        ))


@activity.defn
async def write_compliance_result(args: dict[str, Any]) -> dict[str, Any]:
    """Persist the compliance result to the database.

    Creates/updates:
    - AgentTask with output data and status
    - ComplianceReport record with measure scores and gaps
    - Audit log entries from the agent trail
    """
    restore_correlation_id(args)
    task_id = args.get("task_id", "")
    output = args.get("output", {})
    input_data = args.get("input_data", {})
    compliance_report = args.get("compliance_report", {})
    gap_analysis = args.get("gap_analysis", {})
    audit_trail = args.get("audit_trail", [])

    safe_heartbeat("writing_compliance_result")

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
                task.status = "completed" if not output.get("needs_review") else "review"
                task.confidence_score = output.get("confidence", 0.0)

            # 2. Upsert ComplianceReport
            from app.models.compliance import ComplianceReport

            existing_result = await session.execute(
                select(ComplianceReport).where(
                    ComplianceReport.task_id == task_id
                )
            )
            comp_report = existing_result.scalar_one_or_none()

            org_id_str = input_data.get("organization_id", "")
            if not org_id_str and task is not None:
                org_id_str = str(task.organization_id) if task.organization_id else ""

            # Validate and convert organization_id to UUID
            import uuid as _uuid
            try:
                org_uuid = _uuid.UUID(org_id_str) if org_id_str else None
            except (ValueError, AttributeError):
                logger.warning(
                    "Invalid organization_id '%s' for task %s, falling back to task org_id",
                    org_id_str, task_id,
                )
                org_uuid = task.organization_id if task is not None else None

            if org_uuid is None:
                # Refuse to write synthetic data — fail with a clear error
                error_msg = (
                    f"Cannot persist compliance report for task {task_id}: "
                    f"no valid organization_id provided (got '{org_id_str}'). "
                    f"Supply a valid UUID organization_id in the task input."
                )
                logger.error(error_msg)
                raise ValueError(error_msg)

            # Determine report status based on agent output
            _report_status = "completed"
            if output.get("needs_review"):
                _report_status = "review"
            elif not compliance_report and not output.get("confidence", 0.0):
                # Empty report with zero confidence indicates a failure
                _report_status = "failed"

            if comp_report is None:
                comp_report = ComplianceReport(
                    task_id=task_id,
                    organization_id=org_uuid,
                    measure_set=input_data.get("measure_set", "HEDIS"),
                    reporting_period_start=input_data.get("reporting_period_start", ""),
                    reporting_period_end=input_data.get("reporting_period_end", ""),
                    status=_report_status,
                    measure_scores=compliance_report.get("measure_scores", {}),
                    overall_score=compliance_report.get("overall_score"),
                    gaps_identified=gap_analysis.get("total_gaps", 0),
                    gap_details=gap_analysis.get("gap_details", []),
                    recommendations=compliance_report.get("recommendations", []),
                    report_data=compliance_report,
                )
                session.add(comp_report)
            else:
                comp_report.status = _report_status
                comp_report.measure_scores = compliance_report.get("measure_scores", {})
                comp_report.overall_score = compliance_report.get("overall_score")
                comp_report.gaps_identified = gap_analysis.get("total_gaps", 0)
                comp_report.gap_details = gap_analysis.get("gap_details", [])
                comp_report.recommendations = compliance_report.get("recommendations", [])
                comp_report.report_data = compliance_report

            # 3. Write audit log entries
            if audit_trail:
                from app.core.audit.logger import AuditLogger
                audit_logger = AuditLogger(session)
                for entry in audit_trail:
                    try:
                        await audit_logger.log(
                            action=f"agent:compliance:{entry.get('action', 'unknown')}",
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
        logger.error("Failed to persist compliance result to DB: %s", exc)
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
class ComplianceWorkflow:
    """Temporal workflow for compliance agent execution.

    Orchestrates: validate → run agent → persist compliance report → write result.
    """

    @workflow.run
    async def run(self, workflow_input: WorkflowInput) -> WorkflowResult:
        task_id = workflow_input.task_id
        agent_type = workflow_input.agent_type
        correlation_id = workflow_input.correlation_id or ""

        # Step 1: Validate input
        validation = await workflow.execute_activity(
            validate_compliance_input,
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

        # Step 2: Run compliance agent
        agent_result = await workflow.execute_activity(
            run_compliance_agent_activity,
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

        # Step 3: Write result
        await workflow.execute_activity(
            write_compliance_result,
            {
                "task_id": task_id,
                "output": output,
                "input_data": workflow_input.input_data,
                "compliance_report": agent_data.get("compliance_report", {}),
                "gap_analysis": agent_data.get("gap_analysis", {}),
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


async def run_compliance_workflow(workflow_input: WorkflowInput) -> WorkflowResult:
    """Run the compliance workflow inline (no Temporal)."""
    task_id = workflow_input.task_id
    agent_type = workflow_input.agent_type

    from app.agents.compliance.graph import run_compliance_agent
    from app.core.engine.llm_provider import LLMProvider, MockLLMBackend

    llm_provider = LLMProvider(primary=MockLLMBackend())

    try:
        state = await run_compliance_agent(
            input_data=workflow_input.input_data,
            llm_provider=llm_provider,
            task_id=task_id,
        )

        output = state.get("decision", {})
        confidence = state.get("confidence", 0.0)
        needs_review = state.get("needs_review", False)
        review_reason = state.get("review_reason", "")

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

            await write_compliance_result({
                "task_id": task_id,
                "output": output,
                "input_data": workflow_input.input_data,
                "compliance_report": state.get("compliance_report", {}),
                "gap_analysis": state.get("gap_analysis", {}),
                "audit_trail": audit_trail,
            })
        except Exception as exc:
            logger.error("Inline compliance result persistence failed: %s", exc)
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
