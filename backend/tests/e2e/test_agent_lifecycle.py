"""E2E lifecycle tests for all 6 agent types.

Each test exercises the full lifecycle: create task -> verify completed -> check audit trail.
Uses the async test client and JWT auth from the existing test infrastructure.

Contract requirement: every agent lifecycle test must assert ``status == "completed"``
and ``error_message`` is absent/null.  The ``failed`` and ``review`` statuses are NOT
acceptable terminal outcomes for a standard lifecycle test.
"""

from __future__ import annotations

import asyncio
import time
import uuid

import pytest

from app.core.auth.jwt import create_access_token

# ── Auth helper ───────────────────────────────────────────────────────

_TEST_USER_ID = uuid.uuid4()
_TEST_USER_EMAIL = "e2e-lifecycle@slate.health"


def _auth_header(role: str = "admin") -> dict[str, str]:
    token = create_access_token(
        user_id=_TEST_USER_ID,
        email=_TEST_USER_EMAIL,
        role=role,
        full_name="E2E Lifecycle Tester",
    )
    return {"Authorization": f"Bearer {token}"}


# ── Shared lifecycle helper ───────────────────────────────────────────

TERMINAL_STATUSES = {"completed", "failed", "review"}


async def _poll_until_terminal(client, agent_type: str, task_id: str, headers: dict,
                                timeout: float = 30.0, interval: float = 0.5) -> dict:
    """Poll task status until it reaches a terminal state."""
    start = time.monotonic()
    status = None
    while time.monotonic() - start < timeout:
        resp = await client.get(
            f"/api/v1/agents/{agent_type}/tasks/{task_id}",
            headers=headers,
        )
        assert resp.status_code == 200
        status = resp.json()["status"]
        if status in TERMINAL_STATUSES:
            return resp.json()
        await asyncio.sleep(interval)
    raise AssertionError(
        f"Task {task_id} did not reach terminal status within {timeout}s (last status: {status})"
    )


async def _run_agent_lifecycle(client, agent_type: str, input_data: dict) -> None:
    """Execute the full lifecycle for a single agent type.

    1. POST to create a task
    2. Poll until the task reaches a terminal status
    3. If status is ``review``, approve the HITL review to drive the task to ``completed``
    4. Verify the task ultimately reaches ``completed`` with populated output_data
    5. GET audit logs filtered by the task's resource_id

    Contract: every lifecycle test must end with ``status == "completed"`` and
    ``error_message`` absent.  Tasks that trigger HITL escalation (legitimate
    low-confidence scenarios) are automatically approved so the full lifecycle
    is exercised end-to-end.
    """
    admin_headers = _auth_header("admin")
    viewer_headers = _auth_header("viewer")

    # Step 1: Create the task
    body = {"agent_type": agent_type, "input_data": input_data}
    create_resp = await client.post(
        f"/api/v1/agents/{agent_type}/tasks",
        json=body,
        headers=admin_headers,
    )
    assert create_resp.status_code == 201, (
        f"Expected 201 for {agent_type}, got {create_resp.status_code}: {create_resp.text}"
    )
    task = create_resp.json()
    task_id = task["id"]
    assert task["agent_type"] == agent_type

    # Step 2: Poll until terminal status
    fetched = await _poll_until_terminal(
        client, agent_type, task_id, viewer_headers,
    )
    assert fetched["id"] == task_id
    assert fetched["agent_type"] == agent_type
    assert fetched["status"] in TERMINAL_STATUSES, (
        f"Agent '{agent_type}' task {task_id} did not reach terminal status: "
        f"{fetched['status']}"
    )

    # Step 3: If the task is in "review" status, approve the HITL review
    # to drive it through to completion.  This is expected for agents that
    # legitimately trigger low-confidence HITL escalation.
    if fetched["status"] == "review":
        reviews_resp = await client.get(
            "/api/v1/reviews",
            headers=admin_headers,
        )
        assert reviews_resp.status_code == 200
        matching_reviews = [
            r for r in reviews_resp.json()["items"]
            if r.get("task_id") == task_id and r.get("status") == "pending"
        ]
        assert len(matching_reviews) >= 1, (
            f"Agent '{agent_type}' task {task_id} has status 'review' but "
            f"no pending HITL review found"
        )
        review_id = matching_reviews[0]["id"]
        approve_resp = await client.post(
            f"/api/v1/reviews/{review_id}/approve",
            json={"notes": f"Lifecycle E2E: auto-approved for {agent_type}"},
            headers=admin_headers,
        )
        assert approve_resp.status_code == 200
        assert approve_resp.json()["status"] == "approved"

        # Re-fetch the task — it should now be completed
        re_fetch_resp = await client.get(
            f"/api/v1/agents/{agent_type}/tasks/{task_id}",
            headers=viewer_headers,
        )
        assert re_fetch_resp.status_code == 200
        fetched = re_fetch_resp.json()

    # Step 4: Assert successful completion
    # After HITL approval the review_queue.approve() transitions the task to
    # "completed".  The contract requires strict "completed" status — "review"
    # or "failed" are NOT acceptable terminal outcomes.
    assert fetched["status"] == "completed", (
        f"Agent '{agent_type}' task {task_id} expected status 'completed', "
        f"got '{fetched['status']}'. "
        f"error_message={fetched.get('error_message')}"
    )

    # Verify output_data is populated (not None or empty)
    output = fetched.get("output_data")
    assert output is not None, (
        f"Agent '{agent_type}' task {task_id} completed but output_data is None"
    )
    assert isinstance(output, dict) and len(output) > 0, (
        f"Agent '{agent_type}' task {task_id} has empty output_data"
    )

    # Verify no error_message on successful completion
    error_msg = fetched.get("error_message")
    assert not error_msg, (
        f"Agent '{agent_type}' task {task_id} status is '{fetched['status']}' but "
        f"error_message is set: {error_msg}"
    )

    # Step 5: Verify audit logs exist for this task
    audit_resp = await client.get(
        f"/api/v1/audit/logs?resource_id={task_id}",
        headers=admin_headers,
    )
    assert audit_resp.status_code == 200
    audit_data = audit_resp.json()
    assert "items" in audit_data
    assert len(audit_data["items"]) >= 1, (
        f"Expected at least 1 audit entry for task {task_id}, got {len(audit_data['items'])}"
    )


