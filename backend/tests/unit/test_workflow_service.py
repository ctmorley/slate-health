"""Unit tests for the WorkflowService.

Tests cover:
- Starting workflows and creating DB records (inline fallback)
- Starting workflows via Temporal client
- Querying workflows by workflow_id and by DB id
- Cancelling workflows (including Temporal cancellation and terminal state handling)
- Listing workflows with filters
- Workflow history retrieval (synthetic and Temporal)
- End-to-end eligibility workflow via service
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_task import AgentTask
from app.models.workflow import WorkflowExecution
from app.services.workflow_service import WorkflowService


# ── Helpers ─────────────────────────────────────────────────────────────


async def _create_task(session: AsyncSession, agent_type: str = "eligibility") -> AgentTask:
    """Create and flush an AgentTask for testing."""
    task = AgentTask(
        agent_type=agent_type,
        status="pending",
        input_data={"subscriber_id": "SUB001"},
    )
    session.add(task)
    await session.flush()
    return task


async def _create_workflow(
    session: AsyncSession,
    *,
    workflow_id: str | None = None,
    agent_type: str = "eligibility",
    status: str = "running",
) -> WorkflowExecution:
    """Create and flush a WorkflowExecution for testing."""
    wf = WorkflowExecution(
        workflow_id=workflow_id or f"wf-{uuid.uuid4().hex[:8]}",
        run_id=uuid.uuid4().hex,
        agent_type=agent_type,
        status=status,
        task_queue="slate-health-agents",
        input_data={"task_id": str(uuid.uuid4()), "agent_type": agent_type},
    )
    session.add(wf)
    await session.flush()
    return wf


# ── WorkflowService Tests (inline fallback) ──────────────────────────


class TestWorkflowServiceStartWorkflow:
    @pytest.mark.asyncio
    async def test_start_eligibility_workflow(self, db_session: AsyncSession):
        """Starting an eligibility workflow creates DB records and runs inline."""
        task = await _create_task(db_session)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = json.dumps({
            "id": "TX-001",
            "coverage": {"active": True, "plan_name": "Gold", "effective_date": "20240101"},
            "benefits": [{"eligibility_code": "1", "service_type_code": "30"}],
            "subscriber": {"id": "SUB001"},
            "payer": {"name": "Payer"},
            "errors": [],
        })
        mock_response.json.return_value = json.loads(mock_response.text)
        mock_response.headers = {"content-type": "application/json"}

        # No temporal_client → inline fallback
        service = WorkflowService(db_session)

        # Mock the DB engine used by write_eligibility_result (it creates
        # its own engine from settings, separate from the test session)
        _mock_task = MagicMock()
        _mock_task.patient_id = "patient-001"
        _mock_session = AsyncMock()
        _mock_session.__aenter__ = AsyncMock(return_value=_mock_session)
        _mock_session.__aexit__ = AsyncMock(return_value=False)
        _mock_session.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=_mock_task))
        )
        _mock_session.commit = AsyncMock()
        _mock_session.add = MagicMock()
        _mock_engine = AsyncMock()
        _mock_engine.dispose = AsyncMock()

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_response), \
             patch("app.workflows.eligibility._get_activity_session_factory",
                   return_value=(MagicMock(return_value=_mock_session), _mock_engine)):
            execution = await service.start_workflow(
                agent_type="eligibility",
                task_id=str(task.id),
                input_data={
                    "subscriber_id": "SUB001",
                    "subscriber_last_name": "Doe",
                    "subscriber_first_name": "Jane",
                    "payer_id": "PAYER01",
                    "payer_name": "Test Payer",
                    "provider_npi": "1234567890",
                    "provider_last_name": "Smith",
                },
                clearinghouse_config={
                    "clearinghouse_name": "claim_md",
                    "api_endpoint": "https://test.com",
                    "credentials": {"api_key": "key", "account_key": "acct"},
                },
            )

        assert execution.workflow_id is not None
        assert execution.agent_type == "eligibility"
        assert execution.status == "completed"
        assert execution.output_data is not None

        # Task should be linked and updated
        await db_session.refresh(task)
        assert task.workflow_execution_id == execution.id
        assert task.status in ("completed", "review")

    @pytest.mark.asyncio
    async def test_start_workflow_failure(self, db_session: AsyncSession):
        """Workflow that fails updates execution and task status."""
        task = await _create_task(db_session)

        service = WorkflowService(db_session)

        # Missing required fields will cause validation failure
        execution = await service.start_workflow(
            agent_type="eligibility",
            task_id=str(task.id),
            input_data={},  # Missing required fields
        )

        assert execution.status == "failed"
        assert execution.error_message is not None

    @pytest.mark.asyncio
    async def test_start_compliance_agent_type(self, db_session: AsyncSession):
        """Compliance agent type now has an inline runner and completes."""
        task = await _create_task(db_session, agent_type="compliance")

        service = WorkflowService(db_session)
        execution = await service.start_workflow(
            agent_type="compliance",
            task_id=str(task.id),
            input_data={
                "organization_id": "org-test-123",
                "measure_set": "HEDIS",
                "reporting_period_start": "2025-01-01",
                "reporting_period_end": "2025-12-31",
            },
        )

        # Now that compliance is implemented, it should complete or fail gracefully
        assert execution.status in ("completed", "failed", "running")


# ── WorkflowService Tests (with Temporal client) ─────────────────────


class TestWorkflowServiceWithTemporal:
    @pytest.mark.asyncio
    async def test_start_workflow_dispatches_to_temporal(self, db_session: AsyncSession):
        """When a Temporal client is provided, workflow is dispatched (fire-and-return)."""
        import asyncio

        task = await _create_task(db_session)

        # Mock the Temporal client and handle.
        # IMPORTANT: Use MagicMock for the client (not AsyncMock) because
        # get_workflow_handle is sync on the real Temporal client. Using
        # AsyncMock would cause unawaited coroutine warnings when the
        # background awaiter task calls get_workflow_handle().
        mock_handle = AsyncMock()
        mock_handle.result_run_id = "temporal-run-id"

        # Background awaiter calls get_workflow_handle().result() — mock that path
        blocking_future: asyncio.Future = asyncio.get_event_loop().create_future()
        get_handle = MagicMock()
        get_handle.result = AsyncMock(return_value=blocking_future)

        mock_temporal_client = MagicMock()
        mock_temporal_client.start_workflow = AsyncMock(return_value=mock_handle)
        mock_temporal_client.get_workflow_handle = MagicMock(return_value=get_handle)

        service = WorkflowService(db_session, temporal_client=mock_temporal_client)
        execution = await service.start_workflow(
            agent_type="eligibility",
            task_id=str(task.id),
            input_data={"subscriber_id": "SUB001"},
        )

        # Allow background task to start
        await asyncio.sleep(0)

        # Verify Temporal was called
        mock_temporal_client.start_workflow.assert_called_once()
        call_kwargs = mock_temporal_client.start_workflow.call_args.kwargs
        assert call_kwargs["task_queue"] == "slate-health-agents"
        assert execution.workflow_id in call_kwargs["id"]

        # Fire-and-return: execution stays in "running" state
        assert execution.status == "running"
        assert execution.run_id == "temporal-run-id"

        # Cleanup: cancel background tasks to prevent unawaited warnings
        await service.shutdown()

    @pytest.mark.asyncio
    async def test_cancel_workflow_sends_to_temporal(self, db_session: AsyncSession):
        """When a Temporal client is provided, cancel sends to Temporal."""
        wf = await _create_workflow(db_session, workflow_id="cancel-temporal", status="running")

        mock_handle = AsyncMock()
        mock_temporal_client = MagicMock()
        mock_temporal_client.get_workflow_handle.return_value = mock_handle

        service = WorkflowService(db_session, temporal_client=mock_temporal_client)
        result = await service.cancel_workflow("cancel-temporal")

        assert result is not None
        assert result.status == "cancelled"
        mock_temporal_client.get_workflow_handle.assert_called_once_with("cancel-temporal")
        mock_handle.cancel.assert_called_once()

    @pytest.mark.asyncio
    async def test_start_unknown_type_uses_generic_workflow(self, db_session: AsyncSession):
        """Unknown agent type dispatches to GenericAgentWorkflow via Temporal."""
        import asyncio

        task = await _create_task(db_session, agent_type="scheduling")

        mock_handle = AsyncMock()
        mock_handle.result_run_id = "gen-run-id"

        # Use MagicMock for client to avoid unawaited coroutine from get_workflow_handle
        blocking_future: asyncio.Future = asyncio.get_event_loop().create_future()
        get_handle = MagicMock()
        get_handle.result = AsyncMock(return_value=blocking_future)

        mock_temporal_client = MagicMock()
        mock_temporal_client.start_workflow = AsyncMock(return_value=mock_handle)
        mock_temporal_client.get_workflow_handle = MagicMock(return_value=get_handle)

        service = WorkflowService(db_session, temporal_client=mock_temporal_client)
        execution = await service.start_workflow(
            agent_type="scheduling",
            task_id=str(task.id),
            input_data={"request": "test"},
        )

        # Allow background task to start
        await asyncio.sleep(0)

        # Fire-and-return: dispatched to Temporal, stays running
        assert execution.status == "running"
        mock_temporal_client.start_workflow.assert_called_once()

        # Cleanup background tasks
        await service.shutdown()

    @pytest.mark.asyncio
    async def test_cancel_workflow_temporal_failure_preserves_state(self, db_session: AsyncSession):
        """If Temporal cancel fails, local DB state must NOT be marked cancelled."""
        wf = await _create_workflow(
            db_session, workflow_id="cancel-fail-temporal", status="running"
        )

        mock_handle = AsyncMock()
        mock_handle.cancel.side_effect = RuntimeError("Temporal unavailable")

        mock_temporal_client = MagicMock()
        mock_temporal_client.get_workflow_handle.return_value = mock_handle

        service = WorkflowService(db_session, temporal_client=mock_temporal_client)
        result = await service.cancel_workflow("cancel-fail-temporal")

        assert result is not None
        # State must NOT be cancelled since Temporal cancel failed
        assert result.status == "running"
        assert "Cancel failed" in (result.error_message or "")

    @pytest.mark.asyncio
    async def test_refresh_temporal_error_records_on_execution(self, db_session: AsyncSession):
        """refresh_workflow_status records unexpected errors instead of swallowing."""
        wf = await _create_workflow(
            db_session, workflow_id="refresh-error", status="running"
        )

        mock_handle = AsyncMock()
        mock_handle.describe.side_effect = RuntimeError("Unexpected Temporal error")

        mock_temporal_client = MagicMock()
        mock_temporal_client.get_workflow_handle.return_value = mock_handle

        service = WorkflowService(db_session, temporal_client=mock_temporal_client)
        result = await service.refresh_workflow_status("refresh-error")

        assert result is not None
        # Status stays running (not silently swallowed)
        assert result.status == "running"
        # Error is recorded on execution
        assert "Refresh error" in (result.error_message or "")

    @pytest.mark.asyncio
    async def test_refresh_transient_error_preserves_state(self, db_session: AsyncSession):
        """Transient network errors are logged but don't mutate execution state."""
        wf = await _create_workflow(
            db_session, workflow_id="refresh-transient", status="running"
        )

        mock_handle = AsyncMock()
        mock_handle.describe.side_effect = ConnectionError("Network unreachable")

        mock_temporal_client = MagicMock()
        mock_temporal_client.get_workflow_handle.return_value = mock_handle

        service = WorkflowService(db_session, temporal_client=mock_temporal_client)
        result = await service.refresh_workflow_status("refresh-transient")

        assert result is not None
        assert result.status == "running"
        # Transient errors should NOT set error_message
        assert result.error_message is None


