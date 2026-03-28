"""Tests for Sprint 5 iteration 4 fixes.

Covers:
1. Mock clearinghouse provides deterministic eligibility response
2. Pending EligibilityCheck is created at workflow start
3. Explicit escalate node in eligibility LangGraph graph
4. agent_type is optional in AgentTaskCreate body (path param is authoritative)
5. Pagination metadata returns effective (clamped) limit
6. Audit logs endpoint returns total count
7. API-level integration: POST eligibility → completed with coverage result
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.jwt import create_access_token
from app.dependencies import get_db
from app.main import create_app
from app.models.agent_task import AgentTask


# ── Helper ──────────────────────────────────────────────────────────

_TEST_USER_ID = uuid.uuid4()


def _auth_header(role: str = "admin") -> dict[str, str]:
    token = create_access_token(
        user_id=_TEST_USER_ID,
        email="test@slate.health",
        role=role,
        full_name="Test User",
    )
    return {"Authorization": f"Bearer {token}"}


# ── 1. Mock Clearinghouse ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_mock_clearinghouse_returns_coverage():
    """MockClearinghouse returns a deterministic eligibility response."""
    from app.core.clearinghouse.mock import MockClearinghouse
    from app.core.clearinghouse.base import TransactionRequest, TransactionType

    client = MockClearinghouse()
    request = TransactionRequest(
        transaction_type=TransactionType.ELIGIBILITY_270,
        payload="ISA*00*...",
        sender_id="SENDER01",
        receiver_id="RECEIVER01",
        control_number="123456789",
    )
    response = await client.submit_transaction(request)

    assert response.transaction_id.startswith("MOCK-")
    assert response.status.value == "completed"
    assert response.parsed_response["coverage"]["active"] is True
    assert len(response.parsed_response["benefits"]) > 0
    assert response.parsed_response["benefits"][0]["eligibility_code"] == "1"


@pytest.mark.asyncio
async def test_mock_clearinghouse_registered_in_factory():
    """The 'mock' clearinghouse is available via the factory."""
    from app.core.clearinghouse.factory import get_clearinghouse
    from app.core.clearinghouse.mock import MockClearinghouse

    client = get_clearinghouse(
        clearinghouse_name="mock",
        api_endpoint="http://mock",
    )
    assert isinstance(client, MockClearinghouse)


@pytest.mark.asyncio
async def test_default_clearinghouse_config_uses_mock():
    """submit_to_clearinghouse defaults to mock when no config is provided."""
    from app.workflows.eligibility import submit_to_clearinghouse

    # Build a valid X12 payload arg dict
    args = {
        "x12_payload": {
            "data": {
                "x12_270": "ISA*00*TEST*270*CONTENT",
                "control_number": "999888777",
                "task_id": "test-task-123",
            }
        },
        "clearinghouse_config": None,  # No config → should use mock
    }

    result = await submit_to_clearinghouse(args)
    assert result["success"] is True
    assert result["data"]["transaction_id"].startswith("MOCK-")
    assert result["data"]["status"] == "completed"


# ── 2. Pending EligibilityCheck at workflow start ────────────────────


@pytest.mark.asyncio
async def test_create_pending_eligibility_check_activity():
    """create_pending_eligibility_check creates a pending record in DB."""
    from app.workflows.eligibility import create_pending_eligibility_check

    task_id = str(uuid.uuid4())

    # Mock the DB layer used internally by the activity
    mock_task = MagicMock()
    mock_task.patient_id = uuid.uuid4()

    mock_session = AsyncMock()
    mock_session.add = MagicMock()
    mock_session.commit = AsyncMock()

    # First execute returns the task, second returns None (no existing check)
    task_scalar = MagicMock()
    task_scalar.scalar_one_or_none = MagicMock(return_value=mock_task)
    check_scalar = MagicMock()
    check_scalar.scalar_one_or_none = MagicMock(return_value=None)
    mock_session.execute = AsyncMock(side_effect=[task_scalar, check_scalar])

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    mock_factory = MagicMock(return_value=mock_ctx)
    mock_engine = AsyncMock()
    mock_engine.dispose = AsyncMock()

    with patch("app.workflows.eligibility._get_activity_session_factory",
               return_value=(mock_factory, mock_engine)):
        result = await create_pending_eligibility_check({
            "task_id": task_id,
            "data": {"subscriber_id": "PEND-001"},
        })

    assert result["success"] is True
    # Verify session.add was called with an EligibilityCheck
    add_calls = mock_session.add.call_args_list
    from app.models.eligibility import EligibilityCheck
    elig_adds = [c for c in add_calls if isinstance(c[0][0], EligibilityCheck)]
    assert len(elig_adds) == 1, "Should create exactly one EligibilityCheck"
    assert mock_session.commit.call_count >= 1


@pytest.mark.asyncio
async def test_create_pending_eligibility_check_idempotent():
    """When EligibilityCheck already exists, activity does NOT create a duplicate."""
    from app.workflows.eligibility import create_pending_eligibility_check

    task_id = str(uuid.uuid4())

    mock_task = MagicMock()
    mock_task.patient_id = uuid.uuid4()

    existing_check = MagicMock()  # Simulate existing record

    mock_session = AsyncMock()
    mock_session.add = MagicMock()
    mock_session.commit = AsyncMock()

    task_scalar = MagicMock()
    task_scalar.scalar_one_or_none = MagicMock(return_value=mock_task)
    check_scalar = MagicMock()
    check_scalar.scalar_one_or_none = MagicMock(return_value=existing_check)
    mock_session.execute = AsyncMock(side_effect=[task_scalar, check_scalar])

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    mock_factory = MagicMock(return_value=mock_ctx)
    mock_engine = AsyncMock()
    mock_engine.dispose = AsyncMock()

    with patch("app.workflows.eligibility._get_activity_session_factory",
               return_value=(mock_factory, mock_engine)):
        result = await create_pending_eligibility_check({
            "task_id": task_id,
            "data": {"subscriber_id": "IDEM-001"},
        })

    assert result["success"] is True
    # session.add should NOT be called with EligibilityCheck since record already exists
    from app.models.eligibility import EligibilityCheck
    add_calls = mock_session.add.call_args_list
    elig_adds = [c for c in add_calls if isinstance(c[0][0], EligibilityCheck)]
    assert len(elig_adds) == 0, "Should NOT create a new EligibilityCheck"


# ── 3. Explicit escalate node in LangGraph ───────────────────────────


@pytest.mark.asyncio
async def test_eligibility_graph_has_escalate_node():
    """Eligibility agent graph contains an explicit 'escalate' node."""
    from app.agents.eligibility.graph import EligibilityAgent
    from app.core.engine.llm_provider import LLMProvider, MockLLMBackend

    backend = MockLLMBackend(responses=['{"confidence": 0.85}'])
    provider = LLMProvider(primary=backend)
    agent = EligibilityAgent(llm_provider=provider)

    graph = agent.build_graph()
    assert "escalate" in graph.node_names


@pytest.mark.asyncio
async def test_escalate_node_reached_when_low_confidence():
    """When confidence is low, the graph routes through the escalate node."""
    from app.agents.eligibility.graph import (
        EligibilityAgent,
        escalate_node,
        _evaluate_confidence_router,
    )

    # State with needs_review = True should route to escalate
    state = {"needs_review": True, "confidence": 0.3, "review_reason": "Low confidence"}
    assert _evaluate_confidence_router(state) == "escalate"

    # State with needs_review = False should route to output
    state2 = {"needs_review": False, "confidence": 0.9, "review_reason": ""}
    assert _evaluate_confidence_router(state2) == "output"


@pytest.mark.asyncio
async def test_escalate_node_sets_escalated_flag():
    """The escalate node sets decision.escalated = True."""
    from app.agents.eligibility.graph import escalate_node

    state = {
        "current_node": "",
        "needs_review": True,
        "confidence": 0.3,
        "review_reason": "Multiple coverage matches",
        "payer_rules_applied": [],
        "audit_trail": [],
    }
    result = await escalate_node(state)
    assert result["decision"]["escalated"] is True
    assert result["decision"]["needs_review"] is True
    assert any(e["action"] == "escalated_to_hitl" for e in result["audit_trail"])


@pytest.mark.asyncio
async def test_full_graph_with_low_confidence_routes_through_escalate():
    """Full agent graph execution with low confidence traverses escalate→output."""
    from app.agents.eligibility.graph import EligibilityAgent
    from app.core.engine.llm_provider import LLMProvider, MockLLMBackend

    backend = MockLLMBackend(responses=[
        '{"confidence": 0.3, "decision": {"submission_strategy": "clearinghouse"}, "tool_calls": []}'
    ])
    provider = LLMProvider(primary=backend, phi_safe=True)
    agent = EligibilityAgent(llm_provider=provider)

    state = await agent.run(
        task_id="test-esc-001",
        input_data={
            "subscriber_id": "ESC-001",
            "subscriber_first_name": "Test",
            "subscriber_last_name": "Escalate",
            "ambiguous_coverage": True,  # Triggers low confidence
        },
        patient_context={},
        payer_context={},
    )

    assert state.get("needs_review") is True
    # Verify the escalate node was visited via audit trail
    node_actions = [(e.get("node", ""), e.get("action", "")) for e in state.get("audit_trail", [])]
    escalate_entries = [n for n in node_actions if n[0] == "escalate"]
    assert len(escalate_entries) > 0, "escalate node should appear in audit trail"


# ── 4. agent_type optional in request body ───────────────────────────


def test_agent_task_create_without_agent_type():
    """AgentTaskCreate works without agent_type in body."""
    from app.schemas.agent import AgentTaskCreate

    task = AgentTaskCreate(
        input_data={
            "subscriber_id": "INS-999",
            "subscriber_first_name": "Jane",
            "subscriber_last_name": "Doe",
        },
    )
    assert task.agent_type is None
    assert task.input_data["subscriber_id"] == "INS-999"


def test_agent_task_create_with_agent_type_still_works():
    """AgentTaskCreate still accepts agent_type for backwards compat."""
    from app.schemas.agent import AgentTaskCreate

    task = AgentTaskCreate(
        agent_type="eligibility",
        input_data={"subscriber_id": "X"},
    )
    assert task.agent_type == "eligibility"


@pytest.mark.asyncio
async def test_api_create_task_without_agent_type_in_body(test_engine):
    """POST with agent_type only in path (not body) succeeds."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

    session_factory = async_sessionmaker(
        test_engine, class_=AsyncSession, expire_on_commit=False,
    )

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
        resp = await ac.post(
            "/api/v1/agents/eligibility/tasks",
            json={
                # No agent_type in body!
                "input_data": {
                    "subscriber_id": "NO-BODY-TYPE-001",
                    "subscriber_first_name": "Jane",
                    "subscriber_last_name": "Doe",
                },
            },
            headers=_auth_header(),
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["agent_type"] == "eligibility"


# ── 5. Pagination metadata returns effective limit ────────────────────


@pytest.mark.asyncio
async def test_pagination_returns_effective_limit(test_engine):
    """When requesting limit=9999, the response returns the clamped limit."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

    session_factory = async_sessionmaker(
        test_engine, class_=AsyncSession, expire_on_commit=False,
    )

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
        # Agents endpoint: max 100
        resp = await ac.get(
            "/api/v1/agents/eligibility/tasks?limit=9999",
            headers=_auth_header("viewer"),
        )
        assert resp.status_code == 200
        assert resp.json()["limit"] == 100

        # Reviews endpoint: max 100
        resp = await ac.get(
            "/api/v1/reviews?limit=9999",
            headers=_auth_header("viewer"),
        )
        assert resp.status_code == 200
        assert resp.json()["limit"] == 100

        # Workflows endpoint: max 100
        resp = await ac.get(
            "/api/v1/workflows?limit=9999",
            headers=_auth_header("viewer"),
        )
        assert resp.status_code == 200
        assert resp.json()["limit"] == 100

        # Audit endpoint: max 500
        resp = await ac.get(
            "/api/v1/audit/logs?limit=9999",
            headers=_auth_header("viewer"),
        )
        assert resp.status_code == 200
        assert resp.json()["limit"] == 500


# ── 6. Audit logs endpoint returns total ──────────────────────────────


@pytest.mark.asyncio
async def test_audit_logs_includes_total(test_engine, db_session):
    """GET /api/v1/audit/logs returns total count in response."""
    from app.core.audit.logger import AuditLogger

    # Create some audit entries
    logger = AuditLogger(db_session)
    for i in range(3):
        await logger.log(
            action=f"test_action_{i}",
            actor_type="system",
            resource_type="test",
            resource_id=f"res-{i}",
        )
    await db_session.commit()

    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

    session_factory = async_sessionmaker(
        test_engine, class_=AsyncSession, expire_on_commit=False,
    )

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
        resp = await ac.get(
            "/api/v1/audit/logs",
            headers=_auth_header("viewer"),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "total" in data
        assert data["total"] >= 3
        assert "items" in data
        assert "limit" in data
        assert "offset" in data


# ── 7. API-level integration: eligibility happy path ──────────────────


@pytest.mark.asyncio
async def test_eligibility_api_happy_path(test_engine):
    """POST eligibility task → completes with mock clearinghouse → coverage returned."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

    session_factory = async_sessionmaker(
        test_engine, class_=AsyncSession, expire_on_commit=False,
    )

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
        # Submit eligibility task
        create_resp = await ac.post(
            "/api/v1/agents/eligibility/tasks",
            json={
                "input_data": {
                    "subscriber_id": "HAPPY-001",
                    "subscriber_first_name": "Jane",
                    "subscriber_last_name": "Doe",
                    "subscriber_dob": "19900101",
                    "payer_id": "BCBS01",
                    "payer_name": "Blue Cross",
                },
            },
            headers=_auth_header(),
        )
        assert create_resp.status_code == 201
        task_data = create_resp.json()
        task_id = task_data["id"]

        # The inline workflow runner should have completed the task
        # (no Temporal client = inline fallback with mock clearinghouse)
        get_resp = await ac.get(
            f"/api/v1/agents/eligibility/tasks/{task_id}",
            headers=_auth_header("viewer"),
        )
        assert get_resp.status_code == 200
        task_detail = get_resp.json()
        # Happy-path: task MUST complete (not fail) — with coverage data
        assert task_detail["status"] in ("completed", "review"), (
            f"Expected completed or review but got '{task_detail['status']}'; "
            f"error={task_detail.get('error_message')}"
        )

        # Output data must be non-empty and contain coverage information
        assert task_detail["output_data"] is not None
        output = task_detail["output_data"]
        assert output, "output_data must be non-empty"
        assert "coverage_active" in output or "coverage_details" in output, (
            f"output_data missing coverage fields: {list(output.keys())}"
        )


# ── 8. Strict review-approval flow via API routes ─────────────────────


@pytest.mark.asyncio
async def test_ambiguous_eligibility_creates_review_and_approve_completes(test_engine):
    """Submit eligibility with ambiguous insurance → HITL review created → approve → task completed.

    This is the strict integration test for the review approval acceptance
    criterion.  It exercises the full route-level flow:
      POST task → GET /reviews (pending) → POST /reviews/{id}/approve → task completed.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession
    from app.models.eligibility import EligibilityCheck

    session_factory = async_sessionmaker(
        test_engine, class_=AsyncSession, expire_on_commit=False,
    )

    async def override_get_db():
        async with session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    # Patch the mock clearinghouse to return an ambiguous multi-coverage
    # response that forces confidence < 0.7 and triggers HITL review.
    from app.core.clearinghouse.base import (
        TransactionResponse,
        TransactionStatus,
        TransactionType,
    )

    ambiguous_response = TransactionResponse(
        transaction_id="MOCK-AMBIG-001",
        transaction_type=TransactionType.ELIGIBILITY_271,
        status=TransactionStatus.COMPLETED,
        raw_response="",
        parsed_response={
            "coverage": {"active": True, "effective_date": "2024-01-01"},
            "benefits": [
                {"eligibility_code": "1", "plan_name": "Plan A", "coverage_level": "IND"},
                {"eligibility_code": "1", "plan_name": "Plan B", "coverage_level": "FAM"},
            ],
            "subscriber": {"id": "AMBIG-001", "last_name": "Multi", "first_name": "Cover"},
            "payer": {"id": "P01", "name": "Test Payer"},
            "errors": [],
        },
        errors=[],
    )

    async def _mock_submit(self, request):  # noqa: ARG001
        return ambiguous_response

    app = create_app()
    app.dependency_overrides[get_db] = override_get_db

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # Patch clearinghouse submit to return ambiguous response
        from app.core.clearinghouse.mock import MockClearinghouse
        original_submit = MockClearinghouse.submit_transaction
        MockClearinghouse.submit_transaction = _mock_submit
        try:
            # Step 1: Submit eligibility task
            create_resp = await ac.post(
                "/api/v1/agents/eligibility/tasks",
                json={
                    "input_data": {
                        "subscriber_id": "AMBIG-001",
                        "subscriber_first_name": "Cover",
                        "subscriber_last_name": "Multi",
                        "subscriber_dob": "19850615",
                        "payer_id": "P01",
                        "payer_name": "Test Payer",
                    },
                },
                headers=_auth_header(),
            )
            assert create_resp.status_code == 201
            task_id = create_resp.json()["id"]

            # Step 2: Task should be in review status
            get_resp = await ac.get(
                f"/api/v1/agents/eligibility/tasks/{task_id}",
                headers=_auth_header("viewer"),
            )
            assert get_resp.status_code == 200
            task_detail = get_resp.json()
            assert task_detail["status"] == "review", (
                f"Ambiguous eligibility should be in review, got '{task_detail['status']}'; "
                f"error={task_detail.get('error_message')}"
            )

            # Step 3: GET /reviews should list the pending review
            # Use task_id query parameter to filter reviews for this specific
            # task, avoiding false negatives when accumulated reviews from
            # prior test runs exceed the default page size.
            reviews_resp = await ac.get(
                f"/api/v1/reviews?task_id={task_id}",
                headers=_auth_header("viewer"),
            )
            assert reviews_resp.status_code == 200
            reviews_data = reviews_resp.json()
            items = reviews_data.get("items", reviews_data.get("reviews", []))
            matching = [r for r in items if r.get("task_id") == task_id]
            assert len(matching) >= 1, (
                f"Expected at least 1 review for task {task_id}, "
                f"found {len(matching)} in {len(items)} total reviews"
            )
            review_id = matching[0]["id"]

            # Step 4: Approve the review
            approve_resp = await ac.post(
                f"/api/v1/reviews/{review_id}/approve",
                json={"notes": "Coverage verified manually"},
                headers=_auth_header(),
            )
            assert approve_resp.status_code == 200

            # Step 5: Task should now be completed
            final_resp = await ac.get(
                f"/api/v1/agents/eligibility/tasks/{task_id}",
                headers=_auth_header("viewer"),
            )
            assert final_resp.status_code == 200
            final_detail = final_resp.json()
            assert final_detail["status"] == "completed", (
                f"After approval task should be completed, got '{final_detail['status']}'"
            )

        finally:
            MockClearinghouse.submit_transaction = original_submit


# ── 9. Audit trail completeness after eligibility check ────────────────


@pytest.mark.asyncio
async def test_audit_trail_contains_workflow_stages(test_engine):
    """After a completed eligibility check, audit logs contain workflow stage entries."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

    session_factory = async_sessionmaker(
        test_engine, class_=AsyncSession, expire_on_commit=False,
    )

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
        # Submit eligibility task
        create_resp = await ac.post(
            "/api/v1/agents/eligibility/tasks",
            json={
                "input_data": {
                    "subscriber_id": "AUDIT-001",
                    "subscriber_first_name": "Audit",
                    "subscriber_last_name": "Test",
                    "subscriber_dob": "19900101",
                    "payer_id": "AUD01",
                    "payer_name": "Audit Payer",
                },
            },
            headers=_auth_header(),
        )
        assert create_resp.status_code == 201
        task_id = create_resp.json()["id"]

        # Query audit logs for this task
        audit_resp = await ac.get(
            f"/api/v1/audit/logs?resource_id={task_id}",
            headers=_auth_header("viewer"),
        )
        assert audit_resp.status_code == 200
        audit_data = audit_resp.json()
        items = audit_data.get("items", [])

        # Should have at least task_created and workflow_started entries
        actions = [item["action"] for item in items]
        assert any("task_created" in a for a in actions), (
            f"Expected 'agent_task_created' in audit actions: {actions}"
        )
        assert any("workflow_started" in a for a in actions), (
            f"Expected 'agent_workflow_started' in audit actions: {actions}"
        )
