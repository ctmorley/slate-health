"""Tests for SQLAlchemy model creation and persistence."""

import uuid
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AgentTask,
    AuditLog,
    ClearinghouseConfig,
    Claim,
    ClaimDenial,
    ComplianceReport,
    CredentialingApplication,
    EligibilityCheck,
    Encounter,
    HITLReview,
    Organization,
    Patient,
    Payer,
    PayerRule,
    PHIAccessLog,
    PriorAuthAppeal,
    PriorAuthRequest,
    SchedulingRequest,
    User,
    WorkflowExecution,
)


@pytest.mark.asyncio
async def test_create_organization(db_session: AsyncSession, sample_org_data: dict):
    """Organization can be created and persisted."""
    org = Organization(**sample_org_data)
    db_session.add(org)
    await db_session.flush()

    assert org.id is not None
    assert org.name == "Test Health System"
    assert org.created_at is not None


@pytest.mark.asyncio
async def test_create_user(db_session: AsyncSession):
    """User can be created with required fields."""
    org = Organization(name="Test Org")
    db_session.add(org)
    await db_session.flush()

    user = User(
        email="test@slate.health",
        full_name="Test User",
        role="admin",
        organization_id=org.id,
    )
    db_session.add(user)
    await db_session.flush()

    assert user.id is not None
    assert user.email == "test@slate.health"
    assert user.role == "admin"


@pytest.mark.asyncio
async def test_create_patient_with_encounter(
    db_session: AsyncSession, sample_patient_data: dict
):
    """Patient and linked Encounter can be created."""
    org = Organization(name="Test Org")
    db_session.add(org)
    await db_session.flush()

    patient = Patient(organization_id=org.id, **sample_patient_data)
    db_session.add(patient)
    await db_session.flush()

    encounter = Encounter(
        patient_id=patient.id,
        encounter_type="office_visit",
        encounter_date=datetime.now(timezone.utc),
        status="active",
    )
    db_session.add(encounter)
    await db_session.flush()

    assert patient.id is not None
    assert encounter.patient_id == patient.id


@pytest.mark.asyncio
async def test_create_agent_task(db_session: AsyncSession):
    """AgentTask can be created with valid agent type."""
    task = AgentTask(
        agent_type="eligibility",
        status="pending",
        input_data={"patient_id": "123", "payer": "BCBS"},
    )
    db_session.add(task)
    await db_session.flush()

    assert task.id is not None
    assert task.agent_type == "eligibility"
    assert task.status == "pending"


@pytest.mark.asyncio
async def test_create_audit_log(db_session: AsyncSession):
    """AuditLog can be created as append-only record."""
    log = AuditLog(
        actor_type="system",
        action="eligibility_check_started",
        resource_type="agent_task",
        resource_id=str(uuid.uuid4()),
        phi_accessed=False,
    )
    db_session.add(log)
    await db_session.flush()

    assert log.id is not None
    assert log.action == "eligibility_check_started"


@pytest.mark.asyncio
async def test_create_payer_with_rule(db_session: AsyncSession):
    """Payer and PayerRule can be created."""
    payer = Payer(
        name="Blue Cross Blue Shield",
        payer_id_code="BCBS001",
        payer_type="commercial",
    )
    db_session.add(payer)
    await db_session.flush()

    rule = PayerRule(
        payer_id=payer.id,
        agent_type="eligibility",
        rule_type="timely_filing",
        conditions={"days_limit": 90},
        effective_date=date(2024, 1, 1),
    )
    db_session.add(rule)
    await db_session.flush()

    assert payer.id is not None
    assert rule.payer_id == payer.id


@pytest.mark.asyncio
async def test_create_workflow_execution(db_session: AsyncSession):
    """WorkflowExecution can be created."""
    wf = WorkflowExecution(
        workflow_id=f"wf-{uuid.uuid4()}",
        agent_type="eligibility",
        status="running",
        task_queue="eligibility-queue",
    )
    db_session.add(wf)
    await db_session.flush()

    assert wf.id is not None
    assert wf.status == "running"


