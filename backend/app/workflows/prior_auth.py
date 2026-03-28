"""Prior Authorization agent Temporal workflow.

Orchestrates the full PA lifecycle as a long-running Temporal workflow:
  validate input → check PA required → gather clinical docs → run agent →
  build 278 → submit clearinghouse → track status → handle denial →
  generate appeal → persist result

The PA workflow is long-running because PA adjudication can take days
to weeks, with status polling activities and timer-based follow-ups.
For denied PAs, the workflow continues into the appeal sub-flow.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import asdict
from datetime import timedelta
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

# PA-specific timeouts
PA_STATUS_POLL_INTERVAL = timedelta(hours=4)
PA_MAX_POLL_ATTEMPTS = 30  # ~5 days of polling at 4hr intervals
PA_WORKFLOW_TIMEOUT = timedelta(days=30)


# ── Helper ────────────────────────────────────────────────────────────

def _get_activity_session_factory():
    """Get a session factory suitable for activity DB access."""
    from app.dependencies import _engine, get_session_factory

    if _engine is not None:
        return get_session_factory(), None

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
    from app.dependencies import create_disposable_engine

    engine = create_disposable_engine()
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return factory, engine


async def _resolve_payer_id(db_session: Any, payer_id_str: str) -> str:
    """Resolve a payer identifier to its UUID string.

    Accepts either a UUID string (returned as-is) or a payer_id_code
    (looked up in the payers table).  Returns the UUID string suitable
    for use with the rule engine, or the original string if resolution
    fails.
    """
    import uuid as _uuid

    # If it's already a valid UUID, return as-is
    try:
        _uuid.UUID(payer_id_str)
        return payer_id_str
    except (ValueError, AttributeError):
        pass

    # Look up by payer_id_code
    try:
        from app.models.payer import Payer
        from sqlalchemy import select

        result = await db_session.execute(
            select(Payer).where(Payer.payer_id_code == payer_id_str)
        )
        payer = result.scalar_one_or_none()
        if payer is not None:
            return str(payer.id)
    except Exception as exc:
        logger.warning(
            "Failed to resolve payer_id_code '%s' to UUID: %s",
            payer_id_str, exc,
        )

    return payer_id_str


async def _audit_pa_stage(
    task_id: str,
    stage: str,
    action: str,
    details: dict[str, Any] | None = None,
) -> None:
    """Write an audit log entry for a PA workflow stage."""
    try:
        from app.core.audit.logger import AuditLogger

        factory, engine_to_dispose = _get_activity_session_factory()
        try:
            async with factory() as session:
                audit_logger = AuditLogger(session)
                await audit_logger.log(
                    action=f"prior_auth_workflow:{action}",
                    actor_type="agent",
                    resource_type="agent_task",
                    resource_id=str(task_id),
                    details={"stage": stage, **(details or {})},
                )
                await session.commit()
        finally:
            if engine_to_dispose is not None:
                await engine_to_dispose.dispose()
    except Exception as exc:
        logger.error(
            "AUDIT FAILURE for PA stage '%s', action '%s', task_id '%s': %s",
            stage, action, task_id, exc,
        )


# ── Activities ────────────────────────────────────────────────────────


@activity.defn
async def validate_prior_auth_input(workflow_input: dict[str, Any]) -> dict[str, Any]:
    """Validate prior auth input data."""
    restore_correlation_id(workflow_input)
    input_data = workflow_input.get("input_data", {})
    task_id = workflow_input.get("task_id", "")

    required = ["procedure_code", "subscriber_id", "payer_id", "patient_id"]
    missing = [f for f in required if not input_data.get(f)]
    if missing:
        await _audit_pa_stage(task_id, "validate", "validation_failed",
                              {"missing_fields": missing})
        return asdict(ActivityResult(
            success=False,
            error=f"Missing required fields: {', '.join(missing)}",
        ))

    validated = {
        "task_id": task_id,
        **input_data,
    }

    await _audit_pa_stage(task_id, "validate", "input_validated", {
        "procedure_code": input_data.get("procedure_code", ""),
        "payer_id": input_data.get("payer_id", ""),
    })
    return asdict(ActivityResult(success=True, data=validated))


@activity.defn
async def create_pending_pa_record(workflow_input: dict[str, Any]) -> dict[str, Any]:
    """Create a pending PriorAuthRequest record at workflow start.

    Fails the activity (and triggers Temporal retry) if the record cannot be
    persisted.  Resolves patient_id from the AgentTask if present, or falls
    back to input_data.patient_id so the non-nullable FK can be satisfied.
    """
    restore_correlation_id(workflow_input)
    task_id = workflow_input.get("task_id", "")
    data = workflow_input.get("data", workflow_input.get("input_data", {}))

    safe_heartbeat("creating_pending_pa_record")

    factory, engine_to_dispose = _get_activity_session_factory()
    try:
        async with factory() as session:
            from app.models.prior_auth import PriorAuthRequest
            from app.models.agent_task import AgentTask
            from sqlalchemy import select

            task_result = await session.execute(
                select(AgentTask).where(AgentTask.id == task_id)
            )
            task = task_result.scalar_one_or_none()
            patient_id = task.patient_id if task else None

            # Fall back to patient_id from input_data if not on AgentTask
            if patient_id is None:
                raw_pid = data.get("patient_id", "")
                if raw_pid:
                    import uuid as _uuid
                    try:
                        patient_id = _uuid.UUID(str(raw_pid))
                    except (ValueError, AttributeError):
                        patient_id = None

            # If we still have no patient_id, create a placeholder so the
            # non-nullable FK can be satisfied — link to the task's patient
            # or set on the task itself if provided via input.
            if patient_id is None and task is not None and data.get("patient_id"):
                # Attempt to set patient_id on the task for future lookups
                raw_pid = data.get("patient_id", "")
                try:
                    import uuid as _uuid
                    task.patient_id = _uuid.UUID(str(raw_pid))
                    patient_id = task.patient_id
                    await session.flush()
                except (ValueError, AttributeError):
                    pass

            if patient_id is None:
                error_msg = (
                    f"Cannot create PA record for task {task_id}: "
                    "no patient_id available on AgentTask or in input_data"
                )
                logger.error(error_msg)
                await _audit_pa_stage(task_id, "init", "pa_record_creation_failed", {
                    "error": error_msg,
                })
                return asdict(ActivityResult(
                    success=False,
                    error=error_msg,
                ))

            existing = await session.execute(
                select(PriorAuthRequest).where(PriorAuthRequest.task_id == task_id)
            )
            if existing.scalar_one_or_none() is None:
                pa_record = PriorAuthRequest(
                    task_id=task_id,
                    patient_id=patient_id,
                    status="pending",
                    procedure_code=data.get("procedure_code", ""),
                    diagnosis_codes=data.get("diagnosis_codes", []),
                    clinical_info={},
                    submission_channel="",
                )
                session.add(pa_record)
                await session.commit()

        await _audit_pa_stage(task_id, "init", "pa_record_created", {
            "patient_id": str(patient_id) if patient_id else None,
        })
    except Exception as exc:
        logger.error("Failed to create pending PA record for task %s: %s", task_id, exc)
        raise  # Let Temporal retry the activity
    finally:
        if engine_to_dispose is not None:
            await engine_to_dispose.dispose()

    return asdict(ActivityResult(success=True, data={"task_id": task_id, "patient_id": str(patient_id)}))


@activity.defn
async def execute_prior_auth_agent(validated_input: dict[str, Any]) -> dict[str, Any]:
    """Run the prior auth agent via its LangGraph graph.

    Creates an independent DB session so the agent's graph nodes (e.g.
    ``check_pa_required_node``) can query payer rules via the rule engine.
    Also resolves payer code strings to UUIDs for rule engine compatibility.
    """
    restore_correlation_id(validated_input)
    from app.agents.prior_auth.graph import PriorAuthAgent
    from app.core.engine.llm_provider import LLMProvider, MockLLMBackend

    data = validated_input.get("data", {})

    try:
        from app.core.engine.llm_provider import BedrockBackend
        llm_backend = BedrockBackend()
    except Exception:
        llm_backend = MockLLMBackend(responses=[
            '{"confidence": 0.85, "decision": {"pa_status": "pending"}, "tool_calls": []}'
        ])

    llm_provider = LLMProvider(primary=llm_backend, phi_safe=True)

    # Obtain a DB session so the agent can use the payer rule engine.
    factory, engine_to_dispose = _get_activity_session_factory()
    session = None
    try:
        session = factory()
        db_session = await session.__aenter__()

        # Resolve payer_id from code to UUID if necessary, so the rule
        # engine can match against payer_rules.payer_id (a UUID FK).
        payer_id_str = data.get("payer_id", "")
        if payer_id_str:
            resolved_payer_id = await _resolve_payer_id(db_session, payer_id_str)
            if resolved_payer_id:
                data["payer_id"] = resolved_payer_id

        agent = PriorAuthAgent(llm_provider=llm_provider, session=db_session)

        patient_context = {
            "patient_id": data.get("patient_id", ""),
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

        await db_session.commit()
    except Exception:
        if session is not None:
            try:
                await db_session.rollback()
            except Exception:
                pass
        raise
    finally:
        if session is not None:
            try:
                await db_session.__aexit__(None, None, None)
            except Exception:
                pass
        if engine_to_dispose is not None:
            await engine_to_dispose.dispose()

    agent_decision = {
        "pa_required": state.get("pa_required", True),
        "pa_status": state.get("pa_status", "unknown"),
        "authorization_number": state.get("authorization_number", ""),
        "submission_channel": state.get("submission_channel", ""),
        "confidence": state.get("confidence", 0.0),
        "needs_review": state.get("needs_review", False),
        "review_reason": state.get("review_reason", ""),
        "decision": state.get("decision", {}),
        "clinical_summary": state.get("clinical_summary", {}),
        "clinical_evidence": state.get("clinical_evidence", {}),
        "appeal_letter": state.get("appeal_letter", ""),
        "appeal_package": state.get("appeal_package", {}),
        "peer_to_peer_brief": state.get("peer_to_peer_brief", {}),
        "evidence_gaps": state.get("evidence_gaps", []),
        "x12_278_result": state.get("x12_278_result", {}),
        "davinci_pas_result": state.get("davinci_pas_result", {}),
        "submission_result": state.get("submission_result", {}),
    }

    # Promote lifecycle dates from submission_result to top-level so
    # write_prior_auth_result can persist them to the DB model.
    submission_result = agent_decision.get("submission_result", {})
    if submission_result.get("effective_date") and not agent_decision.get("effective_date"):
        agent_decision["effective_date"] = submission_result["effective_date"]
    if submission_result.get("expiration_date") and not agent_decision.get("expiration_date"):
        agent_decision["expiration_date"] = submission_result["expiration_date"]

    task_id = data.get("task_id", "")

    # Persist per-node audit events from the agent's audit trail
    # This mirrors the in-memory audit trail to the database for complete
    # lifecycle tracking even though the agent ran without a DB session.
    audit_trail = state.get("audit_trail", [])
    for entry in audit_trail:
        node_name = entry.get("node", "") if isinstance(entry, dict) else getattr(entry, "node", "")
        action = entry.get("action", "") if isinstance(entry, dict) else getattr(entry, "action", "")
        details = entry.get("details", {}) if isinstance(entry, dict) else getattr(entry, "details", {})
        await _audit_pa_stage(
            task_id,
            f"agent_node:{node_name}",
            action,
            details if isinstance(details, dict) else {},
        )

    await _audit_pa_stage(task_id, "agent", "agent_reasoning_complete", {
        "pa_required": agent_decision.get("pa_required", True),
        "pa_status": agent_decision.get("pa_status", "unknown"),
        "confidence": agent_decision.get("confidence", 0.0),
        "needs_review": agent_decision.get("needs_review", False),
        "nodes_executed": len(audit_trail),
    })

    return asdict(ActivityResult(
        success=True,
        data={
            **data,
            "agent_decision": agent_decision,
        },
    ))


@activity.defn
async def write_prior_auth_result(args: dict[str, Any]) -> dict[str, Any]:
    """Write the PA result to the database."""
    restore_correlation_id(args)
    task_id = args.get("task_id", "")
    agent_decision = args.get("agent_decision", {})

    safe_heartbeat("writing_pa_result")

    factory, engine_to_dispose = _get_activity_session_factory()
    try:
        async with factory() as session:
            from app.models.prior_auth import PriorAuthRequest, PriorAuthAppeal
            from app.models.agent_task import AgentTask
            from sqlalchemy import select

            # Update or create the PA record (upsert)
            existing_result = await session.execute(
                select(PriorAuthRequest).where(PriorAuthRequest.task_id == task_id)
            )
            pa_record = existing_result.scalar_one_or_none()

            pa_status = agent_decision.get("pa_status", "unknown")
            status_map = {
                "approved": "approved",
                "denied": "denied",
                "pended": "pended",
                "pending": "pending",
                "cancelled": "cancelled",
                "submission_failed": "failed",
            }

            if pa_record is None:
                # Upsert: create the record if it was not created earlier
                # (recovery path for when create_pending_pa_record failed)
                task_result = await session.execute(
                    select(AgentTask).where(AgentTask.id == task_id)
                )
                task = task_result.scalar_one_or_none()
                patient_id = task.patient_id if task else None

                if patient_id is None:
                    import uuid as _uuid
                    raw_pid = agent_decision.get("patient_id", "")
                    if raw_pid:
                        try:
                            patient_id = _uuid.UUID(str(raw_pid))
                        except (ValueError, AttributeError):
                            pass

                if patient_id is not None:
                    pa_record = PriorAuthRequest(
                        task_id=task_id,
                        patient_id=patient_id,
                        status=status_map.get(pa_status, "pending"),
                        procedure_code=agent_decision.get("procedure_code",
                                        agent_decision.get("decision", {}).get("procedure_code", "")),
                        diagnosis_codes=agent_decision.get("diagnosis_codes", []),
                        clinical_info=agent_decision.get("clinical_evidence", {}),
                        submission_channel=agent_decision.get("submission_channel", ""),
                        auth_number=agent_decision.get("authorization_number", ""),
                        determination=pa_status,
                        request_data_278=agent_decision.get("x12_278_result", {}),
                    )
                    session.add(pa_record)
                    await session.flush()
                    logger.info("Created PA record via upsert for task %s", task_id)
                else:
                    logger.error("Cannot upsert PA record for task %s: no patient_id", task_id)

            if pa_record is not None:
                pa_record.status = status_map.get(pa_status, "pending")
                pa_record.submission_channel = agent_decision.get("submission_channel", "")
                pa_record.auth_number = agent_decision.get("authorization_number", "")
                pa_record.determination = pa_status
                pa_record.clinical_info = agent_decision.get("clinical_evidence", {})
                pa_record.request_data_278 = agent_decision.get("x12_278_result", {})

                # Persist lifecycle date fields when available (from poll
                # results or initial determination).
                from datetime import date as _date_type
                eff_raw = agent_decision.get("effective_date", "")
                exp_raw = agent_decision.get("expiration_date", "")
                if eff_raw:
                    if isinstance(eff_raw, _date_type):
                        pa_record.effective_date = eff_raw
                    elif isinstance(eff_raw, str) and eff_raw:
                        try:
                            pa_record.effective_date = _date_type.fromisoformat(eff_raw)
                        except (ValueError, TypeError):
                            pass
                if exp_raw:
                    if isinstance(exp_raw, _date_type):
                        pa_record.expiration_date = exp_raw
                    elif isinstance(exp_raw, str) and exp_raw:
                        try:
                            pa_record.expiration_date = _date_type.fromisoformat(exp_raw)
                        except (ValueError, TypeError):
                            pass

                # If denied with appeal letter, create appeal record with
                # full denial/appeal package including evidence gaps,
                # attachment manifest, and peer-to-peer brief.
                appeal_letter = agent_decision.get("appeal_letter", "")
                if pa_status == "denied" and appeal_letter:
                    appeal_package = agent_decision.get("appeal_package", {})
                    outcome_details = {
                        "evidence_gaps": agent_decision.get("evidence_gaps",
                                         appeal_package.get("evidence_gaps", [])),
                        "attachment_manifest": agent_decision.get("attachment_manifest",
                                               appeal_package.get("attachment_manifest", [])),
                        "peer_to_peer_brief": agent_decision.get("peer_to_peer_brief",
                                              appeal_package.get("peer_to_peer_brief", {})),
                        "evidence_cited": appeal_package.get("evidence_cited", {}),
                        "clinical_references": appeal_package.get("clinical_references", []),
                        "denial_category": appeal_package.get("denial_category",
                                           agent_decision.get("denial_category", "")),
                    }
                    appeal = PriorAuthAppeal(
                        prior_auth_id=pa_record.id,
                        appeal_level=1,
                        status="draft",
                        appeal_letter=appeal_letter,
                        clinical_evidence=agent_decision.get("clinical_evidence", {}),
                        outcome_details=outcome_details,
                    )
                    session.add(appeal)

            await session.commit()

            await _audit_pa_stage(task_id, "write_result", "pa_result_persisted", {
                "pa_status": pa_status,
                "has_appeal": bool(agent_decision.get("appeal_letter")),
                "authorization_number": agent_decision.get("authorization_number", ""),
            })
    except Exception as exc:
        logger.error("Failed to persist PA result to DB: %s", exc)
        raise
    finally:
        if engine_to_dispose is not None:
            await engine_to_dispose.dispose()

    return asdict(ActivityResult(
        success=True,
        data={
            "task_id": task_id,
            "pa_status": agent_decision.get("pa_status", "unknown"),
            "authorization_number": agent_decision.get("authorization_number", ""),
            "confidence": agent_decision.get("confidence", 0.0),
            "needs_review": agent_decision.get("needs_review", False),
            "review_reason": agent_decision.get("review_reason", ""),
            "clinical_summary": agent_decision.get("clinical_summary", {}),
            "appeal_letter": agent_decision.get("appeal_letter", ""),
        },
    ))


@activity.defn
async def poll_pa_status_activity(args: dict[str, Any]) -> dict[str, Any]:
    """Temporal activity that polls PA status via clearinghouse.

    Called repeatedly by the PriorAuthWorkflow polling loop with
    workflow.sleep() intervals between calls.
    """
    restore_correlation_id(args)
    task_id = args.get("task_id", "")
    transaction_id = args.get("transaction_id", "")
    payer_id = args.get("payer_id", "")

    safe_heartbeat("polling_pa_status")

    from app.agents.prior_auth.tools import poll_pa_status as _poll_pa_status

    status_result = await _poll_pa_status(
        transaction_id=transaction_id,
        payer_id=payer_id,
    )

    await _audit_pa_stage(task_id, "poll_status", "status_polled", {
        "transaction_id": transaction_id,
        "status": status_result.get("status", "unknown"),
        "authorization_number": status_result.get("authorization_number", ""),
    })

    return asdict(ActivityResult(
        success=True,
        data={
            "status": status_result.get("status", "unknown"),
            "authorization_number": status_result.get("authorization_number", ""),
            "determination_reason": status_result.get("determination_reason", ""),
            "effective_date": status_result.get("effective_date", ""),
        },
    ))


@activity.defn
async def generate_post_poll_appeal(args: dict[str, Any]) -> dict[str, Any]:
    """Generate appeal artifacts when a PA is denied after status polling.

    This activity runs the full denial-analysis → evidence-gap identification →
    appeal letter drafting → clinical reference attachment flow — the same logic
    as ``handle_denial_node`` + ``generate_appeal_node`` in the agent graph — so
    post-poll denials receive identical treatment to denials detected during the
    initial agent run.
    """
    restore_correlation_id(args)
    task_id = args.get("task_id", "")
    input_data = args.get("input_data", {})
    determination_reason = args.get("determination_reason", "Not specified")
    authorization_number = args.get("authorization_number", "")

    safe_heartbeat("generating_post_poll_appeal")

    try:
        from app.agents.prior_auth.tools import (
            gather_clinical_documents,
            generate_appeal_letter,
            generate_peer_to_peer_brief,
        )
        from app.agents.prior_auth.graph import (
            _categorize_denial,
            _identify_evidence_gaps,
            _build_attachment_manifest,
        )

        # Step 1: Gather clinical evidence (same as agent graph)
        clinical_evidence = await gather_clinical_documents(
            patient_id=input_data.get("patient_id", ""),
            fhir_base_url=input_data.get("fhir_base_url", ""),
            auth_token=input_data.get("fhir_auth_token", ""),
        )

        # Step 2: Denial analysis — categorize and identify evidence gaps
        # (mirrors handle_denial_node logic)
        denial_category = _categorize_denial(determination_reason)
        evidence_gaps = _identify_evidence_gaps(
            denial_category=denial_category,
            clinical_evidence=clinical_evidence,
            procedure_code=input_data.get("procedure_code", ""),
        )
        attachment_manifest = _build_attachment_manifest(clinical_evidence, evidence_gaps)

        patient_name = (
            f"{input_data.get('subscriber_first_name', '')} "
            f"{input_data.get('subscriber_last_name', '')}"
        ).strip()

        # Step 3: Generate appeal letter
        import datetime as _dt
        appeal_result = await generate_appeal_letter(
            patient_name=patient_name,
            patient_dob=input_data.get("subscriber_dob", ""),
            procedure_code=input_data.get("procedure_code", ""),
            procedure_description=input_data.get("procedure_description", "Requested procedure"),
            diagnosis_codes=input_data.get("diagnosis_codes", []),
            payer_name=input_data.get("payer_name", ""),
            auth_number=authorization_number,
            denial_reason=determination_reason,
            denial_date=_dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d"),
            clinical_evidence=clinical_evidence,
            payer_policy_reference=input_data.get("payer_policy_reference", ""),
        )

        # Step 4: Generate peer-to-peer brief
        try:
            p2p_brief = await generate_peer_to_peer_brief(
                procedure_code=input_data.get("procedure_code", ""),
                procedure_description=input_data.get("procedure_description", "Requested procedure"),
                diagnosis_codes=input_data.get("diagnosis_codes", []),
                denial_reason=determination_reason,
                clinical_evidence=clinical_evidence,
            )
        except Exception:
            p2p_brief = {}

        # Step 5: Compile appeal package (mirrors generate_appeal_node)
        appeal_letter = appeal_result.get("appeal_letter", "")
        appeal_package = {
            "appeal_letter": appeal_letter,
            "peer_to_peer_brief": p2p_brief,
            "evidence_cited": appeal_result.get("evidence_cited", {}),
            "clinical_references": appeal_result.get("clinical_references", []),
            "evidence_gaps": evidence_gaps,
            "attachment_manifest": attachment_manifest,
        }

        await _audit_pa_stage(task_id, "post_poll_appeal", "appeal_generated_after_poll", {
            "determination_reason": determination_reason,
            "denial_category": denial_category,
            "appeal_letter_length": len(appeal_letter),
            "has_peer_to_peer_brief": bool(p2p_brief),
            "evidence_gaps_count": len(evidence_gaps),
            "attachment_manifest_count": len(attachment_manifest),
        })

        return asdict(ActivityResult(
            success=True,
            data={
                "appeal_letter": appeal_letter,
                "appeal_package": appeal_package,
                "peer_to_peer_brief": p2p_brief,
                "evidence_gaps": evidence_gaps,
                "attachment_manifest": attachment_manifest,
                "denial_category": denial_category,
            },
        ))
    except Exception as exc:
        logger.error("Failed to generate post-poll appeal for task %s: %s", task_id, exc)
        # Return a minimal result so the workflow can still proceed with HITL review
        await _audit_pa_stage(task_id, "post_poll_appeal", "appeal_generation_failed", {
            "error": str(exc),
        })
        return asdict(ActivityResult(
            success=True,
            data={
                "appeal_letter": f"[Appeal generation failed: {exc}. Manual appeal required.]",
                "appeal_package": {},
                "peer_to_peer_brief": {},
                "evidence_gaps": [],
                "attachment_manifest": [],
                "denial_category": "unknown",
            },
        ))


# ── Temporal Workflow ─────────────────────────────────────────────────


@workflow.defn
class PriorAuthWorkflow:
    """Temporal workflow for prior authorization.

    Long-running workflow that handles the full PA lifecycle including
    status polling and appeal generation for denials.
    """

    @workflow.run
    async def run(self, workflow_input: WorkflowInput) -> WorkflowResult:
        """Execute the full prior authorization workflow."""
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

        # Step 1: Validate input
        validation_result = await workflow.execute_activity(
            validate_prior_auth_input,
            input_dict,
            start_to_close_timeout=AGENT_ACTIVITY_TIMEOUT,
            retry_policy=AGENT_RETRY_POLICY.to_retry_policy(),
        )
        if not validation_result.get("success"):
            return WorkflowResult(
                task_id=task_id,
                agent_type="prior_auth",
                status=WorkflowStatus.FAILED.value,
                error=validation_result.get("error", "Validation failed"),
            )

        # Step 1b: Create pending PA record — required for workflow integrity.
        # If the record cannot be created (e.g. missing patient_id), the
        # workflow must fail rather than proceed without a durable PA row.
        pa_record_result = await workflow.execute_activity(
            create_pending_pa_record,
            {"task_id": task_id, "data": workflow_input.input_data, "correlation_id": correlation_id},
            start_to_close_timeout=DB_ACTIVITY_TIMEOUT,
            heartbeat_timeout=DB_HEARTBEAT_TIMEOUT,
            retry_policy=DB_RETRY_POLICY.to_retry_policy(),
        )
        if not pa_record_result.get("success"):
            return WorkflowResult(
                task_id=task_id,
                agent_type="prior_auth",
                status=WorkflowStatus.FAILED.value,
                error=pa_record_result.get("error", "Failed to create PA record — missing patient identity"),
            )

        # Step 2: Execute agent (runs full LangGraph pipeline)
        agent_result = await workflow.execute_activity(
            execute_prior_auth_agent,
            validation_result,
            start_to_close_timeout=timedelta(minutes=10),  # PA agent is complex
            retry_policy=AGENT_RETRY_POLICY.to_retry_policy(),
        )
        if not agent_result.get("success"):
            return WorkflowResult(
                task_id=task_id,
                agent_type="prior_auth",
                status=WorkflowStatus.FAILED.value,
                error=agent_result.get("error", "Agent execution failed"),
            )

        # Step 3: Check if PA status is still pending/pended — poll with timer
        agent_data = agent_result.get("data", {})
        agent_decision = agent_data.get("agent_decision", {})
        pa_status = agent_decision.get("pa_status", "unknown")

        if pa_status in ("pending", "pended"):
            # Resolve the transaction_id to poll with.
            # The submission result's transaction_id (e.g. "PA-…") is the correct
            # identifier for polling — NOT the X12 278 control_number, which is
            # merely an EDI envelope sequence number.
            poll_transaction_id = (
                agent_decision.get("submission_result", {}).get("transaction_id", "")
                or agent_decision.get("x12_278_result", {}).get("control_number", "")
            )

            # Long-running polling loop: wait and re-check status
            poll_attempts = 0
            while pa_status in ("pending", "pended") and poll_attempts < PA_MAX_POLL_ATTEMPTS:
                await workflow.sleep(PA_STATUS_POLL_INTERVAL)
                poll_attempts += 1

                poll_result = await workflow.execute_activity(
                    poll_pa_status_activity,
                    {
                        "task_id": task_id,
                        "transaction_id": poll_transaction_id,
                        "payer_id": workflow_input.payer_context.get("payer_id", ""),
                        "correlation_id": correlation_id,
                    },
                    start_to_close_timeout=CLEARINGHOUSE_ACTIVITY_TIMEOUT,
                    heartbeat_timeout=CLEARINGHOUSE_HEARTBEAT_TIMEOUT,
                    retry_policy=CLEARINGHOUSE_RETRY_POLICY.to_retry_policy(),
                )

                poll_data = poll_result.get("data", {})
                pa_status = poll_data.get("status", pa_status)

                # Update agent decision with polled status
                if pa_status not in ("pending", "pended"):
                    agent_decision["pa_status"] = pa_status
                    agent_decision["authorization_number"] = poll_data.get("authorization_number", "")
                    agent_decision["determination_reason"] = poll_data.get("determination_reason", "")
                    # Carry lifecycle dates so they are persisted by write_prior_auth_result
                    if poll_data.get("effective_date"):
                        agent_decision["effective_date"] = poll_data["effective_date"]
                    if poll_data.get("expiration_date"):
                        agent_decision["expiration_date"] = poll_data["expiration_date"]

                    # If denied after polling, generate appeal via dedicated activity
                    if pa_status == "denied":
                        appeal_result = await workflow.execute_activity(
                            generate_post_poll_appeal,
                            {
                                "task_id": task_id,
                                "input_data": workflow_input.input_data,
                                "determination_reason": poll_data.get("determination_reason", "Not specified"),
                                "authorization_number": poll_data.get("authorization_number", ""),
                                "correlation_id": correlation_id,
                            },
                            start_to_close_timeout=AGENT_ACTIVITY_TIMEOUT,
                            retry_policy=AGENT_RETRY_POLICY.to_retry_policy(),
                        )
                        appeal_data = appeal_result.get("data", {})
                        agent_decision["appeal_letter"] = appeal_data.get("appeal_letter", "")
                        agent_decision["appeal_package"] = appeal_data.get("appeal_package", {})
                        agent_decision["peer_to_peer_brief"] = appeal_data.get("peer_to_peer_brief", {})
                        agent_decision["evidence_gaps"] = appeal_data.get("evidence_gaps", [])
                        agent_decision["attachment_manifest"] = appeal_data.get("attachment_manifest", [])
                        agent_decision["needs_review"] = True
                        agent_decision["review_reason"] = (
                            f"Prior authorization denied after polling. "
                            f"Reason: {poll_data.get('determination_reason', 'Not specified')}. "
                            f"Appeal letter generated — requires HITL review."
                        )
                        agent_decision["confidence"] = 0.3

            # If still pending after max polls, flag for review
            if pa_status in ("pending", "pended"):
                agent_decision["needs_review"] = True
                agent_decision["review_reason"] = (
                    f"PA status still '{pa_status}' after {poll_attempts} poll attempts "
                    f"(~{poll_attempts * 4} hours). Manual follow-up required."
                )
                agent_decision["confidence"] = 0.4

        # Step 4: Write result to DB
        write_args = {
            "task_id": task_id,
            "agent_decision": agent_decision,
            "correlation_id": correlation_id,
        }
        write_result = await workflow.execute_activity(
            write_prior_auth_result,
            write_args,
            start_to_close_timeout=DB_ACTIVITY_TIMEOUT,
            heartbeat_timeout=DB_HEARTBEAT_TIMEOUT,
            retry_policy=DB_RETRY_POLICY.to_retry_policy(),
        )

        result_data = write_result.get("data", {})
        return WorkflowResult(
            task_id=task_id,
            agent_type="prior_auth",
            status=WorkflowStatus.COMPLETED.value,
            output_data=result_data,
            confidence=result_data.get("confidence", 0.0),
            needs_review=result_data.get("needs_review", False),
            review_reason=result_data.get("review_reason", ""),
        )


# ── Inline Orchestration ─────────────────────────────────────────────


async def run_prior_auth_workflow(workflow_input: WorkflowInput) -> WorkflowResult:
    """Execute the PA workflow inline without Temporal.

    For testing and local development. In production, use PriorAuthWorkflow
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

    # Step 1: Validate
    validation_result = await validate_prior_auth_input(input_dict)
    if not validation_result.get("success"):
        return WorkflowResult(
            task_id=task_id,
            agent_type="prior_auth",
            status=WorkflowStatus.FAILED.value,
            error=validation_result.get("error", "Validation failed"),
        )

    # Step 1b: Create pending record — hard failure if patient identity missing
    pa_record_result = await create_pending_pa_record(
        {"task_id": task_id, "data": workflow_input.input_data}
    )
    if not pa_record_result.get("success"):
        return WorkflowResult(
            task_id=task_id,
            agent_type="prior_auth",
            status=WorkflowStatus.FAILED.value,
            error=pa_record_result.get("error", "Failed to create PA record — missing patient identity"),
        )

    # Step 2: Execute agent
    agent_result = await execute_prior_auth_agent(validation_result)
    if not agent_result.get("success"):
        return WorkflowResult(
            task_id=task_id,
            agent_type="prior_auth",
            status=WorkflowStatus.FAILED.value,
            error=agent_result.get("error", "Agent execution failed"),
        )

    # Step 3: Inline polling loop (mirrors Temporal workflow behavior)
    agent_data = agent_result.get("data", {})
    agent_decision = agent_data.get("agent_decision", {})
    pa_status = agent_decision.get("pa_status", "unknown")

    # Inline polling configuration — shorter intervals than Temporal since
    # inline mode is typically used in dev/test, but the logic is identical.
    INLINE_POLL_INTERVAL_SECS = 5  # seconds (vs 4 hours in Temporal)
    INLINE_MAX_POLL_ATTEMPTS = 10

    if pa_status in ("pending", "pended"):
        import asyncio as _asyncio
        from app.agents.prior_auth.tools import poll_pa_status as _poll_pa_status

        poll_transaction_id = (
            agent_decision.get("submission_result", {}).get("transaction_id", "")
            or agent_decision.get("x12_278_result", {}).get("control_number", "")
        )
        payer_id = workflow_input.payer_context.get("payer_id", "")

        poll_attempts = 0
        while pa_status in ("pending", "pended") and poll_attempts < INLINE_MAX_POLL_ATTEMPTS:
            await _asyncio.sleep(INLINE_POLL_INTERVAL_SECS)
            poll_attempts += 1

            try:
                poll_result = await _poll_pa_status(
                    transaction_id=poll_transaction_id,
                    payer_id=payer_id,
                )
                pa_status = poll_result.get("status", pa_status)

                if pa_status not in ("pending", "pended"):
                    agent_decision["pa_status"] = pa_status
                    agent_decision["authorization_number"] = poll_result.get("authorization_number", "")
                    agent_decision["determination_reason"] = poll_result.get("determination_reason", "")
                    # Carry lifecycle dates through for persistence
                    if poll_result.get("effective_date"):
                        agent_decision["effective_date"] = poll_result["effective_date"]
                    if poll_result.get("expiration_date"):
                        agent_decision["expiration_date"] = poll_result["expiration_date"]

                    # If denied after polling, generate appeal
                    if pa_status == "denied":
                        try:
                            appeal_result = await generate_post_poll_appeal({
                                "task_id": task_id,
                                "input_data": workflow_input.input_data,
                                "determination_reason": poll_result.get("determination_reason", "Not specified"),
                                "authorization_number": poll_result.get("authorization_number", ""),
                            })
                            appeal_data = appeal_result.get("data", {})
                            agent_decision["appeal_letter"] = appeal_data.get("appeal_letter", "")
                            agent_decision["appeal_package"] = appeal_data.get("appeal_package", {})
                            agent_decision["peer_to_peer_brief"] = appeal_data.get("peer_to_peer_brief", {})
                            agent_decision["evidence_gaps"] = appeal_data.get("evidence_gaps", [])
                            agent_decision["attachment_manifest"] = appeal_data.get("attachment_manifest", [])
                        except Exception as exc:
                            logger.warning("Inline appeal generation failed: %s", exc)

                        agent_decision["needs_review"] = True
                        agent_decision["review_reason"] = (
                            f"Prior authorization denied after polling. "
                            f"Reason: {poll_result.get('determination_reason', 'Not specified')}. "
                            f"Appeal letter generated — requires HITL review."
                        )
                        agent_decision["confidence"] = 0.3
            except Exception as exc:
                logger.warning("Inline PA status poll attempt %d failed: %s", poll_attempts, exc)

        # If still pending after max polls, flag for review
        if pa_status in ("pending", "pended"):
            agent_decision["needs_review"] = True
            agent_decision["review_reason"] = (
                f"PA status still '{pa_status}' after {poll_attempts} inline poll attempts. "
                f"Manual follow-up required."
            )
            agent_decision["confidence"] = 0.4

    # Step 4: Write result
    write_args = {
        "task_id": task_id,
        "agent_decision": agent_decision,
    }
    write_result = await write_prior_auth_result(write_args)

    result_data = write_result.get("data", {})
    return WorkflowResult(
        task_id=task_id,
        agent_type="prior_auth",
        status=WorkflowStatus.COMPLETED.value,
        output_data=result_data,
        confidence=result_data.get("confidence", 0.0),
        needs_review=result_data.get("needs_review", False),
        review_reason=result_data.get("review_reason", ""),
    )
