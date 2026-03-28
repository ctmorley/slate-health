"""Credentialing agent Temporal workflow.

Orchestrates the long-running credentialing lifecycle:
  validate input → run credentialing agent → persist credentialing application →
  periodic status check-ins (weekly for up to ~98 days) → expiration alerts →
  final result

Credentialing is a long-running process (90+ days), with scheduled
check-in activities for status updates and expiration monitoring.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import timedelta
from typing import Any

from temporalio import activity, workflow

with workflow.unsafe.imports_passed_through():
    from app.workflows.base import (
        AGENT_ACTIVITY_TIMEOUT,
        AGENT_RETRY_POLICY,
        DB_ACTIVITY_TIMEOUT,
        DB_HEARTBEAT_TIMEOUT,
        DB_RETRY_POLICY,
        LONG_RUNNING_WORKFLOW_TIMEOUT,
        ActivityResult,
        WorkflowInput,
        WorkflowResult,
        WorkflowStatus,
        safe_heartbeat,
        set_workflow_correlation_id,
        restore_correlation_id,
    )

logger = logging.getLogger(__name__)

# Credentialing-specific timeouts
CREDENTIALING_CHECK_IN_INTERVAL = timedelta(days=7)
CREDENTIALING_MAX_CHECK_INS = 14  # ~98 days of weekly checks

# Terminal statuses — stop polling once reached
_TERMINAL_STATUSES = frozenset({"approved", "denied", "revoked", "cancelled"})


# ── Credentialing-specific Activities ─────────────────────────────


@activity.defn
async def validate_credentialing_input(args: dict[str, Any]) -> dict[str, Any]:
    """Validate credentialing-specific input.

    Ensures the request has a valid provider NPI.
    """
    restore_correlation_id(args)
    input_data = args.get("input_data", {})
    task_id = args.get("task_id", "")

    provider_npi = input_data.get("provider_npi", "")
    if not provider_npi or len(provider_npi) != 10 or not provider_npi.isdigit():
        return asdict(ActivityResult(
            success=False,
            error=f"Invalid provider_npi: '{provider_npi}'. Must be exactly 10 digits.",
        ))

    return asdict(ActivityResult(
        success=True,
        data={
            "task_id": task_id,
            "agent_type": "credentialing",
            "input_data": input_data,
        },
    ))


@activity.defn
async def run_credentialing_agent_activity(args: dict[str, Any]) -> dict[str, Any]:
    """Execute the credentialing agent graph.

    Runs the full LangGraph credentialing workflow: NPPES lookup → license
    verification → sanctions check → application compilation → submission.
    """
    restore_correlation_id(args)
    input_data = args.get("input_data", {})
    task_id = args.get("task_id", "")

    safe_heartbeat("running_credentialing_agent")

    from app.agents.credentialing.graph import run_credentialing_agent
    from app.core.engine.llm_provider import LLMProvider, MockLLMBackend

    llm_provider = LLMProvider(primary=MockLLMBackend())

    try:
        state = await run_credentialing_agent(
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
                "agent_type": "credentialing",
                "output": output,
                "confidence": state.get("confidence", 0.0),
                "needs_review": state.get("needs_review", False),
                "review_reason": state.get("review_reason", ""),
                "provider_details": state.get("provider_details", {}),
                "documents_checklist": state.get("documents_checklist", {}),
                "verification_results": state.get("verification_results", {}),
                "application": state.get("application", {}),
                "submission_result": state.get("submission_result", {}),
                "application_status": state.get("application_status", {}),
                "expiration_alerts": state.get("expiration_alerts", []),
                "audit_trail": audit_trail,
            },
        ))
    except Exception as exc:
        return asdict(ActivityResult(
            success=False,
            error=f"Credentialing agent execution failed: {exc}",
        ))


@activity.defn
async def write_credentialing_result(args: dict[str, Any]) -> dict[str, Any]:
    """Persist the credentialing result to the database.

    Creates/updates:
    - AgentTask with output data and status
    - CredentialingApplication record with provider details
    - Audit log entries from the agent trail
    """
    restore_correlation_id(args)
    task_id = args.get("task_id", "")
    output = args.get("output", {})
    input_data = args.get("input_data", {})
    provider_details = args.get("provider_details", {})
    documents_checklist = args.get("documents_checklist", {})
    verification_results = args.get("verification_results", {})
    application = args.get("application", {})
    submission_result = args.get("submission_result", {})
    application_status = args.get("application_status", {})
    audit_trail = args.get("audit_trail", [])

    safe_heartbeat("writing_credentialing_result")

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
                task.confidence_score = output.get("confidence", 0.0)

                # Determine task status based on application lifecycle:
                # - If HITL review is needed, set "review"
                # - If the application is in a non-terminal state
                #   (submitted / under_review), keep task "running" — it
                #   will be marked "completed" after the long-running
                #   polling loop resolves to a terminal status.
                # - Otherwise mark "completed".
                if output.get("needs_review"):
                    task.status = "review"
                elif application_status.get("status") in (
                    "submitted", "under_review",
                ):
                    task.status = "running"
                else:
                    task.status = "completed"

            # 2. Upsert CredentialingApplication
            from app.models.credentialing import CredentialingApplication
            from datetime import date

            existing_result = await session.execute(
                select(CredentialingApplication).where(
                    CredentialingApplication.task_id == task_id
                )
            )
            cred_app = existing_result.scalar_one_or_none()

            npi = provider_details.get("npi", input_data.get("provider_npi", ""))
            provider_name = (
                f"{provider_details.get('first_name', '')} "
                f"{provider_details.get('last_name', '')}"
            ).strip() or "Unknown Provider"

            app_status = application_status.get("status", "pending_documents")

            if cred_app is None:
                cred_app = CredentialingApplication(
                    task_id=task_id,
                    provider_npi=npi,
                    provider_name=provider_name,
                    target_organization=input_data.get("target_organization"),
                    status=app_status,
                    documents_checklist=documents_checklist,
                    missing_documents={"missing": documents_checklist.get("missing", [])},
                    licenses=verification_results.get("licenses", []),
                    sanctions_check=verification_results.get("sanctions_details", {}),
                    application_data=application,
                )
                if submission_result.get("success"):
                    cred_app.submitted_date = date.today()
                session.add(cred_app)
            else:
                cred_app.status = app_status
                cred_app.documents_checklist = documents_checklist
                cred_app.missing_documents = {"missing": documents_checklist.get("missing", [])}
                cred_app.licenses = verification_results.get("licenses", [])
                cred_app.sanctions_check = verification_results.get("sanctions_details", {})
                cred_app.application_data = application

            # 3. Write audit log entries
            if audit_trail:
                from app.core.audit.logger import AuditLogger
                audit_logger = AuditLogger(session)
                for entry in audit_trail:
                    try:
                        await audit_logger.log(
                            action=f"agent:credentialing:{entry.get('action', 'unknown')}",
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
        logger.error("Failed to persist credentialing result to DB: %s", exc)
        raise
    finally:
        if engine_to_dispose is not None:
            await engine_to_dispose.dispose()

    return asdict(ActivityResult(
        success=True,
        data={"task_id": task_id, "output": output},
    ))


@activity.defn
async def check_credentialing_status_activity(args: dict[str, Any]) -> dict[str, Any]:
    """Periodic status check for a credentialing application.

    Queries the credentialing application in the DB and simulates
    status progression: submitted → under_review → approved/denied.
    In production this would poll the payer portal or clearinghouse.
    """
    restore_correlation_id(args)
    task_id = args.get("task_id", "")
    check_in_number = args.get("check_in_number", 0)

    safe_heartbeat(f"checking_credentialing_status_{check_in_number}")

    from app.workflows.eligibility import _get_activity_session_factory
    from sqlalchemy import select

    factory, engine_to_dispose = _get_activity_session_factory()
    try:
        async with factory() as session:
            from app.models.credentialing import CredentialingApplication
            from app.models.agent_task import AgentTask
            from datetime import date

            result = await session.execute(
                select(CredentialingApplication).where(
                    CredentialingApplication.task_id == task_id
                )
            )
            cred_app = result.scalar_one_or_none()

            if cred_app is None:
                return asdict(ActivityResult(
                    success=True,
                    data={
                        "task_id": task_id,
                        "status": "not_found",
                        "terminal": True,
                    },
                ))

            current_status = cred_app.status

            # Simulate status progression based on check-in number.
            # In production, this polls the actual payer/org portal.
            #
            # Lifecycle: submitted → under_review → approved OR denied
            # Decision logic uses the application data to determine
            # whether the outcome is approval or denial.
            new_status = current_status
            if current_status == "submitted" and check_in_number >= 1:
                new_status = "under_review"
            elif current_status == "under_review" and check_in_number >= 8:
                # After ~56 days of review, decision is typically made.
                # Determine outcome: deny if sanctions or missing docs,
                # otherwise approve.
                app_data = cred_app.application_data or {}
                missing_docs = cred_app.missing_documents or {}
                has_missing = bool(missing_docs.get("missing"))
                sanctions_clear = app_data.get("sanctions_clear", True)

                if not sanctions_clear or has_missing:
                    new_status = "denied"
                else:
                    new_status = "approved"

            if new_status != current_status:
                cred_app.status = new_status
                if new_status == "approved":
                    cred_app.approved_date = date.today()
                    # Set expiration 2 years from approval
                    cred_app.expiration_date = date(
                        date.today().year + 2, date.today().month, date.today().day
                    )
                elif new_status == "denied":
                    # Record denial details
                    cred_app.application_data = {
                        **(cred_app.application_data or {}),
                        "denial_date": date.today().isoformat(),
                        "denial_reason": "Application requirements not met",
                    }

                # Also update AgentTask if terminal
                if new_status in _TERMINAL_STATUSES:
                    task_result = await session.execute(
                        select(AgentTask).where(AgentTask.id == task_id)
                    )
                    task = task_result.scalar_one_or_none()
                    if task is not None:
                        task.status = "completed"
                        if task.output_data and isinstance(task.output_data, dict):
                            task.output_data["application_status"] = {
                                "status": new_status,
                            }

                # Audit the status change
                from app.core.audit.logger import AuditLogger
                audit_logger = AuditLogger(session)
                try:
                    await audit_logger.log(
                        action=f"agent:credentialing:status_change",
                        actor_type="agent",
                        resource_type="agent_task",
                        resource_id=str(task_id),
                        details={
                            "previous_status": current_status,
                            "new_status": new_status,
                            "check_in_number": check_in_number,
                        },
                    )
                except Exception as exc:
                    logger.warning("Failed to write status change audit: %s", exc)

                await session.commit()

            is_terminal = new_status in _TERMINAL_STATUSES

            return asdict(ActivityResult(
                success=True,
                data={
                    "task_id": task_id,
                    "previous_status": current_status,
                    "current_status": new_status,
                    "check_in_number": check_in_number,
                    "terminal": is_terminal,
                },
            ))
    except Exception as exc:
        logger.error("Failed to check credentialing status: %s", exc)
        return asdict(ActivityResult(
            success=False,
            error=f"Status check failed: {exc}",
        ))
    finally:
        if engine_to_dispose is not None:
            await engine_to_dispose.dispose()


@activity.defn
async def alert_expiration_activity(args: dict[str, Any]) -> dict[str, Any]:
    """Check for and alert on upcoming credential expirations.

    Queries the credentialing application for expiration dates and
    generates warnings for credentials expiring within 90 days.
    """
    restore_correlation_id(args)
    task_id = args.get("task_id", "")

    safe_heartbeat("checking_expirations")

    from app.workflows.eligibility import _get_activity_session_factory
    from sqlalchemy import select

    factory, engine_to_dispose = _get_activity_session_factory()
    try:
        async with factory() as session:
            from app.models.credentialing import CredentialingApplication
            from datetime import date

            result = await session.execute(
                select(CredentialingApplication).where(
                    CredentialingApplication.task_id == task_id
                )
            )
            cred_app = result.scalar_one_or_none()

            alerts = []
            if cred_app is not None and cred_app.expiration_date:
                days_until = (cred_app.expiration_date - date.today()).days
                if days_until <= 90:
                    alerts.append({
                        "type": "credentialing_expiration",
                        "provider_npi": cred_app.provider_npi,
                        "expiration_date": cred_app.expiration_date.isoformat(),
                        "days_until_expiry": days_until,
                        "severity": "critical" if days_until <= 30 else "warning",
                        "action_required": "Initiate credentialing renewal",
                    })

                # Audit the alert
                if alerts:
                    from app.core.audit.logger import AuditLogger
                    audit_logger = AuditLogger(session)
                    try:
                        await audit_logger.log(
                            action="agent:credentialing:expiration_alert",
                            actor_type="agent",
                            resource_type="agent_task",
                            resource_id=str(task_id),
                            details={
                                "alerts_count": len(alerts),
                                "days_until_expiry": days_until,
                            },
                        )
                    except Exception as exc:
                        logger.warning("Failed to write expiration audit: %s", exc)
                    await session.commit()

            return asdict(ActivityResult(
                success=True,
                data={
                    "task_id": task_id,
                    "alerts": alerts,
                    "alerts_count": len(alerts),
                },
            ))
    except Exception as exc:
        logger.error("Failed to check expirations: %s", exc)
        return asdict(ActivityResult(
            success=False,
            error=f"Expiration check failed: {exc}",
        ))
    finally:
        if engine_to_dispose is not None:
            await engine_to_dispose.dispose()


# ── Workflow Definition ──────────────────────────────────────────────


@workflow.defn
class CredentialingWorkflow:
    """Temporal workflow for credentialing agent execution.

    Orchestrates the full lifecycle:
      1. Validate input
      2. Run credentialing agent (NPPES → licenses → sanctions → compile → submit)
      3. Persist initial result
      4. Periodic check-in loop (weekly for up to ~98 days):
         - Check application status
         - Stop if terminal (approved/denied)
         - Sleep 7 days between checks
      5. Final expiration alert check

    The workflow is long-running (up to 90+ days) with durable state
    persistence across check-in intervals.
    """

    @workflow.run
    async def run(self, workflow_input: WorkflowInput) -> WorkflowResult:
        task_id = workflow_input.task_id
        agent_type = workflow_input.agent_type
        correlation_id = workflow_input.correlation_id or ""

        # Step 1: Validate input
        validation = await workflow.execute_activity(
            validate_credentialing_input,
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

        # Step 2: Run credentialing agent
        agent_result = await workflow.execute_activity(
            run_credentialing_agent_activity,
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

        # Step 3: Write initial result
        await workflow.execute_activity(
            write_credentialing_result,
            {
                "task_id": task_id,
                "output": output,
                "input_data": workflow_input.input_data,
                "provider_details": agent_data.get("provider_details", {}),
                "documents_checklist": agent_data.get("documents_checklist", {}),
                "verification_results": agent_data.get("verification_results", {}),
                "application": agent_data.get("application", {}),
                "submission_result": agent_data.get("submission_result", {}),
                "application_status": agent_data.get("application_status", {}),
                "audit_trail": agent_data.get("audit_trail", []),
                "correlation_id": correlation_id,
            },
            start_to_close_timeout=DB_ACTIVITY_TIMEOUT,
            heartbeat_timeout=DB_HEARTBEAT_TIMEOUT,
            retry_policy=DB_RETRY_POLICY.to_retry_policy(),
        )

        # Step 4: Periodic check-in loop for long-running credentialing
        # Only enter the polling loop if the application was actually submitted
        app_status = agent_data.get("application_status", {})
        if app_status.get("status") in ("submitted", "under_review"):
            final_status = app_status.get("status", "submitted")

            for check_in in range(CREDENTIALING_MAX_CHECK_INS):
                # Sleep for the check-in interval (7 days)
                await workflow.sleep(CREDENTIALING_CHECK_IN_INTERVAL)

                # Check status
                status_result = await workflow.execute_activity(
                    check_credentialing_status_activity,
                    {
                        "task_id": task_id,
                        "check_in_number": check_in + 1,
                        "correlation_id": correlation_id,
                    },
                    start_to_close_timeout=DB_ACTIVITY_TIMEOUT,
                    heartbeat_timeout=DB_HEARTBEAT_TIMEOUT,
                    retry_policy=DB_RETRY_POLICY.to_retry_policy(),
                )

                if status_result.get("success"):
                    status_data = status_result.get("data", {})
                    final_status = status_data.get("current_status", final_status)

                    if status_data.get("terminal", False):
                        break

            # Update output with final status from polling
            output["application_status"] = {"status": final_status}

        # Step 5: Check for expiration alerts
        await workflow.execute_activity(
            alert_expiration_activity,
            {"task_id": task_id, "correlation_id": correlation_id},
            start_to_close_timeout=DB_ACTIVITY_TIMEOUT,
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


async def _run_inline_lifecycle_polling(task_id: str, state: dict) -> dict:
    """Simulate credentialing lifecycle polling in inline mode.

    Performs deterministic status progression:
      submitted → under_review → approved/denied
    This mirrors the Temporal check_credentialing_status_activity logic
    so that inline fallback mode still exercises the full lifecycle.
    """
    from app.workflows.eligibility import _get_activity_session_factory
    from sqlalchemy import select

    factory, engine_to_dispose = _get_activity_session_factory()
    final_status = "submitted"
    try:
        async with factory() as session:
            from app.models.credentialing import CredentialingApplication
            from app.models.agent_task import AgentTask
            from datetime import date

            result = await session.execute(
                select(CredentialingApplication).where(
                    CredentialingApplication.task_id == task_id
                )
            )
            cred_app = result.scalar_one_or_none()

            if cred_app is not None and cred_app.status not in _TERMINAL_STATUSES:
                # Step 1: submitted → under_review
                if cred_app.status == "submitted":
                    cred_app.status = "under_review"
                    await session.flush()

                    from app.core.audit.logger import AuditLogger
                    audit_logger = AuditLogger(session)
                    try:
                        await audit_logger.log(
                            action="agent:credentialing:status_change",
                            actor_type="agent",
                            resource_type="agent_task",
                            resource_id=str(task_id),
                            details={
                                "previous_status": "submitted",
                                "new_status": "under_review",
                                "mode": "inline_lifecycle",
                            },
                        )
                    except Exception as exc:
                        logger.warning("Failed to write inline status audit: %s", exc)

                # Step 2: under_review → approved/denied
                app_data = cred_app.application_data or {}
                missing_docs = cred_app.missing_documents or {}
                has_missing = bool(missing_docs.get("missing"))
                sanctions_clear = app_data.get("sanctions_clear", True)

                if not sanctions_clear or has_missing:
                    new_status = "denied"
                    cred_app.application_data = {
                        **(cred_app.application_data or {}),
                        "denial_date": date.today().isoformat(),
                        "denial_reason": "Application requirements not met",
                    }
                else:
                    new_status = "approved"
                    cred_app.approved_date = date.today()
                    cred_app.expiration_date = date(
                        date.today().year + 2, date.today().month, date.today().day
                    )

                prev_status = cred_app.status
                cred_app.status = new_status
                final_status = new_status

                # Update AgentTask for terminal status
                if new_status in _TERMINAL_STATUSES:
                    task_result = await session.execute(
                        select(AgentTask).where(AgentTask.id == task_id)
                    )
                    task = task_result.scalar_one_or_none()
                    if task is not None:
                        task.status = "completed"
                        if task.output_data and isinstance(task.output_data, dict):
                            task.output_data["application_status"] = {
                                "status": new_status,
                            }

                from app.core.audit.logger import AuditLogger
                audit_logger = AuditLogger(session)
                try:
                    await audit_logger.log(
                        action="agent:credentialing:status_change",
                        actor_type="agent",
                        resource_type="agent_task",
                        resource_id=str(task_id),
                        details={
                            "previous_status": prev_status,
                            "new_status": new_status,
                            "mode": "inline_lifecycle",
                        },
                    )
                except Exception as exc:
                    logger.warning("Failed to write inline status audit: %s", exc)

                await session.commit()
            elif cred_app is not None:
                final_status = cred_app.status
    except Exception as exc:
        logger.warning("Inline lifecycle polling failed: %s (non-fatal)", exc)
    finally:
        if engine_to_dispose is not None:
            await engine_to_dispose.dispose()

    return {"final_status": final_status}


async def run_credentialing_workflow(workflow_input: WorkflowInput) -> WorkflowResult:
    """Run the credentialing workflow inline (no Temporal).

    Used as a fallback for testing or when Temporal is not available.
    Includes deterministic lifecycle progression (submitted → under_review →
    approved/denied) to match the Temporal workflow's periodic check-in
    behaviour.
    """
    task_id = workflow_input.task_id
    agent_type = workflow_input.agent_type

    from app.agents.credentialing.graph import run_credentialing_agent
    from app.core.engine.llm_provider import LLMProvider, MockLLMBackend

    llm_provider = LLMProvider(primary=MockLLMBackend())

    try:
        state = await run_credentialing_agent(
            input_data=workflow_input.input_data,
            llm_provider=llm_provider,
            task_id=task_id,
        )

        output = state.get("decision", {})
        confidence = state.get("confidence", 0.0)
        needs_review = state.get("needs_review", False)
        review_reason = state.get("review_reason", "")

        # Persist inline
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

            await write_credentialing_result({
                "task_id": task_id,
                "output": output,
                "input_data": workflow_input.input_data,
                "provider_details": state.get("provider_details", {}),
                "documents_checklist": state.get("documents_checklist", {}),
                "verification_results": state.get("verification_results", {}),
                "application": state.get("application", {}),
                "submission_result": state.get("submission_result", {}),
                "application_status": state.get("application_status", {}),
                "audit_trail": audit_trail,
            })
        except Exception as exc:
            logger.error("Inline credentialing result persistence failed: %s", exc)
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

        # Run inline lifecycle polling to progress through
        # submitted → under_review → approved/denied
        lifecycle_result = await _run_inline_lifecycle_polling(task_id, state)
        if lifecycle_result.get("final_status"):
            output["application_status"] = {
                "status": lifecycle_result["final_status"],
                "mode": "inline_lifecycle",
            }

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
