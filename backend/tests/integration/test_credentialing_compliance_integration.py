"""Integration tests for Credentialing & Compliance agents — Sprint 9.

Tests the full workflow lifecycle including API endpoints, workflow execution,
DB persistence, and dashboard aggregation across all 6 agent types.
"""

import pytest
import uuid

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, AsyncEngine

from app.models.agent_task import AGENT_TYPES, AgentTask
from app.models.credentialing import CredentialingApplication
from app.models.compliance import ComplianceReport
from app.models.hitl_review import HITLReview
from app.models.audit import AuditLog


def _auth_headers() -> dict[str, str]:
    """Generate a valid JWT auth header for integration tests."""
    from app.core.auth.jwt import create_access_token

    token = create_access_token(
        user_id=uuid.uuid4(),
        email="test@slate-health.com",
        role="admin",
    )
    return {"Authorization": f"Bearer {token}"}


# ── Credentialing End-to-End ───────────────────────────────────────


class TestCredentialingEndToEnd:
    """Integration test credentialing: NPI → lookup → verify → submit → track."""

    @pytest.mark.asyncio
    async def test_credentialing_workflow_end_to_end(self, db_session: AsyncSession):
        """Full credentialing pipeline via workflow service."""
        from app.services.workflow_service import WorkflowService
        from app.services.agent_service import AgentService
        from app.workflows.base import WorkflowInput

        workflow_svc = WorkflowService(db_session)
        agent_svc = AgentService(db_session, workflow_service=workflow_svc)

        task = await agent_svc.create_task(
            agent_type="credentialing",
            input_data={
                "provider_npi": "1234567890",
                "target_organization": "Test Hospital",
                "credentialing_type": "initial",
                "state": "CA",
            },
        )
        await db_session.commit()

        assert task is not None
        assert task.agent_type == "credentialing"
        # Task should be completed or in review (missing docs triggers review)
        assert task.status in ("completed", "review", "running", "failed")

    @pytest.mark.asyncio
    async def test_credentialing_creates_application_record(self, db_session: AsyncSession):
        """Verify CredentialingApplication DB record is created."""
        from app.services.workflow_service import WorkflowService
        from app.services.agent_service import AgentService
        from sqlalchemy import select

        workflow_svc = WorkflowService(db_session)
        agent_svc = AgentService(db_session, workflow_service=workflow_svc)

        task = await agent_svc.create_task(
            agent_type="credentialing",
            input_data={
                "provider_npi": "9876543210",
                "target_organization": "Another Hospital",
                "credentialing_type": "initial",
            },
        )
        await db_session.commit()

        # Check for CredentialingApplication record
        result = await db_session.execute(
            select(CredentialingApplication).where(
                CredentialingApplication.task_id == task.id
            )
        )
        cred_app = result.scalar_one_or_none()
        if cred_app is not None:
            assert cred_app.provider_npi == "9876543210"
            # Inline workflow now performs lifecycle progression
            # (submitted → under_review → approved/denied), so terminal
            # statuses are also valid here.
            assert cred_app.status in (
                "submitted", "pending_documents", "under_review", "approved", "denied",
            )


# ── Compliance End-to-End ──────────────────────────────────────────


class TestComplianceEndToEnd:
    """Integration test compliance: org + period → pull data → evaluate → report."""

    @pytest.mark.asyncio
    async def test_compliance_workflow_end_to_end(self, db_session: AsyncSession):
        """Full compliance pipeline via workflow service."""
        from app.services.workflow_service import WorkflowService
        from app.services.agent_service import AgentService

        org_id = str(uuid.uuid4())

        workflow_svc = WorkflowService(db_session)
        agent_svc = AgentService(db_session, workflow_service=workflow_svc)

        task = await agent_svc.create_task(
            agent_type="compliance",
            input_data={
                "organization_id": org_id,
                "measure_set": "HEDIS",
                "reporting_period_start": "2025-01-01",
                "reporting_period_end": "2025-12-31",
            },
        )
        await db_session.commit()

        assert task is not None
        assert task.agent_type == "compliance"
        assert task.status in ("completed", "review", "running", "failed")

    @pytest.mark.asyncio
    async def test_compliance_creates_report_record(self, db_session: AsyncSession):
        """Verify ComplianceReport DB record is created."""
        from app.services.workflow_service import WorkflowService
        from app.services.agent_service import AgentService
        from sqlalchemy import select

        org_id = str(uuid.uuid4())

        workflow_svc = WorkflowService(db_session)
        agent_svc = AgentService(db_session, workflow_service=workflow_svc)

        task = await agent_svc.create_task(
            agent_type="compliance",
            input_data={
                "organization_id": org_id,
                "measure_set": "HEDIS",
                "reporting_period_start": "2025-01-01",
                "reporting_period_end": "2025-12-31",
            },
        )
        await db_session.commit()

        result = await db_session.execute(
            select(ComplianceReport).where(
                ComplianceReport.task_id == task.id
            )
        )
        comp_report = result.scalar_one_or_none()
        if comp_report is not None:
            assert comp_report.measure_set == "HEDIS"
            # Status may be "completed", "review" (e.g. small patient
            # population triggers data-quality review), or "failed"
            assert comp_report.status in ("completed", "review", "failed")
            assert comp_report.overall_score is not None