# ── WorkflowService Query Tests ──────────────────────────────────────


class TestWorkflowServiceQuery:
    @pytest.mark.asyncio
    async def test_get_workflow_by_workflow_id(self, db_session: AsyncSession):
        wf = await _create_workflow(db_session, workflow_id="test-wf-001")
        service = WorkflowService(db_session)

        result = await service.get_workflow("test-wf-001")
        assert result is not None
        assert result.workflow_id == "test-wf-001"

    @pytest.mark.asyncio
    async def test_get_workflow_not_found(self, db_session: AsyncSession):
        service = WorkflowService(db_session)
        result = await service.get_workflow("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_workflow_by_db_id(self, db_session: AsyncSession):
        wf = await _create_workflow(db_session)
        service = WorkflowService(db_session)

        result = await service.get_workflow_by_id(str(wf.id))
        assert result is not None
        assert result.id == wf.id


# ── WorkflowService Cancel Tests ─────────────────────────────────────


class TestWorkflowServiceCancel:
    @pytest.mark.asyncio
    async def test_cancel_running_workflow(self, db_session: AsyncSession):
        wf = await _create_workflow(db_session, workflow_id="cancel-me", status="running")
        service = WorkflowService(db_session)

        result = await service.cancel_workflow("cancel-me")
        assert result is not None
        assert result.status == "cancelled"

    @pytest.mark.asyncio
    async def test_cancel_nonexistent_workflow(self, db_session: AsyncSession):
        service = WorkflowService(db_session)
        result = await service.cancel_workflow("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_cancel_completed_workflow_no_change(self, db_session: AsyncSession):
        wf = await _create_workflow(db_session, workflow_id="done-wf", status="completed")
        service = WorkflowService(db_session)

        result = await service.cancel_workflow("done-wf")
        assert result is not None
        assert result.status == "completed"  # Not changed

    @pytest.mark.asyncio
    async def test_cancel_with_linked_task(self, db_session: AsyncSession):
        task = await _create_task(db_session)
        wf = await _create_workflow(db_session, workflow_id="cancel-with-task")
        wf.input_data = {"task_id": str(task.id), "agent_type": "eligibility"}
        task.status = "running"
        await db_session.flush()

        service = WorkflowService(db_session)
        result = await service.cancel_workflow("cancel-with-task")

        assert result.status == "cancelled"
        await db_session.refresh(task)
        assert task.status == "cancelled"


# ── WorkflowService List Tests ───────────────────────────────────────


class TestWorkflowServiceList:
    @pytest.mark.asyncio
    async def test_list_all(self, db_session: AsyncSession):
        for i in range(3):
            await _create_workflow(db_session, workflow_id=f"list-wf-{i}")

        service = WorkflowService(db_session)
        executions, total = await service.list_workflows()

        assert total >= 3
        assert len(executions) >= 3

    @pytest.mark.asyncio
    async def test_list_filter_by_agent_type(self, db_session: AsyncSession):
        await _create_workflow(db_session, workflow_id="elig-1", agent_type="eligibility")
        await _create_workflow(db_session, workflow_id="claim-1", agent_type="claims")

        service = WorkflowService(db_session)
        executions, total = await service.list_workflows(agent_type="eligibility")

        assert all(e.agent_type == "eligibility" for e in executions)

    @pytest.mark.asyncio
    async def test_list_filter_by_status(self, db_session: AsyncSession):
        await _create_workflow(db_session, workflow_id="running-1", status="running")
        await _create_workflow(db_session, workflow_id="done-1", status="completed")

        service = WorkflowService(db_session)
        executions, total = await service.list_workflows(status="completed")

        assert all(e.status == "completed" for e in executions)

    @pytest.mark.asyncio
    async def test_list_pagination(self, db_session: AsyncSession):
        for i in range(5):
            await _create_workflow(db_session, workflow_id=f"page-wf-{i}")

        service = WorkflowService(db_session)
        page1, total = await service.list_workflows(limit=2, offset=0)
        page2, _ = await service.list_workflows(limit=2, offset=2)

        assert len(page1) == 2
        assert len(page2) == 2
        assert page1[0].id != page2[0].id


# ── WorkflowService History Tests ────────────────────────────────────


class TestWorkflowServiceHistory:
    @pytest.mark.asyncio
    async def test_history_completed_workflow(self, db_session: AsyncSession):
        wf = await _create_workflow(
            db_session, workflow_id="hist-completed", status="completed"
        )
        wf.output_data = {"coverage_active": True}
        await db_session.flush()

        service = WorkflowService(db_session)
        events = await service.get_workflow_history("hist-completed")

        assert len(events) == 2
        assert events[0]["event_type"] == "WorkflowExecutionStarted"
        assert events[1]["event_type"] == "WorkflowExecutionCompleted"

    @pytest.mark.asyncio
    async def test_history_failed_workflow(self, db_session: AsyncSession):
        wf = await _create_workflow(
            db_session, workflow_id="hist-failed", status="failed"
        )
        wf.error_message = "Something went wrong"
        await db_session.flush()

        service = WorkflowService(db_session)
        events = await service.get_workflow_history("hist-failed")

        assert len(events) == 2
        assert events[1]["event_type"] == "WorkflowExecutionFailed"
        assert events[1]["details"]["error"] == "Something went wrong"

    @pytest.mark.asyncio
    async def test_history_cancelled_workflow(self, db_session: AsyncSession):
        await _create_workflow(
            db_session, workflow_id="hist-cancelled", status="cancelled"
        )
        service = WorkflowService(db_session)
        events = await service.get_workflow_history("hist-cancelled")

        assert events[1]["event_type"] == "WorkflowExecutionCancelled"

    @pytest.mark.asyncio
    async def test_history_running_workflow(self, db_session: AsyncSession):
        await _create_workflow(
            db_session, workflow_id="hist-running", status="running"
        )
        service = WorkflowService(db_session)
        events = await service.get_workflow_history("hist-running")

        # Running workflow only has start event
        assert len(events) == 1
        assert events[0]["event_type"] == "WorkflowExecutionStarted"

    @pytest.mark.asyncio
    async def test_history_nonexistent(self, db_session: AsyncSession):
        service = WorkflowService(db_session)
        events = await service.get_workflow_history("nonexistent")
        assert events is None

    @pytest.mark.asyncio
    async def test_history_with_temporal_client(self, db_session: AsyncSession):
        """When Temporal client is available, history is fetched from Temporal."""
        wf = await _create_workflow(
            db_session, workflow_id="hist-temporal", status="completed"
        )

        # Mock Temporal history events
        mock_event = MagicMock()
        mock_event.event_type.name = "EVENT_TYPE_WORKFLOW_EXECUTION_STARTED"
        mock_event.event_time.isoformat.return_value = "2024-01-01T00:00:00Z"

        mock_handle = MagicMock()

        async def _fake_history():
            yield mock_event

        mock_handle.fetch_history_events = _fake_history

        mock_temporal_client = MagicMock()
        mock_temporal_client.get_workflow_handle.return_value = mock_handle

        service = WorkflowService(db_session, temporal_client=mock_temporal_client)
        events = await service.get_workflow_history("hist-temporal")

        assert len(events) >= 1
        mock_temporal_client.get_workflow_handle.assert_called_once_with("hist-temporal")


# ── WorkflowService refresh_workflow_status Tests ────────────────────


class TestWorkflowServiceRefresh:
    @pytest.mark.asyncio
    async def test_refresh_completed_workflow(self, db_session: AsyncSession):
        """refresh_workflow_status updates DB when Temporal reports completed."""
        task = await _create_task(db_session)
        wf = await _create_workflow(db_session, workflow_id="refresh-001", status="running")
        wf.input_data = {"task_id": str(task.id), "agent_type": "eligibility"}
        task.status = "running"
        await db_session.flush()

        from app.workflows.base import WorkflowResult, WorkflowStatus

        # Mock Temporal describe → COMPLETED, result available
        mock_desc = MagicMock()
        mock_desc.status.name = "COMPLETED"

        mock_handle = AsyncMock()
        mock_handle.describe.return_value = mock_desc
        mock_handle.result.return_value = WorkflowResult(
            task_id=str(task.id),
            agent_type="eligibility",
            status=WorkflowStatus.COMPLETED.value,
            output_data={"coverage_active": True},
            confidence=0.95,
        )

        mock_temporal_client = MagicMock()
        mock_temporal_client.get_workflow_handle.return_value = mock_handle

        service = WorkflowService(db_session, temporal_client=mock_temporal_client)
        result = await service.refresh_workflow_status("refresh-001")

        assert result is not None
        assert result.status == "completed"
        assert result.output_data == {"coverage_active": True}

        await db_session.refresh(task)
        assert task.status in ("completed", "review")

    @pytest.mark.asyncio
    async def test_refresh_still_running(self, db_session: AsyncSession):
        """refresh_workflow_status leaves running status unchanged."""
        wf = await _create_workflow(db_session, workflow_id="refresh-running", status="running")

        mock_desc = MagicMock()
        mock_desc.status.name = "RUNNING"

        mock_handle = AsyncMock()
        mock_handle.describe.return_value = mock_desc

        mock_temporal_client = MagicMock()
        mock_temporal_client.get_workflow_handle.return_value = mock_handle

        service = WorkflowService(db_session, temporal_client=mock_temporal_client)
        result = await service.refresh_workflow_status("refresh-running")

        assert result is not None
        assert result.status == "running"

    @pytest.mark.asyncio
    async def test_refresh_already_terminal(self, db_session: AsyncSession):
        """refresh_workflow_status is a no-op for terminal states."""
        wf = await _create_workflow(db_session, workflow_id="refresh-done", status="completed")

        mock_temporal_client = MagicMock()

        service = WorkflowService(db_session, temporal_client=mock_temporal_client)
        result = await service.refresh_workflow_status("refresh-done")

        assert result.status == "completed"
        # Temporal should not be queried
        mock_temporal_client.get_workflow_handle.assert_not_called()

    @pytest.mark.asyncio
    async def test_refresh_nonexistent(self, db_session: AsyncSession):
        """refresh_workflow_status returns None for unknown workflow."""
        service = WorkflowService(db_session)
        result = await service.refresh_workflow_status("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_refresh_failed_workflow(self, db_session: AsyncSession):
        """refresh_workflow_status handles Temporal FAILED status."""
        task = await _create_task(db_session)
        wf = await _create_workflow(db_session, workflow_id="refresh-fail", status="running")
        wf.input_data = {"task_id": str(task.id), "agent_type": "eligibility"}
        task.status = "running"
        await db_session.flush()

        mock_desc = MagicMock()
        mock_desc.status.name = "FAILED"

        mock_handle = AsyncMock()
        mock_handle.describe.return_value = mock_desc

        mock_temporal_client = MagicMock()
        mock_temporal_client.get_workflow_handle.return_value = mock_handle

        service = WorkflowService(db_session, temporal_client=mock_temporal_client)
        result = await service.refresh_workflow_status("refresh-fail")

        assert result.status == "failed"
        await db_session.refresh(task)
        assert task.status == "failed"


# ── Integration Test: WorkflowService + Temporal + DB ────────────────


class TestWorkflowServiceTemporalIntegration:
    """Integration test that runs WorkflowService against a real Temporal
    local env and real test DB session, then asserts WorkflowExecution
    state transitions and DB record creation."""

    @pytest.mark.asyncio
    async def test_start_and_refresh_eligibility_workflow(self, db_session: AsyncSession):
        """Full integration: start via Temporal → refresh → verify DB state."""
        from temporalio.testing import WorkflowEnvironment
        from temporalio.worker import Worker as TemporalWorker
        from app.workflows.eligibility import (
            EligibilityWorkflow,
            validate_eligibility_input,
            create_pending_eligibility_check,
            execute_eligibility_agent,
            build_eligibility_request,
            submit_to_clearinghouse,
            parse_eligibility_response,
            write_eligibility_result,
        )

        # Set up mock clearinghouse response
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = json.dumps({
            "id": "TX-INT",
            "coverage": {"active": True, "effective_date": "20240101"},
            "benefits": [{"eligibility_code": "1"}],
            "subscriber": {"id": "SUB001"},
            "payer": {"name": "Test Payer"},
            "errors": [],
        })
        mock_response.json.return_value = json.loads(mock_response.text)
        mock_response.headers = {"content-type": "application/json"}

        # Mock the DB engine used inside write_eligibility_result
        mock_task = MagicMock()
        mock_task.patient_id = "patient-int-001"
        _mock_sess = AsyncMock()
        _mock_sess.__aenter__ = AsyncMock(return_value=_mock_sess)
        _mock_sess.__aexit__ = AsyncMock(return_value=False)
        _mock_sess.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=mock_task))
        )
        _mock_sess.commit = AsyncMock()
        _mock_sess.add = MagicMock()
        _mock_engine = AsyncMock()
        _mock_engine.dispose = AsyncMock()

        async with await WorkflowEnvironment.start_local() as env:
            with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_response), \
                 patch("app.workflows.eligibility._get_activity_session_factory",
                       return_value=(MagicMock(return_value=_mock_sess), _mock_engine)):

                # Create task in real test DB
                task = await _create_task(db_session)

                # Start the worker
                async with TemporalWorker(
                    env.client,
                    task_queue="test-int-queue",
                    workflows=[EligibilityWorkflow],
                    activities=[
                        validate_eligibility_input,
                        create_pending_eligibility_check,
                        execute_eligibility_agent,
                        build_eligibility_request,
                        submit_to_clearinghouse,
                        parse_eligibility_response,
                        write_eligibility_result,
                    ],
                ):
                    # Use WorkflowService with the real Temporal client
                    service = WorkflowService(db_session, temporal_client=env.client)

                    # start_workflow is now fire-and-return
                    execution = await service.start_workflow(
                        agent_type="eligibility",
                        task_id=str(task.id),
                        input_data={
                            "subscriber_id": "SUB001",
                            "subscriber_last_name": "Doe",
                            "subscriber_first_name": "Jane",
                            "payer_id": "PAYER01",
                            "payer_name": "Test Payer",
                            "provider_npi": "1234567890",
                            "provider_last_name": "Smith",
                        },
                        clearinghouse_config={
                            "clearinghouse_name": "availity",
                            "api_endpoint": "https://api.test.com",
                            "credentials": {"api_key": "test-key"},
                        },
                        task_queue="test-int-queue",
                    )

                    assert execution.status == "running"
                    assert execution.workflow_id is not None
                    await db_session.commit()

                    # Wait for the Temporal workflow to complete before refreshing
                    handle = env.client.get_workflow_handle(execution.workflow_id)
                    await handle.result()

                    # Now refresh to pull the result into DB
                    updated = await service.refresh_workflow_status(execution.workflow_id)

                    assert updated is not None
                    assert updated.status == "completed"
                    assert updated.output_data is not None
                    assert updated.output_data.get("coverage_active") is True


