"""Integration tests for the Prior Authorization workflow.

Tests cover the contract-required integration scenarios:
1. Full PA lifecycle: request → requirement check → clinical docs → submit → track → complete
2. PA denial + appeal: request → submit → deny → appeal → HITL review → approve
3. Workflow dispatch: prior_auth uses PriorAuthWorkflow in both Temporal and inline paths
4. DB persistence: prior_auth_requests row always created/updated; appeals on denial
5. Audit: per-lifecycle event logs for all major stages
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_task import AgentTask
from app.models.organization import Organization
from app.models.patient import Patient
from app.models.prior_auth import PriorAuthRequest as PriorAuthRequestModel, PriorAuthAppeal
from app.models.workflow import WorkflowExecution
from app.models.audit import AuditLog


# ── Helpers ────────────────────────────────────────────────────────────


class _NondisposableEngine:
    """Wraps an AsyncEngine but makes dispose() a no-op."""

    def __init__(self, real_engine):
        self._real = real_engine

    def __getattr__(self, name):
        if name == "_real":
            raise AttributeError
        return getattr(self._real, name)

    async def dispose(self):
        pass


async def _create_org(session: AsyncSession) -> Organization:
    unique_npi = f"PA{uuid.uuid4().hex[:8]}"
    org = Organization(name="PA Test Health System", npi=unique_npi, tax_id="99-8765432")
    session.add(org)
    await session.flush()
    return org


async def _create_patient(session: AsyncSession, org_id: uuid.UUID) -> Patient:
    patient = Patient(
        organization_id=org_id,
        mrn=f"MRN-PA-{uuid.uuid4().hex[:6]}",
        first_name="John",
        last_name="Smith",
        date_of_birth=date(1965, 3, 15),
        gender="male",
        insurance_member_id="MEM-12345",
    )
    session.add(patient)
    await session.flush()
    return patient


async def _create_pa_task(
    session: AsyncSession,
    patient_id: uuid.UUID,
    org_id: uuid.UUID,
    input_data: dict | None = None,
) -> AgentTask:
    default_input = {
        "procedure_code": "27447",
        "diagnosis_codes": ["M17.11", "M25.561"],
        "subscriber_id": "MEM-12345",
        "subscriber_first_name": "John",
        "subscriber_last_name": "Smith",
        "subscriber_dob": "19650315",
        "payer_id": "BCBS01",
        "payer_name": "Blue Cross Blue Shield",
        "provider_npi": "1234567890",
        "provider_name": "Dr. Johnson",
        "date_of_service": "20250401",
        "patient_id": str(patient_id),
    }
    task = AgentTask(
        agent_type="prior_auth",
        status="pending",
        patient_id=patient_id,
        organization_id=org_id,
        input_data=input_data or default_input,
    )
    session.add(task)
    await session.flush()
    return task


# ── Test: Workflow dispatch maps prior_auth correctly ─────────────────


class TestPriorAuthWorkflowDispatch:
    """Verify prior_auth is mapped in WorkflowService._WORKFLOW_TYPES
    and _INLINE_RUNNERS."""

    def test_prior_auth_in_workflow_types(self):
        from app.services.workflow_service import _WORKFLOW_TYPES
        from app.workflows.prior_auth import PriorAuthWorkflow

        assert "prior_auth" in _WORKFLOW_TYPES
        assert _WORKFLOW_TYPES["prior_auth"] is PriorAuthWorkflow

    def test_prior_auth_in_inline_runners(self):
        from app.services.workflow_service import _INLINE_RUNNERS
        from app.workflows.prior_auth import run_prior_auth_workflow

        assert "prior_auth" in _INLINE_RUNNERS
        assert _INLINE_RUNNERS["prior_auth"] is run_prior_auth_workflow

    @pytest.mark.asyncio
    async def test_workflow_service_dispatches_prior_auth(self, db_session, test_engine):
        """WorkflowService.start_workflow for prior_auth dispatches inline runner."""
        from app.services.workflow_service import WorkflowService

        org = await _create_org(db_session)
        patient = await _create_patient(db_session, org.id)
        task = await _create_pa_task(db_session, patient.id, org.id)
        await db_session.commit()

        safe_engine = _NondisposableEngine(test_engine)

        # Patch the session factory so inline workflow activities use test DB
        with (
            patch("app.workflows.prior_auth._get_activity_session_factory") as mock_factory,
            patch("app.core.audit.logger.AuditLogger.log", new_callable=AsyncMock),
        ):
            from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession as AS
            factory = async_sessionmaker(safe_engine, class_=AS, expire_on_commit=False)
            mock_factory.return_value = (factory, None)

            svc = WorkflowService(db_session, temporal_client=None)
            execution = await svc.start_workflow(
                agent_type="prior_auth",
                task_id=str(task.id),
                input_data=task.input_data,
                patient_context={"patient_id": str(patient.id)},
                payer_context={"payer_id": "BCBS01"},
            )

            assert execution.agent_type == "prior_auth"
            assert execution.status in ("completed", "running", "review")


# ── Test: Full PA Lifecycle (approved) ──────────────────────────────


class TestPriorAuthApprovedLifecycle:
    """Full lifecycle: request → requirement check → clinical docs → submit → track → complete."""

    @pytest.mark.asyncio
    async def test_pa_approved_lifecycle(self, db_session, test_engine):
        from app.workflows.prior_auth import run_prior_auth_workflow
        from app.workflows.base import WorkflowInput

        org = await _create_org(db_session)
        patient = await _create_patient(db_session, org.id)
        task = await _create_pa_task(db_session, patient.id, org.id)
        await db_session.commit()

        safe_engine = _NondisposableEngine(test_engine)

        with (
            patch("app.workflows.prior_auth._get_activity_session_factory") as mock_factory,
            patch("app.core.audit.logger.AuditLogger.log", new_callable=AsyncMock),
            patch(
                "app.agents.prior_auth.tools.poll_pa_status",
                new_callable=AsyncMock,
                return_value={
                    "success": True,
                    "transaction_id": "PA-TEST-001",
                    "status": "approved",
                    "authorization_number": "AUTH-APPROVED-001",
                    "effective_date": "2025-04-01",
                    "expiration_date": "",
                    "determination_reason": "",
                },
            ),
        ):
            from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession as AS
            factory = async_sessionmaker(safe_engine, class_=AS, expire_on_commit=False)
            mock_factory.return_value = (factory, None)

            wf_input = WorkflowInput(
                task_id=str(task.id),
                agent_type="prior_auth",
                input_data=task.input_data,
                patient_context={"patient_id": str(patient.id)},
                payer_context={"payer_id": "BCBS01"},
            )

            result = await run_prior_auth_workflow(wf_input)

            assert result.status == "completed"
            assert result.agent_type == "prior_auth"

            # Verify PA record in DB
            async with factory() as session:
                pa_result = await session.execute(
                    select(PriorAuthRequestModel).where(
                        PriorAuthRequestModel.task_id == task.id
                    )
                )
                pa_record = pa_result.scalar_one_or_none()
                assert pa_record is not None
                assert pa_record.status == "approved"
                assert pa_record.auth_number == "AUTH-APPROVED-001"


# ── Test: PA Denial + Appeal Lifecycle ──────────────────────────────


class TestPriorAuthDenialAppealLifecycle:
    """Denial flow: request → submit → deny → appeal generated → HITL review."""

    @pytest.mark.asyncio
    async def test_pa_denial_creates_appeal(self, db_session, test_engine):
        from app.workflows.prior_auth import run_prior_auth_workflow
        from app.workflows.base import WorkflowInput

        org = await _create_org(db_session)
        patient = await _create_patient(db_session, org.id)
        task = await _create_pa_task(db_session, patient.id, org.id)
        await db_session.commit()

        safe_engine = _NondisposableEngine(test_engine)

        with (
            patch("app.workflows.prior_auth._get_activity_session_factory") as mock_factory,
            patch("app.core.audit.logger.AuditLogger.log", new_callable=AsyncMock),
            patch(
                "app.agents.prior_auth.tools.poll_pa_status",
                new_callable=AsyncMock,
                return_value={
                    "success": True,
                    "transaction_id": "PA-TEST-DENY-001",
                    "status": "denied",
                    "authorization_number": "",
                    "effective_date": "",
                    "expiration_date": "",
                    "determination_reason": "Medical necessity not established per clinical policy",
                },
            ),
        ):
            from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession as AS
            factory = async_sessionmaker(safe_engine, class_=AS, expire_on_commit=False)
            mock_factory.return_value = (factory, None)

            wf_input = WorkflowInput(
                task_id=str(task.id),
                agent_type="prior_auth",
                input_data=task.input_data,
                patient_context={"patient_id": str(patient.id)},
                payer_context={"payer_id": "BCBS01"},
            )

            result = await run_prior_auth_workflow(wf_input)

            assert result.status == "completed"
            assert result.needs_review is True
            assert "denied" in result.review_reason.lower() or "denied" in str(result.output_data).lower()

            # Verify PA record is denied
            async with factory() as session:
                pa_result = await session.execute(
                    select(PriorAuthRequestModel).where(
                        PriorAuthRequestModel.task_id == task.id
                    )
                )
                pa_record = pa_result.scalar_one_or_none()
                assert pa_record is not None
                assert pa_record.status == "denied"

                # Verify appeal record was created
                appeal_result = await session.execute(
                    select(PriorAuthAppeal).where(
                        PriorAuthAppeal.prior_auth_id == pa_record.id
                    )
                )
                appeal = appeal_result.scalar_one_or_none()
                assert appeal is not None
                assert appeal.appeal_level == 1
                assert appeal.status == "draft"
                assert appeal.appeal_letter is not None
                assert len(appeal.appeal_letter) > 100  # Non-trivial letter
                assert "medical necessity" in appeal.appeal_letter.lower()


# ── Test: Denial + Appeal + HITL Review + Approve ─────────────────


class TestPriorAuthDenialAppealHITLApprove:
    """Full denial lifecycle: request → submit → deny → appeal → HITL review → approve."""

    @pytest.mark.asyncio
    async def test_pa_denial_appeal_hitl_approve(self, db_session, test_engine):
        """End-to-end: denied PA → appeal generated → HITL review created → reviewer approves."""
        from app.workflows.prior_auth import run_prior_auth_workflow
        from app.workflows.base import WorkflowInput
        from app.core.hitl.review_queue import ReviewQueue
        from app.models.hitl_review import HITLReview

        org = await _create_org(db_session)
        patient = await _create_patient(db_session, org.id)
        task = await _create_pa_task(db_session, patient.id, org.id)
        await db_session.commit()

        safe_engine = _NondisposableEngine(test_engine)

        with (
            patch("app.workflows.prior_auth._get_activity_session_factory") as mock_factory,
            patch("app.core.audit.logger.AuditLogger.log", new_callable=AsyncMock),
            patch(
                "app.agents.prior_auth.tools.poll_pa_status",
                new_callable=AsyncMock,
                return_value={
                    "success": True,
                    "transaction_id": "PA-TEST-DENY-HITL",
                    "status": "denied",
                    "authorization_number": "",
                    "effective_date": "",
                    "expiration_date": "",
                    "determination_reason": "Medical necessity not established per clinical policy",
                },
            ),
        ):
            from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession as AS
            factory = async_sessionmaker(safe_engine, class_=AS, expire_on_commit=False)
            mock_factory.return_value = (factory, None)

            wf_input = WorkflowInput(
                task_id=str(task.id),
                agent_type="prior_auth",
                input_data=task.input_data,
                patient_context={"patient_id": str(patient.id)},
                payer_context={"payer_id": "BCBS01"},
            )

            result = await run_prior_auth_workflow(wf_input)

            # Step 1: Verify workflow completed with review flag
            assert result.status == "completed"
            assert result.needs_review is True

            # Step 2: Verify appeal was created in DB
            async with factory() as session:
                pa_result = await session.execute(
                    select(PriorAuthRequestModel).where(
                        PriorAuthRequestModel.task_id == task.id
                    )
                )
                pa_record = pa_result.scalar_one_or_none()
                assert pa_record is not None
                assert pa_record.status == "denied"

                appeal_result = await session.execute(
                    select(PriorAuthAppeal).where(
                        PriorAuthAppeal.prior_auth_id == pa_record.id
                    )
                )
                appeal = appeal_result.scalar_one_or_none()
                assert appeal is not None
                assert len(appeal.appeal_letter) > 100

            # Step 3: Create HITL review (simulating what the agent service does)
            async with factory() as review_session:
                review_queue = ReviewQueue(review_session)
                review = await review_queue.create(
                    task_id=str(task.id),
                    reason=result.review_reason,
                    agent_decision=result.output_data,
                    confidence_score=result.confidence,
                )
                await review_session.commit()
                review_id = review.id

            # Step 4: Verify review exists and is pending
            async with factory() as check_session:
                review_result = await check_session.execute(
                    select(HITLReview).where(HITLReview.id == review_id)
                )
                pending_review = review_result.scalar_one_or_none()
                assert pending_review is not None
                assert pending_review.status == "pending"
                # The review reason may reference either the denial or
                # the low confidence threshold that triggered escalation
                # (both are valid — the idempotency guard returns the
                # first review created for this task).
                reason_lower = pending_review.reason.lower()
                assert "denied" in reason_lower or "confidence" in reason_lower

            # Step 5: Approve the review
            async with factory() as approve_session:
                approve_queue = ReviewQueue(approve_session)
                approved = await approve_queue.approve(
                    review_id=str(review_id),
                    reviewer_id=str(uuid.uuid4()),
                    notes="Appeal letter reviewed and approved for submission.",
                )
                await approve_session.commit()

            # Step 6: Verify review is now approved
            async with factory() as final_session:
                final_result = await final_session.execute(
                    select(HITLReview).where(HITLReview.id == review_id)
                )
                final_review = final_result.scalar_one_or_none()
                assert final_review is not None
                assert final_review.status == "approved"
                assert final_review.reviewer_notes == "Appeal letter reviewed and approved for submission."


# ── Test: Audit trail completeness ──────────────────────────────────


class TestPriorAuthAuditTrail:
    """Verify that per-lifecycle event audit logs are persisted."""

    @pytest.mark.asyncio
    async def test_audit_events_logged_for_all_stages(self, db_session, test_engine):
        from app.workflows.prior_auth import run_prior_auth_workflow
        from app.workflows.base import WorkflowInput

        org = await _create_org(db_session)
        patient = await _create_patient(db_session, org.id)
        task = await _create_pa_task(db_session, patient.id, org.id)
        await db_session.commit()

        safe_engine = _NondisposableEngine(test_engine)
        audit_calls = []

        async def _capture_audit(*args, **kwargs):
            audit_calls.append({"args": args, "kwargs": kwargs})

        with (
            patch("app.workflows.prior_auth._get_activity_session_factory") as mock_factory,
            patch("app.workflows.prior_auth._audit_pa_stage") as mock_audit,
            patch(
                "app.agents.prior_auth.tools.poll_pa_status",
                new_callable=AsyncMock,
                return_value={
                    "success": True,
                    "transaction_id": "PA-AUDIT-001",
                    "status": "approved",
                    "authorization_number": "AUTH-AUDIT-001",
                    "effective_date": "2025-04-01",
                    "expiration_date": "",
                    "determination_reason": "",
                },
            ),
        ):
            mock_audit.side_effect = _capture_audit

            from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession as AS
            factory = async_sessionmaker(safe_engine, class_=AS, expire_on_commit=False)
            mock_factory.return_value = (factory, None)

            wf_input = WorkflowInput(
                task_id=str(task.id),
                agent_type="prior_auth",
                input_data=task.input_data,
                patient_context={"patient_id": str(patient.id)},
                payer_context={"payer_id": "BCBS01"},
            )

            result = await run_prior_auth_workflow(wf_input)
            assert result.status == "completed"

            # Verify audit was called for multiple stages
            assert mock_audit.call_count >= 5  # validate, init, agent nodes, agent complete, write_result

            # Extract the action names from audit calls
            audit_actions = [call.args[2] if len(call.args) > 2 else "" for call in mock_audit.call_args_list]
            assert "input_validated" in audit_actions
            assert "pa_record_created" in audit_actions
            assert "agent_reasoning_complete" in audit_actions
            assert "pa_result_persisted" in audit_actions


# ── Test: API route validates prior_auth input ──────────────────────


def _auth_header(role: str = "admin") -> dict[str, str]:
    """Create an Authorization header with a valid JWT."""
    from app.core.auth.jwt import create_access_token
    token = create_access_token(
        user_id=uuid.uuid4(),
        email="test@slate.health",
        role=role,
        full_name="Test User",
    )
    return {"Authorization": f"Bearer {token}"}


class TestPriorAuthAPIValidation:
    """Verify that POST /api/v1/agents/prior_auth/tasks validates via PriorAuthRequest schema."""

    @pytest.mark.asyncio
    async def test_prior_auth_invalid_input_returns_422(self, client):
        """Submitting prior_auth task without required fields returns 422."""
        response = await client.post(
            "/api/v1/agents/prior_auth/tasks",
            json={
                "input_data": {
                    # Missing procedure_code, subscriber_id, patient_id, etc.
                    "payer_id": "BCBS01",
                },
            },
            headers=_auth_header(),
        )
        assert response.status_code == 422
        assert "prior_auth" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_prior_auth_missing_patient_id_returns_422(self, client):
        """Submitting prior_auth task without patient_id returns 422."""
        response = await client.post(
            "/api/v1/agents/prior_auth/tasks",
            json={
                "input_data": {
                    "procedure_code": "27447",
                    "subscriber_id": "MEM-12345",
                    "subscriber_first_name": "John",
                    "subscriber_last_name": "Smith",
                    "payer_id": "BCBS01",
                },
            },
            headers=_auth_header(),
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_prior_auth_missing_payer_id_returns_422(self, client):
        """Submitting prior_auth task without payer_id returns 422."""
        response = await client.post(
            "/api/v1/agents/prior_auth/tasks",
            json={
                "input_data": {
                    "procedure_code": "27447",
                    "subscriber_id": "MEM-12345",
                    "subscriber_first_name": "John",
                    "subscriber_last_name": "Smith",
                    "patient_id": str(uuid.uuid4()),
                },
            },
            headers=_auth_header(),
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_prior_auth_valid_input_accepted(self, client):
        """Submitting prior_auth task with valid fields is accepted."""
        response = await client.post(
            "/api/v1/agents/prior_auth/tasks",
            json={
                "input_data": {
                    "procedure_code": "27447",
                    "subscriber_id": "MEM-12345",
                    "subscriber_first_name": "John",
                    "subscriber_last_name": "Smith",
                    "payer_id": "BCBS01",
                    "patient_id": str(uuid.uuid4()),
                },
            },
            headers=_auth_header(),
        )
        assert response.status_code == 201
        data = response.json()
        assert data["agent_type"] == "prior_auth"
        assert data["status"] in ("pending", "running", "completed", "review", "failed")

    @pytest.mark.asyncio
    async def test_prior_auth_invalid_uuid_patient_id_returns_422(self, client):
        """Submitting prior_auth task with non-UUID patient_id returns 422."""
        response = await client.post(
            "/api/v1/agents/prior_auth/tasks",
            json={
                "input_data": {
                    "procedure_code": "27447",
                    "subscriber_id": "MEM-12345",
                    "subscriber_first_name": "John",
                    "subscriber_last_name": "Smith",
                    "payer_id": "BCBS01",
                    "patient_id": "not-a-uuid",
                },
            },
            headers=_auth_header(),
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_prior_auth_valid_uuid_patient_id_accepted(self, client):
        """Submitting prior_auth task with valid UUID patient_id is accepted."""
        response = await client.post(
            "/api/v1/agents/prior_auth/tasks",
            json={
                "input_data": {
                    "procedure_code": "27447",
                    "subscriber_id": "MEM-12345",
                    "subscriber_first_name": "John",
                    "subscriber_last_name": "Smith",
                    "payer_id": "BCBS01",
                    "patient_id": str(uuid.uuid4()),
                },
            },
            headers=_auth_header(),
        )
        assert response.status_code == 201


# ── Test: Temporal worker activity registration ──────────────────────


class TestTemporalWorkerRegistration:
    """Verify all PA activities are registered in the Temporal worker."""

    def test_all_prior_auth_activities_registered(self):
        """All prior auth activities including generate_post_poll_appeal must be registered."""
        from app.workflows.worker import get_registered_activities
        from app.workflows.prior_auth import (
            validate_prior_auth_input,
            create_pending_pa_record,
            execute_prior_auth_agent,
            write_prior_auth_result,
            poll_pa_status_activity,
            generate_post_poll_appeal,
        )

        activities = get_registered_activities()
        for activity_fn in [
            validate_prior_auth_input,
            create_pending_pa_record,
            execute_prior_auth_agent,
            write_prior_auth_result,
            poll_pa_status_activity,
            generate_post_poll_appeal,
        ]:
            assert activity_fn in activities, (
                f"{activity_fn.__name__} is not registered in the Temporal worker"
            )


# ── Test: HITL auto-creation via WorkflowService for denied PA ────────


class TestPriorAuthHITLAutoCreationViaWorkflowService:
    """Verify that starting a PA workflow through WorkflowService automatically
    creates an HITLReview record when the PA is denied — not manually simulated."""

    @pytest.mark.asyncio
    async def test_denied_pa_auto_creates_hitl_review(self, db_session, test_engine):
        """WorkflowService.start_workflow → denied PA → HITLReview auto-created."""
        from app.services.workflow_service import WorkflowService
        from app.models.hitl_review import HITLReview

        org = await _create_org(db_session)
        patient = await _create_patient(db_session, org.id)
        task = await _create_pa_task(db_session, patient.id, org.id)
        await db_session.commit()

        safe_engine = _NondisposableEngine(test_engine)

        with (
            patch("app.workflows.prior_auth._get_activity_session_factory") as mock_factory,
            patch(
                "app.agents.prior_auth.tools.poll_pa_status",
                new_callable=AsyncMock,
                return_value={
                    "success": True,
                    "transaction_id": "PA-HITL-AUTO-001",
                    "status": "denied",
                    "authorization_number": "",
                    "effective_date": "",
                    "expiration_date": "",
                    "determination_reason": "Medical necessity not established per clinical policy",
                },
            ),
        ):
            from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession as AS
            factory = async_sessionmaker(safe_engine, class_=AS, expire_on_commit=False)
            mock_factory.return_value = (factory, None)

            svc = WorkflowService(db_session, temporal_client=None)
            execution = await svc.start_workflow(
                agent_type="prior_auth",
                task_id=str(task.id),
                input_data=task.input_data,
                patient_context={"patient_id": str(patient.id)},
                payer_context={"payer_id": "BCBS01"},
            )

            # Workflow completed (inline) — check status
            assert execution.status in ("completed", "review")

            # Flush and query for HITL review auto-created by _complete_workflow
            await db_session.flush()

            review_result = await db_session.execute(
                select(HITLReview).where(HITLReview.task_id == task.id)
            )
            review = review_result.scalar_one_or_none()
            assert review is not None, (
                "HITLReview should be auto-created by WorkflowService for denied PA"
            )
            assert review.status == "pending"
            reason_lower = review.reason.lower()
            assert "denied" in reason_lower or "confidence" in reason_lower or "needs_review" in reason_lower

            # Verify the task was set to 'review' status
            refreshed = await db_session.execute(
                select(AgentTask).where(AgentTask.id == task.id)
            )
            refreshed_task = refreshed.scalar_one_or_none()
            assert refreshed_task is not None
            assert refreshed_task.status == "review"


# ── Test: Audit logging with real AuditLogger (reduced mocking) ───────


class TestPriorAuthRealAuditLogging:
    """Verify audit log entries are actually persisted to DB (not mocked)."""

    @pytest.mark.asyncio
    async def test_audit_entries_persisted_to_db(self, db_session, test_engine):
        """PA workflow persists real audit log entries when AuditLogger is not mocked."""
        from app.workflows.prior_auth import run_prior_auth_workflow
        from app.workflows.base import WorkflowInput

        org = await _create_org(db_session)
        patient = await _create_patient(db_session, org.id)
        task = await _create_pa_task(db_session, patient.id, org.id)
        await db_session.commit()

        safe_engine = _NondisposableEngine(test_engine)

        with (
            patch("app.workflows.prior_auth._get_activity_session_factory") as mock_factory,
            # NOTE: AuditLogger.log is NOT mocked — real audit entries are written
            patch(
                "app.agents.prior_auth.tools.poll_pa_status",
                new_callable=AsyncMock,
                return_value={
                    "success": True,
                    "transaction_id": "PA-AUDIT-REAL-001",
                    "status": "approved",
                    "authorization_number": "AUTH-REAL-001",
                    "effective_date": "2025-04-01",
                    "expiration_date": "",
                    "determination_reason": "",
                },
            ),
        ):
            from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession as AS
            factory = async_sessionmaker(safe_engine, class_=AS, expire_on_commit=False)
            mock_factory.return_value = (factory, None)

            wf_input = WorkflowInput(
                task_id=str(task.id),
                agent_type="prior_auth",
                input_data=task.input_data,
                patient_context={"patient_id": str(patient.id)},
                payer_context={"payer_id": "BCBS01"},
            )

            result = await run_prior_auth_workflow(wf_input)
            assert result.status == "completed"

            # Verify real audit entries were persisted to the DB
            async with factory() as session:
                audit_result = await session.execute(
                    select(AuditLog).where(
                        AuditLog.resource_id == str(task.id),
                        AuditLog.action.like("prior_auth_workflow:%"),
                    )
                )
                audit_entries = list(audit_result.scalars().all())

                # Should have multiple audit entries from the real logger
                assert len(audit_entries) >= 3, (
                    f"Expected >=3 audit entries, got {len(audit_entries)}: "
                    f"{[e.action for e in audit_entries]}"
                )

                # Verify key actions are present
                actions = {e.action for e in audit_entries}
                assert "prior_auth_workflow:input_validated" in actions
                assert "prior_auth_workflow:pa_record_created" in actions
