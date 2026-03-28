"""Tests for Sprint 5 iteration 2 fixes.

Covers:
1. WebSocket broadcasts on task completion/failure/review
2. Ambiguous eligibility → HITL review creation → approve → task completed
3. Eligibility-specific schema validation on task create endpoint
4. Per-stage audit logging in eligibility workflow
5. Payer CRUD completeness (get, update, delete payer; delete rule)
6. task_id field in AgentTaskResponse
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timezone
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
from app.models.audit import AuditLog
from app.models.hitl_review import HITLReview
from app.models.payer import Payer, PayerRule


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


# ── 1. WebSocket broadcast is invoked on workflow completion ────────


@pytest.mark.asyncio
async def test_websocket_broadcast_called_on_workflow_completion(db_session):
    """WorkflowService._complete_workflow broadcasts a WebSocket event when a task completes."""
    from app.services.workflow_service import WorkflowService
    from app.workflows.base import WorkflowResult

    service = WorkflowService(db_session)

    # Create a task and execution
    task = AgentTask(
        agent_type="eligibility",
        status="running",
        input_data={"subscriber_id": "S001"},
    )
    db_session.add(task)
    await db_session.flush()

    from app.models.workflow import WorkflowExecution

    execution = WorkflowExecution(
        workflow_id=f"elig-{task.id}-abc",
        run_id="run-123",
        agent_type="eligibility",
        status="running",
    )
    db_session.add(execution)
    await db_session.flush()

    result = WorkflowResult(
        task_id=str(task.id),
        agent_type="eligibility",
        status="completed",
        output_data={"coverage_active": True},
        confidence=0.95,
        needs_review=False,
    )

    with patch("app.api.websocket.broadcast_task_update", new_callable=AsyncMock) as mock_broadcast:
        await service._complete_workflow(execution, task, result)

        # Verify broadcast was called
        mock_broadcast.assert_called_once()
        call_kwargs = mock_broadcast.call_args
        # broadcast_task_update is called with keyword args
        assert str(task.id) in str(call_kwargs)


@pytest.mark.asyncio
async def test_websocket_broadcast_called_on_failure(db_session):
    """WorkflowService._complete_workflow broadcasts a WebSocket event when a task fails."""
    from app.services.workflow_service import WorkflowService
    from app.workflows.base import WorkflowResult

    service = WorkflowService(db_session)

    task = AgentTask(
        agent_type="eligibility",
        status="running",
        input_data={},
    )
    db_session.add(task)
    await db_session.flush()

    from app.models.workflow import WorkflowExecution

    execution = WorkflowExecution(
        workflow_id=f"elig-{task.id}-abc",
        run_id="run-456",
        agent_type="eligibility",
        status="running",
    )
    db_session.add(execution)
    await db_session.flush()

    result = WorkflowResult(
        task_id=str(task.id),
        agent_type="eligibility",
        status="failed",
        error="Clearinghouse timeout",
    )

    with patch("app.api.websocket.broadcast_task_update", new_callable=AsyncMock) as mock_broadcast:
        await service._complete_workflow(execution, task, result)
        mock_broadcast.assert_called_once()


# ── 2. Ambiguous eligibility → HITL review creation ────────────────


@pytest.mark.asyncio
async def test_hitl_review_created_for_ambiguous_eligibility(db_session):
    """When workflow completes with needs_review=True, a HITLReview record is created."""
    from app.services.workflow_service import WorkflowService
    from app.workflows.base import WorkflowResult

    service = WorkflowService(db_session)

    task = AgentTask(
        agent_type="eligibility",
        status="running",
        input_data={"subscriber_id": "AMB-001"},
    )
    db_session.add(task)
    await db_session.flush()

    from app.models.workflow import WorkflowExecution

    execution = WorkflowExecution(
        workflow_id=f"elig-{task.id}-ambig",
        run_id="run-amb",
        agent_type="eligibility",
        status="running",
    )
    db_session.add(execution)
    await db_session.flush()

    result = WorkflowResult(
        task_id=str(task.id),
        agent_type="eligibility",
        status="completed",
        output_data={"coverage_active": True, "ambiguous": True},
        confidence=0.4,
        needs_review=True,
        review_reason="Ambiguous eligibility response",
    )

    with patch("app.api.websocket.broadcast_task_update", new_callable=AsyncMock):
        await service._complete_workflow(execution, task, result)

    # Verify task status is 'review'
    assert task.status == "review"

    # Verify a HITLReview was created
    review_result = await db_session.execute(
        select(HITLReview).where(HITLReview.task_id == task.id)
    )
    review = review_result.scalar_one_or_none()
    assert review is not None
    assert review.status == "pending"
    assert review.confidence_score is not None
    assert review.confidence_score < 0.7


@pytest.mark.asyncio
async def test_no_hitl_review_for_high_confidence(db_session):
    """When workflow completes with high confidence, no HITLReview is created."""
    from app.services.workflow_service import WorkflowService
    from app.workflows.base import WorkflowResult

    service = WorkflowService(db_session)

    task = AgentTask(
        agent_type="eligibility",
        status="running",
        input_data={"subscriber_id": "CLR-001"},
    )
    db_session.add(task)
    await db_session.flush()

    from app.models.workflow import WorkflowExecution

    execution = WorkflowExecution(
        workflow_id=f"elig-{task.id}-clear",
        run_id="run-clr",
        agent_type="eligibility",
        status="running",
    )
    db_session.add(execution)
    await db_session.flush()

    result = WorkflowResult(
        task_id=str(task.id),
        agent_type="eligibility",
        status="completed",
        output_data={"coverage_active": True},
        confidence=0.95,
        needs_review=False,
    )

    with patch("app.api.websocket.broadcast_task_update", new_callable=AsyncMock):
        await service._complete_workflow(execution, task, result)

    assert task.status == "completed"

    review_result = await db_session.execute(
        select(HITLReview).where(HITLReview.task_id == task.id)
    )
    assert review_result.scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_ambiguous_review_approve_completes_task(db_session):
    """Approving a HITL review created by ambiguous eligibility sets task to completed."""
    from app.core.hitl.review_queue import ReviewQueue

    # Create a task in review state
    task = AgentTask(
        agent_type="eligibility",
        status="review",
        input_data={"subscriber_id": "AMB-002"},
        confidence_score=0.4,
    )
    db_session.add(task)
    await db_session.flush()

    # Create a HITL review
    review = HITLReview(
        task_id=task.id,
        status="pending",
        reason="Ambiguous eligibility response",
        confidence_score=0.4,
        agent_decision={"coverage_active": True},
    )
    db_session.add(review)
    await db_session.flush()

    # Approve it
    queue = ReviewQueue(db_session)
    reviewer_id = str(uuid.uuid4())
    approved = await queue.approve(
        str(review.id),
        reviewer_id=reviewer_id,
        notes="Coverage confirmed manually",
    )

    assert approved.status == "approved"

    # Verify task is now completed
    task_result = await db_session.execute(
        select(AgentTask).where(AgentTask.id == task.id)
    )
    updated_task = task_result.scalar_one()
    assert updated_task.status == "completed"


# ── 3. Eligibility-specific schema validation ──────────────────────


@pytest.mark.asyncio
async def test_eligibility_create_validates_input(test_engine):
    """POST eligibility task with missing required fields returns 422."""
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
        # Missing subscriber_id, subscriber_first_name, subscriber_last_name
        resp = await ac.post(
            "/api/v1/agents/eligibility/tasks",
            json={
                "agent_type": "eligibility",
                "input_data": {"payer_id": "BCBS01"},
            },
            headers=_auth_header(),
        )
        assert resp.status_code == 422


@pytest.mark.asyncio
async def test_eligibility_create_with_valid_input(test_engine):
    """POST eligibility task with valid input creates task and returns task_id."""
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
                "agent_type": "eligibility",
                "input_data": {
                    "subscriber_id": "INS-12345",
                    "subscriber_first_name": "Jane",
                    "subscriber_last_name": "Doe",
                    "payer_id": "BCBS01",
                },
            },
            headers=_auth_header(),
        )
        assert resp.status_code == 201
        data = resp.json()
        assert "id" in data
        assert "task_id" in data
        assert data["id"] == data["task_id"]
        assert data["agent_type"] == "eligibility"


# ── 4. Per-stage audit logging ─────────────────────────────────────


@pytest.mark.asyncio
async def test_audit_workflow_stage_writes_entry():
    """_audit_workflow_stage writes an audit log entry with stage info."""
    from app.workflows.eligibility import _audit_workflow_stage

    mock_session = AsyncMock()
    mock_session.flush = AsyncMock()
    mock_session.commit = AsyncMock()
    mock_session.add = MagicMock()

    mock_engine = AsyncMock()
    mock_engine.dispose = AsyncMock()

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    mock_session_factory_cls = MagicMock(return_value=mock_ctx)

    with patch("app.workflows.eligibility._get_activity_session_factory",
               return_value=(mock_session_factory_cls, mock_engine)):

        await _audit_workflow_stage(
            "task-123", "validate", "input_validated",
            {"subscriber_id": "S001"},
        )

        # Verify session.commit was called (audit entry persisted)
        mock_session.commit.assert_called_once()


# ── 5. Payer CRUD completeness ─────────────────────────────────────


@pytest.mark.asyncio
async def test_get_single_payer(test_engine, db_session):
    """GET /api/v1/payers/{id} returns a single payer."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

    # Create a payer directly
    payer = Payer(
        name="Test Payer",
        payer_id_code="TP001",
        payer_type="commercial",
    )
    db_session.add(payer)
    await db_session.flush()
    payer_id = str(payer.id)
    await db_session.commit()

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
            f"/api/v1/payers/{payer_id}",
            headers=_auth_header(),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "Test Payer"
        assert data["payer_id_code"] == "TP001"


