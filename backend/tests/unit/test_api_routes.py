"""Unit tests for Sprint 5 API routes with mock services.

Tests cover: agent routes, review routes, workflow routes, payer routes,
audit routes, dashboard routes, auth routes, WebSocket, and error handling.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import date, datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.auth.jwt import create_access_token
from app.dependencies import get_db
from app.main import create_app
from app.models.agent_task import AgentTask
from app.models.audit import AuditLog
from app.models.hitl_review import HITLReview
from app.models.payer import Payer, PayerRule
from app.models.workflow import WorkflowExecution

# ── Helper: Create a valid JWT for test requests ──────────────────────

_TEST_USER_ID = uuid.uuid4()
_TEST_USER_EMAIL = "test@slate.health"


def _auth_header(role: str = "admin") -> dict[str, str]:
    """Create an Authorization header with a valid JWT."""
    token = create_access_token(
        user_id=_TEST_USER_ID,
        email=_TEST_USER_EMAIL,
        role=role,
        full_name="Test User",
    )
    return {"Authorization": f"Bearer {token}"}


# ── Test Fixtures ─────────────────────────────────────────────────────


@pytest.fixture
async def app_client(test_engine, db_session):
    """Provide a test client with the full API router mounted."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    session_factory = async_sessionmaker(
        test_engine, class_=AsyncSession, expire_on_commit=False,
    )

    # Use a shared session for the test so we can inspect created data
    async def override_get_db():
        async with session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app = create_app()
    app.dependency_overrides[get_db] = override_get_db

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ── Agent Route Tests ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_eligibility_task(app_client):
    """POST /api/v1/agents/eligibility/tasks creates a task and returns 201."""
    body = {
        "agent_type": "eligibility",
        "input_data": {
            "subscriber_id": "INS-12345",
            "subscriber_first_name": "Jane",
            "subscriber_last_name": "Doe",
            "subscriber_dob": "19850615",
            "payer_id": "BCBS01",
            "payer_name": "Blue Cross Blue Shield",
        },
    }
    resp = await app_client.post(
        "/api/v1/agents/eligibility/tasks",
        json=body,
        headers=_auth_header("viewer"),
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["agent_type"] == "eligibility"
    assert data["status"] in ("pending", "running", "completed", "failed", "review")
    assert data["id"] is not None


@pytest.mark.asyncio
async def test_create_scheduling_task(app_client):
    """POST /api/v1/agents/scheduling/tasks creates a task and returns 201."""
    body = {
        "agent_type": "scheduling",
        "input_data": {
            "request_text": "annual checkup with Dr. Smith next Tuesday",
        },
    }
    resp = await app_client.post(
        "/api/v1/agents/scheduling/tasks",
        json=body,
        headers=_auth_header("viewer"),
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["agent_type"] == "scheduling"
    assert data["status"] in ("pending", "running", "completed", "failed", "review")
    assert data["id"] is not None


@pytest.mark.asyncio
async def test_create_claims_task(app_client):
    """POST /api/v1/agents/claims/tasks creates a task and returns 201."""
    body = {
        "agent_type": "claims",
        "input_data": {
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
    }
    resp = await app_client.post(
        "/api/v1/agents/claims/tasks",
        json=body,
        headers=_auth_header("viewer"),
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["agent_type"] == "claims"
    assert data["status"] in ("pending", "running", "completed", "failed", "review")
    assert data["id"] is not None


@pytest.mark.asyncio
async def test_create_task_invalid_agent_type(app_client):
    """POST with invalid agent_type returns 400."""
    resp = await app_client.post(
        "/api/v1/agents/invalid_type/tasks",
        json={"agent_type": "invalid_type", "input_data": {}},
        headers=_auth_header("viewer"),
    )
    assert resp.status_code == 400
    assert "invalid_type" in resp.json()["detail"].lower() or "Invalid" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_list_tasks(app_client):
    """GET /api/v1/agents/eligibility/tasks returns a paginated list."""
    # Create a task first
    await app_client.post(
        "/api/v1/agents/eligibility/tasks",
        json={
            "agent_type": "eligibility",
            "input_data": {"subscriber_id": "X1", "subscriber_first_name": "A", "subscriber_last_name": "B"},
        },
        headers=_auth_header(),
    )

    resp = await app_client.get(
        "/api/v1/agents/eligibility/tasks",
        headers=_auth_header("viewer"),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data
    assert "total" in data
    assert isinstance(data["items"], list)


@pytest.mark.asyncio
async def test_get_task_not_found(app_client):
    """GET task by unknown ID returns 404."""
    fake_id = str(uuid.uuid4())
    resp = await app_client.get(
        f"/api/v1/agents/eligibility/tasks/{fake_id}",
        headers=_auth_header("viewer"),
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_agent_stats(app_client):
    """GET /api/v1/agents/eligibility/stats returns stats."""
    resp = await app_client.get(
        "/api/v1/agents/eligibility/stats",
        headers=_auth_header("viewer"),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["agent_type"] == "eligibility"
    assert "total_tasks" in data
    assert "pending" in data


# ── Review Route Tests ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_reviews(app_client):
    """GET /api/v1/reviews returns paginated review list."""
    resp = await app_client.get(
        "/api/v1/reviews",
        headers=_auth_header("viewer"),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data
    assert "total" in data


@pytest.mark.asyncio
async def test_get_review_not_found(app_client):
    """GET review by unknown ID returns 404."""
    fake_id = str(uuid.uuid4())
    resp = await app_client.get(
        f"/api/v1/reviews/{fake_id}",
        headers=_auth_header("viewer"),
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_approve_review_not_found(app_client):
    """POST approve for unknown review returns 404."""
    fake_id = str(uuid.uuid4())
    resp = await app_client.post(
        f"/api/v1/reviews/{fake_id}/approve",
        json={"notes": "looks good"},
        headers=_auth_header("reviewer"),
    )
    assert resp.status_code == 404


# ── Workflow Route Tests ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_workflows(app_client):
    """GET /api/v1/workflows returns paginated list."""
    resp = await app_client.get(
        "/api/v1/workflows",
        headers=_auth_header("viewer"),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data
    assert "total" in data


@pytest.mark.asyncio
async def test_get_workflow_not_found(app_client):
    """GET workflow by unknown ID returns 404."""
    resp = await app_client.get(
        "/api/v1/workflows/nonexistent-workflow-id",
        headers=_auth_header("viewer"),
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_workflow_history(app_client):
    """GET /api/v1/workflows/{id}/history returns event history."""
    # Create a task to get a workflow
    create_resp = await app_client.post(
        "/api/v1/agents/eligibility/tasks",
        json={
            "agent_type": "eligibility",
            "input_data": {"subscriber_id": "X2", "subscriber_first_name": "C", "subscriber_last_name": "D"},
        },
        headers=_auth_header(),
    )
    task = create_resp.json()

    # List workflows to find the one created
    wf_resp = await app_client.get(
        "/api/v1/workflows",
        headers=_auth_header("viewer"),
    )
    items = wf_resp.json()["items"]
    if items:
        wf_id = items[0]["workflow_id"]
        history_resp = await app_client.get(
            f"/api/v1/workflows/{wf_id}/history",
            headers=_auth_header("viewer"),
        )
        assert history_resp.status_code == 200
        data = history_resp.json()
        assert "events" in data


# ── Payer Route Tests ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_payers(app_client):
    """GET /api/v1/payers returns list."""
    resp = await app_client.get(
        "/api/v1/payers",
        headers=_auth_header("viewer"),
    )
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.asyncio
async def test_create_payer(app_client):
    """POST /api/v1/payers creates a payer (admin only)."""
    body = {
        "name": "Test Payer",
        "payer_id_code": f"TP-{uuid.uuid4().hex[:6]}",
        "payer_type": "commercial",
    }
    resp = await app_client.post(
        "/api/v1/payers",
        json=body,
        headers=_auth_header("admin"),
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Test Payer"


@pytest.mark.asyncio
async def test_create_payer_forbidden_for_viewer(app_client):
    """POST payer as viewer returns 403."""
    body = {
        "name": "Test Payer 2",
        "payer_id_code": f"TP2-{uuid.uuid4().hex[:6]}",
    }
    resp = await app_client.post(
        "/api/v1/payers",
        json=body,
        headers=_auth_header("viewer"),
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_payer_rules_not_found(app_client):
    """GET rules for nonexistent payer returns 404."""
    fake_id = str(uuid.uuid4())
    resp = await app_client.get(
        f"/api/v1/payers/{fake_id}/rules",
        headers=_auth_header("viewer"),
    )
    assert resp.status_code == 404


# ── Audit Route Tests ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_query_audit_logs(app_client):
    """GET /api/v1/audit/logs returns log entries."""
    resp = await app_client.get(
        "/api/v1/audit/logs",
        headers=_auth_header("viewer"),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data


@pytest.mark.asyncio
async def test_phi_access_requires_admin(app_client):
    """GET /api/v1/audit/phi-access requires admin role."""
    resp = await app_client.get(
        "/api/v1/audit/phi-access",
        headers=_auth_header("viewer"),
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_phi_access_as_admin(app_client):
    """GET /api/v1/audit/phi-access works for admin."""
    resp = await app_client.get(
        "/api/v1/audit/phi-access",
        headers=_auth_header("admin"),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data


# ── Dashboard Route Tests ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dashboard_summary(app_client):
    """GET /api/v1/dashboard/summary returns aggregate stats."""
    resp = await app_client.get(
        "/api/v1/dashboard/summary",
        headers=_auth_header("viewer"),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "total_tasks" in data
    assert "agents" in data
    assert isinstance(data["agents"], list)
    # Should have 6 agent types
    assert len(data["agents"]) == 6


@pytest.mark.asyncio
async def test_agent_metrics(app_client):
    """GET /api/v1/dashboard/agents/eligibility/metrics returns metrics."""
    resp = await app_client.get(
        "/api/v1/dashboard/agents/eligibility/metrics",
        headers=_auth_header("viewer"),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["agent_type"] == "eligibility"


@pytest.mark.asyncio
async def test_agent_metrics_invalid_type(app_client):
    """GET metrics for invalid agent type returns 400."""
    resp = await app_client.get(
        "/api/v1/dashboard/agents/invalid_type/metrics",
        headers=_auth_header("viewer"),
    )
    assert resp.status_code == 400


# ── Auth Route Tests ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_auth_login(app_client):
    """POST /api/v1/auth/login returns redirect URL."""
    from app.api.v1.auth import set_oidc_provider
    from app.core.auth.oidc import OIDCProvider

    mock_provider = OIDCProvider(client_id="test-client")
    mock_provider.set_discovery_doc({
        "authorization_endpoint": "https://idp.test/authorize",
    })
    set_oidc_provider(mock_provider)

    try:
        resp = await app_client.post(
            "/api/v1/auth/login",
            json={"provider": "oidc"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "redirect_url" in data
        assert data["provider"] == "oidc"
    finally:
        set_oidc_provider(None)


@pytest.mark.asyncio
async def test_auth_me(app_client):
    """GET /api/v1/auth/me returns user profile."""
    resp = await app_client.get(
        "/api/v1/auth/me",
        headers=_auth_header("viewer"),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["email"] == _TEST_USER_EMAIL
    assert data["roles"] == ["viewer"]


@pytest.mark.asyncio
async def test_auth_me_unauthorized(app_client):
    """GET /api/v1/auth/me without token returns 401."""
    resp = await app_client.get("/api/v1/auth/me")
    assert resp.status_code == 401


# ── Pagination Tests ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pagination_limit_offset(app_client):
    """Verify pagination with limit and offset params."""
    # Create several tasks
    for i in range(5):
        await app_client.post(
            "/api/v1/agents/eligibility/tasks",
            json={
                "agent_type": "eligibility",
                "input_data": {
                    "subscriber_id": f"PAG-{i}",
                    "subscriber_first_name": f"First{i}",
                    "subscriber_last_name": f"Last{i}",
                },
            },
            headers=_auth_header(),
        )

    # Request page with limit=2, offset=0
    resp = await app_client.get(
        "/api/v1/agents/eligibility/tasks?limit=2&offset=0",
        headers=_auth_header("viewer"),
    )
    data = resp.json()
    assert len(data["items"]) <= 2
    assert data["limit"] == 2
    assert data["offset"] == 0


# ── Error Response Tests ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_all_routes_require_auth(app_client):
    """Verify protected routes return 401 without JWT."""
    routes = [
        ("GET", "/api/v1/agents/eligibility/tasks"),
        ("GET", "/api/v1/reviews"),
        ("GET", "/api/v1/workflows"),
        ("GET", "/api/v1/payers"),
        ("GET", "/api/v1/audit/logs"),
        ("GET", "/api/v1/dashboard/summary"),
    ]
    for method, path in routes:
        resp = await app_client.request(method, path)
        assert resp.status_code == 401, f"{method} {path} should require auth"


# ── Pagination Test: 50 tasks, page 2, limit 10 ──────────────────────


@pytest.mark.asyncio
async def test_pagination_50_tasks_page2(app_client):
    """Create 50 tasks, request page 2 with limit=10, verify correct slice."""
    created_ids = []
    for i in range(50):
        resp = await app_client.post(
            "/api/v1/agents/eligibility/tasks",
            json={
                "agent_type": "eligibility",
                "input_data": {
                    "subscriber_id": f"PAG50-{i:03d}",
                    "subscriber_first_name": f"First{i}",
                    "subscriber_last_name": f"Last{i}",
                },
            },
            headers=_auth_header(),
        )
        assert resp.status_code == 201
        created_ids.append(resp.json()["id"])

    # Request page 2 (offset=10, limit=10)
    resp = await app_client.get(
        "/api/v1/agents/eligibility/tasks?limit=10&offset=10",
        headers=_auth_header("viewer"),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 10
    assert data["limit"] == 10
    assert data["offset"] == 10
    # Total should include at least 50 (may include tasks from other tests)
    assert data["total"] >= 50

    # Verify these are different from page 1
    page1_resp = await app_client.get(
        "/api/v1/agents/eligibility/tasks?limit=10&offset=0",
        headers=_auth_header("viewer"),
    )
    page1_ids = {item["id"] for item in page1_resp.json()["items"]}
    page2_ids = {item["id"] for item in data["items"]}
    assert page1_ids.isdisjoint(page2_ids), "Page 1 and page 2 should have different tasks"


# ── Update/Delete Task Tests ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_task(app_client):
    """PUT /api/v1/agents/eligibility/tasks/{id} updates a pending task."""
    # Create a task
    resp = await app_client.post(
        "/api/v1/agents/eligibility/tasks",
        json={
            "agent_type": "eligibility",
            "input_data": {
                "subscriber_id": "UPD-001",
                "subscriber_first_name": "Jane",
                "subscriber_last_name": "Doe",
            },
        },
        headers=_auth_header(),
    )
    assert resp.status_code == 201
    task_id = resp.json()["id"]

    # Update it
    resp = await app_client.put(
        f"/api/v1/agents/eligibility/tasks/{task_id}",
        json={
            "input_data": {
                "subscriber_id": "UPD-002",
                "subscriber_first_name": "Jane",
                "subscriber_last_name": "Doe",
            },
        },
        headers=_auth_header("reviewer"),
    )
    # May be 200 if task is in updatable state, or still return the task
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_update_task_not_found(app_client):
    """PUT with non-existent task returns 404."""
    fake_id = str(uuid.uuid4())
    resp = await app_client.put(
        f"/api/v1/agents/eligibility/tasks/{fake_id}",
        json={"input_data": {"subscriber_id": "X"}},
        headers=_auth_header("reviewer"),
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_task_not_found(app_client):
    """DELETE with non-existent task returns 404."""
    fake_id = str(uuid.uuid4())
    resp = await app_client.delete(
        f"/api/v1/agents/eligibility/tasks/{fake_id}",
        headers=_auth_header("admin"),
    )
    assert resp.status_code == 404


# ── Workflow History 404 Test ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_workflow_history_unknown_returns_404(app_client):
    """GET /api/v1/workflows/{unknown}/history returns 404."""
    resp = await app_client.get(
        "/api/v1/workflows/nonexistent-workflow-id/history",
        headers=_auth_header("viewer"),
    )
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"].lower()


# ── Encounter-ID Passthrough Tests ───────────────────────────────────


@pytest.mark.asyncio
async def test_create_claims_task_with_encounter_id(app_client):
    """POST /api/v1/agents/claims/tasks preserves encounter_id in input_data.

    Verifies the evaluator finding that encounter_id must not be stripped
    by the API validation layer and must appear in the stored task input_data.
    """
    encounter_id = str(uuid.uuid4())
    body = {
        "agent_type": "claims",
        "input_data": {
            "subscriber_id": "SUB-ENC-001",
            "subscriber_first_name": "Alice",
            "subscriber_last_name": "Encounter",
            "diagnosis_codes": ["J06.9"],
            "procedure_codes": ["99213"],
            "total_charge": "200.00",
            "payer_id": "AETNA01",
            "encounter_id": encounter_id,
        },
    }
    resp = await app_client.post(
        "/api/v1/agents/claims/tasks",
        json=body,
        headers=_auth_header("viewer"),
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["agent_type"] == "claims"
    # encounter_id must survive API validation and be present in input_data
    assert data["input_data"]["encounter_id"] == encounter_id, (
        f"encounter_id was stripped from input_data; got keys: {list(data['input_data'].keys())}"
    )


@pytest.mark.asyncio
async def test_create_claims_task_without_encounter_id_still_works(app_client):
    """Claims task creation without encounter_id should succeed normally."""
    body = {
        "agent_type": "claims",
        "input_data": {
            "subscriber_id": "SUB-NO-ENC",
            "subscriber_first_name": "Bob",
            "subscriber_last_name": "NoEncounter",
            "diagnosis_codes": ["M54.5"],
            "procedure_codes": ["99214"],
            "total_charge": "175.00",
        },
    }
    resp = await app_client.post(
        "/api/v1/agents/claims/tasks",
        json=body,
        headers=_auth_header("viewer"),
    )
    assert resp.status_code == 201
    data = resp.json()
    # encounter_id should either be absent or None (excluded by exclude_none)
    assert data["input_data"].get("encounter_id") is None
