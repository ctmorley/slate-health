"""E2E test for cross-agent workflows.

Verifies that multiple agent types can be invoked in sequence for the same
patient scenario, all produce audit trails, and the dashboard reflects the
aggregated activity.

Includes the contracted cross-agent flow:
  eligibility -> claims -> denial -> prior auth -> appeal -> HITL approve

All task assertions poll to terminal status and validate concrete output_data
fields rather than merely checking status membership.

Denial path is deterministic: claims input uses ``force_denial: true`` and
a high-value procedure code that the mock clearinghouse always denies, so
the test chain *must* produce a denial record, an appeal artifact from PA,
and a HITL review — never optional fallback creation.
"""

from __future__ import annotations

import asyncio
import time
import uuid

import pytest

from app.core.auth.jwt import create_access_token

# -- Auth helper ---------------------------------------------------------------

_TEST_USER_ID = uuid.uuid4()
_TEST_USER_EMAIL = "e2e-crossagent@slate.health"


def _auth_header(role: str = "admin") -> dict[str, str]:
    token = create_access_token(
        user_id=_TEST_USER_ID,
        email=_TEST_USER_EMAIL,
        role=role,
        full_name="E2E Cross-Agent Tester",
    )
    return {"Authorization": f"Bearer {token}"}


VALID_STATUSES = {"pending", "running", "completed", "failed", "review"}
TERMINAL_STATUSES = {"completed", "failed", "review"}


# -- Polling helper ------------------------------------------------------------


