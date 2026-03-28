"""Claims & Billing agent Temporal workflow.

Orchestrates the full claims lifecycle with long-running denial management:
  validate input → run claims agent → submit clearinghouse → track status →
  parse remittance → persist claims/denials → write result to DB

The claims workflow is long-running because claim adjudication can take
days to weeks, with status polling and denial management sub-flows.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import asdict
from datetime import timedelta
from decimal import Decimal
from typing import Any

from temporalio import activity, workflow

with workflow.unsafe.imports_passed_through():
    from app.workflows.base import (
        AGENT_ACTIVITY_TIMEOUT,
        AGENT_RETRY_POLICY,
        CLEARINGHOUSE_ACTIVITY_TIMEOUT,
        CLEARINGHOUSE_HEARTBEAT_TIMEOUT,
        CLEARINGHOUSE_RETRY_POLICY,
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


# ── Claims-specific Activities ─────────────────────────────────────


@activity.defn
async def validate_claims_input(args: dict[str, Any]) -> dict[str, Any]:
    """Validate claims-specific input.

    Ensures the request has required structural fields (subscriber_id).
    Missing diagnosis or procedure codes are flagged as warnings so the
    agent graph can route them to HITL review rather than hard-failing.
    This keeps behaviour consistent between the Temporal and inline
    execution paths.
    """
    restore_correlation_id(args)
    input_data = args.get("input_data", {})
    task_id = args.get("task_id", "")
    errors: list[str] = []
    warnings: list[str] = []

    # Hard requirement: must identify the subscriber
    if not input_data.get("subscriber_id"):
        errors.append("subscriber_id is required")

    # Missing codes are warnings — the agent graph will route them to HITL
    if not input_data.get("diagnosis_codes"):
        warnings.append("Missing diagnosis codes (ICD-10): will be flagged for review")
    if not input_data.get("procedure_codes") and not input_data.get("service_lines"):
        warnings.append("Missing procedure codes/service lines: will be flagged for review")

    if errors:
        return asdict(ActivityResult(
            success=False,
            error=f"Validation errors: {'; '.join(errors)}",
        ))

    return asdict(ActivityResult(
        success=True,
        data={
            "task_id": task_id,
            "agent_type": "claims",
            "input_data": input_data,
            "warnings": warnings,
        },
    ))


@activity.defn
async def run_claims_agent_activity(args: dict[str, Any]) -> dict[str, Any]:
    """Execute the claims agent graph.

    Runs the full LangGraph claims workflow: validate_codes → check_payer_rules →
    build_837 → submit → track_status → parse_835 → handle_denial.
    """
    restore_correlation_id(args)
    input_data = args.get("input_data", {})
    task_id = args.get("task_id", "")

    safe_heartbeat("running_claims_agent")

    from app.agents.claims.graph import run_claims_agent
    from app.core.engine.llm_provider import LLMProvider, MockLLMBackend

    llm_provider = LLMProvider(primary=MockLLMBackend())

    try:
        state = await run_claims_agent(
            input_data=input_data,
            llm_provider=llm_provider,
            task_id=task_id,
        )

        output = state.get("decision", {})
        output["confidence"] = state.get("confidence", 0.0)
        output["needs_review"] = state.get("needs_review", False)
        output["review_reason"] = state.get("review_reason", "")

        # Carry forward x12 data and denial info for downstream activities
        x12_data = state.get("x12_837_data", {})
        denial_analyses = state.get("denial_analyses", [])
        remittance_data = state.get("remittance_data", {})
        payment_info = state.get("payment_info", {})
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
                "agent_type": "claims",
                "output": output,
                "confidence": state.get("confidence", 0.0),
                "needs_review": state.get("needs_review", False),
                "review_reason": state.get("review_reason", ""),
                "x12_data": x12_data,
                "denial_analyses": denial_analyses,
                "remittance_data": remittance_data,
                "payment_info": payment_info,
                "audit_trail": audit_trail,
            },
        ))
    except Exception as exc:
        return asdict(ActivityResult(
            success=False,
            error=f"Claims agent execution failed: {exc}",
        ))


@activity.defn
async def submit_claim_to_clearinghouse(args: dict[str, Any]) -> dict[str, Any]:
    """Submit the 837P/837I claim to the clearinghouse.

    Uses the clearinghouse factory to submit via the configured provider
    (Availity, ClaimMD, or mock).  The ``clearinghouse_config`` key in
    *args* selects the real provider; when absent, the activity raises
    so that misconfiguration is surfaced rather than silently degraded.
    """
    restore_correlation_id(args)
    task_id = args.get("task_id", "")
    x12_data = args.get("x12_data", "")
    payer_id = args.get("payer_id", "")
    claim_type = args.get("claim_type", "837P")
    clearinghouse_config = args.get("clearinghouse_config")

    safe_heartbeat("submitting_claim")

    from app.core.clearinghouse.base import TransactionRequest, TransactionType
    from app.core.clearinghouse.factory import get_clearinghouse

    if not clearinghouse_config:
        # Fall back to mock only in non-production (test) environments.
        # Log a warning so that the missing config is visible.
        logger.warning(
            "No clearinghouse_config for claims task %s — using mock clearinghouse",
            task_id,
        )
        clearinghouse_config = {
            "clearinghouse_name": "mock",
            "api_endpoint": "http://mock-clearinghouse",
            "credentials": {},
        }

    try:
        clearinghouse = get_clearinghouse(
            clearinghouse_name=clearinghouse_config["clearinghouse_name"],
            api_endpoint=clearinghouse_config["api_endpoint"],
            credentials=clearinghouse_config.get("credentials"),
        )

        transaction_type = (
            TransactionType.CLAIM_837I
            if claim_type == "837I"
            else TransactionType.CLAIM_837P
        )

        request = TransactionRequest(
            transaction_type=transaction_type,
            sender_id="SENDER01",
            receiver_id=payer_id or "RECEIVER01",
            payload=x12_data if isinstance(x12_data, str) else x12_data.get("x12_837", ""),
        )
        response = await clearinghouse.submit_transaction(request)

        return asdict(ActivityResult(
            success=True,
            data={
                "task_id": task_id,
                "transaction_id": response.transaction_id if hasattr(response, "transaction_id") else "",
                "status": response.status.value if hasattr(response, "status") else "submitted",
            },
        ))
    except Exception as exc:
        return asdict(ActivityResult(
            success=False,
            error=f"Clearinghouse submission failed: {exc}",
        ))


@activity.defn
async def parse_remittance_activity(args: dict[str, Any]) -> dict[str, Any]:
    """Parse an 835 remittance response and extract payment/denial info.

    In the real system, the raw 835 comes from the clearinghouse response.
    For the workflow, this processes whatever remittance data is available
    (from the agent graph or a mock response).
    """
    restore_correlation_id(args)
    task_id = args.get("task_id", "")
    remittance_data = args.get("remittance_data", {})
    raw_835 = args.get("raw_835", "")

    safe_heartbeat("parsing_remittance")

    # If we have raw 835 data, parse it
    if raw_835:
        from app.core.ingestion.x12_client import parse_835
        try:
            remittance_data = parse_835(raw_835)
        except Exception as exc:
            return asdict(ActivityResult(
                success=False,
                error=f"Failed to parse 835 remittance: {exc}",
            ))

    # If no remittance data at all, return awaiting_835 status — do NOT
    # fabricate payment data.  The claim stays in "submitted" / "awaiting_835"
    # until a real 835 arrives.
    if not remittance_data:
        return asdict(ActivityResult(
            success=True,
            data={
                "task_id": task_id,
                "remittance_data": {},
                "payment_info": {},
                "denials": [],
                "has_denials": False,
                "awaiting_835": True,
            },
        ))

    # Extract payment info
    payment = remittance_data.get("payment", {})
    claims = remittance_data.get("claims", [])

    # Aggregate patient responsibility and adjustments across all claims
    total_patient_responsibility = Decimal("0")
    total_adjustments = Decimal("0")
    for claim in claims:
        try:
            total_patient_responsibility += Decimal(str(claim.get("patient_responsibility", "0")))
        except Exception:
            pass
        for adj in claim.get("adjustments", []):
            try:
                total_adjustments += Decimal(str(adj.get("amount", "0")))
            except Exception:
                pass

    payment_info = {
        "total_paid": payment.get("amount", "0.00"),
        "payment_method": payment.get("method", ""),
        "payment_date": payment.get("date", ""),
        "check_number": payment.get("check_number", ""),
        "claims_count": len(claims),
        "patient_responsibility": str(total_patient_responsibility),
        "total_adjustments": str(total_adjustments),
    }

    # Detect denials — normalize paid_amount to Decimal for robust comparison
    denials = []
    for claim in claims:
        is_denied_status = claim.get("status_code") == "4"
        try:
            is_zero_payment = Decimal(str(claim.get("paid_amount", "0"))) == Decimal("0")
        except Exception:
            is_zero_payment = False
        if is_denied_status or is_zero_payment:
            denials.append({
                "claim_id": claim.get("claim_id", ""),
                "status_code": claim.get("status_code", ""),
                "charge_amount": claim.get("charge_amount", "0.00"),
                "paid_amount": claim.get("paid_amount", "0.00"),
                "adjustments": claim.get("adjustments", []),
            })

    return asdict(ActivityResult(
        success=True,
        data={
            "task_id": task_id,
            "remittance_data": remittance_data,
            "payment_info": payment_info,
            "denials": denials,
            "has_denials": len(denials) > 0,
        },
    ))


@activity.defn
async def write_claims_result(args: dict[str, Any]) -> dict[str, Any]:
    """Persist the claims result to the database.

    Creates/updates:
    - AgentTask with output data and status
    - Claim record with submission/payment details
    - ClaimDenial records for any denials
    - Audit log entries for the workflow stages
    """
    restore_correlation_id(args)
    task_id = args.get("task_id", "")
    output = args.get("output", {})
    input_data = args.get("input_data", {})
    clearinghouse_result = args.get("clearinghouse_result", {})
    payment_info = args.get("payment_info", {})
    remittance_data = args.get("remittance_data", {})
    denials = args.get("denials", [])
    denial_analyses = args.get("denial_analyses", [])
    audit_trail = args.get("audit_trail", [])

    safe_heartbeat("writing_claims_result")

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

            # 2. Upsert Claim record (idempotent — keyed on task_id)
            from app.models.claims import Claim, ClaimDenial

            patient_id = None
            if task is not None:
                patient_id = task.patient_id

            existing_claim_result = await session.execute(
                select(Claim).where(Claim.task_id == task_id)
            )
            claim = existing_claim_result.scalar_one_or_none()

            awaiting_835 = args.get("awaiting_835", False)

            # Extract encounter_id from input_data if provided
            encounter_id_raw = input_data.get("encounter_id")
            encounter_id = None
            if encounter_id_raw:
                try:
                    encounter_id = uuid.UUID(str(encounter_id_raw)) if not isinstance(encounter_id_raw, uuid.UUID) else encounter_id_raw
                except (ValueError, AttributeError):
                    logger.warning("Invalid encounter_id '%s'; ignoring", encounter_id_raw)

            if claim is None:
                claim = Claim(
                    task_id=task_id,
                    patient_id=patient_id,
                    encounter_id=encounter_id,
                    claim_type=input_data.get("claim_type", "837P"),
                    status="submitted" if clearinghouse_result else "draft",
                    claim_number=output.get("claim_id", ""),
                    total_charge=Decimal(str(input_data.get("total_charge", "0.00"))),
                    diagnosis_codes=input_data.get("diagnosis_codes", []),
                    procedure_codes=input_data.get("procedure_codes", []),
                    submission_data=clearinghouse_result or {},
                    remittance_data={},
                )
                session.add(claim)
            else:
                # Update mutable fields on retry/replay
                if clearinghouse_result:
                    claim.submission_data = clearinghouse_result
                claim.claim_number = output.get("claim_id", "") or claim.claim_number

            # Populate payment fields only when real remittance is available
            if payment_info and not awaiting_835:
                try:
                    total_paid_decimal = Decimal(str(payment_info.get("total_paid", "0.00")))
                except Exception:
                    total_paid_decimal = Decimal("0")
                claim.total_paid = total_paid_decimal
                try:
                    claim.patient_responsibility = Decimal(str(payment_info.get("patient_responsibility", "0.00")))
                except Exception:
                    pass
                claim.remittance_data = remittance_data or payment_info or {}
                if total_paid_decimal > Decimal("0"):
                    claim.status = "paid"
            elif awaiting_835:
                claim.status = "awaiting_835" if clearinghouse_result else claim.status

            if denials:
                claim.status = "denied"

            await session.flush()

            # 3. Upsert ClaimDenial records (skip if already exist for this claim)
            existing_denials_result = await session.execute(
                select(ClaimDenial).where(ClaimDenial.claim_id == claim.id)
            )
            existing_denial_codes = {
                d.denial_code for d in existing_denials_result.scalars().all()
            }

            for i, denial in enumerate(denials):
                analysis = denial_analyses[i] if i < len(denial_analyses) else {}

                adjustments = denial.get("adjustments", [])
                denial_code = adjustments[0].get("reason_code", "unknown") if adjustments else "unknown"

                # Skip if this denial code already exists for this claim
                if denial_code in existing_denial_codes:
                    continue

                denial_reason = analysis.get("category_description", "") or denial.get("status_code", "")

                claim_denial = ClaimDenial(
                    claim_id=claim.id,
                    denial_code=denial_code,
                    denial_reason=denial_reason or f"Denial code: {denial_code}",
                    denial_category=analysis.get("category", "other"),
                    recommended_action=analysis.get("appeal_recommendation", {}).get("strategy", ""),
                    appeal_status="pending" if analysis.get("appeal_recommendation", {}).get("appealable") else "not_appealable",
                    appeal_details=analysis.get("appeal_recommendation", {}),
                )
                session.add(claim_denial)
                existing_denial_codes.add(denial_code)

            # 4. Write audit log entries from agent trail
            if audit_trail:
                from app.core.audit.logger import AuditLogger
                audit_logger = AuditLogger(session)
                for entry in audit_trail:
                    try:
                        await audit_logger.log(
                            action=f"agent:claims:{entry.get('action', 'unknown')}",
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

            # Write workflow-level audit entries
            from app.core.audit.logger import AuditLogger
            audit_logger = AuditLogger(session)
            await audit_logger.log(
                action="claims_workflow:clearinghouse_submitted",
                actor_type="agent",
                resource_type="agent_task",
                resource_id=str(task_id),
                details={
                    "transaction_id": clearinghouse_result.get("transaction_id", "") if clearinghouse_result else "",
                },
            )
            if payment_info:
                await audit_logger.log(
                    action="claims_workflow:remittance_processed",
                    actor_type="agent",
                    resource_type="agent_task",
                    resource_id=str(task_id),
                    details={"total_paid": payment_info.get("total_paid", "0.00")},
                )
            if denials:
                await audit_logger.log(
                    action="claims_workflow:denials_recorded",
                    actor_type="agent",
                    resource_type="agent_task",
                    resource_id=str(task_id),
                    details={"denial_count": len(denials)},
                )

            await session.commit()
    except Exception as exc:
        logger.error("Failed to persist claims result to DB: %s", exc)
        raise
    finally:
        if engine_to_dispose is not None:
            await engine_to_dispose.dispose()

    return asdict(ActivityResult(
        success=True,
        data={"task_id": task_id, "output": output},
    ))


@activity.defn
async def poll_claim_status(args: dict[str, Any]) -> dict[str, Any]:
    """Poll claim status via 276/277 transaction.

    Used in the long-running denial management loop to check if a
    previously denied claim has been resolved after appeal.
    """
    restore_correlation_id(args)
    task_id = args.get("task_id", "")
    claim_id = args.get("claim_id", "")
    input_data = args.get("input_data", {})

    safe_heartbeat("polling_claim_status")

    from app.agents.claims.tools import check_claim_status, parse_277_response

    # Build 276 status request
    status_result = await check_claim_status(
        subscriber_id=input_data.get("subscriber_id", ""),
        subscriber_last_name=input_data.get("subscriber_last_name", ""),
        subscriber_first_name=input_data.get("subscriber_first_name", ""),
        payer_id=input_data.get("payer_id", ""),
        payer_name=input_data.get("payer_name", ""),
        claim_id=claim_id,
        date_of_service=input_data.get("date_of_service", ""),
    )

    if not status_result.get("success"):
        return asdict(ActivityResult(
            success=False,
            error=f"Failed to build 276 status request: {status_result.get('error', '')}",
        ))

    # In production, submit 276 to clearinghouse and get 277.
    # For dev/testing, use deterministic staged state transitions based
    # on poll_attempt so the denial management loop can reach terminal
    # states and exercise the full lifecycle.
    poll_attempt = args.get("poll_attempt", 0)

    # Staged progression: pending → in_review → finalized (terminal)
    stages = [
        {
            "status": "pending",
            "status_category": "A1",
            "status_code": "20",
            "description": "Claim received and pending adjudication",
            "terminal": False,
        },
        {
            "status": "in_review",
            "status_category": "A2",
            "status_code": "24",
            "description": "Claim under review by payer",
            "terminal": False,
        },
        {
            "status": "finalized",
            "status_category": "A5",
            "status_code": "0",
            "description": "Claim adjudicated and finalized",
            "terminal": True,
        },
    ]

    stage = stages[min(poll_attempt, len(stages) - 1)]

    return asdict(ActivityResult(
        success=True,
        data={
            "task_id": task_id,
            "claim_id": claim_id,
            **stage,
        },
    ))


@activity.defn
async def update_claim_status_activity(args: dict[str, Any]) -> dict[str, Any]:
    """Persist a terminal claim status from denial polling back to the DB.

    Called after poll_claim_status returns a terminal result so the Claim
    record reflects the final adjudication outcome rather than the earlier
    in-flight status.
    """
    restore_correlation_id(args)
    task_id = args.get("task_id", "")
    final_status = args.get("final_status", "")
    status_description = args.get("status_description", "")

    safe_heartbeat("updating_claim_status")

    from app.workflows.eligibility import _get_activity_session_factory
    from sqlalchemy import select
    from app.models.claims import Claim

    factory, engine_to_dispose = _get_activity_session_factory()
    try:
        async with factory() as session:
            result = await session.execute(
                select(Claim).where(Claim.task_id == task_id)
            )
            claim = result.scalar_one_or_none()
            if claim is not None:
                # Map the polling status to a claim status
                status_map = {
                    "paid": "paid",
                    "approved": "paid",
                    "denied": "denied",
                    "rejected": "denied",
                    "finalized": "finalized",
                }
                claim.status = status_map.get(final_status, final_status or claim.status)

                # Write audit entry for the status update
                from app.core.audit.logger import AuditLogger
                audit_logger = AuditLogger(session)
                await audit_logger.log(
                    action="claims_workflow:status_updated_from_poll",
                    actor_type="agent",
                    resource_type="claim",
                    resource_id=str(claim.id),
                    details={
                        "task_id": task_id,
                        "final_status": final_status,
                        "status_description": status_description,
                    },
                )

                await session.commit()
            else:
                logger.warning(
                    "Claim for task %s not found; skipping status update", task_id
                )
    except Exception as exc:
        logger.error("Failed to update claim status: %s", exc)
        raise
    finally:
        if engine_to_dispose is not None:
            await engine_to_dispose.dispose()

    return asdict(ActivityResult(
        success=True,
        data={"task_id": task_id, "final_status": final_status},
    ))


@activity.defn
async def analyze_workflow_denials(args: dict[str, Any]) -> dict[str, Any]:
    """Analyze denials detected during remittance parsing that lack analysis.

    Ensures every denial has a recommendation, filling in gaps when the
    agent graph did not produce denial_analyses (e.g., denials detected
    only at the workflow/remittance level).
    """
    restore_correlation_id(args)
    task_id = args.get("task_id", "")
    denials = args.get("denials", [])
    existing_analyses = args.get("existing_analyses", [])
    input_data = args.get("input_data", {})

    safe_heartbeat("analyzing_workflow_denials")

    from app.agents.claims.tools import analyze_denial

    all_analyses = list(existing_analyses)

    for i, denial in enumerate(denials):
        if i < len(existing_analyses):
            # Already have analysis for this denial
            continue

        adjustments = denial.get("adjustments", [])
        denial_code = adjustments[0].get("reason_code", "unknown") if adjustments else "unknown"

        analysis = await analyze_denial(
            denial_code=denial_code,
            denial_reason=denial.get("denial_reason", ""),
            claim_id=denial.get("claim_id", ""),
            diagnosis_codes=input_data.get("diagnosis_codes", []),
            procedure_codes=input_data.get("procedure_codes", []),
            payer_id=input_data.get("payer_id", ""),
        )
        all_analyses.append(analysis)

    return asdict(ActivityResult(
        success=True,
        data={
            "task_id": task_id,
            "denial_analyses": all_analyses,
        },
    ))


# ── Workflow Definition ──────────────────────────────────────────────


@workflow.defn
class ClaimsWorkflow:
    """Temporal workflow for claims agent execution.

    Orchestrates the full claims lifecycle with support for long-running
    denial management. The workflow handles:
    1. Input validation
    2. Agent execution (code validation, 837 building)
    3. Clearinghouse submission
    4. Remittance parsing (835)
    5. Claims/denial DB persistence
    6. Result persistence with audit trail
    """

    @workflow.run
    async def run(self, workflow_input: WorkflowInput) -> WorkflowResult:
        task_id = workflow_input.task_id
        agent_type = workflow_input.agent_type
        correlation_id = workflow_input.correlation_id or ""

        # Step 1: Validate input
        validation = await workflow.execute_activity(
            validate_claims_input,
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

        # Step 2: Run claims agent (code validation, 837 building, etc.)
        agent_result = await workflow.execute_activity(
            run_claims_agent_activity,
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
        x12_data = agent_data.get("x12_data", {})
        denial_analyses = agent_data.get("denial_analyses", [])
        audit_trail = agent_data.get("audit_trail", [])

        # Step 3: Submit claim to clearinghouse
        clearinghouse_result = {}
        if x12_data and x12_data.get("x12_837"):
            ch_result = await workflow.execute_activity(
                submit_claim_to_clearinghouse,
                {
                    "task_id": task_id,
                    "x12_data": x12_data,
                    "payer_id": workflow_input.input_data.get("payer_id", ""),
                    "claim_type": workflow_input.input_data.get("claim_type", "837P"),
                    "clearinghouse_config": workflow_input.clearinghouse_config,
                    "correlation_id": correlation_id,
                },
                start_to_close_timeout=CLEARINGHOUSE_ACTIVITY_TIMEOUT,
                heartbeat_timeout=CLEARINGHOUSE_HEARTBEAT_TIMEOUT,
                retry_policy=CLEARINGHOUSE_RETRY_POLICY.to_retry_policy(),
            )
            if ch_result.get("success"):
                clearinghouse_result = ch_result.get("data", {})
                output["clearinghouse_transaction_id"] = clearinghouse_result.get("transaction_id", "")
                output["submission_status"] = clearinghouse_result.get("status", "submitted")

        # Step 4: Parse remittance (835)
        # Thread raw_835 from agent data or clearinghouse response so
        # the activity can parse a real 835 when available.
        raw_835 = agent_data.get("raw_835", "") or workflow_input.input_data.get("raw_835", "")
        if not raw_835 and clearinghouse_result:
            raw_835 = clearinghouse_result.get("raw_835", "")

        remittance_result = await workflow.execute_activity(
            parse_remittance_activity,
            {
                "task_id": task_id,
                "remittance_data": agent_data.get("remittance_data", {}),
                "raw_835": raw_835,
                "input_data": workflow_input.input_data,
                "correlation_id": correlation_id,
            },
            start_to_close_timeout=AGENT_ACTIVITY_TIMEOUT,
            retry_policy=AGENT_RETRY_POLICY.to_retry_policy(),
        )

        payment_info = {}
        remittance_data_full = {}
        denials = []
        awaiting_835 = False
        if remittance_result.get("success"):
            rem_data = remittance_result.get("data", {})
            awaiting_835 = rem_data.get("awaiting_835", False)
            payment_info = rem_data.get("payment_info", {})
            remittance_data_full = rem_data.get("remittance_data", {})
            denials = rem_data.get("denials", [])
            if not awaiting_835:
                output["payment_info"] = payment_info

            if denials:
                needs_review = True
                confidence = min(confidence, 0.5) if confidence > 0 else 0.5
                review_reason = review_reason or f"Claim denied: {len(denials)} denial(s) requiring review"

                # Ensure denial analyses exist for all denials — run analysis
                # for any denials not already covered by the agent graph
                if len(denial_analyses) < len(denials):
                    analysis_result = await workflow.execute_activity(
                        analyze_workflow_denials,
                        {
                            "task_id": task_id,
                            "denials": denials,
                            "existing_analyses": denial_analyses,
                            "input_data": workflow_input.input_data,
                            "correlation_id": correlation_id,
                        },
                        start_to_close_timeout=AGENT_ACTIVITY_TIMEOUT,
                        retry_policy=AGENT_RETRY_POLICY.to_retry_policy(),
                    )
                    if analysis_result.get("success"):
                        denial_analyses = analysis_result.get("data", {}).get("denial_analyses", denial_analyses)

        # Step 5: Write full result to DB (AgentTask + Claim + ClaimDenial + audit)
        await workflow.execute_activity(
            write_claims_result,
            {
                "task_id": task_id,
                "output": output,
                "input_data": workflow_input.input_data,
                "clearinghouse_result": clearinghouse_result,
                "payment_info": payment_info,
                "remittance_data": remittance_data_full,
                "denials": denials,
                "denial_analyses": denial_analyses,
                "audit_trail": audit_trail,
                "awaiting_835": awaiting_835,
                "correlation_id": correlation_id,
            },
            start_to_close_timeout=DB_ACTIVITY_TIMEOUT,
            heartbeat_timeout=DB_HEARTBEAT_TIMEOUT,
            retry_policy=DB_RETRY_POLICY.to_retry_policy(),
        )

        # Step 6: Long-running denial management loop
        # If there are denials, poll for status resolution.
        # Denial polling runs independently of the HITL review flag —
        # the review tracks human oversight while polling tracks
        # clearinghouse-side resolution.
        if denials:
            max_polls = 3  # In production this would be much higher
            poll_interval = timedelta(seconds=5)  # In production: hours/days
            claim_id = output.get("claim_id", "")

            for poll_attempt in range(max_polls):
                # Wait before polling
                await workflow.sleep(poll_interval)

                poll_result = await workflow.execute_activity(
                    poll_claim_status,
                    {
                        "task_id": task_id,
                        "claim_id": claim_id,
                        "input_data": workflow_input.input_data,
                        "poll_attempt": poll_attempt,
                        "correlation_id": correlation_id,
                    },
                    start_to_close_timeout=CLEARINGHOUSE_ACTIVITY_TIMEOUT,
                    retry_policy=CLEARINGHOUSE_RETRY_POLICY.to_retry_policy(),
                )

                if poll_result.get("success"):
                    poll_data = poll_result.get("data", {})
                    if poll_data.get("terminal", False):
                        # Claim reached terminal state — persist to DB
                        output["final_status"] = poll_data.get("status")
                        output["status_description"] = poll_data.get("description")

                        await workflow.execute_activity(
                            update_claim_status_activity,
                            {
                                "task_id": task_id,
                                "final_status": poll_data.get("status"),
                                "status_description": poll_data.get("description", ""),
                                "correlation_id": correlation_id,
                            },
                            start_to_close_timeout=DB_ACTIVITY_TIMEOUT,
                            heartbeat_timeout=DB_HEARTBEAT_TIMEOUT,
                            retry_policy=DB_RETRY_POLICY.to_retry_policy(),
                        )
                        break

        return WorkflowResult(
            task_id=task_id,
            agent_type=agent_type,
            status=WorkflowStatus.COMPLETED.value,
            output_data=output,
            confidence=confidence,
            needs_review=needs_review,
            review_reason=review_reason,
            clearinghouse_transaction_id=clearinghouse_result.get("transaction_id"),
        )


# ── Inline runner (fallback when Temporal is unavailable) ──────────


async def run_claims_workflow(workflow_input: WorkflowInput) -> WorkflowResult:
    """Run the claims workflow inline (no Temporal).

    Used as a fallback for testing or when Temporal is not available.
    Executes the full claims pipeline: agent → clearinghouse → 835 parse → DB persist.
    """
    task_id = workflow_input.task_id
    agent_type = workflow_input.agent_type

    from app.agents.claims.graph import run_claims_agent
    from app.core.engine.llm_provider import LLMProvider, MockLLMBackend

    llm_provider = LLMProvider(primary=MockLLMBackend())

    try:
        state = await run_claims_agent(
            input_data=workflow_input.input_data,
            llm_provider=llm_provider,
            task_id=task_id,
        )

        output = state.get("decision", {})
        confidence = state.get("confidence", 0.0)
        needs_review = state.get("needs_review", False)
        review_reason = state.get("review_reason", "")

        x12_data = state.get("x12_837_data", {})
        denial_analyses = state.get("denial_analyses", [])
        audit_trail = state.get("audit_trail", [])

        # Submit to clearinghouse (inline)
        clearinghouse_result = {}
        if x12_data and x12_data.get("x12_837"):
            ch_args = {
                "task_id": task_id,
                "x12_data": x12_data,
                "payer_id": workflow_input.input_data.get("payer_id", ""),
                "claim_type": workflow_input.input_data.get("claim_type", "837P"),
            }
            try:
                ch_result_raw = await submit_claim_to_clearinghouse(ch_args)
                if ch_result_raw.get("success"):
                    clearinghouse_result = ch_result_raw.get("data", {})
                    output["clearinghouse_transaction_id"] = clearinghouse_result.get("transaction_id", "")
                    output["submission_status"] = clearinghouse_result.get("status", "submitted")
            except Exception as exc:
                logger.warning("Inline clearinghouse submission failed: %s", exc)

        # Parse remittance (inline)
        # Thread raw_835 from agent data or input so real 835 parsing works
        raw_835 = state.get("raw_835", "") or workflow_input.input_data.get("raw_835", "")
        if not raw_835 and clearinghouse_result:
            raw_835 = clearinghouse_result.get("raw_835", "")

        payment_info = {}
        remittance_data_full = {}
        denials = []
        awaiting_835 = False
        try:
            rem_result = await parse_remittance_activity({
                "task_id": task_id,
                "remittance_data": state.get("remittance_data", {}),
                "raw_835": raw_835,
                "input_data": workflow_input.input_data,
            })
            if rem_result.get("success"):
                rem_data = rem_result.get("data", {})
                awaiting_835 = rem_data.get("awaiting_835", False)
                payment_info = rem_data.get("payment_info", {})
                remittance_data_full = rem_data.get("remittance_data", {})
                denials = rem_data.get("denials", [])
                if not awaiting_835:
                    output["payment_info"] = payment_info
        except Exception as exc:
            logger.warning("Inline remittance parsing failed: %s", exc)

        # Ensure denial analyses exist for all workflow-detected denials
        if denials and len(denial_analyses) < len(denials):
            try:
                analysis_result = await analyze_workflow_denials({
                    "task_id": task_id,
                    "denials": denials,
                    "existing_analyses": denial_analyses,
                    "input_data": workflow_input.input_data,
                })
                if analysis_result.get("success"):
                    denial_analyses = analysis_result.get("data", {}).get("denial_analyses", denial_analyses)
            except Exception as exc:
                logger.warning("Inline denial analysis failed: %s", exc)

        # Persist claims/denials to DB (inline)
        try:
            # Serialize audit trail entries
            serialized_audit = []
            for entry in audit_trail:
                if isinstance(entry, dict):
                    serialized_audit.append(entry)
                else:
                    serialized_audit.append({
                        "timestamp": getattr(entry, "timestamp", ""),
                        "node": getattr(entry, "node", ""),
                        "action": getattr(entry, "action", ""),
                        "details": getattr(entry, "details", {}),
                    })

            await write_claims_result({
                "task_id": task_id,
                "output": output,
                "input_data": workflow_input.input_data,
                "clearinghouse_result": clearinghouse_result,
                "payment_info": payment_info,
                "remittance_data": remittance_data_full,
                "denials": denials,
                "denial_analyses": denial_analyses,
                "audit_trail": serialized_audit,
                "awaiting_835": awaiting_835,
            })
        except Exception as exc:
            logger.error("Inline claims result persistence failed: %s", exc)
            return WorkflowResult(
                task_id=task_id,
                agent_type=agent_type,
                status=WorkflowStatus.FAILED.value,
                error=f"Persistence failed: {exc}",
                output_data=output,
                confidence=confidence,
                needs_review=needs_review,
                review_reason=review_reason,
                clearinghouse_transaction_id=clearinghouse_result.get("transaction_id"),
            )

        return WorkflowResult(
            task_id=task_id,
            agent_type=agent_type,
            status=WorkflowStatus.COMPLETED.value,
            output_data=output,
            confidence=confidence,
            needs_review=needs_review,
            review_reason=review_reason,
            clearinghouse_transaction_id=clearinghouse_result.get("transaction_id"),
        )
    except Exception as exc:
        return WorkflowResult(
            task_id=task_id,
            agent_type=agent_type,
            status=WorkflowStatus.FAILED.value,
            error=str(exc),
        )
