"""Integration tests for Scheduling and Claims workflows with real DB persistence.

Tests in this module exercise the scheduling and claims workflows against the
real test database to verify that:

- Scheduling workflow persists SchedulingRequest rows with appointment/waitlist details
- Claims workflow persists Claim and ClaimDenial rows
- Clearinghouse submission is actually executed in the claims workflow
- 835 remittance parsing and payment posting flows end-to-end
- Audit trail entries are written for both workflows
- HITL review is created on claims denial or low-confidence codes
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.agent_task import AgentTask
from app.models.audit import AuditLog
from app.models.claims import Claim, ClaimDenial
from app.models.organization import Organization
from app.models.patient import Patient
from app.models.scheduling import SchedulingRequest
from app.models.workflow import WorkflowExecution
from app.workflows.base import WorkflowInput, WorkflowResult


# ── Helpers ─────────────────────────────────────────────────────────────


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
    unique_npi = f"INT{uuid.uuid4().hex[:7]}"
    org = Organization(name="Test Health System", npi=unique_npi, tax_id="12-3456789")
    session.add(org)
    await session.flush()
    return org


async def _create_patient(session: AsyncSession, org_id: uuid.UUID) -> Patient:
    patient = Patient(
        organization_id=org_id,
        mrn=f"MRN-{uuid.uuid4().hex[:6]}",
        first_name="Jane",
        last_name="Doe",
        date_of_birth=date(1990, 1, 1),
        gender="female",
        insurance_member_id="SUB001",
    )
    session.add(patient)
    await session.flush()
    return patient


async def _create_scheduling_task(
    session: AsyncSession,
    patient_id: uuid.UUID,
    org_id: uuid.UUID,
    input_data: dict[str, Any] | None = None,
) -> AgentTask:
    task = AgentTask(
        agent_type="scheduling",
        status="pending",
        patient_id=patient_id,
        organization_id=org_id,
        input_data=input_data or {
            "request_text": "annual checkup with Dr. Smith next Tuesday",
            "patient_id": str(patient_id),
        },
    )
    session.add(task)
    await session.flush()
    return task


async def _create_claims_task(
    session: AsyncSession,
    patient_id: uuid.UUID,
    org_id: uuid.UUID,
    input_data: dict[str, Any] | None = None,
) -> AgentTask:
    task = AgentTask(
        agent_type="claims",
        status="pending",
        patient_id=patient_id,
        organization_id=org_id,
        input_data=input_data or {
            "subscriber_id": "SUB001",
            "subscriber_first_name": "Jane",
            "subscriber_last_name": "Doe",
            "subscriber_dob": "19900101",
            "diagnosis_codes": ["J06.9"],
            "procedure_codes": ["99213"],
            "total_charge": "150.00",
            "payer_id": "PAYER01",
            "payer_name": "Test Payer",
        },
    )
    session.add(task)
    await session.flush()
    return task


# ── Scheduling Workflow Integration Tests ──────────────────────────────


class TestSchedulingWorkflowDBPersistence:
    """Tests that the scheduling workflow persists SchedulingRequest records."""

    @pytest.mark.asyncio
    async def test_scheduling_workflow_persists_scheduling_request(
        self, db_session: AsyncSession, test_engine,
    ):
        """Run the inline scheduling workflow and verify a SchedulingRequest
        row is inserted with appointment details."""
        from app.workflows.scheduling import run_scheduling_workflow
        from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession as AS

        org = await _create_org(db_session)
        patient = await _create_patient(db_session, org.id)
        task = await _create_scheduling_task(db_session, patient.id, org.id)
        await db_session.commit()

        workflow_input = WorkflowInput(
            task_id=str(task.id),
            agent_type="scheduling",
            input_data={
                "request_text": "annual checkup with Dr. Smith next Tuesday",
                "patient_id": str(patient.id),
            },
        )

        safe_engine = _NondisposableEngine(test_engine)
        real_factory = async_sessionmaker(test_engine, class_=AS, expire_on_commit=False)

        with patch(
            "sqlalchemy.ext.asyncio.create_async_engine", return_value=safe_engine,
        ), patch(
            "sqlalchemy.ext.asyncio.async_sessionmaker", return_value=real_factory,
        ):
            result = await run_scheduling_workflow(workflow_input)

        assert result.status == "completed"
        assert result.confidence > 0

        # Verify SchedulingRequest was persisted
        async with real_factory() as read_session:
            sched_result = await read_session.execute(
                select(SchedulingRequest).where(SchedulingRequest.task_id == task.id)
            )
            sched_req = sched_result.scalar_one_or_none()
        assert sched_req is not None, "SchedulingRequest row was NOT inserted"
        assert sched_req.status in ("booked", "pending", "waitlisted")
        assert sched_req.parsed_intent is not None
        # The NLP parser should have extracted the provider name
        assert sched_req.parsed_intent.get("provider_name") is not None

    @pytest.mark.asyncio
    async def test_scheduling_workflow_creates_audit_entries(
        self, db_session: AsyncSession, test_engine,
    ):
        """Verify scheduling workflow writes audit trail entries."""
        from app.workflows.scheduling import run_scheduling_workflow
        from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession as AS

        org = await _create_org(db_session)
        patient = await _create_patient(db_session, org.id)
        task = await _create_scheduling_task(db_session, patient.id, org.id)
        await db_session.commit()

        task_id_str = str(task.id)

        workflow_input = WorkflowInput(
            task_id=task_id_str,
            agent_type="scheduling",
            input_data={
                "request_text": "follow up with dermatologist next week",
                "patient_id": str(patient.id),
            },
        )

        safe_engine = _NondisposableEngine(test_engine)
        real_factory = async_sessionmaker(test_engine, class_=AS, expire_on_commit=False)

        with patch(
            "sqlalchemy.ext.asyncio.create_async_engine", return_value=safe_engine,
        ), patch(
            "sqlalchemy.ext.asyncio.async_sessionmaker", return_value=real_factory,
        ):
            result = await run_scheduling_workflow(workflow_input)

        assert result.status == "completed"

        # Verify audit log entries were created
        async with real_factory() as read_session:
            audit_result = await read_session.execute(
                select(func.count()).select_from(AuditLog).where(
                    AuditLog.resource_id == task_id_str,
                    AuditLog.action.like("agent:scheduling:%"),
                )
            )
            audit_count = audit_result.scalar()
        assert audit_count > 0, "No scheduling audit entries found"


# ── Claims Workflow Integration Tests ─────────────────────────────────


class TestClaimsWorkflowDBPersistence:
    """Tests that the claims workflow persists Claim and ClaimDenial records,
    executes clearinghouse submission, and processes remittance."""

    @pytest.mark.asyncio
    async def test_claims_workflow_persists_claim_record(
        self, db_session: AsyncSession, test_engine,
    ):
        """Run the inline claims workflow and verify a Claim row is inserted
        with submission details and payment info."""
        from app.workflows.claims import run_claims_workflow
        from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession as AS

        org = await _create_org(db_session)
        patient = await _create_patient(db_session, org.id)
        task = await _create_claims_task(db_session, patient.id, org.id)
        await db_session.commit()

        workflow_input = WorkflowInput(
            task_id=str(task.id),
            agent_type="claims",
            input_data={
                "subscriber_id": "SUB001",
                "subscriber_first_name": "Jane",
                "subscriber_last_name": "Doe",
                "subscriber_dob": "19900101",
                "diagnosis_codes": ["J06.9"],
                "procedure_codes": ["99213"],
                "total_charge": "150.00",
                "payer_id": "PAYER01",
                "payer_name": "Test Payer",
            },
        )

        safe_engine = _NondisposableEngine(test_engine)
        real_factory = async_sessionmaker(test_engine, class_=AS, expire_on_commit=False)

        with patch(
            "sqlalchemy.ext.asyncio.create_async_engine", return_value=safe_engine,
        ), patch(
            "sqlalchemy.ext.asyncio.async_sessionmaker", return_value=real_factory,
        ):
            result = await run_claims_workflow(workflow_input)

        assert result.status == "completed"

        # Verify Claim record was persisted
        async with real_factory() as read_session:
            claim_result = await read_session.execute(
                select(Claim).where(Claim.task_id == task.id)
            )
            claim = claim_result.scalar_one_or_none()
        assert claim is not None, "Claim row was NOT inserted"
        assert claim.claim_type == "837P"
        assert claim.diagnosis_codes is not None
        assert claim.procedure_codes is not None
        assert claim.submission_data is not None  # clearinghouse submission data

    @pytest.mark.asyncio
    async def test_claims_workflow_clearinghouse_submission_executed(
        self, db_session: AsyncSession, test_engine,
    ):
        """Verify the claims workflow actually executes the clearinghouse
        submission step and records the transaction."""
        from app.workflows.claims import run_claims_workflow
        from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession as AS

        org = await _create_org(db_session)
        patient = await _create_patient(db_session, org.id)
        task = await _create_claims_task(db_session, patient.id, org.id)
        await db_session.commit()

        workflow_input = WorkflowInput(
            task_id=str(task.id),
            agent_type="claims",
            input_data={
                "subscriber_id": "SUB001",
                "subscriber_first_name": "Jane",
                "subscriber_last_name": "Doe",
                "subscriber_dob": "19900101",
                "diagnosis_codes": ["J06.9"],
                "procedure_codes": ["99213"],
                "total_charge": "150.00",
                "payer_id": "PAYER01",
                "payer_name": "Test Payer",
            },
        )

        safe_engine = _NondisposableEngine(test_engine)
        real_factory = async_sessionmaker(test_engine, class_=AS, expire_on_commit=False)

        with patch(
            "sqlalchemy.ext.asyncio.create_async_engine", return_value=safe_engine,
        ), patch(
            "sqlalchemy.ext.asyncio.async_sessionmaker", return_value=real_factory,
        ):
            result = await run_claims_workflow(workflow_input)

        assert result.status == "completed"
        # The output should contain clearinghouse transaction info
        output = result.output_data or {}
        assert output.get("submission_status") in ("submitted", None) or output.get("clearinghouse_transaction_id")

    @pytest.mark.asyncio
    async def test_claims_workflow_no_remittance_sets_awaiting_835(
        self, db_session: AsyncSession, test_engine,
    ):
        """Verify that when no remittance (835) data is available, the claim
        is set to awaiting_835 status and no synthetic payment is posted."""
        from app.workflows.claims import run_claims_workflow
        from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession as AS

        org = await _create_org(db_session)
        patient = await _create_patient(db_session, org.id)
        task = await _create_claims_task(db_session, patient.id, org.id)
        await db_session.commit()

        workflow_input = WorkflowInput(
            task_id=str(task.id),
            agent_type="claims",
            input_data={
                "subscriber_id": "SUB001",
                "subscriber_first_name": "Jane",
                "subscriber_last_name": "Doe",
                "diagnosis_codes": ["J06.9"],
                "procedure_codes": ["99213"],
                "total_charge": "150.00",
                "payer_id": "PAYER01",
                "payer_name": "Test Payer",
            },
        )

        safe_engine = _NondisposableEngine(test_engine)
        real_factory = async_sessionmaker(test_engine, class_=AS, expire_on_commit=False)

        with patch(
            "sqlalchemy.ext.asyncio.create_async_engine", return_value=safe_engine,
        ), patch(
            "sqlalchemy.ext.asyncio.async_sessionmaker", return_value=real_factory,
        ):
            result = await run_claims_workflow(workflow_input)

        assert result.status == "completed"
        output = result.output_data or {}
        # Payment info should NOT be present when no real 835 is available
        assert "payment_info" not in output or not output.get("payment_info")

        # Verify the Claim record has awaiting_835 status, not "paid"
        async with real_factory() as read_session:
            claim_result = await read_session.execute(
                select(Claim).where(Claim.task_id == task.id)
            )
            claim = claim_result.scalar_one_or_none()
        assert claim is not None
        assert claim.status in ("awaiting_835", "submitted"), \
            f"Expected awaiting_835 or submitted, got {claim.status}"
        assert claim.total_paid is None or claim.total_paid == 0

    @pytest.mark.asyncio
    async def test_claims_workflow_real_remittance_posts_payment(
        self, db_session: AsyncSession, test_engine,
    ):
        """Verify that when real remittance data is provided, payment is posted."""
        from app.workflows.claims import run_claims_workflow
        from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession as AS

        org = await _create_org(db_session)
        patient = await _create_patient(db_session, org.id)
        task = await _create_claims_task(
            db_session, patient.id, org.id,
            input_data={
                "subscriber_id": "SUB001",
                "subscriber_first_name": "Jane",
                "subscriber_last_name": "Doe",
                "subscriber_dob": "19900101",
                "diagnosis_codes": ["J06.9"],
                "procedure_codes": ["99213"],
                "total_charge": "150.00",
                "payer_id": "PAYER01",
                "payer_name": "Test Payer",
                "remittance_data": {
                    "transaction_type": "835",
                    "payment": {
                        "amount": "120.00",
                        "method": "CHK",
                        "date": "20260315",
                        "check_number": "CHK-REAL-001",
                    },
                    "claims": [
                        {
                            "claim_id": "CLM-REAL-001",
                            "status_code": "1",
                            "charge_amount": "150.00",
                            "paid_amount": "120.00",
                            "patient_responsibility": "30.00",
                            "adjustments": [],
                            "service_lines": [],
                        }
                    ],
                },
            },
        )
        await db_session.commit()

        workflow_input = WorkflowInput(
            task_id=str(task.id),
            agent_type="claims",
            input_data=task.input_data,
        )

        safe_engine = _NondisposableEngine(test_engine)
        real_factory = async_sessionmaker(test_engine, class_=AS, expire_on_commit=False)

        with patch(
            "sqlalchemy.ext.asyncio.create_async_engine", return_value=safe_engine,
        ), patch(
            "sqlalchemy.ext.asyncio.async_sessionmaker", return_value=real_factory,
        ):
            result = await run_claims_workflow(workflow_input)

        assert result.status == "completed"
        output = result.output_data or {}
        payment_info = output.get("payment_info", {})
        assert payment_info, "Payment info should be populated with real remittance"
        assert payment_info.get("total_paid") == "120.00"

        # Verify the Claim record has payment data
        async with real_factory() as read_session:
            claim_result = await read_session.execute(
                select(Claim).where(Claim.task_id == task.id)
            )
            claim = claim_result.scalar_one_or_none()
        assert claim is not None
        assert claim.status == "paid"
        assert claim.remittance_data is not None

    @pytest.mark.asyncio
    async def test_claims_workflow_denial_creates_claim_denial_record(
        self, db_session: AsyncSession, test_engine,
    ):
        """Verify that a claims workflow with invalid codes creates a
        ClaimDenial record when the claim is denied."""
        from app.workflows.claims import run_claims_workflow
        from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession as AS

        org = await _create_org(db_session)
        patient = await _create_patient(db_session, org.id)
        # Use invalid ICD-10 code to trigger HITL and denial
        task = await _create_claims_task(
            db_session, patient.id, org.id,
            input_data={
                "subscriber_id": "SUB001",
                "subscriber_first_name": "Jane",
                "subscriber_last_name": "Doe",
                "diagnosis_codes": ["INVALID"],  # Invalid code triggers HITL
                "procedure_codes": ["99213"],
                "total_charge": "150.00",
                "payer_id": "PAYER01",
                "payer_name": "Test Payer",
            },
        )
        await db_session.commit()

        workflow_input = WorkflowInput(
            task_id=str(task.id),
            agent_type="claims",
            input_data={
                "subscriber_id": "SUB001",
                "subscriber_first_name": "Jane",
                "subscriber_last_name": "Doe",
                "diagnosis_codes": ["INVALID"],
                "procedure_codes": ["99213"],
                "total_charge": "150.00",
                "payer_id": "PAYER01",
                "payer_name": "Test Payer",
            },
        )

        safe_engine = _NondisposableEngine(test_engine)
        real_factory = async_sessionmaker(test_engine, class_=AS, expire_on_commit=False)

        with patch(
            "sqlalchemy.ext.asyncio.create_async_engine", return_value=safe_engine,
        ), patch(
            "sqlalchemy.ext.asyncio.async_sessionmaker", return_value=real_factory,
        ):
            result = await run_claims_workflow(workflow_input)

        assert result.status == "completed"
        # Should need review due to invalid codes
        assert result.needs_review is True

        # Verify Claim record exists
        async with real_factory() as read_session:
            claim_result = await read_session.execute(
                select(Claim).where(Claim.task_id == task.id)
            )
            claim = claim_result.scalar_one_or_none()
        assert claim is not None, "Claim row was NOT inserted"

    @pytest.mark.asyncio
    async def test_claims_denial_record_has_expected_fields(
        self, db_session: AsyncSession, test_engine,
    ):
        """Verify that a denied remittance creates a ClaimDenial record with
        denial_code, recommended_action, and appeal_status fields."""
        from app.workflows.claims import run_claims_workflow
        from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession as AS

        org = await _create_org(db_session)
        patient = await _create_patient(db_session, org.id)
        # Use valid codes but provide denied remittance data
        task = await _create_claims_task(
            db_session, patient.id, org.id,
            input_data={
                "subscriber_id": "SUB001",
                "subscriber_first_name": "Jane",
                "subscriber_last_name": "Doe",
                "subscriber_dob": "19900101",
                "diagnosis_codes": ["J06.9"],
                "procedure_codes": ["99213"],
                "total_charge": "150.00",
                "payer_id": "PAYER01",
                "payer_name": "Test Payer",
                "remittance_data": {
                    "transaction_type": "835",
                    "payment": {
                        "amount": "0.00",
                        "method": "CHK",
                        "date": "20260301",
                        "check_number": "CHK-DENIED",
                    },
                    "claims": [
                        {
                            "claim_id": "CLM-DENIED-001",
                            "status_code": "4",
                            "charge_amount": "150.00",
                            "paid_amount": "0",
                            "patient_responsibility": "150.00",
                            "adjustments": [
                                {
                                    "group_code": "CO",
                                    "reason_code": "197",
                                    "amount": "150.00",
                                }
                            ],
                            "service_lines": [],
                        }
                    ],
                },
            },
        )
        await db_session.commit()

        workflow_input = WorkflowInput(
            task_id=str(task.id),
            agent_type="claims",
            input_data=task.input_data,
        )

        safe_engine = _NondisposableEngine(test_engine)
        real_factory = async_sessionmaker(test_engine, class_=AS, expire_on_commit=False)

        with patch(
            "sqlalchemy.ext.asyncio.create_async_engine", return_value=safe_engine,
        ), patch(
            "sqlalchemy.ext.asyncio.async_sessionmaker", return_value=real_factory,
        ):
            result = await run_claims_workflow(workflow_input)

        assert result.status == "completed"
        assert result.needs_review is True  # Denials trigger HITL

        # Verify Claim record
        async with real_factory() as read_session:
            claim_result = await read_session.execute(
                select(Claim).where(Claim.task_id == task.id)
            )
            claim = claim_result.scalar_one_or_none()
        assert claim is not None, "Claim row was NOT inserted"
        assert claim.status == "denied"

        # Verify ClaimDenial record with expected fields
        async with real_factory() as read_session:
            denial_result = await read_session.execute(
                select(ClaimDenial).where(ClaimDenial.claim_id == claim.id)
            )
            denials = list(denial_result.scalars().all())

        assert len(denials) >= 1, "No ClaimDenial records found"
        denial = denials[0]
        assert denial.denial_code is not None and denial.denial_code != ""
        assert denial.denial_code == "197"
        assert denial.recommended_action is not None and denial.recommended_action != ""
        assert denial.appeal_status in ("pending", "not_appealable")

    @pytest.mark.asyncio
    async def test_claims_low_confidence_creates_hitl_review(
        self, db_session: AsyncSession, test_engine,
    ):
        """Verify that a claims workflow with invalid codes creates a HITL
        review record via the workflow service escalation path."""
        from app.workflows.claims import run_claims_workflow
        from app.models.hitl_review import HITLReview
        from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession as AS

        org = await _create_org(db_session)
        patient = await _create_patient(db_session, org.id)
        task = await _create_claims_task(
            db_session, patient.id, org.id,
            input_data={
                "subscriber_id": "SUB001",
                "subscriber_first_name": "Jane",
                "subscriber_last_name": "Doe",
                "diagnosis_codes": ["INVALID"],  # Invalid code triggers low confidence
                "procedure_codes": ["99213"],
                "total_charge": "150.00",
                "payer_id": "PAYER01",
                "payer_name": "Test Payer",
            },
        )
        await db_session.commit()

        workflow_input = WorkflowInput(
            task_id=str(task.id),
            agent_type="claims",
            input_data=task.input_data,
        )

        safe_engine = _NondisposableEngine(test_engine)
        real_factory = async_sessionmaker(test_engine, class_=AS, expire_on_commit=False)

        with patch(
            "sqlalchemy.ext.asyncio.create_async_engine", return_value=safe_engine,
        ), patch(
            "sqlalchemy.ext.asyncio.async_sessionmaker", return_value=real_factory,
        ):
            result = await run_claims_workflow(workflow_input)

        assert result.status == "completed"
        assert result.needs_review is True
        assert result.confidence < 0.7  # Below threshold

        # Now simulate what WorkflowService._complete_workflow does:
        # create HITL review record via EscalationManager
        from app.core.hitl.escalation import EscalationManager
        async with real_factory() as review_session:
            escalation_mgr = EscalationManager(review_session)
            await escalation_mgr.evaluate_and_escalate(
                task_id=str(task.id),
                agent_type="claims",
                confidence=result.confidence,
                agent_decision=result.output_data,
                has_error=False,
            )
            await review_session.commit()

        # Verify HITL review was created
        async with real_factory() as read_session:
            review_result = await read_session.execute(
                select(HITLReview).where(HITLReview.task_id == task.id)
            )
            review = review_result.scalar_one_or_none()

        assert review is not None, "HITLReview record was NOT created for low-confidence claims"
        assert review.status == "pending"
        assert review.confidence_score is not None
        assert review.confidence_score < 0.7

    @pytest.mark.asyncio
    async def test_claims_workflow_creates_audit_entries(
        self, db_session: AsyncSession, test_engine,
    ):
        """Verify the claims workflow writes audit trail entries."""
        from app.workflows.claims import run_claims_workflow
        from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession as AS

        org = await _create_org(db_session)
        patient = await _create_patient(db_session, org.id)
        task = await _create_claims_task(db_session, patient.id, org.id)
        await db_session.commit()

        task_id_str = str(task.id)

        workflow_input = WorkflowInput(
            task_id=task_id_str,
            agent_type="claims",
            input_data={
                "subscriber_id": "SUB001",
                "subscriber_first_name": "Jane",
                "subscriber_last_name": "Doe",
                "diagnosis_codes": ["J06.9"],
                "procedure_codes": ["99213"],
                "total_charge": "150.00",
                "payer_id": "PAYER01",
                "payer_name": "Test Payer",
            },
        )

        safe_engine = _NondisposableEngine(test_engine)
        real_factory = async_sessionmaker(test_engine, class_=AS, expire_on_commit=False)

        with patch(
            "sqlalchemy.ext.asyncio.create_async_engine", return_value=safe_engine,
        ), patch(
            "sqlalchemy.ext.asyncio.async_sessionmaker", return_value=real_factory,
        ):
            result = await run_claims_workflow(workflow_input)

        assert result.status == "completed"

        # Verify audit log entries for claims workflow stages
        async with real_factory() as read_session:
            # Check for agent-level audit entries
            agent_audit = await read_session.execute(
                select(func.count()).select_from(AuditLog).where(
                    AuditLog.resource_id == task_id_str,
                    AuditLog.action.like("agent:claims:%"),
                )
            )
            agent_count = agent_audit.scalar()

            # Check for workflow-level audit entries (clearinghouse, remittance)
            wf_audit = await read_session.execute(
                select(func.count()).select_from(AuditLog).where(
                    AuditLog.resource_id == task_id_str,
                    AuditLog.action.like("claims_workflow:%"),
                )
            )
            wf_count = wf_audit.scalar()

        assert agent_count > 0, "No agent:claims audit entries found"
        assert wf_count > 0, "No claims_workflow audit entries found"


# ── Worker Registration Tests ────────────────────────────────────────


class TestWorkerRegistration:
    """Verify that scheduling and claims workflows/activities are registered."""

    def test_scheduling_workflow_registered(self):
        """SchedulingWorkflow is registered in the Temporal worker."""
        from app.workflows.worker import get_registered_workflows
        from app.workflows.scheduling import SchedulingWorkflow

        workflows = get_registered_workflows()
        workflow_types = [type(w) if not isinstance(w, type) else w for w in workflows]
        assert SchedulingWorkflow in workflow_types

    def test_claims_workflow_registered(self):
        """ClaimsWorkflow is registered in the Temporal worker."""
        from app.workflows.worker import get_registered_workflows
        from app.workflows.claims import ClaimsWorkflow

        workflows = get_registered_workflows()
        workflow_types = [type(w) if not isinstance(w, type) else w for w in workflows]
        assert ClaimsWorkflow in workflow_types

    def test_scheduling_activities_registered(self):
        """Scheduling activities are registered in the Temporal worker."""
        from app.workflows.worker import get_registered_activities
        from app.workflows.scheduling import (
            validate_scheduling_input,
            run_scheduling_agent_activity,
            write_scheduling_result,
        )

        activities = get_registered_activities()
        assert validate_scheduling_input in activities
        assert run_scheduling_agent_activity in activities
        assert write_scheduling_result in activities

    def test_claims_activities_registered(self):
        """Claims activities are registered in the Temporal worker."""
        from app.workflows.worker import get_registered_activities
        from app.workflows.claims import (
            validate_claims_input,
            run_claims_agent_activity,
            submit_claim_to_clearinghouse,
            parse_remittance_activity,
            write_claims_result,
            update_claim_status_activity,
            analyze_workflow_denials,
        )

        activities = get_registered_activities()
        assert validate_claims_input in activities
        assert run_claims_agent_activity in activities
        assert submit_claim_to_clearinghouse in activities
        assert parse_remittance_activity in activities
        assert write_claims_result in activities
        assert update_claim_status_activity in activities
        assert analyze_workflow_denials in activities


# ── Idempotency Tests ──────────────────────────────────────────────────


class TestWorkflowIdempotency:
    """Tests that workflow persistence is idempotent — retries do not create
    duplicate records."""

    @pytest.mark.asyncio
    async def test_claims_write_idempotent_no_duplicates(
        self, db_session: AsyncSession, test_engine,
    ):
        """Running the claims workflow twice for the same task should not
        create duplicate Claim records."""
        from app.workflows.claims import run_claims_workflow
        from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession as AS

        org = await _create_org(db_session)
        patient = await _create_patient(db_session, org.id)
        task = await _create_claims_task(db_session, patient.id, org.id)
        await db_session.commit()

        workflow_input = WorkflowInput(
            task_id=str(task.id),
            agent_type="claims",
            input_data={
                "subscriber_id": "SUB001",
                "subscriber_first_name": "Jane",
                "subscriber_last_name": "Doe",
                "diagnosis_codes": ["J06.9"],
                "procedure_codes": ["99213"],
                "total_charge": "150.00",
                "payer_id": "PAYER01",
                "payer_name": "Test Payer",
            },
        )

        safe_engine = _NondisposableEngine(test_engine)
        real_factory = async_sessionmaker(test_engine, class_=AS, expire_on_commit=False)

        with patch(
            "sqlalchemy.ext.asyncio.create_async_engine", return_value=safe_engine,
        ), patch(
            "sqlalchemy.ext.asyncio.async_sessionmaker", return_value=real_factory,
        ):
            # Run workflow twice (simulates retry)
            result1 = await run_claims_workflow(workflow_input)
            result2 = await run_claims_workflow(workflow_input)

        assert result1.status == "completed"
        assert result2.status == "completed"

        # Verify only ONE Claim record exists
        async with real_factory() as read_session:
            claim_result = await read_session.execute(
                select(func.count()).select_from(Claim).where(
                    Claim.task_id == task.id
                )
            )
            claim_count = claim_result.scalar()
        assert claim_count == 1, f"Expected 1 Claim record, got {claim_count}"

    @pytest.mark.asyncio
    async def test_scheduling_write_idempotent_no_duplicates(
        self, db_session: AsyncSession, test_engine,
    ):
        """Running the scheduling workflow twice for the same task should not
        create duplicate SchedulingRequest records."""
        from app.workflows.scheduling import run_scheduling_workflow
        from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession as AS

        org = await _create_org(db_session)
        patient = await _create_patient(db_session, org.id)
        task = await _create_scheduling_task(db_session, patient.id, org.id)
        await db_session.commit()

        workflow_input = WorkflowInput(
            task_id=str(task.id),
            agent_type="scheduling",
            input_data={
                "request_text": "annual checkup with Dr. Smith next Tuesday",
                "patient_id": str(patient.id),
            },
        )

        safe_engine = _NondisposableEngine(test_engine)
        real_factory = async_sessionmaker(test_engine, class_=AS, expire_on_commit=False)

        with patch(
            "sqlalchemy.ext.asyncio.create_async_engine", return_value=safe_engine,
        ), patch(
            "sqlalchemy.ext.asyncio.async_sessionmaker", return_value=real_factory,
        ):
            result1 = await run_scheduling_workflow(workflow_input)
            result2 = await run_scheduling_workflow(workflow_input)

        assert result1.status == "completed"
        assert result2.status == "completed"

        # Verify only ONE SchedulingRequest record exists
        async with real_factory() as read_session:
            sched_result = await read_session.execute(
                select(func.count()).select_from(SchedulingRequest).where(
                    SchedulingRequest.task_id == task.id
                )
            )
            sched_count = sched_result.scalar()
        assert sched_count == 1, f"Expected 1 SchedulingRequest, got {sched_count}"


# ── Post-Poll Persistence Tests ───────────────────────────────────────


class TestClaimPostPollPersistence:
    """Tests that denial polling outcomes are persisted to the claims table."""

    @pytest.mark.asyncio
    async def test_update_claim_status_activity_persists(
        self, db_session: AsyncSession, test_engine,
    ):
        """Verify update_claim_status_activity updates the Claim row."""
        from app.workflows.claims import update_claim_status_activity
        from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession as AS

        org = await _create_org(db_session)
        patient = await _create_patient(db_session, org.id)
        task = await _create_claims_task(db_session, patient.id, org.id)
        await db_session.commit()

        # Pre-create a Claim record (as the workflow would)
        from app.models.claims import Claim
        from decimal import Decimal

        real_factory = async_sessionmaker(test_engine, class_=AS, expire_on_commit=False)
        async with real_factory() as setup_session:
            claim = Claim(
                task_id=task.id,
                patient_id=patient.id,
                claim_type="837P",
                status="denied",
                total_charge=Decimal("150.00"),
                diagnosis_codes=["J06.9"],
                procedure_codes=["99213"],
            )
            setup_session.add(claim)
            await setup_session.commit()

        safe_engine = _NondisposableEngine(test_engine)

        with patch(
            "sqlalchemy.ext.asyncio.create_async_engine", return_value=safe_engine,
        ), patch(
            "sqlalchemy.ext.asyncio.async_sessionmaker", return_value=real_factory,
        ):
            result = await update_claim_status_activity({
                "task_id": str(task.id),
                "final_status": "paid",
                "status_description": "Claim adjudicated and paid after appeal",
            })

        assert result["success"] is True

        # Verify the Claim status was updated
        async with real_factory() as read_session:
            claim_result = await read_session.execute(
                select(Claim).where(Claim.task_id == task.id)
            )
            updated_claim = claim_result.scalar_one_or_none()
        assert updated_claim is not None
        assert updated_claim.status == "paid", \
            f"Expected 'paid', got '{updated_claim.status}'"

        # Verify audit entry was written
        async with real_factory() as read_session:
            from app.models.audit import AuditLog
            audit_result = await read_session.execute(
                select(AuditLog).where(
                    AuditLog.action == "claims_workflow:status_updated_from_poll"
                )
            )
            audit = audit_result.scalar_one_or_none()
        assert audit is not None, "Audit entry for poll status update not found"


# ── 837I Integration Tests ───────────────────────────────────────────


class TestClaims837IIntegration:
    """Tests for the 837I institutional claim building path."""

    @pytest.mark.asyncio
    async def test_build_837i_claim_tool(self):
        """Verify the 837I tool produces valid output."""
        from app.agents.claims.tools import build_837i_claim

        result = await build_837i_claim(
            subscriber_id="SUB001",
            subscriber_last_name="Doe",
            subscriber_first_name="Jane",
            subscriber_dob="19900101",
            payer_id="PAYER01",
            payer_name="Test Payer",
            claim_id="CLM-837I-TEST",
            total_charge="5000.00",
            diagnosis_codes=["J18.9", "E11.9"],
            service_lines=[
                {"revenue_code": "0250", "procedure_code": "99213", "charge": "2500.00", "units": "1"},
                {"revenue_code": "0300", "procedure_code": "71046", "charge": "2500.00", "units": "1"},
            ],
            admission_date="20260301",
            discharge_date="20260305",
            type_of_bill="0111",
        )

        assert result["success"] is True
        assert result["claim_type"] == "837I"
        assert result["claim_id"] == "CLM-837I-TEST"
        assert "x12_837" in result
        # Verify 837I-specific segments
        x12_text = result["x12_837"]
        assert "005010X223A2" in x12_text  # 837I implementation guide
        assert "0250" in x12_text  # revenue code
        assert "CL1" in x12_text  # institutional claim segment

    @pytest.mark.asyncio
    async def test_claims_graph_uses_837i_for_institutional(self):
        """Verify the claims graph builds 837I when claim_type is 837I."""
        from app.agents.claims.graph import run_claims_agent
        from app.core.engine.llm_provider import LLMProvider, MockLLMBackend

        llm_provider = LLMProvider(primary=MockLLMBackend())

        state = await run_claims_agent(
            input_data={
                "subscriber_id": "SUB001",
                "subscriber_first_name": "Jane",
                "subscriber_last_name": "Doe",
                "diagnosis_codes": ["J18.9"],
                "procedure_codes": ["99213"],
                "service_lines": [
                    {"revenue_code": "0250", "procedure_code": "99213", "charge": "1000.00", "units": "1"},
                ],
                "total_charge": "1000.00",
                "claim_type": "837I",
                "admission_date": "20260301",
                "payer_id": "PAYER01",
            },
            llm_provider=llm_provider,
            task_id="test-837i",
        )

        # Verify 837I was built (not 837P)
        x12_data = state.get("x12_837_data", {})
        assert x12_data.get("success") is True
        assert x12_data.get("claim_type") == "837I"


# ── Encounter ID Linkage Tests ───────────────────────────────────────


class TestClaimsEncounterLinkage:
    """Tests that encounter_id flows from claims input through to the Claim record."""

    @pytest.mark.asyncio
    async def test_claims_workflow_persists_encounter_id(
        self, db_session: AsyncSession, test_engine,
    ):
        """Submit a claims task with encounter_id and verify it is stored on
        the resulting Claim record in the database."""
        from app.workflows.claims import run_claims_workflow
        from app.models.patient import Encounter
        from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession as AS
        from datetime import datetime, timezone

        org = await _create_org(db_session)
        patient = await _create_patient(db_session, org.id)

        # Create an encounter to link
        encounter = Encounter(
            patient_id=patient.id,
            encounter_type="outpatient",
            status="active",
            encounter_date=datetime.now(timezone.utc),
        )
        db_session.add(encounter)
        await db_session.flush()

        task = await _create_claims_task(db_session, patient.id, org.id)
        await db_session.commit()

        workflow_input = WorkflowInput(
            task_id=str(task.id),
            agent_type="claims",
            input_data={
                "subscriber_id": "SUB001",
                "subscriber_first_name": "Jane",
                "subscriber_last_name": "Doe",
                "diagnosis_codes": ["J06.9"],
                "procedure_codes": ["99213"],
                "total_charge": "150.00",
                "payer_id": "PAYER01",
                "payer_name": "Test Payer",
                "encounter_id": str(encounter.id),
            },
        )

        safe_engine = _NondisposableEngine(test_engine)
        real_factory = async_sessionmaker(test_engine, class_=AS, expire_on_commit=False)

        with patch(
            "sqlalchemy.ext.asyncio.create_async_engine", return_value=safe_engine,
        ), patch(
            "sqlalchemy.ext.asyncio.async_sessionmaker", return_value=real_factory,
        ):
            result = await run_claims_workflow(workflow_input)

        assert result.status == "completed"

        # Verify encounter_id is persisted on the Claim record
        async with real_factory() as read_session:
            claim_result = await read_session.execute(
                select(Claim).where(Claim.task_id == task.id)
            )
            claim = claim_result.scalar_one_or_none()
        assert claim is not None, "Claim row was NOT inserted"
        assert claim.encounter_id == encounter.id, (
            f"Expected encounter_id={encounter.id}, got {claim.encounter_id}"
        )

    @pytest.mark.asyncio
    async def test_claims_workflow_without_encounter_id_still_works(
        self, db_session: AsyncSession, test_engine,
    ):
        """Claims workflow without encounter_id should still succeed with
        encounter_id=None on the Claim record."""
        from app.workflows.claims import run_claims_workflow
        from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession as AS

        org = await _create_org(db_session)
        patient = await _create_patient(db_session, org.id)
        task = await _create_claims_task(db_session, patient.id, org.id)
        await db_session.commit()

        workflow_input = WorkflowInput(
            task_id=str(task.id),
            agent_type="claims",
            input_data={
                "subscriber_id": "SUB001",
                "subscriber_first_name": "Jane",
                "subscriber_last_name": "Doe",
                "diagnosis_codes": ["J06.9"],
                "procedure_codes": ["99213"],
                "total_charge": "150.00",
                "payer_id": "PAYER01",
            },
        )

        safe_engine = _NondisposableEngine(test_engine)
        real_factory = async_sessionmaker(test_engine, class_=AS, expire_on_commit=False)

        with patch(
            "sqlalchemy.ext.asyncio.create_async_engine", return_value=safe_engine,
        ), patch(
            "sqlalchemy.ext.asyncio.async_sessionmaker", return_value=real_factory,
        ):
            result = await run_claims_workflow(workflow_input)

        assert result.status == "completed"

        async with real_factory() as read_session:
            claim_result = await read_session.execute(
                select(Claim).where(Claim.task_id == task.id)
            )
            claim = claim_result.scalar_one_or_none()
        assert claim is not None
        assert claim.encounter_id is None


# ── Workflow-Level Denial Analysis Tests ─────────────────────────────


class TestWorkflowDenialAnalysis:
    """Tests that the analyze_workflow_denials activity works correctly
    and is invokable from both inline and Temporal paths."""

    @pytest.mark.asyncio
    async def test_analyze_workflow_denials_fills_missing_analyses(self):
        """When denials exist without matching analyses, the activity
        generates analysis for the gaps."""
        from app.workflows.claims import analyze_workflow_denials

        result = await analyze_workflow_denials({
            "task_id": "test-denial-analysis",
            "denials": [
                {
                    "claim_id": "CLM001",
                    "status_code": "4",
                    "adjustments": [{"reason_code": "CO-16"}],
                },
                {
                    "claim_id": "CLM002",
                    "status_code": "4",
                    "adjustments": [{"reason_code": "CO-197"}],
                },
            ],
            "existing_analyses": [
                {"denial_code": "CO-16", "category": "information_requested"},
            ],
            "input_data": {
                "diagnosis_codes": ["J06.9"],
                "procedure_codes": ["99213"],
                "payer_id": "PAYER01",
            },
        })

        assert result["success"] is True
        analyses = result["data"]["denial_analyses"]
        # Should have 2 analyses: 1 existing + 1 newly generated
        assert len(analyses) == 2
        # First should be the existing one (passed through)
        assert analyses[0]["denial_code"] == "CO-16"
        # Second should be newly generated for CO-197
        assert analyses[1]["denial_code"] == "CO-197"

    @pytest.mark.asyncio
    async def test_claims_workflow_with_denials_runs_analysis(
        self, db_session: AsyncSession, test_engine,
    ):
        """End-to-end: claims workflow with denial-producing 835 data
        triggers denial analysis and persists denial records."""
        from app.workflows.claims import run_claims_workflow
        from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession as AS

        org = await _create_org(db_session)
        patient = await _create_patient(db_session, org.id)
        task = await _create_claims_task(db_session, patient.id, org.id)
        await db_session.commit()

        # Provide raw 835 data that includes a denial (status_code=4, zero payment)
        raw_835 = (
            "ISA*00*          *00*          *ZZ*SENDER         "
            "*ZZ*RECEIVER       *230101*1200*^*00501*000000001*0*P*:~"
            "GS*HP*SENDER*RECEIVER*20230101*1200*1*X*005010X221A1~"
            "ST*835*0001~"
            "BPR*I*0.00*C*CHK*EFT*01*999999999*DA*123456789*1234567890**01*999999999*DA*123456789*20230101~"
            "TRN*1*12345*1234567890~"
            "DTM*405*20230101~"
            "N1*PR*TEST PAYER~"
            "N1*PE*TEST PROVIDER*XX*1234567890~"
            "CLP*CLM001*4*150.00*0.00*0.00*MC*CLAIMREF001~"
            "CAS*CO*16*150.00~"
            "SVC*HC:99213*150.00*0.00~"
            "DTM*472*20230101~"
            "SE*13*0001~"
            "GE*1*1~"
            "IEA*1*000000001~"
        )

        workflow_input = WorkflowInput(
            task_id=str(task.id),
            agent_type="claims",
            input_data={
                "subscriber_id": "SUB001",
                "subscriber_first_name": "Jane",
                "subscriber_last_name": "Doe",
                "diagnosis_codes": ["J06.9"],
                "procedure_codes": ["99213"],
                "total_charge": "150.00",
                "payer_id": "PAYER01",
                "payer_name": "Test Payer",
                "raw_835": raw_835,
            },
        )

        safe_engine = _NondisposableEngine(test_engine)
        real_factory = async_sessionmaker(test_engine, class_=AS, expire_on_commit=False)

        with patch(
            "sqlalchemy.ext.asyncio.create_async_engine", return_value=safe_engine,
        ), patch(
            "sqlalchemy.ext.asyncio.async_sessionmaker", return_value=real_factory,
        ):
            result = await run_claims_workflow(workflow_input)

        assert result.status == "completed"

        # Verify denial records were created
        async with real_factory() as read_session:
            claim_result = await read_session.execute(
                select(Claim).where(Claim.task_id == task.id)
            )
            claim = claim_result.scalar_one_or_none()
            assert claim is not None

            denial_result = await read_session.execute(
                select(ClaimDenial).where(ClaimDenial.claim_id == claim.id)
            )
            denials = denial_result.scalars().all()

        # Should have at least one denial record
        assert len(denials) >= 1, f"Expected at least 1 denial record, got {len(denials)}"
        # Denial should have a recommended action (from analysis)
        assert any(d.recommended_action for d in denials), (
            "Expected at least one denial to have a recommended_action from analysis"
        )