@pytest.mark.asyncio
async def test_create_eligibility_check(db_session: AsyncSession):
    """EligibilityCheck can be created with FK references."""
    org = Organization(name="Test Org")
    db_session.add(org)
    await db_session.flush()

    patient = Patient(
        organization_id=org.id,
        mrn="MRN-EC-001",
        first_name="Test",
        last_name="Patient",
        date_of_birth=date(1990, 1, 1),
        gender="male",
    )
    db_session.add(patient)
    await db_session.flush()

    task = AgentTask(agent_type="eligibility", status="pending", input_data={})
    db_session.add(task)
    await db_session.flush()

    check = EligibilityCheck(
        task_id=task.id,
        patient_id=patient.id,
        status="pending",
    )
    db_session.add(check)
    await db_session.flush()

    assert check.id is not None
    assert check.task_id == task.id
    assert check.patient_id == patient.id


@pytest.mark.asyncio
async def test_create_scheduling_request(db_session: AsyncSession):
    """SchedulingRequest can be created with NLP-parsed intent."""
    org = Organization(name="Test Org")
    db_session.add(org)
    await db_session.flush()

    patient = Patient(
        organization_id=org.id,
        mrn="MRN-SR-001",
        first_name="Test",
        last_name="Patient",
        date_of_birth=date(1985, 3, 15),
        gender="female",
    )
    db_session.add(patient)
    await db_session.flush()

    task = AgentTask(agent_type="scheduling", status="pending", input_data={})
    db_session.add(task)
    await db_session.flush()

    req = SchedulingRequest(
        task_id=task.id,
        patient_id=patient.id,
        request_text="Annual checkup with Dr. Smith next Tuesday",
        parsed_intent={"provider": "Dr. Smith", "type": "annual_checkup"},
        status="pending",
    )
    db_session.add(req)
    await db_session.flush()

    assert req.id is not None
    assert req.request_text == "Annual checkup with Dr. Smith next Tuesday"


@pytest.mark.asyncio
async def test_create_claim_with_denial(db_session: AsyncSession):
    """Claim and ClaimDenial can be created with linked lifecycle."""
    org = Organization(name="Test Org")
    db_session.add(org)
    await db_session.flush()

    patient = Patient(
        organization_id=org.id,
        mrn="MRN-CL-001",
        first_name="Test",
        last_name="Patient",
        date_of_birth=date(1975, 8, 20),
        gender="male",
    )
    db_session.add(patient)
    await db_session.flush()

    task = AgentTask(agent_type="claims", status="pending", input_data={})
    db_session.add(task)
    await db_session.flush()

    from decimal import Decimal

    claim = Claim(
        task_id=task.id,
        patient_id=patient.id,
        claim_type="837P",
        status="denied",
        claim_number="CLM-2024-001",
        total_charge=Decimal("1250.00"),
        diagnosis_codes={"primary": "Z00.00"},
        procedure_codes={"1": "99213"},
    )
    db_session.add(claim)
    await db_session.flush()

    denial = ClaimDenial(
        claim_id=claim.id,
        denial_code="CO-4",
        denial_reason="Service not covered under plan",
        denial_category="coverage",
        recommended_action="Appeal with clinical documentation",
    )
    db_session.add(denial)
    await db_session.flush()

    assert claim.id is not None
    assert denial.claim_id == claim.id
    assert denial.denial_code == "CO-4"


