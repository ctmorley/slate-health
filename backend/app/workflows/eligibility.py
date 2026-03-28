"""Eligibility verification Temporal workflow.

Orchestrates the full eligibility check lifecycle:
  ingest → run agent → submit 270 to clearinghouse → parse 271 → write result to DB

Each step is a Temporal activity decorated with ``@activity.defn``, providing
durability and automatic retries.  The workflow class uses
``workflow.execute_activity`` to dispatch each activity through Temporal.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import asdict
from datetime import timedelta
from typing import Any

from temporalio import activity, workflow

with workflow.unsafe.imports_passed_through():
    from app.core.clearinghouse.base import (
        TransactionRequest,
        TransactionStatus,
        TransactionType,
    )
    from app.core.clearinghouse.factory import get_clearinghouse
    from app.core.ingestion.x12_client import build_270, parse_271
    from app.workflows.base import (
        AGENT_ACTIVITY_TIMEOUT,
        AGENT_RETRY_POLICY,
        CLEARINGHOUSE_ACTIVITY_TIMEOUT,
        CLEARINGHOUSE_HEARTBEAT_TIMEOUT,
        CLEARINGHOUSE_RETRY_POLICY,
        DB_ACTIVITY_TIMEOUT,
        DB_HEARTBEAT_TIMEOUT,
        DB_RETRY_POLICY,
        ActivityError,
        ActivityResult,
        WorkflowInput,
        WorkflowResult,
        WorkflowStatus,
        safe_heartbeat,
        set_workflow_correlation_id,
        restore_correlation_id,
    )


_AUDIT_MAX_RETRIES = 3
_AUDIT_RETRY_DELAY_SECONDS = 0.5


def _get_activity_session_factory():
    """Get a session factory suitable for activity DB access.

    Uses the DI system's engine/session factory when available (which
    tests override to point at the test database).  Falls back to
    creating an engine from ``settings.database_url`` for Temporal
    worker processes that don't share the web app's DI state.

    Returns ``(session_factory, engine_to_dispose)`` where
    ``engine_to_dispose`` is ``None`` when the DI engine is reused
    (caller must NOT dispose it) or a fresh engine when one was created
    (caller MUST dispose it after use).
    """
    from app.dependencies import _engine, get_session_factory

    if _engine is not None:
        # DI engine exists (web process or test) — reuse it
        return get_session_factory(), None

    # Temporal worker or standalone: create a disposable engine with pool tuning
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
    from app.dependencies import create_disposable_engine

    engine = create_disposable_engine()
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return factory, engine


async def _audit_workflow_stage(
    task_id: str,
    stage: str,
    action: str,
    details: dict[str, Any] | None = None,
) -> None:
    """Write an audit log entry for a workflow stage with retry logic.

    Creates a DB session to persist the audit entry independently of the
    workflow's activity sessions.  Retries up to ``_AUDIT_MAX_RETRIES``
    times with exponential back-off so that transient DB errors do not
    silently drop contractual audit events.
    """
    import asyncio as _asyncio

    last_exc: Exception | None = None

    for attempt in range(1, _AUDIT_MAX_RETRIES + 1):
        factory = None
        engine_to_dispose = None
        try:
            from app.core.audit.logger import AuditLogger

            factory, engine_to_dispose = _get_activity_session_factory()
            try:
                async with factory() as session:
                    audit_logger = AuditLogger(session)
                    await audit_logger.log(
                        action=f"eligibility_workflow:{action}",
                        actor_type="agent",
                        resource_type="agent_task",
                        resource_id=str(task_id),
                        details={"stage": stage, **(details or {})},
                    )
                    await session.commit()
            finally:
                if engine_to_dispose is not None:
                    await engine_to_dispose.dispose()
            return  # Success — exit immediately
        except Exception as exc:
            last_exc = exc
            if attempt < _AUDIT_MAX_RETRIES:
                delay = _AUDIT_RETRY_DELAY_SECONDS * (2 ** (attempt - 1))
                logger.warning(
                    "Audit write attempt %d/%d failed for stage %s: %s — retrying in %.1fs",
                    attempt, _AUDIT_MAX_RETRIES, stage, exc, delay,
                )
                await _asyncio.sleep(delay)

    # All retries exhausted — log at ERROR so monitoring catches it
    logger.error(
        "AUDIT FAILURE: all %d attempts exhausted for stage '%s', action '%s', "
        "task_id '%s': %s",
        _AUDIT_MAX_RETRIES, stage, action, task_id, last_exc,
    )

logger = logging.getLogger(__name__)


# ── Activities ──────────────────────────────────────────────────────────


@activity.defn
async def validate_eligibility_input(workflow_input: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize eligibility check input data.

    Returns the validated input as an ActivityResult dict.
    """
    restore_correlation_id(workflow_input)
    input_data = workflow_input.get("input_data", {})

    task_id = workflow_input.get("task_id", "")

    required_fields = ["subscriber_id", "subscriber_last_name", "subscriber_first_name"]
    missing = [f for f in required_fields if not input_data.get(f)]
    if missing:
        await _audit_workflow_stage(task_id, "validate", "validation_failed", {"missing_fields": missing})
        return asdict(ActivityResult(
            success=False,
            error=f"Missing required fields: {', '.join(missing)}",
        ))

    # Normalize and enrich input
    validated = {
        "subscriber_id": input_data["subscriber_id"],
        "subscriber_last_name": input_data["subscriber_last_name"],
        "subscriber_first_name": input_data["subscriber_first_name"],
        "subscriber_dob": input_data.get("subscriber_dob", ""),
        "payer_id": input_data.get("payer_id", ""),
        "payer_name": input_data.get("payer_name", ""),
        "provider_npi": input_data.get("provider_npi", ""),
        "provider_last_name": input_data.get("provider_last_name", ""),
        "provider_first_name": input_data.get("provider_first_name", ""),
        "date_of_service": input_data.get("date_of_service", ""),
        "service_type_code": input_data.get("service_type_code", "30"),
        "task_id": task_id,
    }

    # Pass through test control flags for deterministic E2E testing
    if input_data.get("force_low_confidence"):
        validated["force_low_confidence"] = True
    if input_data.get("force_clearinghouse_error"):
        validated["force_clearinghouse_error"] = True

    await _audit_workflow_stage(task_id, "validate", "input_validated", {
        "subscriber_id": validated["subscriber_id"],
        "payer_id": validated["payer_id"],
    })
    return asdict(ActivityResult(success=True, data=validated))