@pytest.mark.asyncio
async def test_get_nonexistent_payer(test_engine):
    """GET /api/v1/payers/{id} returns 404 for missing payer."""
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
            f"/api/v1/payers/{uuid.uuid4()}",
            headers=_auth_header(),
        )
        assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_payer(test_engine, db_session):
    """DELETE /api/v1/payers/{id} soft-deletes the payer."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

    payer = Payer(
        name="Delete Me Payer",
        payer_id_code="DEL001",
    )
    db_session.add(payer)
    await db_session.flush()
    payer_id = str(payer.id)
    await db_session.commit()

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
        resp = await ac.delete(
            f"/api/v1/payers/{payer_id}",
            headers=_auth_header(),
        )
        assert resp.status_code == 204


@pytest.mark.asyncio
async def test_delete_payer_rule(test_engine, db_session):
    """DELETE /api/v1/payers/{payer_id}/rules/{rule_id} soft-deletes the rule."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

    payer = Payer(name="Rule Payer", payer_id_code="RP001")
    db_session.add(payer)
    await db_session.flush()

    rule = PayerRule(
        payer_id=payer.id,
        agent_type="eligibility",
        rule_type="coverage_check",
        conditions={"check_type": "standard"},
        effective_date=date(2024, 1, 1),
    )
    db_session.add(rule)
    await db_session.flush()
    payer_id = str(payer.id)
    rule_id = str(rule.id)
    await db_session.commit()

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
        resp = await ac.delete(
            f"/api/v1/payers/{payer_id}/rules/{rule_id}",
            headers=_auth_header(),
        )
        assert resp.status_code == 204