@pytest.mark.asyncio
async def test_create_prior_auth_with_appeal(db_session: AsyncSession):
    """PriorAuthRequest and PriorAuthAppeal lifecycle."""
    org = Organization(name="Test Org")
    db_session.add(org)
    await db_session.flush()

    patient = Patient(
        organization_id=org.id,
        mrn="MRN-PA-001",
        first_name="Test",
        last_name="Patient",
        date_of_birth=date(1960, 12, 1),
        gender="female",
    )
    db_session.add(patient)
    await db_session.flush()

    task = AgentTask(agent_type="prior_auth", status="pending", input_data={})
    db_session.add(task)
    await db_session.flush()

    pa = PriorAuthRequest(
        task_id=task.id,
        patient_id=patient.id,
        status="denied",
        procedure_code="27447",
        diagnosis_codes={"primary": "M17.11"},
        clinical_info={"severity": "severe"},
    )
    db_session.add(pa)
    await db_session.flush()

    appeal = PriorAuthAppeal(
        prior_auth_id=pa.id,
        appeal_level=1,
        status="draft",
        appeal_letter="Medical necessity justification...",
        clinical_evidence={"labs": [], "imaging": ["MRI knee"]},
    )
    db_session.add(appeal)
    await db_session.flush()

    assert pa.id is not None
    assert appeal.prior_auth_id == pa.id
    assert appeal.appeal_level == 1


@pytest.mark.asyncio
async def test_create_credentialing_application(db_session: AsyncSession):
    """CredentialingApplication tracks provider enrollment."""
    task = AgentTask(agent_type="credentialing", status="pending", input_data={})
    db_session.add(task)
    await db_session.flush()

    app = CredentialingApplication(
        task_id=task.id,
        provider_npi="1234567890",
        provider_name="Dr. Jane Smith",
        status="initiated",
        documents_checklist={"license": True, "dea": False, "board_cert": True},
        missing_documents={"dea": "DEA certificate required"},
    )
    db_session.add(app)
    await db_session.flush()

    assert app.id is not None
    assert app.provider_npi == "1234567890"
    assert app.missing_documents["dea"] == "DEA certificate required"


@pytest.mark.asyncio
async def test_create_compliance_report(db_session: AsyncSession):
    """ComplianceReport can be created with measure scores."""
    org = Organization(name="Test Health System")
    db_session.add(org)
    await db_session.flush()

    task = AgentTask(agent_type="compliance", status="pending", input_data={})
    db_session.add(task)
    await db_session.flush()

    report = ComplianceReport(
        task_id=task.id,
        organization_id=org.id,
        measure_set="HEDIS",
        reporting_period_start="2024-01-01",
        reporting_period_end="2024-12-31",
        status="completed",
        overall_score=0.87,
        gaps_identified=15,
        measure_scores={"BCS": 0.92, "CCS": 0.78, "CDC": 0.91},
        gap_details={"CCS": {"gap_count": 10, "patients": []}},
    )
    db_session.add(report)
    await db_session.flush()

    assert report.id is not None
    assert report.overall_score == 0.87
    assert report.gaps_identified == 15


@pytest.mark.asyncio
async def test_create_hitl_review(db_session: AsyncSession):
    """HITLReview can be created for agent task escalation."""
    task = AgentTask(
        agent_type="eligibility",
        status="review",
        input_data={"patient_id": "123"},
        confidence_score=0.45,
    )
    db_session.add(task)
    await db_session.flush()

    review = HITLReview(
        task_id=task.id,
        status="pending",
        reason="Low confidence: multiple coverage matches",
        agent_decision={"coverage_a": {"active": True}, "coverage_b": {"active": True}},
        confidence_score=0.45,
    )
    db_session.add(review)
    await db_session.flush()

    assert review.id is not None
    assert review.task_id == task.id
    assert review.confidence_score == 0.45


@pytest.mark.asyncio
async def test_create_phi_access_log(db_session: AsyncSession):
    """PHIAccessLog tracks HIPAA-required PHI access records."""
    log = PHIAccessLog(
        user_id=uuid.uuid4(),
        access_type="read",
        resource_type="patient",
        resource_id=str(uuid.uuid4()),
        reason="Eligibility verification",
        phi_fields_accessed={"fields": ["name", "dob", "insurance_id"]},
    )
    db_session.add(log)
    await db_session.flush()

    assert log.id is not None
    assert log.access_type == "read"
    assert log.reason == "Eligibility verification"


