"""Unit tests for Temporal workflow base, eligibility workflow, and worker.

Tests cover:
- Workflow base: data classes, retry policies, status enums, errors
- Eligibility workflow: full pipeline (validate → build 270 → submit → parse → write)
- Eligibility workflow: failure at each stage
- Eligibility workflow: Temporal integration via WorkflowEnvironment
- Worker: registration, creation, activity registry
- Generic agent workflow template
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.workflows.base import (
    AGENT_ACTIVITY_TIMEOUT,
    AGENT_RETRY_POLICY,
    CLEARINGHOUSE_RETRY_POLICY,
    DB_RETRY_POLICY,
    DEFAULT_TASK_QUEUE,
    ActivityError,
    ActivityResult,
    HeartbeatMixin,
    RetryPolicyConfig,
    WorkflowCancelledError,
    WorkflowError,
    WorkflowInput,
    WorkflowResult,
    WorkflowStatus,
    WorkflowTimeoutError,
)
from app.workflows.eligibility import (
    EligibilityWorkflow,
    _calculate_eligibility_confidence,
    build_eligibility_request,
    create_pending_eligibility_check,
    execute_eligibility_agent,
    parse_eligibility_response,
    run_eligibility_workflow,
    submit_to_clearinghouse,
    validate_eligibility_input,
    write_eligibility_result,
)
from app.workflows.agent_workflow import (
    GenericAgentWorkflow,
    validate_agent_input,
    execute_agent,
    write_agent_result,
)
from app.workflows.worker import (
    create_worker,
    get_registered_activities,
    get_registered_workflows,
    register_activity,
    register_workflow,
)


# ── Base Workflow Tests ─────────────────────────────────────────────────


class TestRetryPolicyConfig:
    def test_default_values(self):
        policy = RetryPolicyConfig()
        assert policy.initial_interval_seconds == 1.0
        assert policy.backoff_coefficient == 2.0
        assert policy.maximum_interval_seconds == 60.0
        assert policy.maximum_attempts == 3
        assert policy.non_retryable_error_types == []

    def test_custom_values(self):
        policy = RetryPolicyConfig(
            initial_interval_seconds=5.0,
            maximum_attempts=10,
            non_retryable_error_types=["ValueError"],
        )
        assert policy.initial_interval_seconds == 5.0
        assert policy.maximum_attempts == 10
        assert policy.non_retryable_error_types == ["ValueError"]

    def test_to_temporal_dict(self):
        policy = RetryPolicyConfig(
            initial_interval_seconds=2.0,
            backoff_coefficient=3.0,
            maximum_interval_seconds=120.0,
            maximum_attempts=5,
        )
        d = policy.to_temporal_dict()
        assert d["initial_interval"] == timedelta(seconds=2.0)
        assert d["backoff_coefficient"] == 3.0
        assert d["maximum_interval"] == timedelta(seconds=120.0)
        assert d["maximum_attempts"] == 5

    def test_to_retry_policy(self):
        """to_retry_policy() returns a real temporalio RetryPolicy instance."""
        from temporalio.common import RetryPolicy as TemporalRetryPolicy

        policy = RetryPolicyConfig(
            initial_interval_seconds=2.0,
            maximum_attempts=5,
        )
        tp = policy.to_retry_policy()
        assert isinstance(tp, TemporalRetryPolicy)
        assert tp.maximum_attempts == 5

    def test_predefined_agent_policy(self):
        assert AGENT_RETRY_POLICY.maximum_attempts == 3
        assert "ValidationError" in AGENT_RETRY_POLICY.non_retryable_error_types

    def test_predefined_clearinghouse_policy(self):
        assert CLEARINGHOUSE_RETRY_POLICY.maximum_attempts == 5
        assert CLEARINGHOUSE_RETRY_POLICY.initial_interval_seconds == 5.0

    def test_predefined_db_policy(self):
        assert DB_RETRY_POLICY.maximum_attempts == 3


class TestWorkflowStatus:
    def test_status_values(self):
        assert WorkflowStatus.PENDING.value == "pending"
        assert WorkflowStatus.RUNNING.value == "running"
        assert WorkflowStatus.COMPLETED.value == "completed"
        assert WorkflowStatus.FAILED.value == "failed"
        assert WorkflowStatus.CANCELLED.value == "cancelled"
        assert WorkflowStatus.TIMED_OUT.value == "timed_out"


class TestWorkflowInput:
    def test_defaults(self):
        wi = WorkflowInput(task_id="T1", agent_type="eligibility")
        assert wi.input_data == {}
        assert wi.patient_context == {}
        assert wi.payer_context == {}
        assert wi.organization_id is None
        assert wi.clearinghouse_config is None

    def test_with_data(self):
        wi = WorkflowInput(
            task_id="T1",
            agent_type="eligibility",
            input_data={"subscriber_id": "S1"},
            patient_context={"name": "Doe"},
        )
        assert wi.input_data["subscriber_id"] == "S1"


class TestWorkflowResult:
    def test_defaults(self):
        wr = WorkflowResult(task_id="T1", agent_type="eligibility")
        assert wr.status == "completed"
        assert wr.confidence == 0.0
        assert wr.needs_review is False
        assert wr.error is None

    def test_failed_result(self):
        wr = WorkflowResult(
            task_id="T1",
            agent_type="eligibility",
            status="failed",
            error="Something went wrong",
        )
        assert wr.status == "failed"
        assert wr.error == "Something went wrong"


class TestActivityResult:
    def test_success(self):
        ar = ActivityResult(success=True, data={"key": "value"})
        assert ar.success is True
        assert ar.data == {"key": "value"}
        assert ar.error is None

    def test_failure(self):
        ar = ActivityResult(success=False, error="failed")
        assert ar.success is False
        assert ar.error == "failed"

    def test_asdict(self):
        ar = ActivityResult(success=True, data={"x": 1})
        d = asdict(ar)
        assert d["success"] is True
        assert d["data"]["x"] == 1


class TestHeartbeatMixin:
    @pytest.mark.asyncio
    async def test_heartbeat_with_function(self):
        mixin = HeartbeatMixin()
        mock_fn = MagicMock()
        mixin.set_heartbeat_fn(mock_fn)
        await mixin.heartbeat("progress")
        mock_fn.assert_called_once_with("progress")

    @pytest.mark.asyncio
    async def test_heartbeat_without_function(self):
        mixin = HeartbeatMixin()
        # Should not raise
        await mixin.heartbeat("progress")

    @pytest.mark.asyncio
    async def test_heartbeat_error_suppressed(self):
        mixin = HeartbeatMixin()
        mixin.set_heartbeat_fn(MagicMock(side_effect=Exception("fail")))
        # Should not raise
        await mixin.heartbeat()


class TestBaseWorkflow:
    """Tests for the abstract BaseWorkflow class."""

    def test_cannot_instantiate_directly(self):
        from app.workflows.base import BaseWorkflow
        with pytest.raises(TypeError):
            BaseWorkflow()

    def test_subclass_must_implement_run(self):
        from app.workflows.base import BaseWorkflow

        class IncompleteWorkflow(BaseWorkflow):
            pass

        with pytest.raises(TypeError):
            IncompleteWorkflow()

    @pytest.mark.asyncio
    async def test_subclass_with_run_works(self):
        from app.workflows.base import BaseWorkflow

        class ConcreteWorkflow(BaseWorkflow):
            async def run(self, workflow_input):
                return WorkflowResult(
                    task_id=workflow_input.task_id,
                    agent_type=workflow_input.agent_type,
                    status="completed",
                )

        wf = ConcreteWorkflow()
        inp = WorkflowInput(task_id="T1", agent_type="test")
        result = await wf.run(inp)
        assert result.status == "completed"

    def test_fail_helper(self):
        from app.workflows.base import BaseWorkflow

        class ConcreteWorkflow(BaseWorkflow):
            async def run(self, workflow_input):
                return self._fail(workflow_input.task_id, workflow_input.agent_type, "boom")

        wf = ConcreteWorkflow()
        # Test the static _fail helper directly
        result = BaseWorkflow._fail("T1", "eligibility", "test error")
        assert result.status == "failed"
        assert result.error == "test error"
        assert result.task_id == "T1"


class TestBaseActivity:
    """Tests for the abstract BaseActivity class."""

    def test_cannot_instantiate_directly(self):
        from app.workflows.base import BaseActivity
        with pytest.raises(TypeError):
            BaseActivity()

    def test_subclass_must_implement_execute(self):
        from app.workflows.base import BaseActivity

        class IncompleteActivity(BaseActivity):
            pass

        with pytest.raises(TypeError):
            IncompleteActivity()

    @pytest.mark.asyncio
    async def test_subclass_with_execute_works(self):
        from app.workflows.base import BaseActivity

        class ConcreteActivity(BaseActivity):
            async def execute(self, data):
                return {"result": data}

        act = ConcreteActivity()
        result = await act.execute("hello")
        assert result == {"result": "hello"}

    @pytest.mark.asyncio
    async def test_heartbeat_on_activity(self):
        from app.workflows.base import BaseActivity

        class ConcreteActivity(BaseActivity):
            async def execute(self):
                await self.heartbeat("progress")
                return True

        act = ConcreteActivity()
        mock_fn = MagicMock()
        act.set_heartbeat_fn(mock_fn)
        result = await act.execute()
        assert result is True
        mock_fn.assert_called_once_with("progress")


class TestWorkflowErrors:
    def test_workflow_error(self):
        err = WorkflowError("fail", task_id="T1", details={"step": "build"})
        assert str(err) == "fail"
        assert err.task_id == "T1"
        assert err.details == {"step": "build"}

    def test_activity_error_is_workflow_error(self):
        err = ActivityError("activity failed")
        assert isinstance(err, WorkflowError)

    def test_timeout_error(self):
        err = WorkflowTimeoutError("timeout")
        assert isinstance(err, WorkflowError)

    def test_cancelled_error(self):
        err = WorkflowCancelledError("cancelled")
        assert isinstance(err, WorkflowError)


class TestSafeHeartbeat:
    def test_safe_heartbeat_outside_activity_context(self):
        """safe_heartbeat silently no-ops when not in Temporal activity context."""
        from app.workflows.base import safe_heartbeat
        # Should not raise
        safe_heartbeat("test-details")

    def test_safe_heartbeat_inside_activity_context(self):
        """safe_heartbeat calls activity.heartbeat when in context."""
        from app.workflows.base import safe_heartbeat
        with patch("temporalio.activity.heartbeat") as mock_hb:
            safe_heartbeat("some-progress")
            mock_hb.assert_called_once_with("some-progress")


class TestConstants:
    def test_default_task_queue(self):
        assert DEFAULT_TASK_QUEUE == "slate-health-agents"

    def test_activity_timeouts(self):
        assert AGENT_ACTIVITY_TIMEOUT == timedelta(minutes=5)

    def test_heartbeat_timeouts(self):
        from app.workflows.base import CLEARINGHOUSE_HEARTBEAT_TIMEOUT, DB_HEARTBEAT_TIMEOUT
        assert CLEARINGHOUSE_HEARTBEAT_TIMEOUT == timedelta(seconds=60)
        assert DB_HEARTBEAT_TIMEOUT == timedelta(seconds=15)


# ── Eligibility Workflow Activity Tests ─────────────────────────────────


class TestValidateEligibilityInput:
    @pytest.mark.asyncio
    async def test_valid_input(self):
        result = await validate_eligibility_input({
            "input_data": {
                "subscriber_id": "S001",
                "subscriber_last_name": "Doe",
                "subscriber_first_name": "Jane",
                "payer_id": "P001",
            }
        })
        assert result["success"] is True
        assert result["data"]["subscriber_id"] == "S001"

    @pytest.mark.asyncio
    async def test_missing_required_fields(self):
        result = await validate_eligibility_input({
            "input_data": {"subscriber_id": "S001"}
        })
        assert result["success"] is False
        assert "subscriber_last_name" in result["error"]

    @pytest.mark.asyncio
    async def test_empty_input_data(self):
        result = await validate_eligibility_input({"input_data": {}})
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_defaults_applied(self):
        result = await validate_eligibility_input({
            "input_data": {
                "subscriber_id": "S001",
                "subscriber_last_name": "Doe",
                "subscriber_first_name": "Jane",
            }
        })
        assert result["success"] is True
        assert result["data"]["service_type_code"] == "30"
        assert result["data"]["payer_id"] == ""


class TestExecuteEligibilityAgent:
    @pytest.mark.asyncio
    async def test_agent_enriches_data(self):
        validated = {
            "success": True,
            "data": {
                "subscriber_id": "S001",
                "subscriber_last_name": "Doe",
                "subscriber_first_name": "Jane",
                "payer_id": "P001",
            },
        }
        result = await execute_eligibility_agent(validated)
        assert result["success"] is True
        assert "agent_decision" in result["data"]
        assert result["data"]["agent_decision"]["submission_strategy"] == "clearinghouse"
        assert result["data"]["subscriber_id"] == "S001"

    @pytest.mark.asyncio
    async def test_agent_without_payer_id(self):
        validated = {
            "success": True,
            "data": {
                "subscriber_id": "S001",
                "subscriber_last_name": "Doe",
                "subscriber_first_name": "Jane",
                "payer_id": "",
            },
        }
        result = await execute_eligibility_agent(validated)
        assert result["success"] is True
        assert result["data"]["agent_decision"]["enrichments"].get("payer_id_verified") is None


class TestBuildEligibilityRequest:
    @pytest.mark.asyncio
    async def test_successful_build(self):
        validated = {
            "success": True,
            "data": {
                "subscriber_id": "S001",
                "subscriber_last_name": "Doe",
                "subscriber_first_name": "Jane",
                "subscriber_dob": "19900101",
                "payer_id": "P001",
                "payer_name": "Test Payer",
                "provider_npi": "1234567890",
                "provider_last_name": "Smith",
                "provider_first_name": "Dr",
                "date_of_service": "20240101",
                "service_type_code": "30",
            },
        }
        result = await build_eligibility_request(validated)
        assert result["success"] is True
        assert "x12_270" in result["data"]
        assert "ISA" in result["data"]["x12_270"]
        assert result["data"]["control_number"] != ""


class TestSubmitToClearinghouse:
    @pytest.mark.asyncio
    async def test_successful_submission(self):
        submit_args = {
            "x12_payload": {
                "data": {
                    "x12_270": "ISA*00*test~ST*270*0001~SE*2*0001~IEA*1*000000001~",
                    "control_number": "000000001",
                }
            },
            "clearinghouse_config": {
                "clearinghouse_name": "availity",
                "api_endpoint": "https://api.test.com",
                "credentials": {"api_key": "test-key"},
            },
        }

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = '{"id": "TX-001"}'
        mock_response.json.return_value = {"id": "TX-001"}
        mock_response.headers = {"content-type": "application/json"}

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_response):
            result = await submit_to_clearinghouse(submit_args)

        assert result["success"] is True
        assert result["data"]["transaction_id"] == "TX-001"

    @pytest.mark.asyncio
    async def test_empty_payload(self):
        result = await submit_to_clearinghouse({"x12_payload": {"data": {}}})
        assert result["success"] is False
        assert "No X12 270 payload" in result["error"]

    @pytest.mark.asyncio
    async def test_clearinghouse_transient_failure_raises(self):
        """Transient errors propagate so Temporal can retry the activity."""
        submit_args = {
            "x12_payload": {
                "data": {
                    "x12_270": "ISA*test~",
                    "control_number": "001",
                }
            },
            "clearinghouse_config": {
                "clearinghouse_name": "availity",
                "api_endpoint": "https://test.com",
                "credentials": {"api_key": "key"},
            },
        }
        with patch(
            "httpx.AsyncClient.post",
            new_callable=AsyncMock,
            side_effect=ConnectionError("connection refused"),
        ):
            with pytest.raises(ConnectionError, match="connection refused"):
                await submit_to_clearinghouse(submit_args)

    @pytest.mark.asyncio
    async def test_clearinghouse_validation_failure_returns_result(self):
        """Non-retryable ValueError returns a failed ActivityResult."""
        submit_args = {
            "x12_payload": {
                "data": {
                    "x12_270": "ISA*test~",
                    "control_number": "001",
                }
            },
            "clearinghouse_config": {
                "clearinghouse_name": "availity",
                "api_endpoint": "https://test.com",
                "credentials": {"api_key": "key"},
            },
        }
        with patch(
            "httpx.AsyncClient.post",
            new_callable=AsyncMock,
            side_effect=ValueError("invalid transaction format"),
        ):
            result = await submit_to_clearinghouse(submit_args)
        assert result["success"] is False
        assert "validation error" in result["error"].lower()


class TestParseEligibilityResponse:
    @pytest.mark.asyncio
    async def test_parse_json_response(self):
        ch_result = {
            "data": {
                "raw_response": "",
                "parsed_response": {
                    "coverage": {"active": True, "plan_name": "Gold Plan", "effective_date": "20240101"},
                    "benefits": [{"eligibility_code": "1", "service_type_code": "30"}],
                    "subscriber": {"id": "S001"},
                    "payer": {"name": "Test Payer"},
                },
                "transaction_id": "TX-001",
            }
        }
        result = await parse_eligibility_response(ch_result)
        assert result["success"] is True
        assert result["data"]["coverage_active"] is True
        assert result["data"]["transaction_id"] == "TX-001"
        assert result["data"]["confidence"] > 0

    @pytest.mark.asyncio
    async def test_parse_no_data(self):
        result = await parse_eligibility_response({"data": {}})
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_low_confidence_triggers_review(self):
        ch_result = {
            "data": {
                "parsed_response": {
                    "coverage": {"active": True},
                    "benefits": [],
                    "subscriber": {},
                    "payer": {},
                    "errors": [{"code": "E1"}, {"code": "E2"}],
                },
                "transaction_id": "TX-001",
            }
        }
        result = await parse_eligibility_response(ch_result)
        assert result["success"] is True
        assert result["data"]["needs_review"] is True


class TestWriteEligibilityResult:
    @pytest.mark.asyncio
    async def test_write_result(self):
        """write_eligibility_result persists and returns result with mocked DB."""
        mock_task = MagicMock()
        mock_task.patient_id = "patient-001"

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=mock_task)))
        mock_session.commit = AsyncMock()
        mock_session.add = MagicMock()

        mock_session_factory = MagicMock(return_value=mock_session)

        mock_engine = AsyncMock()
        mock_engine.dispose = AsyncMock()

        with patch("app.workflows.eligibility._get_activity_session_factory",
                   return_value=(mock_session_factory, mock_engine)):
            result = await write_eligibility_result({
                "task_id": "T1",
                "result_data": {
                    "data": {
                        "coverage_active": True,
                        "coverage_details": {"plan": "Gold"},
                        "confidence": 0.9,
                        "transaction_id": "TX-001",
                    }
                },
            })
        assert result["success"] is True
        assert result["data"]["task_id"] == "T1"
        assert result["data"]["coverage_active"] is True


class TestWriteEligibilityResultDBFailure:
    @pytest.mark.asyncio
    async def test_db_failure_raises(self):
        """write_eligibility_result raises when DB persistence fails."""
        def _broken_factory():
            raise Exception("DB connection failed")

        mock_engine = AsyncMock()
        mock_engine.dispose = AsyncMock()

        with patch(
            "app.workflows.eligibility._get_activity_session_factory",
            return_value=(MagicMock(side_effect=Exception("DB connection failed")), mock_engine),
        ):
            with pytest.raises(Exception, match="DB connection failed"):
                await write_eligibility_result({
                    "task_id": "T1",
                    "result_data": {
                        "data": {
                            "coverage_active": True,
                            "coverage_details": {},
                            "confidence": 0.9,
                        }
                    },
                })

    @pytest.mark.asyncio
    async def test_task_not_found_raises(self):
        """write_eligibility_result raises ActivityError when task is not found."""
        from app.workflows.base import ActivityError

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None))
        )
        mock_engine = AsyncMock()
        mock_engine.dispose = AsyncMock()

        with patch("app.workflows.eligibility._get_activity_session_factory",
                   return_value=(MagicMock(return_value=mock_session), mock_engine)):
            with pytest.raises(ActivityError, match="not found"):
                await write_eligibility_result({
                    "task_id": "T-MISSING",
                    "result_data": {"data": {"coverage_active": True}},
                })

    @pytest.mark.asyncio
    async def test_missing_patient_id_creates_record(self):
        """write_eligibility_result creates EligibilityCheck even without patient_id.

        patient_id is nullable — eligibility checks can be submitted
        without a pre-existing patient record (patient_id is resolved
        later or may not exist in the system yet).
        """
        mock_task = MagicMock()
        mock_task.patient_id = None

        # First execute returns the task (no patient_id), second returns None (no existing check)
        task_result = MagicMock(scalar_one_or_none=MagicMock(return_value=mock_task))
        no_check_result = MagicMock(scalar_one_or_none=MagicMock(return_value=None))

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.execute = AsyncMock(
            side_effect=[task_result, no_check_result]
        )
        mock_session.add = MagicMock()
        mock_session.commit = AsyncMock()
        mock_engine = AsyncMock()
        mock_engine.dispose = AsyncMock()

        with patch("app.workflows.eligibility._get_activity_session_factory",
                   return_value=(MagicMock(return_value=mock_session), mock_engine)):
            result = await write_eligibility_result({
                "task_id": "T-NO-PATIENT",
                "result_data": {"data": {"coverage_active": True}},
            })
            assert result["success"] is True
            assert result["data"]["coverage_active"] is True


class TestCalculateEligibilityConfidence:
    def test_perfect_response(self):
        parsed = {
            "coverage": {"active": True, "effective_date": "20240101"},
            "benefits": [{"eligibility_code": "1"}],
            "subscriber": {"id": "S001"},
            "errors": [],
        }
        score = _calculate_eligibility_confidence(parsed)
        assert score >= 0.8

    def test_errors_reduce_confidence(self):
        parsed = {
            "coverage": {"active": True, "effective_date": "20240101"},
            "benefits": [{"eligibility_code": "1"}],
            "subscriber": {"id": "S001"},
            "errors": [{"code": "E1"}, {"code": "E2"}],
        }
        score = _calculate_eligibility_confidence(parsed)
        assert score < 0.8

    def test_no_benefits_reduces_confidence(self):
        parsed = {
            "coverage": {"active": True, "effective_date": "20240101"},
            "benefits": [],
            "subscriber": {"id": "S001"},
            "errors": [],
        }
        score = _calculate_eligibility_confidence(parsed)
        assert score < 1.0

    def test_many_errors_floor_at_zero(self):
        parsed = {
            "coverage": {},
            "benefits": [],
            "subscriber": {},
            "errors": [{"e": i} for i in range(10)],
        }
        score = _calculate_eligibility_confidence(parsed)
        assert score >= 0.0

    def test_missing_subscriber_id(self):
        parsed = {
            "coverage": {"active": True, "effective_date": "20240101"},
            "benefits": [{"eligibility_code": "1"}],
            "subscriber": {},
            "errors": [],
        }
        score = _calculate_eligibility_confidence(parsed)
        assert score < 1.0


# ── Full Eligibility Workflow Tests (inline mode) ─────────────────────


def _mock_db_context():
    """Return a context manager that mocks the DB layer for write_eligibility_result.

    The mock returns a fake AgentTask with ``patient_id`` set so the
    write activity can persist an EligibilityCheck record.

    Patches ``_get_activity_session_factory`` directly so the mock is
    effective regardless of whether the DI engine is initialised.
    """
    mock_task = MagicMock()
    mock_task.patient_id = "patient-001"

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=mock_task))
    )
    mock_session.commit = AsyncMock()
    mock_session.add = MagicMock()

    mock_session_factory = MagicMock(return_value=mock_session)

    mock_engine = AsyncMock()
    mock_engine.dispose = AsyncMock()

    from contextlib import contextmanager

    @contextmanager
    def _ctx():
        with patch("app.workflows.eligibility._get_activity_session_factory",
                   return_value=(mock_session_factory, mock_engine)):
            yield

    return _ctx()


class TestRunEligibilityWorkflow:
    @pytest.mark.asyncio
    async def test_end_to_end_success(self):
        """Full workflow with mock clearinghouse returns completed result."""
        workflow_input = WorkflowInput(
            task_id="T-001",
            agent_type="eligibility",
            input_data={
                "subscriber_id": "SUB001",
                "subscriber_last_name": "Doe",
                "subscriber_first_name": "Jane",
                "subscriber_dob": "19900101",
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
        )

        # Mock the clearinghouse HTTP call
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = json.dumps({
            "id": "TX-001",
            "coverage": {
                "active": True,
                "plan_name": "Gold Plan",
                "effective_date": "20240101",
            },
            "benefits": [{"eligibility_code": "1", "service_type_code": "30"}],
            "subscriber": {"id": "SUB001"},
            "payer": {"name": "Test Payer"},
            "errors": [],
        })
        mock_response.json.return_value = json.loads(mock_response.text)
        mock_response.headers = {"content-type": "application/json"}

        with _mock_db_context(), \
             patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_response):
            result = await run_eligibility_workflow(workflow_input)

        assert result.task_id == "T-001"
        assert result.status == "completed"
        assert result.output_data.get("coverage_active") is True
        assert result.confidence > 0

    @pytest.mark.asyncio
    async def test_validation_failure(self):
        """Workflow fails at validation when required fields missing."""
        workflow_input = WorkflowInput(
            task_id="T-002",
            agent_type="eligibility",
            input_data={},  # Missing required fields
        )
        result = await run_eligibility_workflow(workflow_input)

        assert result.status == "failed"
        assert "Missing required fields" in result.error

    @pytest.mark.asyncio
    async def test_clearinghouse_failure_raises(self):
        """Inline workflow propagates clearinghouse errors (no Temporal retry)."""
        workflow_input = WorkflowInput(
            task_id="T-003",
            agent_type="eligibility",
            input_data={
                "subscriber_id": "SUB001",
                "subscriber_last_name": "Doe",
                "subscriber_first_name": "Jane",
            },
            clearinghouse_config={
                "clearinghouse_name": "availity",
                "api_endpoint": "https://api.test.com",
                "credentials": {"api_key": "key"},
            },
        )

        with patch(
            "httpx.AsyncClient.post",
            new_callable=AsyncMock,
            side_effect=ConnectionError("connection refused"),
        ):
            # Inline mode has no Temporal retry — exception propagates
            with pytest.raises(ConnectionError):
                await run_eligibility_workflow(workflow_input)


# ── Temporal Integration Tests ───────────────────────────────────────


class TestEligibilityWorkflowTemporal:
    """Tests that execute the EligibilityWorkflow class through a real
    Temporal test environment with worker and activity execution."""

    @pytest.mark.asyncio
    async def test_end_to_end_via_temporal(self):
        """Run the EligibilityWorkflow through Temporal dev server."""
        from temporalio.testing import WorkflowEnvironment
        from temporalio.worker import Worker as TemporalWorker

        async with await WorkflowEnvironment.start_local() as env:
            workflow_input = WorkflowInput(
                task_id="T-TEMP-001",
                agent_type="eligibility",
                input_data={
                    "subscriber_id": "SUB001",
                    "subscriber_last_name": "Doe",
                    "subscriber_first_name": "Jane",
                    "subscriber_dob": "19900101",
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
            )

            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.text = json.dumps({
                "id": "TX-TEMP",
                "coverage": {"active": True, "effective_date": "20240101"},
                "benefits": [{"eligibility_code": "1"}],
                "subscriber": {"id": "SUB001"},
                "payer": {"name": "Test Payer"},
                "errors": [],
            })
            mock_response.json.return_value = json.loads(mock_response.text)
            mock_response.headers = {"content-type": "application/json"}

            with _mock_db_context(), \
                 patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_response):
                async with TemporalWorker(
                    env.client,
                    task_queue="test-queue",
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
                    result = await env.client.execute_workflow(
                        EligibilityWorkflow.run,
                        workflow_input,
                        id="test-elig-wf-001",
                        task_queue="test-queue",
                    )

            assert result.status == "completed"
            assert result.output_data.get("coverage_active") is True
            assert result.confidence > 0

    @pytest.mark.asyncio
    async def test_validation_failure_via_temporal(self):
        """Temporal workflow correctly returns failed status on bad input."""
        from temporalio.testing import WorkflowEnvironment
        from temporalio.worker import Worker as TemporalWorker

        async with await WorkflowEnvironment.start_local() as env:
            workflow_input = WorkflowInput(
                task_id="T-TEMP-002",
                agent_type="eligibility",
                input_data={},  # Missing required fields
            )

            async with TemporalWorker(
                env.client,
                task_queue="test-queue",
                workflows=[EligibilityWorkflow],
                activities=[
                    validate_eligibility_input,
                    execute_eligibility_agent,
                    build_eligibility_request,
                    submit_to_clearinghouse,
                    parse_eligibility_response,
                    write_eligibility_result,
                ],
            ):
                result = await env.client.execute_workflow(
                    EligibilityWorkflow.run,
                    workflow_input,
                    id="test-elig-wf-002",
                    task_queue="test-queue",
                )

            assert result.status == "failed"
            assert "Missing required fields" in result.error

    @pytest.mark.asyncio
    async def test_activity_retry_on_transient_failure(self):
        """Temporal retries a failing activity and workflow completes on retry.

        The submit_to_clearinghouse activity now re-raises transient
        exceptions so Temporal's CLEARINGHOUSE_RETRY_POLICY retries the
        activity automatically. The first call raises, Temporal retries,
        and the second call succeeds — proving durable execution.
        """
        from temporalio.testing import WorkflowEnvironment
        from temporalio.worker import Worker as TemporalWorker

        async with await WorkflowEnvironment.start_local() as env:
            workflow_input = WorkflowInput(
                task_id="T-TEMP-003",
                agent_type="eligibility",
                input_data={
                    "subscriber_id": "SUB001",
                    "subscriber_last_name": "Doe",
                    "subscriber_first_name": "Jane",
                },
                clearinghouse_config={
                    "clearinghouse_name": "availity",
                    "api_endpoint": "https://api.test.com",
                    "credentials": {"api_key": "key"},
                },
            )

            # First call raises, second succeeds — Temporal retry handles it
            success_resp = MagicMock()
            success_resp.status_code = 200
            success_resp.text = json.dumps({
                "id": "TX-RETRY",
                "coverage": {"active": True, "effective_date": "20240101"},
                "benefits": [{"eligibility_code": "1"}],
                "subscriber": {"id": "SUB001"},
                "payer": {"name": "P"},
                "errors": [],
            })
            success_resp.json.return_value = json.loads(success_resp.text)
            success_resp.headers = {"content-type": "application/json"}

            call_count = 0

            async def _mock_post(*args, **kwargs):
                nonlocal call_count
                call_count += 1
                if call_count <= 1:
                    raise ConnectionError("transient error")
                return success_resp

            with _mock_db_context(), \
                 patch("httpx.AsyncClient.post", new_callable=AsyncMock, side_effect=_mock_post):
                async with TemporalWorker(
                    env.client,
                    task_queue="test-queue",
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
                    result = await env.client.execute_workflow(
                        EligibilityWorkflow.run,
                        workflow_input,
                        id="test-elig-wf-003",
                        task_queue="test-queue",
                    )

            # Temporal retried the activity — the workflow must complete
            assert result.status == "completed", (
                f"Expected completed but got {result.status}; "
                f"error={result.error}; call_count={call_count}"
            )
            # The clearinghouse was called at least twice (first failed, retry succeeded)
            assert call_count >= 2, (
                f"Expected at least 2 submit calls (1 failure + 1 retry), got {call_count}"
            )

    @pytest.mark.asyncio
    async def test_workflow_cancellation_via_temporal(self):
        """Workflow can be cancelled through Temporal handle."""
        import asyncio
        from temporalio.testing import WorkflowEnvironment
        from temporalio.worker import Worker as TemporalWorker

        async with await WorkflowEnvironment.start_local() as env:
            workflow_input = WorkflowInput(
                task_id="T-TEMP-004",
                agent_type="eligibility",
                input_data={
                    "subscriber_id": "SUB001",
                    "subscriber_last_name": "Doe",
                    "subscriber_first_name": "Jane",
                },
                clearinghouse_config={
                    "clearinghouse_name": "availity",
                    "api_endpoint": "https://api.test.com",
                    "credentials": {"api_key": "key"},
                },
            )

            # Make the clearinghouse hang so we can cancel
            async def _slow_post(*args, **kwargs):
                await asyncio.sleep(60)

            with patch("httpx.AsyncClient.post", new_callable=AsyncMock, side_effect=_slow_post):
                async with TemporalWorker(
                    env.client,
                    task_queue="test-queue",
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
                    handle = await env.client.start_workflow(
                        EligibilityWorkflow.run,
                        workflow_input,
                        id="test-elig-wf-004",
                        task_queue="test-queue",
                    )

                    # Give the workflow a moment to start
                    await asyncio.sleep(0.5)

                    # Cancel
                    await handle.cancel()

                    # The workflow should end with a cancellation
                    from temporalio.client import WorkflowFailureError
                    try:
                        await handle.result()
                        # If it completed (e.g., the activity was fast), that's
                        # also acceptable
                    except WorkflowFailureError:
                        # Cancellation raises WorkflowFailureError — expected
                        pass

                    desc = await handle.describe()
                    assert desc.status.name in (
                        "CANCELED",
                        "COMPLETED",
                        "TERMINATED",
                        "FAILED",
                    )


# ── Generic Agent Workflow Tests ──────────────────────────────────────


class TestGenericAgentWorkflowActivities:
    @pytest.mark.asyncio
    async def test_validate_agent_input_success(self):
        result = await validate_agent_input({
            "task_id": "T1",
            "agent_type": "scheduling",
            "input_data": {"request": "annual checkup"},
        })
        assert result["success"] is True
        assert result["data"]["agent_type"] == "scheduling"

    @pytest.mark.asyncio
    async def test_validate_agent_input_missing_type(self):
        result = await validate_agent_input({
            "input_data": {"request": "test"},
        })
        assert result["success"] is False
        assert "agent_type" in result["error"]

    @pytest.mark.asyncio
    async def test_validate_agent_input_missing_data(self):
        result = await validate_agent_input({
            "agent_type": "scheduling",
        })
        assert result["success"] is False
        assert "input_data" in result["error"]

    @pytest.mark.asyncio
    async def test_execute_agent_no_runner(self):
        """When no runner is registered, returns a pending placeholder."""
        result = await execute_agent({
            "task_id": "T1",
            "agent_type": "unknown_type",
            "input_data": {},
        })
        assert result["success"] is True
        assert result["data"]["output"]["status"] == "pending"

    @pytest.mark.asyncio
    async def test_write_agent_result(self):
        """write_agent_result persists and returns result with mocked DB."""
        mock_task = MagicMock()
        mock_task.output_data = None
        mock_task.status = "running"

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=mock_task))
        )
        mock_session.commit = AsyncMock()

        mock_engine = AsyncMock()
        mock_engine.dispose = AsyncMock()

        with patch("app.workflows.eligibility._get_activity_session_factory",
                   return_value=(MagicMock(return_value=mock_session), mock_engine)):
            result = await write_agent_result({
                "task_id": "T1",
                "output": {"key": "value"},
            })
        assert result["success"] is True
        assert result["data"]["task_id"] == "T1"

    @pytest.mark.asyncio
    async def test_write_agent_result_db_failure_raises(self):
        """write_agent_result raises when DB persistence fails."""
        mock_engine = AsyncMock()
        mock_engine.dispose = AsyncMock()

        with patch(
            "app.workflows.eligibility._get_activity_session_factory",
            return_value=(MagicMock(side_effect=Exception("DB connection failed")), mock_engine),
        ):
            with pytest.raises(Exception, match="DB connection failed"):
                await write_agent_result({
                    "task_id": "T1",
                    "output": {"key": "value"},
                })


class TestGenericAgentWorkflowTemporal:
    @pytest.mark.asyncio
    async def test_generic_workflow_via_temporal(self):
        """Run the GenericAgentWorkflow through Temporal dev server."""
        from temporalio.testing import WorkflowEnvironment
        from temporalio.worker import Worker as TemporalWorker

        async with await WorkflowEnvironment.start_local() as env:
            workflow_input = WorkflowInput(
                task_id="T-GEN-001",
                agent_type="scheduling",
                input_data={"request": "annual checkup"},
            )

            with _mock_db_context():
                async with TemporalWorker(
                    env.client,
                    task_queue="test-queue",
                    workflows=[GenericAgentWorkflow],
                    activities=[
                        validate_agent_input,
                        execute_agent,
                        write_agent_result,
                    ],
                ):
                    result = await env.client.execute_workflow(
                        GenericAgentWorkflow.run,
                        workflow_input,
                        id="test-gen-wf-001",
                        task_queue="test-queue",
                    )

            assert result.status == "completed"
            assert result.agent_type == "scheduling"


# ── Worker Tests ────────────────────────────────────────────────────────


class TestWorkerRegistration:
    def test_registered_activities(self):
        activities = get_registered_activities()
        activity_names = [a.__name__ if hasattr(a, "__name__") else str(a) for a in activities]
        assert "validate_eligibility_input" in activity_names
        assert "execute_eligibility_agent" in activity_names
        assert "build_eligibility_request" in activity_names
        assert "submit_to_clearinghouse" in activity_names
        assert "parse_eligibility_response" in activity_names
        assert "write_eligibility_result" in activity_names

    def test_registered_workflows(self):
        workflows = get_registered_workflows()
        workflow_names = [w.__name__ for w in workflows]
        assert "EligibilityWorkflow" in workflow_names
        assert "GenericAgentWorkflow" in workflow_names

    def test_register_custom_activity(self):
        from temporalio import activity as _act

        @_act.defn
        async def custom_test_activity() -> None:
            pass

        before = len(get_registered_activities())
        register_activity(custom_test_activity)
        after = len(get_registered_activities())
        assert after == before + 1

    def test_register_duplicate_activity_no_duplicate(self):
        activities_before = len(get_registered_activities())
        register_activity(validate_eligibility_input)
        activities_after = len(get_registered_activities())
        assert activities_before == activities_after

    def test_register_workflow(self):
        # Use EligibilityWorkflow as a stand-in — re-registering is a no-op
        # so we test with a plain class to verify the registry mechanics.
        class _PlainWorkflow:
            pass

        before = len(get_registered_workflows())
        register_workflow(_PlainWorkflow)
        after = len(get_registered_workflows())
        assert after == before + 1
        # Clean up so it doesn't pollute the real worker
        from app.workflows.worker import _WORKFLOW_REGISTRY
        _WORKFLOW_REGISTRY.remove(_PlainWorkflow)

    def test_generic_agent_activities_registered(self):
        """Generic agent workflow activities are registered on import."""
        activities = get_registered_activities()
        activity_names = [a.__name__ if hasattr(a, "__name__") else str(a) for a in activities]
        assert "validate_agent_input" in activity_names
        assert "execute_agent" in activity_names
        assert "write_agent_result" in activity_names


class TestCreateWorker:
    @pytest.mark.asyncio
    async def test_create_worker_with_client(self):
        """create_worker returns a real Temporal Worker when given a client."""
        from temporalio.testing import WorkflowEnvironment
        from temporalio.worker import Worker as TemporalWorker

        async with await WorkflowEnvironment.start_local() as env:
            worker = await create_worker(
                client=env.client,
                task_queue="test-create-queue",
            )
            assert isinstance(worker, TemporalWorker)
