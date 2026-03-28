"""Unit tests for AgentService and DashboardService."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_task import AgentTask
from app.services.agent_service import AgentService
from app.services.dashboard_service import DashboardService


@pytest.mark.asyncio
async def test_create_task_stores_in_db(db_session: AsyncSession):
    """AgentService.create_task persists an AgentTask record."""
    service = AgentService(db_session)
    task = await service.create_task(
        agent_type="eligibility",
        input_data={
            "subscriber_id": "INS-001",
            "subscriber_first_name": "Jane",
            "subscriber_last_name": "Doe",
        },
    )
    assert task is not None
    assert task.agent_type == "eligibility"
    assert task.id is not None


@pytest.mark.asyncio
async def test_create_task_invalid_agent_type(db_session: AsyncSession):
    """AgentService.create_task raises ValueError for invalid agent_type."""
    service = AgentService(db_session)
    with pytest.raises(ValueError, match="Invalid agent_type"):
        await service.create_task(
            agent_type="nonexistent",
            input_data={},
        )


@pytest.mark.asyncio
async def test_list_tasks_with_filter(db_session: AsyncSession):
    """AgentService.list_tasks filters by agent_type."""
    service = AgentService(db_session)

    # Create tasks of different types
    await service.create_task(
        agent_type="eligibility",
        input_data={"subscriber_id": "A", "subscriber_first_name": "A", "subscriber_last_name": "A"},
    )

    tasks, total = await service.list_tasks(agent_type="eligibility")
    assert total >= 1
    for t in tasks:
        assert t.agent_type == "eligibility"


@pytest.mark.asyncio
async def test_get_task_by_id(db_session: AsyncSession):
    """AgentService.get_task retrieves by ID."""
    service = AgentService(db_session)
    task = await service.create_task(
        agent_type="eligibility",
        input_data={"subscriber_id": "B", "subscriber_first_name": "B", "subscriber_last_name": "B"},
    )

    fetched = await service.get_task(str(task.id))
    assert fetched is not None
    assert fetched.id == task.id


@pytest.mark.asyncio
async def test_get_task_not_found(db_session: AsyncSession):
    """AgentService.get_task returns None for unknown ID."""
    service = AgentService(db_session)
    result = await service.get_task(str(uuid.uuid4()))
    assert result is None


@pytest.mark.asyncio
async def test_get_agent_stats(db_session: AsyncSession):
    """AgentService.get_agent_stats returns correct counts."""
    service = AgentService(db_session)
    await service.create_task(
        agent_type="eligibility",
        input_data={"subscriber_id": "S1", "subscriber_first_name": "F", "subscriber_last_name": "L"},
    )

    stats = await service.get_agent_stats("eligibility")
    assert stats["agent_type"] == "eligibility"
    assert stats["total_tasks"] >= 1


@pytest.mark.asyncio
async def test_dashboard_summary(db_session: AsyncSession):
    """DashboardService.get_summary returns aggregate stats."""
    service = DashboardService(db_session)
    summary = await service.get_summary()
    assert "total_tasks" in summary
    assert "agents" in summary
    assert len(summary["agents"]) == 6  # all 6 agent types


@pytest.mark.asyncio
async def test_create_task_passes_clearinghouse_config(db_session: AsyncSession):
    """AgentService.create_task looks up org clearinghouse config and passes
    it to the workflow service, so workflows use the real clearinghouse
    instead of silently falling back to mock.
    """
    from unittest.mock import AsyncMock, patch, MagicMock
    from app.services.workflow_service import WorkflowService

    # Create a mock clearinghouse config record
    mock_ch_config = MagicMock()
    mock_ch_config.clearinghouse_name = "availity"
    mock_ch_config.api_endpoint = "https://api.availity.com"
    mock_ch_config.credentials = {"client_id": "test"}

    mock_workflow_service = AsyncMock(spec=WorkflowService)
    mock_execution = MagicMock()
    mock_execution.id = uuid.uuid4()
    mock_execution.workflow_id = "test-wf-id"
    mock_workflow_service.start_workflow = AsyncMock(return_value=mock_execution)

    org_id = uuid.uuid4()

    with patch(
        "app.core.payer.registry.PayerRegistry.get_clearinghouse_config",
        new_callable=AsyncMock,
        return_value=mock_ch_config,
    ):
        service = AgentService(db_session, workflow_service=mock_workflow_service)
        await service.create_task(
            agent_type="eligibility",
            input_data={"subscriber_id": "X"},
            organization_id=org_id,
        )

    # Verify start_workflow was called with clearinghouse_config populated
    call_kwargs = mock_workflow_service.start_workflow.call_args
    assert call_kwargs is not None
    ch_cfg = call_kwargs.kwargs.get("clearinghouse_config")
    assert ch_cfg is not None
    assert ch_cfg["clearinghouse_name"] == "availity"
    assert ch_cfg["api_endpoint"] == "https://api.availity.com"
    assert ch_cfg["credentials"] == {"client_id": "test"}


@pytest.mark.asyncio
async def test_create_task_no_org_passes_none_clearinghouse_config(db_session: AsyncSession):
    """When no organization_id is provided, clearinghouse_config should be None."""
    from unittest.mock import AsyncMock, MagicMock
    from app.services.workflow_service import WorkflowService

    mock_workflow_service = AsyncMock(spec=WorkflowService)
    mock_execution = MagicMock()
    mock_execution.id = uuid.uuid4()
    mock_execution.workflow_id = "test-wf-id"
    mock_workflow_service.start_workflow = AsyncMock(return_value=mock_execution)

    service = AgentService(db_session, workflow_service=mock_workflow_service)
    await service.create_task(
        agent_type="eligibility",
        input_data={"subscriber_id": "Y"},
    )

    call_kwargs = mock_workflow_service.start_workflow.call_args
    assert call_kwargs is not None
    ch_cfg = call_kwargs.kwargs.get("clearinghouse_config")
    assert ch_cfg is None


@pytest.mark.asyncio
async def test_dashboard_agent_metrics(db_session: AsyncSession):
    """DashboardService.get_agent_metrics returns per-agent data."""
    service = DashboardService(db_session)
    metrics = await service.get_agent_metrics("eligibility")
    assert metrics["agent_type"] == "eligibility"
    assert "total_tasks" in metrics