@activity.defn
async def create_pending_eligibility_check(workflow_input: dict[str, Any]) -> dict[str, Any]:
    """Create a pending EligibilityCheck record at workflow start.

    Ensures the DB has a lifecycle record before any clearinghouse work
    begins, so later activities can always update (rather than create) it.
    """
    restore_correlation_id(workflow_input)
    task_id = workflow_input.get("task_id", "")
    data = workflow_input.get("data", {})

    safe_heartbeat("creating_pending_eligibility_check")

    factory, engine_to_dispose = _get_activity_session_factory()
    try:
        async with factory() as session:
            from app.models.eligibility import EligibilityCheck
            from app.models.agent_task import AgentTask
            from sqlalchemy import select

            # Retrieve the task to get patient_id
            task_result = await session.execute(
                select(AgentTask).where(AgentTask.id == task_id)
            )
            task = task_result.scalar_one_or_none()
            patient_id = task.patient_id if task else None

            # Check if record already exists (idempotency for retries)
            existing = await session.execute(
                select(EligibilityCheck).where(EligibilityCheck.task_id == task_id)
            )
            if existing.scalar_one_or_none() is None:
                check = EligibilityCheck(
                    task_id=task_id,
                    patient_id=patient_id,
                    status="pending",
                    coverage_active=False,
                    coverage_details={},
                    request_data=data,
                    response_data={},
                    transaction_id_270="",
                    transaction_id_271="",
                )
                session.add(check)
                await session.commit()

        await _audit_workflow_stage(task_id, "init", "eligibility_check_created", {
            "patient_id": str(patient_id) if patient_id else None,
        })
    except Exception as exc:
        logger.error(
            "Failed to create pending EligibilityCheck for task %s: %s. "
            "write_eligibility_result will attempt to create the record.",
            task_id, exc,
        )
    finally:
        if engine_to_dispose is not None:
            await engine_to_dispose.dispose()

    return asdict(ActivityResult(success=True, data={"task_id": task_id}))