@pytest.mark.asyncio
async def test_create_clearinghouse_config(db_session: AsyncSession):
    """ClearinghouseConfig stores per-org clearinghouse settings."""
    org = Organization(name="Test Org")
    db_session.add(org)
    await db_session.flush()

    config = ClearinghouseConfig(
        organization_id=org.id,
        clearinghouse_name="Availity",
        api_endpoint="https://api.availity.com/v1",
        credentials={"api_key": "***", "api_secret": "***"},
        is_active=True,
        supported_transactions={"270": True, "837": True, "276": True},
    )
    db_session.add(config)
    await db_session.flush()

    assert config.id is not None
    assert config.clearinghouse_name == "Availity"
    assert config.is_active is True


@pytest.mark.asyncio
async def test_all_models_importable():
    """All models can be imported without errors."""
    from app.models import (
        AgentTask,
        AuditLog,
        Base,
        Claim,
        ClaimDenial,
        ClearinghouseConfig,
        ComplianceReport,
        CredentialingApplication,
        EligibilityCheck,
        Encounter,
        HITLReview,
        Organization,
        Patient,
        Payer,
        PayerRule,
        PHIAccessLog,
        PriorAuthRequest,
        PriorAuthAppeal,
        SchedulingRequest,
        User,
        WorkflowExecution,
    )

    # Verify they all have __tablename__
    models_with_tables = [
        User, Organization, Patient, Encounter, AgentTask,
        WorkflowExecution, HITLReview, AuditLog, PHIAccessLog,
        Payer, PayerRule, ClearinghouseConfig, EligibilityCheck,
        SchedulingRequest, Claim, ClaimDenial, PriorAuthRequest,
        PriorAuthAppeal, CredentialingApplication, ComplianceReport,
    ]
    for model in models_with_tables:
        assert hasattr(model, "__tablename__")