# ── 6. task_id field in response ───────────────────────────────────


def test_agent_task_response_has_task_id():
    """AgentTaskResponse includes task_id field matching id."""
    from app.schemas.agent import AgentTaskResponse

    task_uuid = uuid.uuid4()
    resp = AgentTaskResponse(
        id=task_uuid,
        task_id=task_uuid,
        agent_type="eligibility",
        status="completed",
    )
    assert resp.task_id == task_uuid
    assert resp.id == resp.task_id


# ── 7. Audit trail for eligibility workflow stages ──────────────────


@pytest.mark.asyncio
async def test_audit_trail_per_stage_in_agent_service(db_session):
    """AgentService.create_task logs both task_created and workflow_started audit entries."""
    from app.services.agent_service import AgentService

    service = AgentService(db_session)

    task = await service.create_task(
        agent_type="eligibility",
        input_data={
            "subscriber_id": "INS-999",
            "subscriber_first_name": "Test",
            "subscriber_last_name": "Audit",
        },
    )

    # Query audit logs for this task
    audit_result = await db_session.execute(
        select(AuditLog).where(AuditLog.resource_id == str(task.id))
    )
    entries = list(audit_result.scalars().all())

    actions = {e.action for e in entries}
    assert "agent_task_created" in actions
    assert "agent_workflow_started" in actions