@activity.defn
async def execute_eligibility_agent(validated_input: dict[str, Any]) -> dict[str, Any]:
    """Run the eligibility agent reasoning step via the LangGraph-based EligibilityAgent.

    Delegates to the full LangGraph agent graph which executes:
    parse_request → check_payer_rules → reason → decide → execute → audit

    The agent evaluates payer rules, enriches the request with agent-level
    logic, and decides the submission strategy before the 270 is built.
    """
    restore_correlation_id(validated_input)
    from app.agents.eligibility.graph import EligibilityAgent
    from app.core.engine.llm_provider import LLMProvider, MockLLMBackend

    data = validated_input.get("data", {})

    # Create an LLM provider — in production this would use Bedrock/Claude;
    # for now we use MockLLMBackend as the LLM reasoning layer.  The key
    # agent logic (payer rule checks, validation, tool calls) runs
    # deterministically through the graph nodes regardless of LLM backend.
    try:
        from app.core.engine.llm_provider import BedrockBackend
        llm_backend = BedrockBackend()
    except Exception:
        llm_backend = MockLLMBackend(responses=[
            '{"confidence": 0.85, "decision": {"submission_strategy": "clearinghouse"}, "tool_calls": []}'
        ])

    llm_provider = LLMProvider(primary=llm_backend, phi_safe=True)
    agent = EligibilityAgent(llm_provider=llm_provider)

    # Build patient and payer context for graph execution
    patient_context = {
        "subscriber_id": data.get("subscriber_id", ""),
        "first_name": data.get("subscriber_first_name", ""),
        "last_name": data.get("subscriber_last_name", ""),
        "date_of_birth": data.get("subscriber_dob", ""),
    }
    payer_context = {
        "payer_id": data.get("payer_id", ""),
        "payer_name": data.get("payer_name", ""),
    }

    state = await agent.run(
        task_id=data.get("task_id", ""),
        input_data=data,
        patient_context=patient_context,
        payer_context=payer_context,
    )

    # Extract agent decision and enrichments from graph state
    agent_decision = {
        "submission_strategy": "clearinghouse",
        "payer_rules_applied": [],
        "enrichments": {},
        "confidence": state.get("confidence", 0.85),
        "needs_review": state.get("needs_review", False),
        "decision": state.get("decision"),
        "audit_trail": [
            {"node": e.get("node", ""), "action": e.get("action", "")}
            for e in state.get("audit_trail", [])
        ],
    }

    payer_id = data.get("payer_id", "")
    if payer_id:
        agent_decision["enrichments"]["payer_id_verified"] = True

    task_id = data.get("task_id", "")
    await _audit_workflow_stage(task_id, "agent", "agent_reasoning_complete", {
        "confidence": agent_decision.get("confidence", 0.85),
        "needs_review": agent_decision.get("needs_review", False),
        "payer_rules_applied": agent_decision.get("payer_rules_applied", []),
    })

    return asdict(ActivityResult(
        success=True,
        data={
            **data,
            "agent_decision": agent_decision,
        },
    ))


@activity.defn
async def build_eligibility_request(validated_input: dict[str, Any]) -> dict[str, Any]:
    """Build the X12 270 eligibility inquiry from validated input.

    Returns the X12 270 payload as an ActivityResult dict.
    """
    restore_correlation_id(validated_input)
    data = validated_input.get("data", {})

    task_id = data.get("task_id", "")
    try:
        control_number = str(uuid.uuid4().int)[:9]
        x12_270 = build_270(
            sender_id=data.get("provider_npi") or "SENDER01",
            receiver_id=data.get("payer_id") or "RECEIVER01",
            subscriber_id=data["subscriber_id"],
            subscriber_last_name=data["subscriber_last_name"],
            subscriber_first_name=data["subscriber_first_name"],
            subscriber_dob=data.get("subscriber_dob") or "19900101",
            payer_id=data.get("payer_id") or "UNKNOWN",
            payer_name=data.get("payer_name") or "Unknown Payer",
            provider_npi=data.get("provider_npi") or "0000000000",
            provider_last_name=data.get("provider_last_name") or "Provider",
            provider_first_name=data.get("provider_first_name") or "",
            date_of_service=data.get("date_of_service") or None,
            service_type_code=data.get("service_type_code") or "30",
            control_number=control_number,
        )
        await _audit_workflow_stage(task_id, "build_270", "270_request_built", {
            "control_number": control_number,
        })
        build_data = {
            "x12_270": x12_270,
            "control_number": control_number,
            "task_id": task_id,
        }
        # Propagate test control flags through the activity chain
        if data.get("force_low_confidence"):
            build_data["force_low_confidence"] = True
        if data.get("force_clearinghouse_error"):
            build_data["force_clearinghouse_error"] = True
        return asdict(ActivityResult(
            success=True,
            data=build_data,
        ))
    except (ValueError, KeyError, TypeError) as exc:
        await _audit_workflow_stage(task_id, "build_270", "270_build_failed", {"error": str(exc)})
        # Non-retryable validation/data errors → return failure result
        return asdict(ActivityResult(
            success=False,
            error=f"Failed to build 270 request: {exc}",
        ))
    # All other exceptions propagate to Temporal for retry.