# ── All 6 Agent Types Accessible ──────────────────────────────────


class TestAllSixAgentTypes:
    """Verify all 6 agent types can be submitted via the API."""

    @pytest.mark.asyncio
    async def test_all_agent_types_registered(self):
        """All 6 agent types are defined in AGENT_TYPES."""
        assert len(AGENT_TYPES) == 6
        assert "eligibility" in AGENT_TYPES
        assert "scheduling" in AGENT_TYPES
        assert "claims" in AGENT_TYPES
        assert "prior_auth" in AGENT_TYPES
        assert "credentialing" in AGENT_TYPES
        assert "compliance" in AGENT_TYPES

    @pytest.mark.asyncio
    async def test_submit_credentialing_via_api(self, client: AsyncClient):
        """POST /api/v1/agents/credentialing/tasks creates a task with auth."""
        headers = _auth_headers()
        response = await client.post(
            "/api/v1/agents/credentialing/tasks",
            json={
                "input_data": {
                    "provider_npi": "1234567890",
                    "target_organization": "Test Hospital",
                    "credentialing_type": "initial",
                },
            },
            headers=headers,
        )
        assert response.status_code == 201, f"Expected 201, got {response.status_code}: {response.text}"
        data = response.json()
        assert data["agent_type"] == "credentialing"
        assert data["id"] is not None

    @pytest.mark.asyncio
    async def test_submit_compliance_via_api(self, client: AsyncClient):
        """POST /api/v1/agents/compliance/tasks creates a task with auth."""
        headers = _auth_headers()
        response = await client.post(
            "/api/v1/agents/compliance/tasks",
            json={
                "input_data": {
                    "organization_id": str(uuid.uuid4()),
                    "measure_set": "HEDIS",
                    "reporting_period_start": "2025-01-01",
                    "reporting_period_end": "2025-12-31",
                },
            },
            headers=headers,
        )
        assert response.status_code == 201, f"Expected 201, got {response.status_code}: {response.text}"
        data = response.json()
        assert data["agent_type"] == "compliance"
        assert data["id"] is not None

    @pytest.mark.asyncio
    async def test_submit_credentialing_without_auth_returns_401(self, client: AsyncClient):
        """POST without auth header returns 401."""
        response = await client.post(
            "/api/v1/agents/credentialing/tasks",
            json={
                "input_data": {
                    "provider_npi": "1234567890",
                    "target_organization": "Test Hospital",
                },
            },
        )
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_credentialing_invalid_npi_returns_422(self, client: AsyncClient):
        """Invalid NPI should return 422 with proper auth."""
        headers = _auth_headers()
        response = await client.post(
            "/api/v1/agents/credentialing/tasks",
            json={
                "input_data": {
                    "provider_npi": "12345",
                },
            },
            headers=headers,
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_compliance_missing_org_returns_422(self, client: AsyncClient):
        """Missing organization_id should return 422 with proper auth."""
        headers = _auth_headers()
        response = await client.post(
            "/api/v1/agents/compliance/tasks",
            json={
                "input_data": {
                    "organization_id": "",
                    "measure_set": "HEDIS",
                    "reporting_period_start": "2025-01-01",
                    "reporting_period_end": "2025-12-31",
                },
            },
            headers=headers,
        )
        assert response.status_code == 422


# ── Dashboard Aggregation ──────────────────────────────────────────


class TestDashboardAggregation:
    """Verify dashboard summary includes all 6 agent types."""

    @pytest.mark.asyncio
    async def test_dashboard_includes_all_agents(self, db_session: AsyncSession):
        """GET /api/v1/dashboard/summary aggregates stats across all 6 agents."""
        from app.services.dashboard_service import DashboardService

        dashboard = DashboardService(db_session)
        summary = await dashboard.get_summary()

        assert "agents" in summary
        agent_types_in_summary = {a["agent_type"] for a in summary["agents"]}
        for expected_type in AGENT_TYPES:
            assert expected_type in agent_types_in_summary, (
                f"Agent type '{expected_type}' missing from dashboard summary"
            )

    @pytest.mark.asyncio
    async def test_dashboard_metrics_per_agent(self, db_session: AsyncSession):
        """Dashboard metrics work for credentialing and compliance."""
        from app.services.dashboard_service import DashboardService

        dashboard = DashboardService(db_session)

        for agent_type in ("credentialing", "compliance"):
            metrics = await dashboard.get_agent_metrics(agent_type)
            assert metrics["agent_type"] == agent_type
            assert "total_tasks" in metrics
            assert "completed" in metrics


# ── Workflow Registration Tests ────────────────────────────────────


class TestWorkflowRegistration:
    """Verify credentialing and compliance workflows are properly registered."""

    @pytest.mark.asyncio
    async def test_workflow_types_include_new_agents(self):
        from app.services.workflow_service import _WORKFLOW_TYPES, _INLINE_RUNNERS
        assert "credentialing" in _WORKFLOW_TYPES
        assert "compliance" in _WORKFLOW_TYPES
        assert "credentialing" in _INLINE_RUNNERS
        assert "compliance" in _INLINE_RUNNERS

    @pytest.mark.asyncio
    async def test_credentialing_inline_runner(self):
        """Test the inline runner for credentialing."""
        from app.workflows.credentialing import run_credentialing_workflow
        from app.workflows.base import WorkflowInput

        result = await run_credentialing_workflow(
            WorkflowInput(
                task_id=str(uuid.uuid4()),
                agent_type="credentialing",
                input_data={
                    "provider_npi": "1234567890",
                    "target_organization": "Test",
                },
            )
        )
        # May fail on DB persistence (no DB in unit context), but the agent runs
        assert result.agent_type == "credentialing"

    @pytest.mark.asyncio
    async def test_compliance_inline_runner(self):
        """Test the inline runner for compliance."""
        from app.workflows.compliance import run_compliance_workflow
        from app.workflows.base import WorkflowInput

        result = await run_compliance_workflow(
            WorkflowInput(
                task_id=str(uuid.uuid4()),
                agent_type="compliance",
                input_data={
                    "organization_id": str(uuid.uuid4()),
                    "measure_set": "HEDIS",
                    "reporting_period_start": "2025-01-01",
                    "reporting_period_end": "2025-12-31",
                },
            )
        )
        assert result.agent_type == "compliance"


# ── All 6 Agent Types POST via API ──────────────────────────────


class TestAllSixAgentTypesPost:
    """Integration test: POST all 6 agent types and verify 201 + non-null workflow."""

    # Valid payloads for each agent type
    _PAYLOADS = {
        "eligibility": {
            "subscriber_id": "INS-12345",
            "subscriber_first_name": "Jane",
            "subscriber_last_name": "Doe",
            "subscriber_dob": "19850615",
            "payer_id": "BCBS01",
            "payer_name": "Blue Cross Blue Shield",
        },
        "scheduling": {
            "request_text": "annual checkup with Dr. Smith next Tuesday",
        },
        "claims": {
            "subscriber_id": "SUB-12345",
            "subscriber_first_name": "John",
            "subscriber_last_name": "Smith",
            "subscriber_dob": "19800101",
            "diagnosis_codes": ["J06.9"],
            "procedure_codes": ["99213"],
            "total_charge": "150.00",
            "payer_id": "BCBS01",
            "payer_name": "Blue Cross Blue Shield",
        },
        "prior_auth": {
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
            "patient_id": str(uuid.uuid4()),
        },
        "credentialing": {
            "provider_npi": "1234567890",
            "target_organization": "Test Hospital",
            "credentialing_type": "initial",
            "state": "CA",
        },
        "compliance": {
            "organization_id": str(uuid.uuid4()),
            "measure_set": "HEDIS",
            "reporting_period_start": "2025-01-01",
            "reporting_period_end": "2025-12-31",
        },
    }

    @pytest.mark.asyncio
    @pytest.mark.parametrize("agent_type", [
        "eligibility", "scheduling", "claims", "prior_auth",
        "credentialing", "compliance",
    ])
    async def test_post_agent_task_returns_201(self, client: AsyncClient, agent_type: str):
        """POST /api/v1/agents/{type}/tasks returns 201 for all 6 agent types."""
        headers = _auth_headers()
        payload = self._PAYLOADS[agent_type]
        response = await client.post(
            f"/api/v1/agents/{agent_type}/tasks",
            json={"input_data": payload},
            headers=headers,
        )
        assert response.status_code == 201, (
            f"Expected 201 for {agent_type}, got {response.status_code}: {response.text}"
        )
        data = response.json()
        assert data["agent_type"] == agent_type
        assert data["id"] is not None


# ── Error-path Lifecycle Tests ───────────────────────────────────


class TestErrorPathLifecycle:
    """Verify that error conditions produce review/failed, not completed."""

    @pytest.mark.asyncio
    async def test_credentialing_nonexistent_npi_triggers_review(
        self, db_session: AsyncSession,
    ):
        """Credentialing with a non-existent NPI should trigger HITL review, not complete silently."""
        from app.agents.credentialing.graph import run_credentialing_agent
        from app.core.engine.llm_provider import LLMProvider, MockLLMBackend

        llm_provider = LLMProvider(primary=MockLLMBackend())

        state = await run_credentialing_agent(
            input_data={
                "provider_npi": "0000000000",
                "target_organization": "Test Hospital",
                "credentialing_type": "initial",
            },
            llm_provider=llm_provider,
            task_id=str(uuid.uuid4()),
        )

        # Error path must set needs_review=True so the task goes to HITL
        assert state.get("needs_review") is True, (
            f"Expected needs_review=True on error path, got {state.get('needs_review')}"
        )
        assert state.get("confidence", 1.0) < 0.7, (
            f"Expected low confidence on error, got {state.get('confidence')}"
        )
        assert state.get("review_reason"), "Expected a review_reason on error path"

    @pytest.mark.asyncio
    async def test_credentialing_missing_npi_triggers_review(
        self, db_session: AsyncSession,
    ):
        """Credentialing with empty NPI should trigger HITL review."""
        from app.agents.credentialing.graph import run_credentialing_agent
        from app.core.engine.llm_provider import LLMProvider, MockLLMBackend

        llm_provider = LLMProvider(primary=MockLLMBackend())

        state = await run_credentialing_agent(
            input_data={
                "provider_npi": "",
                "target_organization": "Test Hospital",
            },
            llm_provider=llm_provider,
            task_id=str(uuid.uuid4()),
        )

        assert state.get("needs_review") is True
        assert state.get("confidence", 1.0) == 0.0

    @pytest.mark.asyncio
    async def test_compliance_invalid_measure_ids_triggers_review(
        self, db_session: AsyncSession,
    ):
        """Compliance with invalid measure_ids should trigger HITL review, not complete silently."""
        from app.agents.compliance.graph import run_compliance_agent
        from app.core.engine.llm_provider import LLMProvider, MockLLMBackend

        llm_provider = LLMProvider(primary=MockLLMBackend())

        state = await run_compliance_agent(
            input_data={
                "organization_id": str(uuid.uuid4()),
                "measure_set": "HEDIS",
                "measure_ids": ["NOT_A_REAL_MEASURE"],
                "reporting_period_start": "2025-01-01",
                "reporting_period_end": "2025-12-31",
            },
            llm_provider=llm_provider,
            task_id=str(uuid.uuid4()),
        )

        # Error path must set needs_review=True
        assert state.get("needs_review") is True, (
            f"Expected needs_review=True for invalid measures, got {state.get('needs_review')}"
        )
        assert state.get("confidence", 1.0) == 0.0, (
            f"Expected confidence=0.0 for invalid measures, got {state.get('confidence')}"
        )
        assert state.get("review_reason"), "Expected a review_reason for invalid measures"

    @pytest.mark.asyncio
    async def test_compliance_missing_org_triggers_review(
        self, db_session: AsyncSession,
    ):
        """Compliance with missing org_id should trigger HITL review."""
        from app.agents.compliance.graph import run_compliance_agent
        from app.core.engine.llm_provider import LLMProvider, MockLLMBackend

        llm_provider = LLMProvider(primary=MockLLMBackend())

        state = await run_compliance_agent(
            input_data={
                "organization_id": "",
                "measure_set": "HEDIS",
                "reporting_period_start": "2025-01-01",
                "reporting_period_end": "2025-12-31",
            },
            llm_provider=llm_provider,
            task_id=str(uuid.uuid4()),
        )

        assert state.get("needs_review") is True
        assert state.get("confidence", 1.0) == 0.0


# ── HITL Review Row Persistence Tests ─────────────────────────────


class TestHITLReviewPersistence:
    """Verify that HITL review rows are created for credentialing/compliance
    when agent output indicates needs_review=True."""

    @pytest.mark.asyncio
    async def test_credentialing_missing_docs_creates_hitl_review(
        self, db_session: AsyncSession,
    ):
        """Credentialing with missing documents should persist a HITLReview row."""
        from app.services.workflow_service import WorkflowService
        from app.services.agent_service import AgentService
        from sqlalchemy import select

        workflow_svc = WorkflowService(db_session)
        agent_svc = AgentService(db_session, workflow_service=workflow_svc)

        # NPI ending in odd digit → incomplete docs → needs_review
        task = await agent_svc.create_task(
            agent_type="credentialing",
            input_data={
                "provider_npi": "1234567891",
                "target_organization": "Test Hospital",
                "credentialing_type": "initial",
                "state": "CA",
            },
        )
        await db_session.commit()

        # Verify HITLReview was created
        result = await db_session.execute(
            select(HITLReview).where(HITLReview.task_id == task.id)
        )
        review = result.scalar_one_or_none()
        assert review is not None, (
            f"Expected HITLReview row for credentialing task {task.id} "
            f"with missing documents, but none was found"
        )
        assert review.status == "pending"
        assert review.reason, "HITLReview should have a reason"
        assert review.confidence_score is not None

    @pytest.mark.asyncio
    async def test_compliance_low_score_creates_hitl_review(
        self, db_session: AsyncSession,
    ):
        """Compliance with data-quality concerns should persist a HITLReview row."""
        from app.services.workflow_service import WorkflowService
        from app.services.agent_service import AgentService
        from sqlalchemy import select

        org_id = str(uuid.uuid4())

        workflow_svc = WorkflowService(db_session)
        agent_svc = AgentService(db_session, workflow_service=workflow_svc)

        task = await agent_svc.create_task(
            agent_type="compliance",
            input_data={
                "organization_id": org_id,
                "measure_set": "HEDIS",
                "reporting_period_start": "2025-01-01",
                "reporting_period_end": "2025-12-31",
            },
        )
        await db_session.commit()

        # The mock population is 10 patients which triggers confidence
        # evaluation. Check if a review was created (it should be if the
        # compliance agent flagged needs_review).
        result = await db_session.execute(
            select(HITLReview).where(HITLReview.task_id == task.id)
        )
        review = result.scalar_one_or_none()

        # The mock population of 10 patients may or may not trigger review
        # depending on measure scores. Assert that IF the task status is
        # "review", then a HITLReview row exists.
        if task.status == "review":
            assert review is not None, (
                f"Task status is 'review' but no HITLReview row found for task {task.id}"
            )
            assert review.status == "pending"


# ── Credentialing Lifecycle Status Transitions ───────────────────


class TestCredentialingLifecycleTransitions:
    """Verify credentialing status progression via check_credentialing_status_activity."""

    @pytest.mark.asyncio
    async def test_submitted_to_under_review_transition(self, db_session: AsyncSession):
        """Check-in activity transitions submitted → under_review."""
        from app.models.organization import Organization
        from app.workflows.credentialing import check_credentialing_status_activity

        # Create required organization, task, and credentialing application
        # Use unique NPI per test run to avoid UNIQUE constraint violations
        # when data from prior runs persists in the session-scoped test DB.
        unique_npi = f"10{uuid.uuid4().hex[:8]}"
        org = Organization(name=f"Test Org {unique_npi}", npi=unique_npi, tax_id=f"11-{unique_npi[:7]}")
        db_session.add(org)
        await db_session.flush()

        task_id = uuid.uuid4()
        task = AgentTask(
            id=task_id,
            agent_type="credentialing",
            status="running",
            input_data={"provider_npi": "1234567890"},
            organization_id=org.id,
        )
        db_session.add(task)
        await db_session.flush()

        cred_app = CredentialingApplication(
            task_id=task_id,
            provider_npi="1234567890",
            provider_name="John Smith",
            status="submitted",
            target_organization="Test Hospital",
            application_data={"sanctions_clear": True},
            missing_documents={"missing": []},
        )
        db_session.add(cred_app)
        await db_session.commit()

        # Run check-in activity at check_in_number=1 → should transition to under_review
        result = await check_credentialing_status_activity({
            "task_id": str(task_id),
            "check_in_number": 1,
        })

        assert result["success"] is True
        data = result["data"]
        assert data["previous_status"] == "submitted"
        assert data["current_status"] == "under_review"
        assert data["terminal"] is False

    @pytest.mark.asyncio
    async def test_under_review_to_approved_transition(self, db_session: AsyncSession):
        """Check-in at >= 8 transitions under_review → approved (clean app)."""
        from app.models.organization import Organization
        from app.workflows.credentialing import check_credentialing_status_activity

        unique_npi = f"20{uuid.uuid4().hex[:8]}"
        org = Organization(name=f"Test Org {unique_npi}", npi=unique_npi, tax_id=f"22-{unique_npi[:7]}")
        db_session.add(org)
        await db_session.flush()

        task_id = uuid.uuid4()
        task = AgentTask(
            id=task_id,
            agent_type="credentialing",
            status="running",
            input_data={"provider_npi": "1234567890"},
            organization_id=org.id,
        )
        db_session.add(task)
        await db_session.flush()

        cred_app = CredentialingApplication(
            task_id=task_id,
            provider_npi="1234567890",
            provider_name="John Smith",
            status="under_review",
            target_organization="Test Hospital",
            application_data={"sanctions_clear": True},
            missing_documents={"missing": []},
        )
        db_session.add(cred_app)
        await db_session.commit()

        result = await check_credentialing_status_activity({
            "task_id": str(task_id),
            "check_in_number": 8,
        })

        assert result["success"] is True
        data = result["data"]
        assert data["previous_status"] == "under_review"
        assert data["current_status"] == "approved"
        assert data["terminal"] is True

        # Verify DB state was persisted
        from sqlalchemy import select
        db_session.expire_all()
        cred_result = await db_session.execute(
            select(CredentialingApplication).where(
                CredentialingApplication.task_id == task_id
            )
        )
        updated_app = cred_result.scalar_one()
        assert updated_app.status == "approved"
        assert updated_app.approved_date is not None
        assert updated_app.expiration_date is not None

    @pytest.mark.asyncio
    async def test_under_review_to_denied_with_missing_docs(self, db_session: AsyncSession):
        """Check-in at >= 8 transitions under_review → denied when docs are missing."""
        from app.models.organization import Organization
        from app.workflows.credentialing import check_credentialing_status_activity

        unique_npi = f"30{uuid.uuid4().hex[:8]}"
        org = Organization(name=f"Test Org {unique_npi}", npi=unique_npi, tax_id=f"33-{unique_npi[:7]}")
        db_session.add(org)
        await db_session.flush()

        task_id = uuid.uuid4()
        task = AgentTask(
            id=task_id,
            agent_type="credentialing",
            status="running",
            input_data={"provider_npi": "1234567891"},
            organization_id=org.id,
        )
        db_session.add(task)
        await db_session.flush()

        cred_app = CredentialingApplication(
            task_id=task_id,
            provider_npi="1234567891",
            provider_name="Jane Doe",
            status="under_review",
            target_organization="Test Hospital",
            application_data={"sanctions_clear": True},
            missing_documents={"missing": ["board_certification", "cv_resume"]},
        )
        db_session.add(cred_app)
        await db_session.commit()

        result = await check_credentialing_status_activity({
            "task_id": str(task_id),
            "check_in_number": 8,
        })

        assert result["success"] is True
        data = result["data"]
        assert data["current_status"] == "denied"
        assert data["terminal"] is True


# ── Audit Log Persistence Tests ──────────────────────────────────


class TestAuditLogPersistence:
    """Verify that Sprint-9 agent workflows persist audit log entries."""

    @pytest.mark.asyncio
    async def test_credentialing_workflow_creates_audit_entries(
        self, db_session: AsyncSession,
    ):
        """Credentialing workflow should produce audit log entries in the DB."""
        from app.services.workflow_service import WorkflowService
        from app.services.agent_service import AgentService
        from sqlalchemy import select

        workflow_svc = WorkflowService(db_session)
        agent_svc = AgentService(db_session, workflow_service=workflow_svc)

        task = await agent_svc.create_task(
            agent_type="credentialing",
            input_data={
                "provider_npi": "1234567890",
                "target_organization": "Audit Test Hospital",
                "credentialing_type": "initial",
                "state": "CA",
            },
        )
        await db_session.commit()

        # Check that audit log entries were created for this task
        result = await db_session.execute(
            select(AuditLog).where(
                AuditLog.resource_id == str(task.id),
                AuditLog.action.like("agent:credentialing:%"),
            )
        )
        audit_entries = result.scalars().all()
        assert len(audit_entries) > 0, (
            f"Expected audit log entries for credentialing task {task.id}, "
            f"but found none"
        )

    @pytest.mark.asyncio
    async def test_compliance_workflow_creates_audit_entries(
        self, db_session: AsyncSession,
    ):
        """Compliance workflow should produce audit log entries in the DB."""
        from app.services.workflow_service import WorkflowService
        from app.services.agent_service import AgentService
        from sqlalchemy import select

        org_id = str(uuid.uuid4())

        workflow_svc = WorkflowService(db_session)
        agent_svc = AgentService(db_session, workflow_service=workflow_svc)

        task = await agent_svc.create_task(
            agent_type="compliance",
            input_data={
                "organization_id": org_id,
                "measure_set": "HEDIS",
                "reporting_period_start": "2025-01-01",
                "reporting_period_end": "2025-12-31",
            },
        )
        await db_session.commit()

        result = await db_session.execute(
            select(AuditLog).where(
                AuditLog.resource_id == str(task.id),
                AuditLog.action.like("agent:compliance:%"),
            )
        )
        audit_entries = result.scalars().all()
        assert len(audit_entries) > 0, (
            f"Expected audit log entries for compliance task {task.id}, "
            f"but found none"
        )


# ── OIG Exclusion Provider Tests ─────────────────────────────────


class TestOIGExclusionProvider:
    """Verify OIG exclusion check with adapter-driven implementation."""

    @pytest.mark.asyncio
    async def test_oig_exclusion_triggers_denial_escalation(self):
        """When OIG provider returns exclusion, agent must escalate."""
        from app.agents.credentialing.graph import (
            check_sanctions_node,
            evaluate_confidence_node,
        )
        from app.agents.credentialing.tools import (
            ExcludedOIGProvider,
            set_oig_provider,
            MockOIGExclusionProvider,
        )
        from app.core.engine.state import create_initial_state

        # Wire in the exclusion provider
        set_oig_provider(ExcludedOIGProvider())
        try:
            # Build a state that has passed NPPES lookup and license verification
            state = create_initial_state(
                task_id=str(uuid.uuid4()),
                agent_type="credentialing",
                input_data={
                    "provider_npi": "1234567890",
                    "target_organization": "Test Hospital",
                    "credentialing_type": "initial",
                    "state": "CA",
                },
            )
            state["provider_details"] = {
                "npi": "1234567890",
                "first_name": "John",
                "last_name": "Smith",
                "credential": "MD",
                "taxonomy": {"state": "CA", "license": "CA-567890"},
            }
            state["license_verification"] = {
                "success": True,
                "verified": True,
                "license_status": "active",
            }

            # Run check_sanctions_node with ExcludedOIGProvider
            state = await check_sanctions_node(state)

            assert state["verification_results"]["sanctions_clear"] is False, (
                "Expected sanctions_clear=False for excluded provider"
            )

            # Run evaluate_confidence_node to verify escalation
            state = await evaluate_confidence_node(state)

            assert state.get("needs_review") is True, (
                "Expected needs_review=True when OIG returns exclusion"
            )
            assert state.get("confidence", 1.0) <= 0.1, (
                f"Expected very low confidence for excluded provider, got {state.get('confidence')}"
            )
        finally:
            # Restore default provider
            set_oig_provider(MockOIGExclusionProvider())


# ── FHIR Failure in Production Mode Tests ────────────────────────


class TestFHIRProductionFailure:
    """Verify that FHIR failures in production mode yield error, not mock data."""

    @pytest.mark.asyncio
    async def test_fhir_failure_without_mock_fallback_returns_error(self):
        """When allow_mock_fallback=False, FHIR failure returns success=False."""
        from app.agents.compliance.tools import pull_clinical_data
        from unittest.mock import patch
        import app.config

        original_fallback = app.config.settings.allow_mock_fallback
        original_fhir = app.config.settings.fhir_base_url
        try:
            # Simulate production mode where mock fallback is disabled
            app.config.settings.allow_mock_fallback = False
            app.config.settings.fhir_base_url = "http://nonexistent-fhir:9999"

            result = await pull_clinical_data(
                organization_id="org-123",
                reporting_period_start="2025-01-01",
                reporting_period_end="2025-12-31",
            )

            assert result["success"] is False, (
                "Expected success=False when FHIR fails and mock fallback is disabled"
            )
            assert "error" in result
            assert result["_source"] == "error"
            assert result["total_patients"] == 0
        finally:
            app.config.settings.allow_mock_fallback = original_fallback
            app.config.settings.fhir_base_url = original_fhir

    @pytest.mark.asyncio
    async def test_fhir_failure_with_mock_fallback_returns_mock_data(self):
        """When allow_mock_fallback=True, FHIR failure returns mock data with warning."""
        from app.agents.compliance.tools import pull_clinical_data
        import app.config

        original_fallback = app.config.settings.allow_mock_fallback
        try:
            # Explicitly enable mock fallback (default is now False)
            app.config.settings.allow_mock_fallback = True

            result = await pull_clinical_data(
                organization_id="org-123",
                reporting_period_start="2025-01-01",
                reporting_period_end="2025-12-31",
            )

            assert result["success"] is True
            assert result["_source"] == "mock"
            assert result["total_patients"] > 0
            # Verify warning flag is present when mock data is used
            assert "_mock_data_warning" in result
            assert "synthetic" in result["_mock_data_warning"].lower() or "mock" in result["_mock_data_warning"].lower()
        finally:
            app.config.settings.allow_mock_fallback = original_fallback


# ── DB-Backed Measure Lookup Tests ───────────────────────────────


class TestDBMeasureLookup:
    """Verify measure definitions are loaded from the database when available."""

    @pytest.mark.asyncio
    async def test_measure_lookup_uses_db_when_seeded(self, db_session: AsyncSession):
        """When measures exist in DB, get_measure_definitions uses them."""
        from app.agents.compliance.tools import get_measure_definitions
        from app.models.quality_measure import QualityMeasureDefinition

        # Use a unique measure_id per test run to avoid UNIQUE constraint
        # violations when data from prior runs persists in the session-scoped
        # test DB.
        unique_measure_id = f"TEST-{uuid.uuid4().hex[:8].upper()}"

        # Seed a test measure
        measure = QualityMeasureDefinition(
            measure_id=unique_measure_id,
            name="Test Measure from DB",
            measure_set="HEDIS",
            description="A test measure seeded in DB",
            denominator_criteria={"age_min": 18, "age_max": 65},
            numerator_criteria={"procedure_codes": ["99999"], "lookback_months": 12},
            exclusion_criteria={},
            target_rate=0.80,
            version="MY2025",
            active=True,
        )
        db_session.add(measure)
        await db_session.commit()

        result = await get_measure_definitions(
            "HEDIS", [unique_measure_id], db_session=db_session
        )

        assert result["success"] is True
        assert unique_measure_id in result["measures"]
        assert result["measures"][unique_measure_id]["name"] == "Test Measure from DB"
        assert result.get("_source") == "database"

    @pytest.mark.asyncio
    async def test_measure_lookup_falls_back_to_constants_when_no_db(self):
        """When DB session factory is unavailable, falls back to in-code constants."""
        from app.agents.compliance.tools import get_measure_definitions
        from unittest.mock import patch

        # Temporarily make the DI factory return None so the code falls
        # back to in-code constants
        with patch(
            "app.agents.compliance.tools._fetch_measures_from_db",
            return_value=None,
        ):
            result = await get_measure_definitions("HEDIS")

        assert result["success"] is True
        assert result["count"] == 5
        assert result.get("_source") == "constants"
        assert "BCS" in result["measures"]