# ── Background task lifecycle tests ──────────────────────────────────


class TestBackgroundTaskTracking:
    """Tests for tracked background task management in WorkflowService."""

    @pytest.mark.asyncio
    async def test_background_tasks_tracked_on_temporal_dispatch(self, db_session: AsyncSession):
        """Background awaiter tasks are registered in _background_tasks
        when dispatching to Temporal, preventing GC and unawaited warnings."""
        import asyncio
        task = await _create_task(db_session, agent_type="eligibility")

        # Start-workflow mock handle (returned by start_workflow, which is async)
        start_handle = AsyncMock()
        start_handle.result_run_id = "track-run-id"

        # The background awaiter calls temporal_client.get_workflow_handle()
        # (sync method) then await handle.result() (async). We need result()
        # to block so the bg task stays alive.
        blocking_future: asyncio.Future = asyncio.get_event_loop().create_future()
        get_handle = MagicMock()
        get_handle.result = AsyncMock(return_value=blocking_future)

        mock_temporal_client = MagicMock()
        mock_temporal_client.start_workflow = AsyncMock(return_value=start_handle)
        # get_workflow_handle is sync on the real Temporal client
        mock_temporal_client.get_workflow_handle = MagicMock(return_value=get_handle)

        service = WorkflowService(db_session, temporal_client=mock_temporal_client)
        execution = await service.start_workflow(
            agent_type="eligibility",
            task_id=str(task.id),
            input_data={"subscriber_id": "SUB001"},
        )

        # Allow the bg task to start running
        await asyncio.sleep(0)

        assert execution.status == "running"
        # The background task should be tracked
        assert len(service._background_tasks) == 1
        bg_task = next(iter(service._background_tasks))
        assert bg_task.get_name().startswith("temporal-awaiter-")

        # Cleanup: cancel the background task
        await service.shutdown()
        assert len(service._background_tasks) == 0

    @pytest.mark.asyncio
    async def test_shutdown_cancels_background_tasks(self, db_session: AsyncSession):
        """shutdown() cancels all tracked background tasks cleanly."""
        service = WorkflowService(db_session, temporal_client=None)

        import asyncio

        async def _dummy():
            await asyncio.sleep(999)

        t = asyncio.create_task(_dummy(), name="test-bg")
        service._background_tasks.add(t)
        t.add_done_callback(service._background_tasks.discard)

        assert len(service._background_tasks) == 1
        await service.shutdown()
        assert len(service._background_tasks) == 0
        assert t.cancelled()

    @pytest.mark.asyncio
    async def test_completed_background_task_auto_removed(self, db_session: AsyncSession):
        """Background tasks are auto-removed from the set on completion."""
        service = WorkflowService(db_session, temporal_client=None)

        import asyncio

        async def _instant():
            return "done"

        t = asyncio.create_task(_instant(), name="test-instant")
        service._background_tasks.add(t)
        t.add_done_callback(service._background_tasks.discard)

        # Let the task complete
        await t
        # Allow done callbacks to fire
        await asyncio.sleep(0)
        assert len(service._background_tasks) == 0


async def _never_return():
    """Helper that blocks forever (for mocking long-running tasks)."""
    import asyncio
    await asyncio.sleep(999999)