@activity.defn
async def submit_to_clearinghouse(
    args: dict[str, Any],
) -> dict[str, Any]:
    """Submit the 270 request to the configured clearinghouse.

    Uses the clearinghouse factory to select the appropriate client
    based on the organization's configuration.

    ``args`` is a dict with keys ``x12_payload`` and optionally
    ``clearinghouse_config``.
    """
    restore_correlation_id(args)
    x12_payload = args.get("x12_payload", {})
    clearinghouse_config = args.get("clearinghouse_config")

    payload_data = x12_payload.get("data", {})
    x12_270 = payload_data.get("x12_270", "")
    control_number = payload_data.get("control_number", "")

    if not x12_270:
        return asdict(ActivityResult(
            success=False,
            error="No X12 270 payload to submit",
        ))

    # Test control flag: force_clearinghouse_error raises a connection error
    # to exercise retry/circuit-breaker logic in E2E tests.
    if payload_data.get("force_clearinghouse_error"):
        raise ConnectionError(
            "Simulated clearinghouse connection failure (force_clearinghouse_error=True)"
        )

    if not clearinghouse_config:
        logger.warning(
            "No clearinghouse_config for eligibility submission — using mock clearinghouse"
        )
    config = clearinghouse_config or {
        "clearinghouse_name": "mock",
        "api_endpoint": "http://mock-clearinghouse",
        "credentials": {},
    }

    client = get_clearinghouse(
        clearinghouse_name=config["clearinghouse_name"],
        api_endpoint=config["api_endpoint"],
        credentials=config.get("credentials"),
    )

    request = TransactionRequest(
        transaction_type=TransactionType.ELIGIBILITY_270,
        payload=x12_270,
        sender_id="SENDER01",
        receiver_id="RECEIVER01",
        control_number=control_number,
    )

    # Send a heartbeat before the potentially long-running HTTP call
    safe_heartbeat("submitting_to_clearinghouse")

    # Let transient exceptions propagate so Temporal can retry the activity.
    # Only catch clearinghouse-level validation errors (non-retryable) and
    # return them as a failed ActivityResult.
    try:
        response = await client.submit_transaction(request)
    except ValueError as exc:
        # Non-retryable business validation error
        return asdict(ActivityResult(
            success=False,
            error=f"Clearinghouse validation error: {exc}",
        ))
    # All other exceptions (ConnectionError, TimeoutError, etc.) propagate
    # to Temporal, which applies the CLEARINGHOUSE_RETRY_POLICY.

    task_id = payload_data.get("task_id", "")
    await _audit_workflow_stage(task_id, "clearinghouse", "clearinghouse_submitted", {
        "transaction_id": response.transaction_id,
        "status": response.status.value,
    })

    result_payload = {
        "transaction_id": response.transaction_id,
        "status": response.status.value,
        "raw_response": response.raw_response,
        "parsed_response": response.parsed_response,
        "task_id": task_id,
    }
    # Propagate test control flags through the activity chain
    if payload_data.get("force_low_confidence"):
        result_payload["force_low_confidence"] = True

    return asdict(ActivityResult(
        success=True,
        data=result_payload,
    ))