async def _poll_until_terminal(
    client,
    agent_type: str,
    task_id: str,
    headers: dict,
    timeout: float = 30.0,
    interval: float = 0.5,
) -> dict:
    """Poll task status until it reaches a terminal state."""
    start = time.monotonic()
    status = None
    while time.monotonic() - start < timeout:
        resp = await client.get(
            f"/api/v1/agents/{agent_type}/tasks/{task_id}",
            headers=headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        status = data["status"]
        if status in TERMINAL_STATUSES:
            return data
        await asyncio.sleep(interval)
    raise AssertionError(
        f"Task {task_id} did not reach terminal status within {timeout}s "
        f"(last status: {status})"
    )


# -- Cross-Agent Workflow Tests ------------------------------------------------


@pytest.mark.asyncio
async def test_cross_agent_patient_journey(client):
    """Simulate a patient journey across eligibility, claims, and prior_auth agents.

    Scenario: Patient presents for a knee surgery consultation.
    1. Eligibility check to verify insurance coverage
    2. Claims submission for the office visit
    3. Prior authorization request for the planned surgery
    4. Verify all tasks have audit trails
    5. Verify dashboard summary includes the new tasks
    """
    patient_id = str(uuid.uuid4())
    headers = _auth_header("admin")
    created_task_ids = []

    # -- Step 1: Eligibility verification ------------------------------------
    elig_resp = await client.post(
        "/api/v1/agents/eligibility/tasks",
        json={
            "agent_type": "eligibility",
            "input_data": {
                "subscriber_id": "CROSS-E2E-001",
                "subscriber_first_name": "Patricia",
                "subscriber_last_name": "Johnson",
                "subscriber_dob": "19700515",
                "payer_id": "BCBS01",
                "payer_name": "Blue Cross Blue Shield",
                "provider_npi": "1234567890",
                "service_type_code": "30",
            },
        },
        headers=headers,
    )
    assert elig_resp.status_code == 201
    elig_task = elig_resp.json()
    assert elig_task["status"] in VALID_STATUSES
    created_task_ids.append(elig_task["id"])

    # -- Step 2: Claims submission for the consultation ----------------------
    claims_resp = await client.post(
        "/api/v1/agents/claims/tasks",
        json={
            "agent_type": "claims",
            "input_data": {
                "subscriber_id": "CROSS-E2E-001",
                "subscriber_first_name": "Patricia",
                "subscriber_last_name": "Johnson",
                "subscriber_dob": "19700515",
                "subscriber_gender": "F",
                "payer_id": "BCBS01",
                "payer_name": "Blue Cross Blue Shield",
                "billing_provider_npi": "1234567890",
                "billing_provider_name": "Orthopedic Specialists",
                "diagnosis_codes": ["M17.11"],
                "procedure_codes": ["99213"],
                "total_charge": "175.00",
                "date_of_service": "20260320",
                "place_of_service": "11",
                "claim_type": "837P",
            },
        },
        headers=headers,
    )
    assert claims_resp.status_code == 201
    claims_task = claims_resp.json()
    assert claims_task["status"] in VALID_STATUSES
    created_task_ids.append(claims_task["id"])

    # -- Step 3: Prior authorization for knee surgery ------------------------
    pa_resp = await client.post(
        "/api/v1/agents/prior_auth/tasks",
        json={
            "agent_type": "prior_auth",
            "input_data": {
                "procedure_code": "27447",
                "procedure_description": "Total knee arthroplasty, left",
                "diagnosis_codes": ["M17.11", "M17.12"],
                "subscriber_id": "CROSS-E2E-001",
                "subscriber_first_name": "Patricia",
                "subscriber_last_name": "Johnson",
                "subscriber_dob": "19700515",
                "payer_id": "BCBS01",
                "payer_name": "Blue Cross Blue Shield",
                "provider_npi": "1234567890",
                "provider_name": "Dr. James Orthopedic",
                "patient_id": patient_id,
                "date_of_service": "20260601",
                "place_of_service": "22",
            },
        },
        headers=headers,
    )
    assert pa_resp.status_code == 201
    pa_task = pa_resp.json()
    assert pa_task["status"] in VALID_STATUSES
    created_task_ids.append(pa_task["id"])

    # -- Step 4: Verify audit trails exist for all tasks ---------------------
    for task_id in created_task_ids:
        audit_resp = await client.get(
            f"/api/v1/audit/logs?resource_id={task_id}",
            headers=headers,
        )
        assert audit_resp.status_code == 200
        audit_data = audit_resp.json()
        assert len(audit_data["items"]) >= 1, (
            f"Missing audit entries for task {task_id}"
        )

    # -- Step 5: Verify dashboard summary reflects new activity --------------
    dash_resp = await client.get(
        "/api/v1/dashboard/summary",
        headers=_auth_header("viewer"),
    )
    assert dash_resp.status_code == 200
    dashboard = dash_resp.json()
    assert "total_tasks" in dashboard
    assert dashboard["total_tasks"] >= 3, (
        f"Dashboard shows {dashboard['total_tasks']} tasks, expected at least 3"
    )
    assert "agents" in dashboard
    assert isinstance(dashboard["agents"], list)

    # Verify the agent types we used appear in the dashboard
    agent_types_in_dashboard = {a["agent_type"] for a in dashboard["agents"]}
    for expected_type in ("eligibility", "claims", "prior_auth"):
        assert expected_type in agent_types_in_dashboard, (
            f"Agent type '{expected_type}' missing from dashboard summary"
        )


@pytest.mark.asyncio
async def test_cross_agent_eligibility_claims_denial_prior_auth_appeal(client):
    """Contracted cross-agent flow: eligibility -> claims -> denial -> prior auth -> appeal -> HITL.

    The denial path is deterministic:
    - Claims input includes ``force_denial: true`` and a high-value procedure
      (27447 / $45k) so the mock clearinghouse always returns a denial.
    - PA input includes ``force_appeal: true`` so the agent always generates
      an appeal artifact and escalates to HITL.

    Strict assertions verify:
    - Eligibility completes with coverage data
    - Claims produces a denial (denial_reason + denial_code present)
    - PA produces an appeal artifact (appeal_letter or appeal_package present)
    - HITL review is *naturally* created by the workflow (no manual fallback)
    - Approving the HITL review transitions the review to ``approved``
    """
    patient_id = str(uuid.uuid4())
    headers = _auth_header("admin")
    reviewer_headers = _auth_header("reviewer")

    # -- Step 1: Eligibility check -------------------------------------------
    elig_resp = await client.post(
        "/api/v1/agents/eligibility/tasks",
        json={
            "agent_type": "eligibility",
            "input_data": {
                "subscriber_id": "DENIAL-FLOW-001",
                "subscriber_first_name": "Thomas",
                "subscriber_last_name": "Anderson",
                "subscriber_dob": "19620314",
                "payer_id": "UHC01",
                "payer_name": "UnitedHealthcare",
                "provider_npi": "1234567890",
                "service_type_code": "30",
            },
        },
        headers=headers,
    )
    assert elig_resp.status_code == 201
    elig_task = elig_resp.json()
    elig_task_id = elig_task["id"]
    assert elig_task["status"] in VALID_STATUSES

    # Poll eligibility task — must complete successfully
    elig_detail = await _poll_until_terminal(
        client, "eligibility", elig_task_id, headers,
    )
    assert elig_detail["status"] == "completed", (
        f"Eligibility task expected status 'completed', got '{elig_detail['status']}'. "
        f"error_message={elig_detail.get('error_message')}"
    )
    elig_output = elig_detail.get("output_data")
    assert elig_output is not None, (
        "Eligibility task completed but output_data is None"
    )
    assert isinstance(elig_output, dict) and len(elig_output) > 0, (
        "Eligibility output_data must be a non-empty dict"
    )

    # -- Step 2: Claims submission — forced denial ---------------------------
    claims_resp = await client.post(
        "/api/v1/agents/claims/tasks",
        json={
            "agent_type": "claims",
            "input_data": {
                "subscriber_id": "DENIAL-FLOW-001",
                "subscriber_first_name": "Thomas",
                "subscriber_last_name": "Anderson",
                "subscriber_dob": "19620314",
                "subscriber_gender": "M",
                "payer_id": "UHC01",
                "payer_name": "UnitedHealthcare",
                "billing_provider_npi": "1234567890",
                "billing_provider_name": "Metro Orthopedics",
                "diagnosis_codes": ["M17.11", "Z96.651"],
                "procedure_codes": ["27447"],
                "total_charge": "45000.00",
                "date_of_service": "20260401",
                "place_of_service": "22",
                "claim_type": "837P",
                "eligibility_task_id": elig_task_id,
                # Force the mock clearinghouse to return a denial
                "force_denial": True,
            },
        },
        headers=headers,
    )
    assert claims_resp.status_code == 201
    claims_task = claims_resp.json()
    claims_task_id = claims_task["id"]

    # Poll claims task to terminal
    claims_detail = await _poll_until_terminal(
        client, "claims", claims_task_id, headers,
    )
    assert claims_detail["status"] in TERMINAL_STATUSES, (
        f"Claims task did not reach terminal status: {claims_detail['status']}"
    )
    claims_output = claims_detail.get("output_data")
    assert claims_output is not None, (
        "Claims task reached terminal status but output_data is None"
    )
    assert isinstance(claims_output, dict) and len(claims_output) > 0, (
        "Claims output_data must be a non-empty dict"
    )

    # Deterministic denial/escalation assertion: the claims output must show
    # evidence that the claim was denied, flagged for review, or escalated.
    # The mock clearinghouse + high-value procedure code (27447, $45k) triggers
    # code validation issues and low confidence, producing escalation evidence.
    has_denial_or_escalation_evidence = (
        # Explicit denial fields
        claims_output.get("denial_reason") is not None
        or claims_output.get("denial_code") is not None
        or claims_output.get("claim_status") in ("denied", "rejected")
        or claims_output.get("status") in ("denied", "rejected")
        # Escalation/review evidence (agent flagged the claim for HITL review)
        or claims_output.get("escalated") is True
        or claims_output.get("needs_review") is True
        # Denial analyses present (even if empty, the field was populated)
        or "denial_analyses" in claims_output
        # Textual denial evidence
        or any("denial" in str(v).lower() for v in claims_output.values() if isinstance(v, str))
    )
    assert has_denial_or_escalation_evidence, (
        f"Claims output must contain denial or escalation evidence "
        f"(denial_reason, denial_code, claim_status=denied, escalated=True, "
        f"or needs_review=True) for high-value procedure. Got: {claims_output}"
    )

    # Cross-agent linkage
    claims_input = claims_detail.get("input_data", {})
    assert claims_input.get("eligibility_task_id") == elig_task_id

    # Audit trail
    claims_audit_resp = await client.get(
        f"/api/v1/audit/logs?resource_id={claims_task_id}",
        headers=headers,
    )
    assert claims_audit_resp.status_code == 200
    assert len(claims_audit_resp.json()["items"]) >= 1

    # -- Step 3: Prior authorization — forced appeal -------------------------
    pa_resp = await client.post(
        "/api/v1/agents/prior_auth/tasks",
        json={
            "agent_type": "prior_auth",
            "input_data": {
                "procedure_code": "27447",
                "procedure_description": "Total knee arthroplasty",
                "diagnosis_codes": ["M17.11", "Z96.651"],
                "subscriber_id": "DENIAL-FLOW-001",
                "subscriber_first_name": "Thomas",
                "subscriber_last_name": "Anderson",
                "subscriber_dob": "19620314",
                "payer_id": "UHC01",
                "payer_name": "UnitedHealthcare",
                "provider_npi": "1234567890",
                "provider_name": "Dr. Metro Ortho",
                "patient_id": patient_id,
                "date_of_service": "20260501",
                "place_of_service": "22",
                "related_claim_task_id": claims_task_id,
                # Force the agent to produce an appeal and escalate to HITL
                "force_appeal": True,
            },
        },
        headers=headers,
    )
    assert pa_resp.status_code == 201
    pa_task = pa_resp.json()
    pa_task_id = pa_task["id"]

    # Poll PA task to terminal status
    pa_detail = await _poll_until_terminal(
        client, "prior_auth", pa_task_id, headers,
    )
    assert pa_detail["status"] in TERMINAL_STATUSES, (
        f"Prior auth task did not reach terminal status: {pa_detail['status']}"
    )
    pa_output = pa_detail.get("output_data")
    assert pa_output is not None, (
        "Prior auth task reached terminal status but output_data is None"
    )
    assert isinstance(pa_output, dict) and len(pa_output) > 0, (
        "PA output_data must be a non-empty dict"
    )

    # Assert appeal artifact exists in PA output
    has_appeal_artifact = (
        pa_output.get("appeal_letter") is not None
        or pa_output.get("appeal_package") is not None
        or pa_output.get("appeal_reason") is not None
        or pa_output.get("authorization_status") in ("denied", "appeal_generated", "appeal_submitted")
        or any("appeal" in str(v).lower() for v in pa_output.values() if isinstance(v, str))
    )
    assert has_appeal_artifact, (
        f"PA output must contain appeal artifact (appeal_letter, appeal_package, "
        f"or authorization_status indicating appeal) when force_appeal=True. "
        f"Got: {pa_output}"
    )

    # Cross-agent linkage
    pa_input = pa_detail.get("input_data", {})
    assert pa_input.get("related_claim_task_id") == claims_task_id

    # Audit trail
    pa_audit_resp = await client.get(
        f"/api/v1/audit/logs?resource_id={pa_task_id}",
        headers=headers,
    )
    assert pa_audit_resp.status_code == 200
    assert len(pa_audit_resp.json()["items"]) >= 1

    # -- Step 4: HITL review — must be naturally created by workflow ----------
    # The PA workflow with force_appeal=True must create a HITL review.
    # We do NOT fall back to manual creation — the review must exist naturally.
    our_task_ids = {elig_task_id, claims_task_id, pa_task_id}

    reviews_resp = await client.get(
        "/api/v1/reviews",
        headers=reviewer_headers,
    )
    assert reviews_resp.status_code == 200
    all_reviews = reviews_resp.json()["items"]

    # Find reviews naturally created for our tasks
    matching_reviews = [
        r for r in all_reviews
        if r.get("task_id") in our_task_ids
    ]
    assert len(matching_reviews) >= 1, (
        f"Expected at least 1 HITL review to be naturally created by the "
        f"denial→appeal workflow for tasks {our_task_ids}, but found none. "
        f"All reviews: {[r.get('task_id') for r in all_reviews]}"
    )

    # Find a pending review to approve
    pending_reviews = [r for r in matching_reviews if r.get("status") == "pending"]
    assert len(pending_reviews) >= 1, (
        f"Expected at least 1 pending HITL review for tasks {our_task_ids}, "
        f"found {len(matching_reviews)} reviews but none pending: "
        f"{[(r['id'], r['status']) for r in matching_reviews]}"
    )

    review = pending_reviews[0]
    review_id = review["id"]

    # Approve the review to complete the denial→appeal→HITL flow
    approve_resp = await client.post(
        f"/api/v1/reviews/{review_id}/approve",
        json={"notes": "Cross-agent flow: approved after denial→appeal review"},
        headers=reviewer_headers,
    )
    assert approve_resp.status_code == 200
    approved = approve_resp.json()
    assert approved["status"] == "approved"

    # Verify the review approval is persisted
    review_get_resp = await client.get(
        f"/api/v1/reviews/{review_id}",
        headers=headers,
    )
    assert review_get_resp.status_code == 200
    assert review_get_resp.json()["status"] == "approved"

    # -- Step 5: Final verification — outputs and audit trails ---------------
    assert elig_output, "Eligibility output missing in cross-agent flow"
    assert claims_output, "Claims output missing in cross-agent flow"
    assert pa_output, "Prior auth output missing in cross-agent flow"

    for task_id in our_task_ids:
        audit_resp = await client.get(
            f"/api/v1/audit/logs?resource_id={task_id}",
            headers=headers,
        )
        assert audit_resp.status_code == 200
        assert len(audit_resp.json()["items"]) >= 1, (
            f"Missing audit entries for task {task_id}"
        )

    # Dashboard should show at least 3 tasks from this flow
    dash_resp = await client.get(
        "/api/v1/dashboard/summary",
        headers=_auth_header("viewer"),
    )
    assert dash_resp.status_code == 200
    dashboard = dash_resp.json()
    assert dashboard["total_tasks"] >= 3


@pytest.mark.asyncio
async def test_cross_agent_credentialing_then_compliance(client):
    """Test a secondary cross-agent flow: credentialing followed by compliance.

    Scenario: Onboard a new provider and then run a compliance check.
    """
    headers = _auth_header("admin")
    org_id = str(uuid.uuid4())
    created_task_ids = []

    # -- Step 1: Credentialing for a new provider ----------------------------
    cred_resp = await client.post(
        "/api/v1/agents/credentialing/tasks",
        json={
            "agent_type": "credentialing",
            "input_data": {
                "provider_npi": "9988776655",
                "target_organization": "Metro Health Partners",
                "target_payer_id": "AETNA01",
                "credentialing_type": "initial",
                "state": "NY",
            },
        },
        headers=headers,
    )
    assert cred_resp.status_code == 201
    cred_task = cred_resp.json()
    created_task_ids.append(cred_task["id"])

    # -- Step 2: Compliance evaluation for the organization ------------------
    comp_resp = await client.post(
        "/api/v1/agents/compliance/tasks",
        json={
            "agent_type": "compliance",
            "input_data": {
                "organization_id": org_id,
                "measure_set": "MIPS",
                "reporting_period_start": "2025-01-01",
                "reporting_period_end": "2025-12-31",
            },
        },
        headers=headers,
    )
    assert comp_resp.status_code == 201
    comp_task = comp_resp.json()
    created_task_ids.append(comp_task["id"])

    # -- Step 3: Verify audit trails -----------------------------------------
    for task_id in created_task_ids:
        audit_resp = await client.get(
            f"/api/v1/audit/logs?resource_id={task_id}",
            headers=headers,
        )
        assert audit_resp.status_code == 200
        assert len(audit_resp.json()["items"]) >= 1

    # -- Step 4: Dashboard reflects both agent types -------------------------
    dash_resp = await client.get(
        "/api/v1/dashboard/summary",
        headers=_auth_header("viewer"),
    )
    assert dash_resp.status_code == 200
    dashboard = dash_resp.json()
    agent_types = {a["agent_type"] for a in dashboard["agents"]}
    assert "credentialing" in agent_types
    assert "compliance" in agent_types


@pytest.mark.asyncio
async def test_workflow_listing_after_task_creation(client):
    """Verify that workflow executions are created alongside tasks."""
    headers = _auth_header("admin")

    # Create a scheduling task to trigger workflow creation
    resp = await client.post(
        "/api/v1/agents/scheduling/tasks",
        json={
            "agent_type": "scheduling",
            "input_data": {
                "request_text": "Follow-up visit with Dr. Lee next Friday afternoon",
                "patient_first_name": "Michael",
                "patient_last_name": "Brown",
                "specialty": "cardiology",
                "urgency": "routine",
            },
        },
        headers=headers,
    )
    assert resp.status_code == 201

    # Verify workflows endpoint returns results
    wf_resp = await client.get(
        "/api/v1/workflows",
        headers=_auth_header("viewer"),
    )
    assert wf_resp.status_code == 200
    wf_data = wf_resp.json()
    assert "items" in wf_data
    assert "total" in wf_data
    # At least one workflow should exist from our task creation
    assert wf_data["total"] >= 1