@pytest.mark.asyncio
async def test_all_model_types_persistable(db_session: AsyncSession):
    """Every model type can be instantiated, flushed, and queried.

    This is a comprehensive smoke test that proves all 20 model types
    work end-to-end through the ORM against the actual schema, without
    requiring Docker/PostgreSQL.
    """
    # 1. Organization
    org = Organization(name="Smoke Test Org", npi="9999999999", tax_id="99-9999999")
    db_session.add(org)
    await db_session.flush()
    assert org.id is not None

    # 2. User
    user = User(email="smoke@test.com", full_name="Smoke Tester", role="admin", organization_id=org.id)
    db_session.add(user)
    await db_session.flush()

    # 3. Patient
    patient = Patient(
        organization_id=org.id, mrn="SMOKE-001",
        first_name="Smoke", last_name="Test",
        date_of_birth=date(2000, 1, 1), gender="other",
    )
    db_session.add(patient)
    await db_session.flush()

    # 4. Encounter
    encounter = Encounter(
        patient_id=patient.id, encounter_type="office_visit",
        encounter_date=datetime.now(timezone.utc), status="active",
    )
    db_session.add(encounter)
    await db_session.flush()

    # 5. WorkflowExecution
    wf = WorkflowExecution(
        workflow_id=f"smoke-wf-{uuid.uuid4()}", agent_type="eligibility",
        status="running", task_queue="smoke-queue",
    )
    db_session.add(wf)
    await db_session.flush()

    # 6. AgentTask (all 6 agent types)
    tasks = {}
    for agent_type in ["eligibility", "scheduling", "claims", "prior_auth", "credentialing", "compliance"]:
        task = AgentTask(agent_type=agent_type, status="pending", input_data={"test": True})
        db_session.add(task)
        await db_session.flush()
        tasks[agent_type] = task

    # 7. HITLReview
    review = HITLReview(
        task_id=tasks["eligibility"].id, status="pending",
        reason="Smoke test", confidence_score=0.5,
    )
    db_session.add(review)
    await db_session.flush()

    # 8. AuditLog
    audit = AuditLog(
        actor_type="system", action="smoke_test",
        resource_type="test", phi_accessed=False,
    )
    db_session.add(audit)
    await db_session.flush()

    # 9. PHIAccessLog
    phi_log = PHIAccessLog(
        user_id=user.id, access_type="read",
        resource_type="patient", reason="smoke test",
    )
    db_session.add(phi_log)
    await db_session.flush()

    # 10. Payer
    payer = Payer(name="Smoke Payer", payer_id_code=f"SMOKE-{uuid.uuid4().hex[:8]}", payer_type="commercial")
    db_session.add(payer)
    await db_session.flush()

    # 11. PayerRule
    rule = PayerRule(
        payer_id=payer.id, agent_type="eligibility", rule_type="timely_filing",
        conditions={"days": 90}, effective_date=date(2024, 1, 1),
    )
    db_session.add(rule)
    await db_session.flush()

    # 12. ClearinghouseConfig
    ch_config = ClearinghouseConfig(
        organization_id=org.id, clearinghouse_name="Availity",
        api_endpoint="https://api.test.com", is_active=True,
    )
    db_session.add(ch_config)
    await db_session.flush()

    # 13. EligibilityCheck
    ec = EligibilityCheck(task_id=tasks["eligibility"].id, patient_id=patient.id, status="pending")
    db_session.add(ec)
    await db_session.flush()

    # 14. SchedulingRequest
    sr = SchedulingRequest(task_id=tasks["scheduling"].id, patient_id=patient.id, status="pending")
    db_session.add(sr)
    await db_session.flush()

    # 15. Claim
    claim = Claim(
        task_id=tasks["claims"].id, patient_id=patient.id,
        claim_type="837P", status="draft",
    )
    db_session.add(claim)
    await db_session.flush()

    # 16. ClaimDenial
    denial = ClaimDenial(
        claim_id=claim.id, denial_code="CO-4",
        denial_reason="Not covered",
    )
    db_session.add(denial)
    await db_session.flush()

    # 17. PriorAuthRequest
    pa = PriorAuthRequest(
        task_id=tasks["prior_auth"].id, patient_id=patient.id,
        status="pending", procedure_code="27447",
    )
    db_session.add(pa)
    await db_session.flush()

    # 18. PriorAuthAppeal
    appeal = PriorAuthAppeal(
        prior_auth_id=pa.id, appeal_level=1, status="draft",
    )
    db_session.add(appeal)
    await db_session.flush()

    # 19. CredentialingApplication
    cred = CredentialingApplication(
        task_id=tasks["credentialing"].id, provider_npi="1111111111",
        provider_name="Dr. Smoke", status="initiated",
    )
    db_session.add(cred)
    await db_session.flush()

    # 20. ComplianceReport
    comp = ComplianceReport(
        task_id=tasks["compliance"].id, organization_id=org.id,
        measure_set="HEDIS", reporting_period_start="2024-01-01",
        reporting_period_end="2024-12-31", status="pending",
    )
    db_session.add(comp)
    await db_session.flush()

    # Verify all objects got IDs (proves persistence worked)
    all_objects = [
        org, user, patient, encounter, wf, review, audit, phi_log,
        payer, rule, ch_config, ec, sr, claim, denial, pa, appeal, cred, comp,
    ]
    all_objects.extend(tasks.values())
    for obj in all_objects:
        assert obj.id is not None, f"{type(obj).__name__} failed to persist (id is None)"

    # Verify we can query back
    result = await db_session.execute(select(Organization).where(Organization.id == org.id))
    queried_org = result.scalar_one()
    assert queried_org.name == "Smoke Test Org"