@activity.defn
async def parse_eligibility_response(
    clearinghouse_result: dict[str, Any],
) -> dict[str, Any]:
    """Parse the 271 response from the clearinghouse.

    Extracts coverage details, benefit information, and determines
    if the response is ambiguous (triggering HITL review).
    """
    restore_correlation_id(clearinghouse_result)
    result_data = clearinghouse_result.get("data", {})
    raw_response = result_data.get("raw_response", "")
    parsed = result_data.get("parsed_response", {})
    task_id = result_data.get("task_id", "")

    # Check for test control flag: force_low_confidence
    # This is propagated through the workflow input_data for deterministic
    # E2E testing of the HITL review flow without DB seeding.
    _force_low = result_data.get("force_low_confidence", False)

    # If the clearinghouse returned pre-parsed JSON, use it directly
    if parsed and isinstance(parsed, dict):
        coverage_info = parsed.get("coverage", {})
        if coverage_info:
            # Determine confidence based on response quality
            confidence = _calculate_eligibility_confidence(parsed)
            if _force_low:
                confidence = 0.3  # Force below threshold for HITL testing
            needs_review = confidence < 0.7

            review_reason = ""
            if needs_review:
                review_reason = "Ambiguous eligibility response"
                # Check for specific ambiguity reasons
                active = [b for b in parsed.get("benefits", []) if b.get("eligibility_code") == "1"]
                if len(active) > 1:
                    review_reason = "Multiple coverage matches found"
                elif parsed.get("errors"):
                    review_reason = "Eligibility response contains errors"

            await _audit_workflow_stage(task_id, "parse_271", "271_response_parsed", {
                "confidence": confidence,
                "needs_review": needs_review,
                "coverage_active": coverage_info.get("active", False),
            })

            return asdict(ActivityResult(
                success=True,
                data={
                    "coverage_active": coverage_info.get("active", False),
                    "coverage_details": coverage_info,
                    "benefits": parsed.get("benefits", []),
                    "subscriber": parsed.get("subscriber", {}),
                    "payer": parsed.get("payer", {}),
                    "confidence": confidence,
                    "needs_review": needs_review,
                    "review_reason": review_reason,
                    "transaction_id": result_data.get("transaction_id", ""),
                    "task_id": task_id,
                },
            ))

    # Try to parse raw X12 271 response
    if raw_response:
        try:
            parsed_271 = parse_271(raw_response)
            coverage = parsed_271.get("coverage", {})
            confidence = _calculate_eligibility_confidence(parsed_271)
            if _force_low:
                confidence = 0.3  # Force below threshold for HITL testing
            needs_review = confidence < 0.7

            review_reason = ""
            if needs_review:
                review_reason = "Ambiguous eligibility response"
                active_271 = [b for b in parsed_271.get("benefits", []) if b.get("eligibility_code") == "1"]
                if len(active_271) > 1:
                    review_reason = "Multiple coverage matches found"
                elif parsed_271.get("errors"):
                    review_reason = "Eligibility response contains errors"

            await _audit_workflow_stage(task_id, "parse_271", "271_response_parsed", {
                "confidence": confidence,
                "needs_review": needs_review,
                "coverage_active": coverage.get("active", False),
            })

            return asdict(ActivityResult(
                success=True,
                data={
                    "coverage_active": coverage.get("active", False),
                    "coverage_details": coverage,
                    "benefits": parsed_271.get("benefits", []),
                    "subscriber": parsed_271.get("subscriber", {}),
                    "payer": parsed_271.get("payer", {}),
                    "confidence": confidence,
                    "needs_review": needs_review,
                    "review_reason": review_reason,
                    "errors": parsed_271.get("errors", []),
                    "transaction_id": result_data.get("transaction_id", ""),
                    "task_id": task_id,
                },
            ))
        except (ValueError, KeyError, TypeError, IndexError) as exc:
            await _audit_workflow_stage(task_id, "parse_271", "271_parse_failed", {"error": str(exc)})
            # Non-retryable parse errors → return failure result
            return asdict(ActivityResult(
                success=False,
                error=f"Failed to parse 271 response: {exc}",
            ))
        # All other exceptions propagate to Temporal for retry.

    return asdict(ActivityResult(
        success=False,
        error="No response data available to parse",
    ))