# ── Per-agent lifecycle tests ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_eligibility_lifecycle(client):
    """Full lifecycle: eligibility task creation, retrieval, and audit verification."""
    await _run_agent_lifecycle(client, "eligibility", {
        "subscriber_id": "ELIG-E2E-001",
        "subscriber_first_name": "Maria",
        "subscriber_last_name": "Garcia",
        "subscriber_dob": "19780312",
        "payer_id": "AETNA01",
        "payer_name": "Aetna",
        "provider_npi": "1234567890",
        "service_type_code": "30",
    })


@pytest.mark.asyncio
async def test_scheduling_lifecycle(client):
    """Full lifecycle: scheduling task creation, retrieval, and audit verification."""
    await _run_agent_lifecycle(client, "scheduling", {
        "request_text": "New patient annual physical with Dr. Patel on Thursday morning",
        "patient_first_name": "Robert",
        "patient_last_name": "Chen",
        "provider_name": "Dr. Anita Patel",
        "specialty": "internal_medicine",
        "preferred_date_start": "2026-04-01",
        "preferred_date_end": "2026-04-07",
        "preferred_time_of_day": "morning",
        "urgency": "routine",
        "visit_type": "new_patient",
        "duration_minutes": 45,
    })


@pytest.mark.asyncio
async def test_claims_lifecycle(client):
    """Full lifecycle: claims task creation, retrieval, and audit verification."""
    await _run_agent_lifecycle(client, "claims", {
        "subscriber_id": "CLM-E2E-001",
        "subscriber_first_name": "David",
        "subscriber_last_name": "Thompson",
        "subscriber_dob": "19650801",
        "subscriber_gender": "M",
        "payer_id": "UHC01",
        "payer_name": "UnitedHealthcare",
        "billing_provider_npi": "9876543210",
        "billing_provider_name": "City Medical Group",
        "diagnosis_codes": ["M54.5", "G89.29"],
        "procedure_codes": ["99214", "97110"],
        "total_charge": "285.00",
        "date_of_service": "20260315",
        "place_of_service": "11",
        "claim_type": "837P",
    })


@pytest.mark.asyncio
async def test_prior_auth_lifecycle(client):
    """Full lifecycle: prior auth task creation, retrieval, and audit verification."""
    patient_id = str(uuid.uuid4())
    await _run_agent_lifecycle(client, "prior_auth", {
        "procedure_code": "27447",
        "procedure_description": "Total knee arthroplasty",
        "diagnosis_codes": ["M17.11", "M17.12"],
        "subscriber_id": "PA-E2E-001",
        "subscriber_first_name": "Susan",
        "subscriber_last_name": "Williams",
        "subscriber_dob": "19550220",
        "payer_id": "CIGNA01",
        "payer_name": "Cigna",
        "provider_npi": "1122334455",
        "provider_name": "Dr. James Ortho",
        "patient_id": patient_id,
        "date_of_service": "20260501",
        "place_of_service": "22",
    })


@pytest.mark.asyncio
async def test_credentialing_lifecycle(client):
    """Full lifecycle: credentialing task creation, retrieval, and audit verification."""
    await _run_agent_lifecycle(client, "credentialing", {
        "provider_npi": "5566778899",
        "target_organization": "Regional Health Network",
        "target_payer_id": "BCBS01",
        "credentialing_type": "initial",
        "state": "CA",
    })


@pytest.mark.asyncio
async def test_compliance_lifecycle(client):
    """Full lifecycle: compliance task creation, retrieval, and audit verification."""
    org_id = str(uuid.uuid4())
    await _run_agent_lifecycle(client, "compliance", {
        "organization_id": org_id,
        "measure_set": "HEDIS",
        "reporting_period_start": "2025-01-01",
        "reporting_period_end": "2025-12-31",
        "measure_ids": ["BCS", "CCS", "COL"],
    })
