"""E2E test for the Human-in-the-Loop (HITL) review flow.

Exercises the full deterministic path:
  task creation (with force_low_confidence) -> poll to terminal -> find review
  -> approve/reject -> verify.

Uses the ``force_low_confidence`` input flag to deterministically trigger
low confidence (0.3) in the eligibility workflow, which causes the standard
escalation path to create a HITL review.  No DB seeding fallbacks are used —
the review must be created naturally by the workflow's confidence evaluation
and EscalationManager.
"""

from __future__ import annotations

import asyncio
import time
import uuid

import pytest

from app.core.auth.jwt import create_access_token

# -- Auth helpers --------------------------------------------------------------

_VIEWER_ID = uuid.uuid4()
_REVIEWER_ID = uuid.uuid4()
_VIEWER_EMAIL = "e2e-hitl-viewer@slate.health"
_REVIEWER_EMAIL = "e2e-hitl-reviewer@slate.health"

TERMINAL_STATUSES = {"completed", "failed", "review"}


def _auth_header(role: str = "admin", user_id: uuid.UUID | None = None,
                 email: str | None = None) -> dict[str, str]:
    uid = user_id or (
        _REVIEWER_ID if role == "reviewer" else _VIEWER_ID
    )
    mail = email or (
        _REVIEWER_EMAIL if role == "reviewer" else _VIEWER_EMAIL
    )
    token = create_access_token(
        user_id=uid,
        email=mail,
        role=role,
        full_name=f"E2E HITL {role.title()}",
    )
    return {"Authorization": f"Bearer {token}"}


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


async def _find_review_for_tasks(client, task_ids: set[str], headers: dict) -> dict | None:
    """Query the reviews endpoint and return the first review matching any of *task_ids*."""
    reviews_resp = await client.get(
        "/api/v1/reviews",
        headers=headers,
    )
    assert reviews_resp.status_code == 200
    for r in reviews_resp.json()["items"]:
        if r.get("task_id") in task_ids:
            return r
    return None


async def _ensure_review_exists(client, headers_admin: dict, headers_reviewer: dict) -> dict:
    """Create a task with forced low confidence to deterministically trigger HITL review.

    Strategy:
      1. Create an eligibility task with ``force_low_confidence=True`` in input_data.
         This flag propagates through the workflow pipeline and forces the
         confidence score to 0.3 (below the 0.7 threshold), which deterministically
         triggers HITL review creation via the standard escalation path.
      2. Poll to terminal status (should reach "review").
      3. Verify the review was created naturally by the workflow.

    This approach validates the complete HITL trigger chain (confidence evaluation
    → escalation → review creation) rather than seeding DB records directly.

    Returns the review dict from the API.
    """
    # -- Create an eligibility task with forced low confidence ----------------
    create_resp = await client.post(
        "/api/v1/agents/eligibility/tasks",
        json={
            "agent_type": "eligibility",
            "input_data": {
                "subscriber_id": f"HITL-{uuid.uuid4().hex[:8]}",
                "subscriber_first_name": "Alice",
                "subscriber_last_name": "Mendez",
                "subscriber_dob": "19900415",
                "payer_id": "BCBS01",
                "payer_name": "Blue Cross Blue Shield",
                "provider_npi": "1122334455",
                "service_type_code": "30",
                "force_low_confidence": True,
            },
        },
        headers=headers_admin,
    )
    assert create_resp.status_code == 201
    task = create_resp.json()
    task_id = task["id"]

    # Poll to terminal — should reach "review" status due to low confidence
    result = await _poll_until_terminal(client, "eligibility", task_id, headers_admin)
    assert result["status"] == "review", (
        f"Expected task to reach 'review' status with force_low_confidence=True, "
        f"got '{result['status']}'. The HITL escalation path may not be triggering. "
        f"output_data={result.get('output_data')}, error={result.get('error_message')}"
    )

    # Find the review created naturally by the workflow's escalation logic
    review = await _find_review_for_tasks(client, {task_id}, headers_reviewer)
    assert review is not None, (
        f"No HITL review found for task {task_id} despite force_low_confidence=True. "
        f"The escalation manager should have created a review when confidence < 0.7."
    )
    return review


# -- HITL Review Flow Tests ----------------------------------------------------


@pytest.mark.asyncio
async def test_hitl_review_approve_flow(client):
    """Create a task that triggers HITL review, then approve it.

    Uses eligibility agent with force_low_confidence=True to
    deterministically trigger review creation via the standard
    escalation path.
    """
    admin_headers = _auth_header("admin")
    reviewer_headers = _auth_header("reviewer")
    viewer_headers = _auth_header("viewer")

    # Step 1 & 2: Create task(s) and obtain a pending review
    review = await _ensure_review_exists(client, admin_headers, reviewer_headers)
    review_id = review["id"]
    task_id = review["task_id"]

    assert review["status"] in ("pending", "flagged")

    # Step 3: Approve the review
    approve_resp = await client.post(
        f"/api/v1/reviews/{review_id}/approve",
        json={"notes": "Verified coverage manually via payer portal"},
        headers=reviewer_headers,
    )
    assert approve_resp.status_code == 200
    approved = approve_resp.json()
    assert approved["status"] == "approved"
    assert approved["id"] == review_id

    # Step 4: Verify the review is now marked approved when fetched directly
    review_get_resp = await client.get(
        f"/api/v1/reviews/{review_id}",
        headers=viewer_headers,
    )
    assert review_get_resp.status_code == 200
    assert review_get_resp.json()["status"] == "approved"

    # Step 5: Verify the associated task reflects the review outcome
    agent_type = review.get("agent_type", "eligibility")
    task_resp = await client.get(
        f"/api/v1/agents/{agent_type}/tasks/{task_id}",
        headers=viewer_headers,
    )
    assert task_resp.status_code == 200
    # After approval the ReviewQueue sets the task to "completed"
    assert task_resp.json()["status"] == "completed", (
        f"Expected task status 'completed' after approval, "
        f"got '{task_resp.json()['status']}'"
    )


@pytest.mark.asyncio
async def test_hitl_review_reject_flow(client):
    """Create a task that triggers HITL review, then reject it.

    This tests the rejection path of the HITL flow.
    """
    admin_headers = _auth_header("admin")
    reviewer_headers = _auth_header("reviewer")
    viewer_headers = _auth_header("viewer")

    # Create task(s) and obtain a pending review
    review = await _ensure_review_exists(client, admin_headers, reviewer_headers)
    review_id = review["id"]

    # Reject the review
    reject_resp = await client.post(
        f"/api/v1/reviews/{review_id}/reject",
        json={"notes": "Coverage data appears inconsistent, rejecting for re-check"},
        headers=reviewer_headers,
    )
    assert reject_resp.status_code == 200
    rejected = reject_resp.json()
    assert rejected["status"] == "rejected"

    # Verify rejection is persisted
    review_get_resp = await client.get(
        f"/api/v1/reviews/{review_id}",
        headers=viewer_headers,
    )
    assert review_get_resp.status_code == 200
    assert review_get_resp.json()["status"] == "rejected"


@pytest.mark.asyncio
async def test_hitl_reviews_pagination(client):
    """Verify reviews endpoint supports pagination parameters."""
    resp = await client.get(
        "/api/v1/reviews?limit=5&offset=0",
        headers=_auth_header("viewer"),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data
    assert "total" in data
    assert data["limit"] == 5
    assert data["offset"] == 0