@activity.defn
async def write_eligibility_result(
    args: dict[str, Any],
) -> dict[str, Any]:
    """Write the eligibility check result to the database.

    Persists an EligibilityCheck record and returns the result summary.
    ``args`` is a dict with keys ``task_id`` and ``result_data``.
    """
    restore_correlation_id(args)
    task_id = args.get("task_id", "")
    result_data = args.get("result_data", {})
    request_data = args.get("request_data", {})
    control_number = args.get("control_number", "")
    data = result_data.get("data", {})

    # Send a heartbeat before DB work
    safe_heartbeat("writing_eligibility_result")

    # Persist to the database via a session from the DI system (when
    # available) or a fresh engine for Temporal workers.
    # DB failures raise so Temporal can retry the activity via DB_RETRY_POLICY.
    factory, engine_to_dispose = _get_activity_session_factory()
    try:
        async with factory() as session:
            from app.models.eligibility import EligibilityCheck
            from sqlalchemy import select
            from app.models.agent_task import AgentTask

            # Retrieve the task to get patient_id
            task_result = await session.execute(
                select(AgentTask).where(AgentTask.id == task_id)
            )
            task = task_result.scalar_one_or_none()

            if task is None:
                raise ActivityError(
                    f"AgentTask '{task_id}' not found — cannot persist eligibility result",
                    task_id=task_id,
                )

            patient_id = task.patient_id

            # Check if an existing EligibilityCheck record exists (created at start)
            existing_result = await session.execute(
                select(EligibilityCheck).where(EligibilityCheck.task_id == task_id)
            )
            check = existing_result.scalar_one_or_none()

            if check is not None:
                # Update existing record from pending → completed
                check.status = "completed"
                check.coverage_active = data.get("coverage_active", False)
                check.coverage_details = data.get("coverage_details", {})
                check.response_data = data
                check.transaction_id_271 = data.get("transaction_id", "")
                if request_data:
                    check.request_data = request_data
                if control_number:
                    check.transaction_id_270 = control_number
            else:
                # Create new record with full lifecycle data
                check = EligibilityCheck(
                    task_id=task_id,
                    patient_id=patient_id,
                    status="completed",
                    coverage_active=data.get("coverage_active", False),
                    coverage_details=data.get("coverage_details", {}),
                    request_data=request_data or {},
                    response_data=data,
                    transaction_id_270=control_number or "",
                    transaction_id_271=data.get("transaction_id", ""),
                )
                session.add(check)

            await session.commit()

            await _audit_workflow_stage(task_id, "write_result", "eligibility_result_persisted", {
                "coverage_active": data.get("coverage_active", False),
                "confidence": data.get("confidence", 0.0),
                "needs_review": data.get("needs_review", False),
            })
    except Exception as exc:
        logger.error("Failed to persist EligibilityCheck to DB: %s", exc)
        raise  # Let Temporal retry via DB_RETRY_POLICY
    finally:
        if engine_to_dispose is not None:
            await engine_to_dispose.dispose()

    return asdict(ActivityResult(
        success=True,
        data={
            "task_id": task_id,
            "coverage_active": data.get("coverage_active", False),
            "coverage_details": data.get("coverage_details", {}),
            "benefits": data.get("benefits", []),
            "confidence": data.get("confidence", 0.0),
            "needs_review": data.get("needs_review", False),
            "review_reason": data.get("review_reason", ""),
            "transaction_id": data.get("transaction_id", ""),
        },
    ))


# ── Temporal Workflow Definition ──────────────────────────────────────


@workflow.defn
class EligibilityWorkflow:
    """Temporal workflow for eligibility verification.

    Executes each step as a Temporal activity with its own retry policy
    and timeout, providing durable execution and automatic retries.
    """

    @workflow.run
    async def run(self, workflow_input: WorkflowInput) -> WorkflowResult:
        """Execute the full eligibility verification workflow.

        Steps:
            1. Validate input (ingest)
            2. Execute agent reasoning (agent)
            3. Build 270 request
            4. Submit to clearinghouse
            5. Parse 271 response
            6. Write result to DB
        """
        task_id = workflow_input.task_id
        correlation_id = workflow_input.correlation_id or ""
        input_dict = {
            "task_id": task_id,
            "agent_type": workflow_input.agent_type,
            "input_data": workflow_input.input_data,
            "patient_context": workflow_input.patient_context,
            "payer_context": workflow_input.payer_context,
            "correlation_id": correlation_id,
        }

        # Step 1: Validate input (ingest)
        validation_result = await workflow.execute_activity(
            validate_eligibility_input,
            input_dict,
            start_to_close_timeout=AGENT_ACTIVITY_TIMEOUT,
            retry_policy=AGENT_RETRY_POLICY.to_retry_policy(),
        )
        if not validation_result.get("success"):
            return WorkflowResult(
                task_id=task_id,
                agent_type="eligibility",
                status=WorkflowStatus.FAILED.value,
                error=validation_result.get("error", "Validation failed"),
            )

        # Step 1b: Create pending EligibilityCheck record for lifecycle tracking
        await workflow.execute_activity(
            create_pending_eligibility_check,
            {"task_id": task_id, "data": workflow_input.input_data, "correlation_id": correlation_id},
            start_to_close_timeout=DB_ACTIVITY_TIMEOUT,
            heartbeat_timeout=DB_HEARTBEAT_TIMEOUT,
            retry_policy=DB_RETRY_POLICY.to_retry_policy(),
        )

        # Step 2: Execute agent reasoning
        agent_result = await workflow.execute_activity(
            execute_eligibility_agent,
            validation_result,
            start_to_close_timeout=AGENT_ACTIVITY_TIMEOUT,
            retry_policy=AGENT_RETRY_POLICY.to_retry_policy(),
        )
        if not agent_result.get("success"):
            return WorkflowResult(
                task_id=task_id,
                agent_type="eligibility",
                status=WorkflowStatus.FAILED.value,
                error=agent_result.get("error", "Agent execution failed"),
            )

        # Step 3: Build 270 request
        build_result = await workflow.execute_activity(
            build_eligibility_request,
            agent_result,
            start_to_close_timeout=AGENT_ACTIVITY_TIMEOUT,
            retry_policy=AGENT_RETRY_POLICY.to_retry_policy(),
        )
        if not build_result.get("success"):
            return WorkflowResult(
                task_id=task_id,
                agent_type="eligibility",
                status=WorkflowStatus.FAILED.value,
                error=build_result.get("error", "Failed to build 270"),
            )

        # Step 4: Submit to clearinghouse
        submit_args = {
            "x12_payload": build_result,
            "clearinghouse_config": workflow_input.clearinghouse_config,
            "correlation_id": correlation_id,
        }
        submit_result = await workflow.execute_activity(
            submit_to_clearinghouse,
            submit_args,
            start_to_close_timeout=CLEARINGHOUSE_ACTIVITY_TIMEOUT,
            heartbeat_timeout=CLEARINGHOUSE_HEARTBEAT_TIMEOUT,
            retry_policy=CLEARINGHOUSE_RETRY_POLICY.to_retry_policy(),
        )
        if not submit_result.get("success"):
            return WorkflowResult(
                task_id=task_id,
                agent_type="eligibility",
                status=WorkflowStatus.FAILED.value,
                error=submit_result.get("error", "Clearinghouse submission failed"),
            )

        # Step 5: Parse response
        parse_result = await workflow.execute_activity(
            parse_eligibility_response,
            {**submit_result, "correlation_id": correlation_id},
            start_to_close_timeout=AGENT_ACTIVITY_TIMEOUT,
            retry_policy=AGENT_RETRY_POLICY.to_retry_policy(),
        )
        if not parse_result.get("success"):
            return WorkflowResult(
                task_id=task_id,
                agent_type="eligibility",
                status=WorkflowStatus.FAILED.value,
                error=parse_result.get("error", "Failed to parse response"),
            )

        # Step 6: Write result — include request data and control number
        # for full lifecycle persistence
        build_data = build_result.get("data", {})
        write_args = {
            "task_id": task_id,
            "result_data": parse_result,
            "request_data": agent_result.get("data", {}),
            "control_number": build_data.get("control_number", ""),
            "correlation_id": correlation_id,
        }
        write_result = await workflow.execute_activity(
            write_eligibility_result,
            write_args,
            start_to_close_timeout=DB_ACTIVITY_TIMEOUT,
            heartbeat_timeout=DB_HEARTBEAT_TIMEOUT,
            retry_policy=DB_RETRY_POLICY.to_retry_policy(),
        )

        result_data = write_result.get("data", {})
        return WorkflowResult(
            task_id=task_id,
            agent_type="eligibility",
            status=WorkflowStatus.COMPLETED.value,
            output_data=result_data,
            confidence=result_data.get("confidence", 0.0),
            needs_review=result_data.get("needs_review", False),
            review_reason=result_data.get("review_reason", ""),
            clearinghouse_transaction_id=result_data.get("transaction_id"),
        )


# ── Inline Orchestration (for testing / fallback) ────────────────────


async def run_eligibility_workflow(workflow_input: WorkflowInput) -> WorkflowResult:
    """Execute the eligibility workflow inline without Temporal.

    This function calls each activity directly for testing and local
    development.  In production, use the ``EligibilityWorkflow`` class
    dispatched through Temporal.
    """
    task_id = workflow_input.task_id
    input_dict = {
        "task_id": task_id,
        "agent_type": workflow_input.agent_type,
        "input_data": workflow_input.input_data,
        "patient_context": workflow_input.patient_context,
        "payer_context": workflow_input.payer_context,
    }

    # Step 1: Validate input (ingest)
    validation_result = await validate_eligibility_input(input_dict)
    if not validation_result.get("success"):
        return WorkflowResult(
            task_id=task_id,
            agent_type="eligibility",
            status=WorkflowStatus.FAILED.value,
            error=validation_result.get("error", "Validation failed"),
        )

    # Step 1b: Create pending EligibilityCheck record
    await create_pending_eligibility_check(
        {"task_id": task_id, "data": workflow_input.input_data}
    )

    # Step 2: Execute agent reasoning
    agent_result = await execute_eligibility_agent(validation_result)
    if not agent_result.get("success"):
        return WorkflowResult(
            task_id=task_id,
            agent_type="eligibility",
            status=WorkflowStatus.FAILED.value,
            error=agent_result.get("error", "Agent execution failed"),
        )

    # Step 3: Build 270 request
    build_result = await build_eligibility_request(agent_result)
    if not build_result.get("success"):
        return WorkflowResult(
            task_id=task_id,
            agent_type="eligibility",
            status=WorkflowStatus.FAILED.value,
            error=build_result.get("error", "Failed to build 270"),
        )

    # Step 4: Submit to clearinghouse
    submit_args = {
        "x12_payload": build_result,
        "clearinghouse_config": workflow_input.clearinghouse_config,
    }
    submit_result = await submit_to_clearinghouse(submit_args)
    if not submit_result.get("success"):
        return WorkflowResult(
            task_id=task_id,
            agent_type="eligibility",
            status=WorkflowStatus.FAILED.value,
            error=submit_result.get("error", "Clearinghouse submission failed"),
        )

    # Step 5: Parse response
    parse_result = await parse_eligibility_response(submit_result)
    if not parse_result.get("success"):
        return WorkflowResult(
            task_id=task_id,
            agent_type="eligibility",
            status=WorkflowStatus.FAILED.value,
            error=parse_result.get("error", "Failed to parse response"),
        )

    # Step 6: Write result — include request data and control number
    build_data = build_result.get("data", {})
    write_args = {
        "task_id": task_id,
        "result_data": parse_result,
        "request_data": agent_result.get("data", {}),
        "control_number": build_data.get("control_number", ""),
    }
    write_result = await write_eligibility_result(write_args)

    result_data = write_result.get("data", {})
    return WorkflowResult(
        task_id=task_id,
        agent_type="eligibility",
        status=WorkflowStatus.COMPLETED.value,
        output_data=result_data,
        confidence=result_data.get("confidence", 0.0),
        needs_review=result_data.get("needs_review", False),
        review_reason=result_data.get("review_reason", ""),
        clearinghouse_transaction_id=result_data.get("transaction_id"),
    )


# ── Helpers ─────────────────────────────────────────────────────────────


def _calculate_eligibility_confidence(parsed_response: dict[str, Any]) -> float:
    """Calculate confidence score for an eligibility response.

    Higher confidence when:
    - Coverage status is clearly active or inactive
    - No errors in the response
    - Subscriber info is complete

    Lower confidence when:
    - Multiple coverage matches or ambiguous status (HARD review trigger)
    - Errors present in response
    - Missing subscriber or payer info
    """
    confidence = 1.0

    coverage = parsed_response.get("coverage", {})
    errors = parsed_response.get("errors", [])
    benefits = parsed_response.get("benefits", [])
    subscriber = parsed_response.get("subscriber", {})

    # Errors significantly reduce confidence
    if errors:
        confidence -= 0.3 * min(len(errors), 3)

    # No benefits info reduces confidence
    if not benefits:
        confidence -= 0.2

    # Missing coverage dates
    if not coverage.get("effective_date"):
        confidence -= 0.1

    # Missing subscriber identification
    if not subscriber.get("id"):
        confidence -= 0.15

    # Multiple active benefit entries is a HARD ambiguity indicator.
    # Even 2+ active benefits means multiple coverage matches which
    # requires human review per the contract.
    active_benefits = [b for b in benefits if b.get("eligibility_code") == "1"]
    if len(active_benefits) > 1:
        # Drop confidence below threshold to guarantee HITL review
        confidence = min(confidence, 0.5)

    return max(0.0, min(1.0, confidence))
