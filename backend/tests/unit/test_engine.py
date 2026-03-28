"""Unit tests for the LangGraph agent engine components.

Covers: graph builder, LLM provider, tool executor, agent state,
and base agent class.
"""

from __future__ import annotations

import uuid

import pytest

from app.core.engine.state import (
    BaseAgentState,
    PatientContext,
    PayerContext,
    ToolCall,
    ToolResult,
    create_initial_state,
)
from app.core.engine.llm_provider import (
    LLMError,
    LLMProvider,
    LLMResponse,
    MockLLMBackend,
    TokenUsage,
)
from app.core.engine.tool_executor import (
    ToolDefinition,
    ToolExecutor,
)
from app.core.engine.graph_builder import (
    AgentGraph,
    GraphBuilder,
    default_ingest_node,
    default_audit_node,
    decide_router,
    execute_router,
)


# ── State Tests ─────────────────────────────────────────────────────


class TestAgentState:
    def test_create_initial_state(self):
        state = create_initial_state(
            task_id="task-1",
            agent_type="eligibility",
            input_data={"patient_id": "p1"},
        )
        assert state["task_id"] == "task-1"
        assert state["agent_type"] == "eligibility"
        assert state["confidence"] == 0.0
        assert state["needs_review"] is False
        assert state["error"] is None
        assert state["audit_trail"] == []
        assert state["iteration"] == 0
        assert state["max_iterations"] == 10

    def test_create_initial_state_with_patient_context(self):
        pc = PatientContext(patient_id="p1", first_name="Jane", last_name="Doe")
        state = create_initial_state(
            task_id="t1",
            agent_type="claims",
            patient_context=pc,
        )
        assert state["patient_context"]["patient_id"] == "p1"
        assert state["patient_context"]["first_name"] == "Jane"

    def test_create_initial_state_with_custom_max_iterations(self):
        state = create_initial_state(
            task_id="t1", agent_type="claims", max_iterations=5
        )
        assert state["max_iterations"] == 5


# ── LLM Provider Tests ─────────────────────────────────────────────


class TestMockLLMBackend:
    async def test_mock_returns_response(self):
        backend = MockLLMBackend(responses=["Hello from mock"])
        resp = await backend.invoke([{"role": "user", "content": "Hi"}])
        assert resp.content == "Hello from mock"
        assert resp.model == "mock-model"
        assert resp.stop_reason == "end_turn"

    async def test_mock_records_call_history(self):
        backend = MockLLMBackend(responses=["R1", "R2"])
        await backend.invoke([{"role": "user", "content": "First"}])
        await backend.invoke([{"role": "user", "content": "Second"}])
        assert len(backend.call_history) == 2
        assert backend.call_history[0]["messages"][0]["content"] == "First"

    async def test_mock_cycles_last_response(self):
        backend = MockLLMBackend(responses=["Only one"])
        r1 = await backend.invoke([{"role": "user", "content": "1"}])
        r2 = await backend.invoke([{"role": "user", "content": "2"}])
        assert r1.content == "Only one"
        assert r2.content == "Only one"


class TestLLMProvider:
    async def test_send_with_mock_backend(self):
        backend = MockLLMBackend(responses=["Test response"])
        provider = LLMProvider(primary=backend, phi_safe=False)
        resp = await provider.send(
            [{"role": "user", "content": "Hello"}],
        )
        assert resp.content == "Test response"
        assert provider.token_usage.total_calls == 1

    async def test_phi_deidentification(self):
        backend = MockLLMBackend(responses=["Safe response"])
        provider = LLMProvider(
            primary=backend,
            phi_safe=True,
            additional_names=["John Smith"],
        )
        await provider.send(
            [{"role": "user", "content": "Patient John Smith, SSN 123-45-6789"}],
        )
        # Verify the message sent to the backend was de-identified
        sent_content = backend.call_history[0]["messages"][0]["content"]
        assert "John Smith" not in sent_content
        assert "123-45-6789" not in sent_content

    async def test_phi_safe_system_prompt(self):
        backend = MockLLMBackend(responses=["OK"])
        provider = LLMProvider(primary=backend, phi_safe=True)
        await provider.send(
            [{"role": "user", "content": "Hi"}],
            system_prompt="Patient SSN is 123-45-6789",
        )
        sent_system = backend.call_history[0]["system_prompt"]
        assert "123-45-6789" not in sent_system

    async def test_fallback_on_primary_failure(self):
        class FailingBackend:
            async def invoke(self, messages, **kwargs):
                raise LLMError("Primary down")

        fallback = MockLLMBackend(responses=["Fallback response"])
        provider = LLMProvider(
            primary=FailingBackend(), fallback=fallback, phi_safe=False
        )
        resp = await provider.send([{"role": "user", "content": "test"}])
        assert resp.content == "Fallback response"

    async def test_error_without_fallback(self):
        class FailingBackend:
            async def invoke(self, messages, **kwargs):
                raise LLMError("Primary down")

        provider = LLMProvider(primary=FailingBackend(), phi_safe=False)
        with pytest.raises(LLMError, match="Primary down"):
            await provider.send([{"role": "user", "content": "test"}])

    async def test_token_tracking(self):
        backend = MockLLMBackend(responses=["R1", "R2"])
        provider = LLMProvider(primary=backend, phi_safe=False)
        await provider.send([{"role": "user", "content": "msg1"}])
        await provider.send([{"role": "user", "content": "msg2"}])
        assert provider.token_usage.total_calls == 2
        assert provider.token_usage.total_tokens > 0


# ── Bedrock Backend Tests ──────────────────────────────────────────


class TestBedrockBackend:
    """Unit tests for BedrockBackend with monkeypatched boto3."""

    def _make_mock_boto3_response(self, body_dict: dict):
        """Create a mock boto3 invoke_model response."""
        import io
        import json

        class MockStreamBody:
            def __init__(self, data):
                self._data = data
            def read(self):
                return self._data

        return {
            "body": MockStreamBody(json.dumps(body_dict).encode()),
        }

    async def test_bedrock_invoke_parses_response(self, monkeypatch):
        """Verify BedrockBackend correctly parses a normal Bedrock response."""
        from app.core.engine.llm_provider import BedrockBackend

        response_payload = {
            "content": [{"type": "text", "text": "Hello from Claude"}],
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "stop_reason": "end_turn",
        }

        mock_response = self._make_mock_boto3_response(response_payload)
        call_args = []

        class MockClient:
            def invoke_model(self, **kwargs):
                call_args.append(kwargs)
                return mock_response

        backend = BedrockBackend(model_id="anthropic.claude-test", region="us-east-1")
        backend._client = MockClient()

        resp = await backend.invoke(
            [{"role": "user", "content": "Hello"}],
            system_prompt="You are helpful.",
            max_tokens=1024,
        )

        assert resp.content == "Hello from Claude"
        assert resp.model == "anthropic.claude-test"
        assert resp.input_tokens == 10
        assert resp.output_tokens == 5
        assert resp.stop_reason == "end_turn"
        assert resp.latency_ms > 0

        # Verify the request was correctly formed
        assert len(call_args) == 1
        import json
        body = json.loads(call_args[0]["body"])
        assert body["system"] == "You are helpful."
        assert body["max_tokens"] == 1024
        assert body["messages"] == [{"role": "user", "content": "Hello"}]

    async def test_bedrock_retry_on_throttling(self, monkeypatch):
        """Verify retry logic on throttling errors."""
        from app.core.engine.llm_provider import BedrockBackend, LLMRateLimitError

        response_payload = {
            "content": [{"type": "text", "text": "Success after retry"}],
            "usage": {"input_tokens": 5, "output_tokens": 3},
            "stop_reason": "end_turn",
        }
        mock_response = self._make_mock_boto3_response(response_payload)

        call_count = 0

        class MockClient:
            def invoke_model(self, **kwargs):
                nonlocal call_count
                call_count += 1
                if call_count <= 1:
                    raise Exception("ThrottlingException: Rate exceeded")
                return mock_response

        backend = BedrockBackend(
            model_id="anthropic.claude-test",
            max_retries=3,
        )
        backend._client = MockClient()

        # Monkeypatch asyncio.sleep to avoid actual waiting
        import asyncio
        sleep_calls = []
        original_sleep = asyncio.sleep
        async def mock_sleep(seconds):
            sleep_calls.append(seconds)
        monkeypatch.setattr(asyncio, "sleep", mock_sleep)

        resp = await backend.invoke(
            [{"role": "user", "content": "test"}],
        )

        assert resp.content == "Success after retry"
        assert call_count == 2
        assert len(sleep_calls) == 1  # One retry sleep

    async def test_bedrock_exhausted_retries_raises_rate_limit(self, monkeypatch):
        """Verify LLMRateLimitError after all retries exhausted."""
        from app.core.engine.llm_provider import BedrockBackend, LLMRateLimitError

        class MockClient:
            def invoke_model(self, **kwargs):
                raise Exception("ThrottlingException: Rate exceeded")

        backend = BedrockBackend(
            model_id="anthropic.claude-test",
            max_retries=2,
        )
        backend._client = MockClient()

        import asyncio
        async def mock_sleep(seconds):
            pass
        monkeypatch.setattr(asyncio, "sleep", mock_sleep)

        with pytest.raises(LLMRateLimitError, match="Rate limited"):
            await backend.invoke(
                [{"role": "user", "content": "test"}],
            )

    async def test_bedrock_non_throttling_error_raises_immediately(self):
        """Non-throttling errors should not trigger retries."""
        from app.core.engine.llm_provider import BedrockBackend, LLMError

        call_count = 0

        class MockClient:
            def invoke_model(self, **kwargs):
                nonlocal call_count
                call_count += 1
                raise Exception("ValidationException: Invalid input")

        backend = BedrockBackend(
            model_id="anthropic.claude-test",
            max_retries=3,
        )
        backend._client = MockClient()

        with pytest.raises(LLMError, match="Bedrock invocation failed"):
            await backend.invoke(
                [{"role": "user", "content": "test"}],
            )

        # Should fail immediately without retries
        assert call_count == 1

    async def test_bedrock_fallback_on_primary_failure(self, monkeypatch):
        """Verify LLMProvider falls back to secondary backend when Bedrock fails."""
        from app.core.engine.llm_provider import BedrockBackend, LLMError

        class MockClient:
            def invoke_model(self, **kwargs):
                raise Exception("ServiceUnavailableException: Service down")

        primary = BedrockBackend(model_id="anthropic.claude-test", max_retries=1)
        primary._client = MockClient()
        fallback = MockLLMBackend(responses=["Fallback response"])

        provider = LLMProvider(
            primary=primary, fallback=fallback, phi_safe=False
        )
        resp = await provider.send([{"role": "user", "content": "test"}])
        assert resp.content == "Fallback response"

    async def test_bedrock_empty_content_response(self):
        """Verify handling of empty content in Bedrock response."""
        from app.core.engine.llm_provider import BedrockBackend

        response_payload = {
            "content": [],
            "usage": {"input_tokens": 5, "output_tokens": 0},
            "stop_reason": "end_turn",
        }
        mock_response = self._make_mock_boto3_response(response_payload)

        class MockClient:
            def invoke_model(self, **kwargs):
                return mock_response

        backend = BedrockBackend(model_id="anthropic.claude-test")
        backend._client = MockClient()

        resp = await backend.invoke(
            [{"role": "user", "content": "test"}],
        )
        assert resp.content == ""
        assert resp.output_tokens == 0


# ── LLM Decision Parsing Tests ───────────────────────────────────


class TestParseLLMDecision:
    """Tests for the structured LLM output parser."""

    def test_parse_valid_json(self):
        from app.core.engine.graph_builder import parse_llm_decision

        content = '{"confidence": 0.85, "decision": {"action": "approve"}, "tool_calls": []}'
        result = parse_llm_decision(content)
        assert result["confidence"] == 0.85
        assert result["decision"] == {"action": "approve"}
        assert result["tool_calls"] == []

    def test_parse_json_with_tool_calls(self):
        from app.core.engine.graph_builder import parse_llm_decision

        content = '{"confidence": 0.7, "decision": {}, "tool_calls": [{"tool_name": "lookup", "parameters": {"id": "p1"}}]}'
        result = parse_llm_decision(content)
        assert len(result["tool_calls"]) == 1
        assert result["tool_calls"][0]["tool_name"] == "lookup"

    def test_parse_json_in_markdown_fence(self):
        from app.core.engine.graph_builder import parse_llm_decision

        content = 'Here is my analysis:\n```json\n{"confidence": 0.9, "decision": {"ok": true}, "tool_calls": []}\n```'
        result = parse_llm_decision(content)
        assert result["confidence"] == 0.9

    def test_parse_embedded_json_in_text(self):
        from app.core.engine.graph_builder import parse_llm_decision

        content = 'After analysis, I believe: {"confidence": 0.6, "decision": {"status": "partial"}} and more text.'
        result = parse_llm_decision(content)
        assert result["confidence"] == 0.6

    def test_parse_no_json_fallback(self):
        from app.core.engine.graph_builder import parse_llm_decision

        content = "I analyzed the case and believe the patient is eligible."
        result = parse_llm_decision(content)
        assert result["confidence"] == 0.0
        assert "raw_response" in result["decision"]
        assert result["tool_calls"] == []

    def test_parse_clamps_confidence(self):
        from app.core.engine.graph_builder import parse_llm_decision

        content = '{"confidence": 1.5, "decision": {}, "tool_calls": []}'
        result = parse_llm_decision(content)
        assert result["confidence"] == 1.0

        content = '{"confidence": -0.3, "decision": {}, "tool_calls": []}'
        result = parse_llm_decision(content)
        assert result["confidence"] == 0.0

    def test_parse_malformed_confidence(self):
        from app.core.engine.graph_builder import parse_llm_decision

        content = '{"confidence": "high", "decision": {}, "tool_calls": []}'
        result = parse_llm_decision(content)
        assert result["confidence"] == 0.0

    def test_parse_missing_fields_uses_defaults(self):
        from app.core.engine.graph_builder import parse_llm_decision

        content = '{"confidence": 0.8}'
        result = parse_llm_decision(content)
        assert result["confidence"] == 0.8
        assert result["decision"] == {}
        assert result["tool_calls"] == []


# ── Tool Executor Tests ─────────────────────────────────────────────


class TestToolExecutor:
    def _make_executor_with_tools(self) -> ToolExecutor:
        executor = ToolExecutor()

        async def lookup_patient(patient_id: str, **kwargs) -> dict:
            return {"patient_id": patient_id, "name": "Jane Doe"}

        async def check_coverage(member_id: str, payer_id: str, **kwargs) -> dict:
            return {"active": True, "member_id": member_id}

        async def submit_claim(claim_data: dict, **kwargs) -> dict:
            return {"claim_id": "CLM-001", "status": "submitted"}

        executor.register(
            "lookup_patient",
            description="Look up patient by ID",
            parameters={
                "patient_id": {"type": "string", "description": "Patient UUID"},
            },
            required_params=["patient_id"],
            handler=lookup_patient,
        )
        executor.register(
            "check_coverage",
            description="Check insurance coverage",
            parameters={
                "member_id": {"type": "string", "description": "Member ID"},
                "payer_id": {"type": "string", "description": "Payer ID"},
            },
            required_params=["member_id", "payer_id"],
            handler=check_coverage,
        )
        executor.register(
            "submit_claim",
            description="Submit a claim",
            parameters={
                "claim_data": {"type": "object", "description": "Claim data"},
            },
            required_params=["claim_data"],
            handler=submit_claim,
        )
        return executor

    def test_available_tools(self):
        executor = self._make_executor_with_tools()
        assert set(executor.available_tools) == {
            "lookup_patient",
            "check_coverage",
            "submit_claim",
        }

    def test_get_tool_schemas(self):
        executor = self._make_executor_with_tools()
        schemas = executor.get_tool_schemas()
        assert len(schemas) == 3
        names = {s["name"] for s in schemas}
        assert "lookup_patient" in names

    def test_validate_valid_call(self):
        executor = self._make_executor_with_tools()
        errors = executor.validate_tool_call(
            ToolCall(tool_name="lookup_patient", parameters={"patient_id": "p1"})
        )
        assert errors == []

    def test_validate_missing_required_param(self):
        executor = self._make_executor_with_tools()
        errors = executor.validate_tool_call(
            ToolCall(tool_name="check_coverage", parameters={"member_id": "m1"})
        )
        assert any("payer_id" in e for e in errors)

    def test_validate_unknown_tool(self):
        executor = self._make_executor_with_tools()
        errors = executor.validate_tool_call(
            ToolCall(tool_name="nonexistent", parameters={})
        )
        assert any("Unknown tool" in e for e in errors)

    def test_validate_wrong_type(self):
        executor = self._make_executor_with_tools()
        errors = executor.validate_tool_call(
            ToolCall(tool_name="lookup_patient", parameters={"patient_id": 123})
        )
        assert any("type" in e.lower() for e in errors)

    def test_validate_empty_tool_name(self):
        executor = self._make_executor_with_tools()
        errors = executor.validate_tool_call(ToolCall(tool_name="", parameters={}))
        assert any("required" in e.lower() for e in errors)

    async def test_execute_valid_tool(self):
        executor = self._make_executor_with_tools()
        result = await executor.execute(
            ToolCall(tool_name="lookup_patient", parameters={"patient_id": "p1"})
        )
        assert result["success"] is True
        assert result["result"]["patient_id"] == "p1"

    async def test_execute_invalid_tool_returns_error(self):
        executor = self._make_executor_with_tools()
        result = await executor.execute(
            ToolCall(tool_name="nonexistent", parameters={})
        )
        assert result["success"] is False
        assert "Unknown tool" in result["error"]

    async def test_execute_missing_params_returns_error(self):
        executor = self._make_executor_with_tools()
        result = await executor.execute(
            ToolCall(tool_name="check_coverage", parameters={"member_id": "m1"})
        )
        assert result["success"] is False
        assert "payer_id" in result["error"]

    async def test_execute_handler_exception(self):
        executor = ToolExecutor()

        async def failing_tool(**kwargs):
            raise ValueError("Something went wrong")

        executor.register(
            "failing",
            handler=failing_tool,
            required_params=[],
        )
        result = await executor.execute(
            ToolCall(tool_name="failing", parameters={})
        )
        assert result["success"] is False
        assert "Something went wrong" in result["error"]

    async def test_execute_many(self):
        executor = self._make_executor_with_tools()
        results = await executor.execute_many(
            [
                ToolCall(tool_name="lookup_patient", parameters={"patient_id": "p1"}),
                ToolCall(tool_name="check_coverage", parameters={"member_id": "m1", "payer_id": "pay1"}),
                ToolCall(tool_name="nonexistent", parameters={}),
            ]
        )
        assert len(results) == 3
        assert results[0]["success"] is True
        assert results[1]["success"] is True
        assert results[2]["success"] is False


# ── Graph Builder Tests ─────────────────────────────────────────────


class TestGraphBuilder:
    def test_build_graph_has_all_standard_nodes(self):
        builder = GraphBuilder()
        graph = builder.build()
        assert "ingest" in graph.node_names
        assert "reason" in graph.node_names
        assert "decide" in graph.node_names
        assert "execute" in graph.node_names
        assert "audit" in graph.node_names

    async def test_run_graph_with_mock_llm(self):
        # LLM returns structured JSON with high confidence
        llm_response = '{"confidence": 0.92, "decision": {"action": "approve"}, "tool_calls": []}'
        backend = MockLLMBackend(responses=[llm_response])
        provider = LLMProvider(primary=backend, phi_safe=False)

        builder = GraphBuilder(
            llm_provider=provider,
            confidence_threshold=0.7,
        )
        graph = builder.build()

        state = create_initial_state(
            task_id="t1",
            agent_type="eligibility",
            input_data={"patient_id": "p1"},
        )

        result = await graph.run(state)
        assert result["current_node"] == "audit"
        assert len(result["audit_trail"]) > 0
        # Confidence is now derived from LLM output
        assert result["confidence"] == 0.92
        assert result["needs_review"] is False

    async def test_run_graph_low_confidence_triggers_review(self):
        # LLM returns structured JSON with low confidence
        llm_response = '{"confidence": 0.4, "decision": {"status": "uncertain"}, "tool_calls": []}'
        backend = MockLLMBackend(responses=[llm_response])
        provider = LLMProvider(primary=backend, phi_safe=False)

        builder = GraphBuilder(
            llm_provider=provider,
            confidence_threshold=0.7,
        )
        graph = builder.build()

        state = create_initial_state(
            task_id="t1",
            agent_type="claims",
        )

        result = await graph.run(state)
        assert result["needs_review"] is True
        assert result["confidence"] == 0.4
        assert "below threshold" in result["review_reason"].lower()

    async def test_run_graph_without_llm(self):
        builder = GraphBuilder(confidence_threshold=0.5)
        graph = builder.build()

        state = create_initial_state(
            task_id="t1",
            agent_type="eligibility",
        )
        state["confidence"] = 0.8

        result = await graph.run(state)
        assert result["needs_review"] is False
        assert len(result["audit_trail"]) > 0

    async def test_custom_node_override(self):
        async def custom_ingest(state: dict) -> dict:
            state["current_node"] = "ingest"
            state["input_data"]["custom_field"] = "injected"
            return state

        builder = GraphBuilder()
        builder.set_node("ingest", custom_ingest)
        graph = builder.build()

        state = create_initial_state(
            task_id="t1",
            agent_type="eligibility",
        )
        state["confidence"] = 0.9

        result = await graph.run(state)
        assert result["input_data"].get("custom_field") == "injected"

    async def test_default_ingest_node(self):
        state = create_initial_state(
            task_id="t1",
            agent_type="eligibility",
            input_data={"key": "value"},
        )
        result = await default_ingest_node(state)
        assert result["current_node"] == "ingest"
        assert len(result["audit_trail"]) == 1
        assert result["audit_trail"][0]["action"] == "ingest_started"

    async def test_default_audit_node(self):
        state = create_initial_state(
            task_id="t1",
            agent_type="eligibility",
        )
        state["confidence"] = 0.85
        result = await default_audit_node(state)
        assert result["current_node"] == "audit"
        last_entry = result["audit_trail"][-1]
        assert last_entry["action"] == "execution_completed"
        assert last_entry["details"]["confidence"] == 0.85

    def test_decide_router_needs_review(self):
        state = create_initial_state(task_id="t1", agent_type="test")
        state["needs_review"] = True
        assert decide_router(state) == "audit"

    def test_decide_router_no_review(self):
        state = create_initial_state(task_id="t1", agent_type="test")
        state["needs_review"] = False
        assert decide_router(state) == "execute"

    def test_execute_router_to_audit(self):
        state = create_initial_state(task_id="t1", agent_type="test")
        state["tool_results"] = [
            ToolResult(tool_name="t1", success=True, result="ok", error=None)
        ]
        assert execute_router(state) == "audit"

    def test_execute_router_retry_on_failure(self):
        state = create_initial_state(task_id="t1", agent_type="test")
        state["iteration"] = 1
        state["max_iterations"] = 10
        state["tool_results"] = [
            ToolResult(tool_name="t1", success=False, result=None, error="fail")
        ]
        assert execute_router(state) == "reason"


# ── Base Agent Tests ────────────────────────────────────────────────


class TestBaseAgent:
    """Test a concrete subclass of BaseAgent."""

    def _make_test_agent(self, responses=None):
        from app.agents.base import BaseAgent

        default_response = (
            '{"confidence": 0.85, "decision": {"action": "complete"}, "tool_calls": []}'
        )

        class TestAgent(BaseAgent):
            agent_type = "eligibility"
            confidence_threshold = 0.7

            def get_tools(self) -> list[ToolDefinition]:
                async def dummy_tool(query: str = "", **kwargs) -> dict:
                    return {"result": f"looked up: {query}"}

                return [
                    ToolDefinition(
                        name="dummy_lookup",
                        description="Dummy lookup tool",
                        parameters={"query": {"type": "string"}},
                        required_params=["query"],
                        handler=dummy_tool,
                    )
                ]

            def build_graph(self) -> AgentGraph:
                return self._build_default_graph()

        backend = MockLLMBackend(responses=responses or [default_response])
        provider = LLMProvider(primary=backend, phi_safe=False)
        return TestAgent(llm_provider=provider)

    async def test_agent_runs_to_completion(self):
        agent = self._make_test_agent()
        state = await agent.run(
            task_id="task-1",
            input_data={"patient_id": "p1"},
        )
        # Should have completed the full graph cycle
        assert len(state["audit_trail"]) > 0
        assert state["agent_type"] == "eligibility"

    async def test_agent_with_high_confidence(self):
        # LLM returns high-confidence structured output
        from app.agents.base import BaseAgent

        class ConfidentAgent(BaseAgent):
            agent_type = "scheduling"
            confidence_threshold = 0.7

            def get_tools(self) -> list[ToolDefinition]:
                return []

            def build_graph(self) -> AgentGraph:
                return self._build_default_graph()

        llm_response = '{"confidence": 0.95, "decision": {"action": "approved"}, "tool_calls": []}'
        backend = MockLLMBackend(responses=[llm_response])
        provider = LLMProvider(primary=backend, phi_safe=False)
        agent = ConfidentAgent(llm_provider=provider)
        state = await agent.run(task_id="t2", input_data={})
        assert state["needs_review"] is False
        assert state["confidence"] == 0.95

    async def test_agent_with_low_confidence_needs_review(self):
        from app.agents.base import BaseAgent

        class UncertainAgent(BaseAgent):
            agent_type = "claims"
            confidence_threshold = 0.7

            def get_tools(self) -> list[ToolDefinition]:
                return []

            def build_graph(self) -> AgentGraph:
                return self._build_default_graph()

        llm_response = '{"confidence": 0.3, "decision": {"status": "uncertain"}, "tool_calls": []}'
        backend = MockLLMBackend(responses=[llm_response])
        provider = LLMProvider(primary=backend, phi_safe=False)
        agent = UncertainAgent(llm_provider=provider)
        state = await agent.run(task_id="t3", input_data={})
        assert state["needs_review"] is True
        assert state["confidence"] == 0.3

    async def test_agent_tool_registration(self):
        agent = self._make_test_agent()
        # The tool executor should have the agent's tools registered
        assert "dummy_lookup" in agent._tool_executor.available_tools

    async def test_low_confidence_agent_creates_hitl_review(self, db_session):
        """When a base agent runs with low confidence and a DB session,
        _evaluate_escalation() should create a HITLReview record."""
        from app.agents.base import BaseAgent
        from app.models.agent_task import AgentTask
        from app.models.hitl_review import HITLReview
        from app.models.organization import Organization
        from sqlalchemy import select

        class LowConfAgent(BaseAgent):
            agent_type = "eligibility"
            confidence_threshold = 0.7

            def get_tools(self):
                return []

            def build_graph(self):
                return self._build_default_graph()

        # Create DB fixtures
        org = Organization(name="Test Org")
        db_session.add(org)
        await db_session.flush()

        task = AgentTask(
            agent_type="eligibility",
            status="running",
            organization_id=org.id,
            input_data={"test": True},
        )
        db_session.add(task)
        await db_session.flush()

        llm_response = '{"confidence": 0.3, "decision": {"status": "uncertain"}, "tool_calls": []}'
        backend = MockLLMBackend(responses=[llm_response])
        provider = LLMProvider(primary=backend, phi_safe=False)
        agent = LowConfAgent(llm_provider=provider, session=db_session)

        state = await agent.run(task_id=str(task.id), input_data={})

        assert state["needs_review"] is True
        assert state["confidence"] == 0.3

        # Verify a HITLReview was created in the DB
        stmt = select(HITLReview).where(HITLReview.task_id == task.id)
        result = await db_session.execute(stmt)
        review = result.scalar_one_or_none()
        assert review is not None
        assert review.status == "pending"
        assert review.confidence_score == 0.3

        # Verify the task status was updated to 'review'
        await db_session.refresh(task)
        assert task.status == "review"

    async def test_high_confidence_agent_no_hitl_review(self, db_session):
        """When a base agent runs with high confidence, no HITLReview is created."""
        from app.agents.base import BaseAgent
        from app.models.agent_task import AgentTask
        from app.models.hitl_review import HITLReview
        from app.models.organization import Organization
        from sqlalchemy import select

        class HighConfAgent(BaseAgent):
            agent_type = "eligibility"
            confidence_threshold = 0.7

            def get_tools(self):
                return []

            def build_graph(self):
                return self._build_default_graph()

        org = Organization(name="Test Org")
        db_session.add(org)
        await db_session.flush()

        task = AgentTask(
            agent_type="eligibility",
            status="running",
            organization_id=org.id,
            input_data={"test": True},
        )
        db_session.add(task)
        await db_session.flush()

        llm_response = '{"confidence": 0.95, "decision": {"action": "approved"}, "tool_calls": []}'
        backend = MockLLMBackend(responses=[llm_response])
        provider = LLMProvider(primary=backend, phi_safe=False)
        agent = HighConfAgent(llm_provider=provider, session=db_session)

        state = await agent.run(task_id=str(task.id), input_data={})

        assert state["needs_review"] is False

        # Verify NO HITLReview was created
        stmt = select(HITLReview).where(HITLReview.task_id == task.id)
        result = await db_session.execute(stmt)
        review = result.scalar_one_or_none()
        assert review is None

        # Task status should NOT be 'review'
        await db_session.refresh(task)
        assert task.status == "running"  # unchanged


# ── Bedrock Timeout Tests ─────────────────────────────────────────────


class TestBedrockTimeout:
    async def test_timeout_raises_llm_timeout_error(self):
        """BedrockBackend raises LLMTimeoutError when call exceeds timeout."""
        import asyncio
        from unittest.mock import MagicMock, patch
        from app.core.engine.llm_provider import (
            BedrockBackend,
            LLMTimeoutError,
        )

        backend = BedrockBackend(timeout=0.01, max_retries=1)

        # Mock the boto3 client to simulate a slow call
        mock_client = MagicMock()

        def slow_invoke(**kwargs):
            import time
            time.sleep(1)  # Much longer than 0.01s timeout
            return {"body": MagicMock(read=lambda: b'{}')}

        mock_client.invoke_model = slow_invoke
        backend._client = mock_client

        with pytest.raises(LLMTimeoutError, match="timed out"):
            await backend.invoke(
                [{"role": "user", "content": "test"}],
                max_tokens=10,
            )

    async def test_timeout_retries_then_raises(self):
        """BedrockBackend retries on timeout before finally raising."""
        import asyncio
        from unittest.mock import MagicMock
        from app.core.engine.llm_provider import (
            BedrockBackend,
            LLMTimeoutError,
        )

        backend = BedrockBackend(timeout=0.01, max_retries=2)

        mock_client = MagicMock()
        call_count = 0

        def slow_invoke(**kwargs):
            nonlocal call_count
            call_count += 1
            import time
            time.sleep(1)
            return {"body": MagicMock(read=lambda: b'{}')}

        mock_client.invoke_model = slow_invoke
        backend._client = mock_client

        with pytest.raises(LLMTimeoutError):
            await backend.invoke(
                [{"role": "user", "content": "test"}],
                max_tokens=10,
            )


class TestBedrockTransientRetry:
    """Tests for expanded transient error retry in BedrockBackend."""

    def _make_mock_boto3_response(self, body_dict: dict):
        import io
        import json

        class MockStreamBody:
            def __init__(self, data):
                self._data = data
            def read(self):
                return self._data

        return {
            "body": MockStreamBody(json.dumps(body_dict).encode()),
        }

    async def test_retries_on_service_unavailable(self, monkeypatch):
        """ServiceUnavailableException should be retried, not fail immediately."""
        from app.core.engine.llm_provider import BedrockBackend, LLMError

        response_payload = {
            "content": [{"type": "text", "text": "Recovered"}],
            "usage": {"input_tokens": 5, "output_tokens": 3},
            "stop_reason": "end_turn",
        }
        mock_response = self._make_mock_boto3_response(response_payload)
        call_count = 0

        class MockClient:
            def invoke_model(self, **kwargs):
                nonlocal call_count
                call_count += 1
                if call_count <= 1:
                    raise Exception("ServiceUnavailableException: Service is temporarily unavailable")
                return mock_response

        backend = BedrockBackend(model_id="anthropic.claude-test", max_retries=3)
        backend._client = MockClient()

        import asyncio
        sleep_calls = []
        async def mock_sleep(seconds):
            sleep_calls.append(seconds)
        monkeypatch.setattr(asyncio, "sleep", mock_sleep)

        resp = await backend.invoke([{"role": "user", "content": "test"}])
        assert resp.content == "Recovered"
        assert call_count == 2
        assert len(sleep_calls) == 1

    async def test_retries_on_internal_server_error(self, monkeypatch):
        """InternalServerError should be retried."""
        from app.core.engine.llm_provider import BedrockBackend, LLMError

        response_payload = {
            "content": [{"type": "text", "text": "OK"}],
            "usage": {"input_tokens": 5, "output_tokens": 2},
            "stop_reason": "end_turn",
        }
        mock_response = self._make_mock_boto3_response(response_payload)
        call_count = 0

        class MockClient:
            def invoke_model(self, **kwargs):
                nonlocal call_count
                call_count += 1
                if call_count <= 2:
                    raise Exception("InternalServerError: 500")
                return mock_response

        backend = BedrockBackend(model_id="anthropic.claude-test", max_retries=3)
        backend._client = MockClient()

        import asyncio
        async def mock_sleep(seconds):
            pass
        monkeypatch.setattr(asyncio, "sleep", mock_sleep)

        resp = await backend.invoke([{"role": "user", "content": "test"}])
        assert resp.content == "OK"
        assert call_count == 3

    async def test_retries_on_connection_error(self, monkeypatch):
        """ConnectionError (OSError subclass) should be retried."""
        from app.core.engine.llm_provider import BedrockBackend, LLMError

        response_payload = {
            "content": [{"type": "text", "text": "Connected"}],
            "usage": {"input_tokens": 5, "output_tokens": 2},
            "stop_reason": "end_turn",
        }
        mock_response = self._make_mock_boto3_response(response_payload)
        call_count = 0

        class MockClient:
            def invoke_model(self, **kwargs):
                nonlocal call_count
                call_count += 1
                if call_count <= 1:
                    raise ConnectionError("Could not connect to endpoint")
                return mock_response

        backend = BedrockBackend(model_id="anthropic.claude-test", max_retries=3)
        backend._client = MockClient()

        import asyncio
        async def mock_sleep(seconds):
            pass
        monkeypatch.setattr(asyncio, "sleep", mock_sleep)

        resp = await backend.invoke([{"role": "user", "content": "test"}])
        assert resp.content == "Connected"
        assert call_count == 2

    async def test_transient_exhaustion_raises_llm_error(self, monkeypatch):
        """Transient errors that exhaust retries should raise LLMError."""
        from app.core.engine.llm_provider import BedrockBackend, LLMError

        class MockClient:
            def invoke_model(self, **kwargs):
                raise Exception("ServiceUnavailableException: still down")

        backend = BedrockBackend(model_id="anthropic.claude-test", max_retries=2)
        backend._client = MockClient()

        import asyncio
        async def mock_sleep(seconds):
            pass
        monkeypatch.setattr(asyncio, "sleep", mock_sleep)

        with pytest.raises(LLMError, match="transient errors"):
            await backend.invoke([{"role": "user", "content": "test"}])

    async def test_validation_error_still_fails_immediately(self):
        """Non-transient validation errors should NOT be retried."""
        from app.core.engine.llm_provider import BedrockBackend, LLMError

        call_count = 0

        class MockClient:
            def invoke_model(self, **kwargs):
                nonlocal call_count
                call_count += 1
                raise Exception("ValidationException: Invalid input")

        backend = BedrockBackend(model_id="anthropic.claude-test", max_retries=3)
        backend._client = MockClient()

        with pytest.raises(LLMError, match="Bedrock invocation failed"):
            await backend.invoke([{"role": "user", "content": "test"}])

        assert call_count == 1

    async def test_retries_on_botocore_endpoint_connection_error(self, monkeypatch):
        """botocore EndpointConnectionError should be retried via isinstance check."""
        from app.core.engine.llm_provider import BedrockBackend, LLMError

        response_payload = {
            "content": [{"type": "text", "text": "Reconnected"}],
            "usage": {"input_tokens": 5, "output_tokens": 3},
            "stop_reason": "end_turn",
        }
        mock_response = self._make_mock_boto3_response(response_payload)
        call_count = 0

        try:
            from botocore.exceptions import EndpointConnectionError
            _has_botocore = True
        except ImportError:
            _has_botocore = False

        if not _has_botocore:
            pytest.skip("botocore not installed")

        class MockClient:
            def invoke_model(self, **kwargs):
                nonlocal call_count
                call_count += 1
                if call_count <= 1:
                    raise EndpointConnectionError(endpoint_url="https://bedrock.us-east-1.amazonaws.com")
                return mock_response

        backend = BedrockBackend(model_id="anthropic.claude-test", max_retries=3)
        backend._client = MockClient()

        import asyncio
        async def mock_sleep(seconds):
            pass
        monkeypatch.setattr(asyncio, "sleep", mock_sleep)

        resp = await backend.invoke([{"role": "user", "content": "test"}])
        assert resp.content == "Reconnected"
        assert call_count == 2

    async def test_retries_on_botocore_client_error_service_unavailable(self, monkeypatch):
        """botocore ClientError with ServiceUnavailableException code should be retried."""
        from app.core.engine.llm_provider import BedrockBackend, LLMError

        response_payload = {
            "content": [{"type": "text", "text": "OK after retry"}],
            "usage": {"input_tokens": 5, "output_tokens": 3},
            "stop_reason": "end_turn",
        }
        mock_response = self._make_mock_boto3_response(response_payload)
        call_count = 0

        try:
            from botocore.exceptions import ClientError
            _has_botocore = True
        except ImportError:
            _has_botocore = False

        if not _has_botocore:
            pytest.skip("botocore not installed")

        class MockClient:
            def invoke_model(self, **kwargs):
                nonlocal call_count
                call_count += 1
                if call_count <= 1:
                    raise ClientError(
                        error_response={
                            "Error": {
                                "Code": "ServiceUnavailableException",
                                "Message": "Service is temporarily unavailable",
                            }
                        },
                        operation_name="InvokeModel",
                    )
                return mock_response

        backend = BedrockBackend(model_id="anthropic.claude-test", max_retries=3)
        backend._client = MockClient()

        import asyncio
        async def mock_sleep(seconds):
            pass
        monkeypatch.setattr(asyncio, "sleep", mock_sleep)

        resp = await backend.invoke([{"role": "user", "content": "test"}])
        assert resp.content == "OK after retry"
        assert call_count == 2

    async def test_botocore_client_error_validation_not_retried(self):
        """botocore ClientError with ValidationException should NOT be retried."""
        from app.core.engine.llm_provider import BedrockBackend, LLMError

        try:
            from botocore.exceptions import ClientError
            _has_botocore = True
        except ImportError:
            _has_botocore = False

        if not _has_botocore:
            pytest.skip("botocore not installed")

        call_count = 0

        class MockClient:
            def invoke_model(self, **kwargs):
                nonlocal call_count
                call_count += 1
                raise ClientError(
                    error_response={
                        "Error": {
                            "Code": "ValidationException",
                            "Message": "Invalid model ID",
                        }
                    },
                    operation_name="InvokeModel",
                )

        backend = BedrockBackend(model_id="anthropic.claude-test", max_retries=3)
        backend._client = MockClient()

        with pytest.raises(LLMError, match="Bedrock invocation failed"):
            await backend.invoke([{"role": "user", "content": "test"}])

        assert call_count == 1, "ValidationException should not be retried"

    async def test_bedrock_retry_uses_jitter(self, monkeypatch):
        """Bedrock retry backoff should use jitter (not fixed delays)."""
        from app.core.engine.llm_provider import BedrockBackend, LLMError

        response_payload = {
            "content": [{"type": "text", "text": "OK"}],
            "usage": {"input_tokens": 5, "output_tokens": 2},
            "stop_reason": "end_turn",
        }
        mock_response = self._make_mock_boto3_response(response_payload)
        call_count = 0

        class MockClient:
            def invoke_model(self, **kwargs):
                nonlocal call_count
                call_count += 1
                if call_count <= 2:
                    raise ConnectionError("transient failure")
                return mock_response

        backend = BedrockBackend(model_id="anthropic.claude-test", max_retries=3)
        backend._client = MockClient()

        import asyncio
        sleep_durations = []

        async def capture_sleep(seconds):
            sleep_durations.append(seconds)

        monkeypatch.setattr(asyncio, "sleep", capture_sleep)

        resp = await backend.invoke([{"role": "user", "content": "test"}])
        assert resp.content == "OK"
        assert len(sleep_durations) == 2
        # Jitter means delays should be in range [0, 2^attempt] capped at 10
        for i, duration in enumerate(sleep_durations):
            max_expected = min(2 ** i, 10.0)
            assert 0 <= duration <= max_expected, (
                f"Delay {duration} not in jitter range [0, {max_expected}]"
            )

    async def test_retries_on_botocore_read_timeout_error(self, monkeypatch):
        """botocore ReadTimeoutError (transport-level) should be retried."""
        from app.core.engine.llm_provider import BedrockBackend

        response_payload = {
            "content": [{"type": "text", "text": "OK after read timeout"}],
            "usage": {"input_tokens": 5, "output_tokens": 3},
            "stop_reason": "end_turn",
        }
        mock_response = self._make_mock_boto3_response(response_payload)
        call_count = 0

        try:
            from botocore.exceptions import ReadTimeoutError as BotoReadTimeoutError
            _has_botocore = True
        except ImportError:
            _has_botocore = False

        if not _has_botocore:
            pytest.skip("botocore not installed")

        class MockClient:
            def invoke_model(self, **kwargs):
                nonlocal call_count
                call_count += 1
                if call_count <= 1:
                    raise BotoReadTimeoutError(endpoint_url="https://bedrock.us-east-1.amazonaws.com")
                return mock_response

        backend = BedrockBackend(model_id="anthropic.claude-test", max_retries=3)
        backend._client = MockClient()

        import asyncio
        async def mock_sleep(seconds):
            pass
        monkeypatch.setattr(asyncio, "sleep", mock_sleep)

        resp = await backend.invoke([{"role": "user", "content": "test"}])
        assert resp.content == "OK after read timeout"
        assert call_count == 2

    async def test_retries_on_botocore_connection_closed_error(self, monkeypatch):
        """botocore ConnectionClosedError (transport-level) should be retried."""
        from app.core.engine.llm_provider import BedrockBackend

        response_payload = {
            "content": [{"type": "text", "text": "OK after conn closed"}],
            "usage": {"input_tokens": 5, "output_tokens": 3},
            "stop_reason": "end_turn",
        }
        mock_response = self._make_mock_boto3_response(response_payload)
        call_count = 0

        try:
            from botocore.exceptions import ConnectionClosedError
            _has_botocore = True
        except ImportError:
            _has_botocore = False

        if not _has_botocore:
            pytest.skip("botocore not installed")

        class MockClient:
            def invoke_model(self, **kwargs):
                nonlocal call_count
                call_count += 1
                if call_count <= 1:
                    raise ConnectionClosedError(endpoint_url="https://bedrock.us-east-1.amazonaws.com")
                return mock_response

        backend = BedrockBackend(model_id="anthropic.claude-test", max_retries=3)
        backend._client = MockClient()

        import asyncio
        async def mock_sleep(seconds):
            pass
        monkeypatch.setattr(asyncio, "sleep", mock_sleep)

        resp = await backend.invoke([{"role": "user", "content": "test"}])
        assert resp.content == "OK after conn closed"
        assert call_count == 2

    async def test_retries_on_generic_botocore_error(self, monkeypatch):
        """Generic BotoCoreError (base class) should be retried as transient."""
        from app.core.engine.llm_provider import BedrockBackend

        response_payload = {
            "content": [{"type": "text", "text": "OK after botocore error"}],
            "usage": {"input_tokens": 5, "output_tokens": 3},
            "stop_reason": "end_turn",
        }
        mock_response = self._make_mock_boto3_response(response_payload)
        call_count = 0

        try:
            from botocore.exceptions import BotoCoreError
            _has_botocore = True
        except ImportError:
            _has_botocore = False

        if not _has_botocore:
            pytest.skip("botocore not installed")

        class TransientBotoCoreIssue(BotoCoreError):
            fmt = "Transient botocore issue"

        class MockClient:
            def invoke_model(self, **kwargs):
                nonlocal call_count
                call_count += 1
                if call_count <= 1:
                    raise TransientBotoCoreIssue()
                return mock_response

        backend = BedrockBackend(model_id="anthropic.claude-test", max_retries=3)
        backend._client = MockClient()

        import asyncio
        async def mock_sleep(seconds):
            pass
        monkeypatch.setattr(asyncio, "sleep", mock_sleep)

        resp = await backend.invoke([{"role": "user", "content": "test"}])
        assert resp.content == "OK after botocore error"
        assert call_count == 2

    async def test_retries_on_os_error(self, monkeypatch):
        """OSError (network-level) should be retried as transient."""
        from app.core.engine.llm_provider import BedrockBackend

        response_payload = {
            "content": [{"type": "text", "text": "OK after os error"}],
            "usage": {"input_tokens": 5, "output_tokens": 3},
            "stop_reason": "end_turn",
        }
        mock_response = self._make_mock_boto3_response(response_payload)
        call_count = 0

        class MockClient:
            def invoke_model(self, **kwargs):
                nonlocal call_count
                call_count += 1
                if call_count <= 1:
                    raise OSError("Network unreachable")
                return mock_response

        backend = BedrockBackend(model_id="anthropic.claude-test", max_retries=3)
        backend._client = MockClient()

        import asyncio
        async def mock_sleep(seconds):
            pass
        monkeypatch.setattr(asyncio, "sleep", mock_sleep)

        resp = await backend.invoke([{"role": "user", "content": "test"}])
        assert resp.content == "OK after os error"
        assert call_count == 2

    async def test_retries_on_botocore_client_error_throttling(self, monkeypatch):
        """botocore ClientError ThrottlingException should be retried and classified as rate-limit."""
        from app.core.engine.llm_provider import BedrockBackend, LLMRateLimitError

        try:
            from botocore.exceptions import ClientError
            _has_botocore = True
        except ImportError:
            _has_botocore = False

        if not _has_botocore:
            pytest.skip("botocore not installed")

        class MockClient:
            def invoke_model(self, **kwargs):
                raise ClientError(
                    error_response={
                        "Error": {
                            "Code": "ThrottlingException",
                            "Message": "Rate exceeded",
                        }
                    },
                    operation_name="InvokeModel",
                )

        backend = BedrockBackend(model_id="anthropic.claude-test", max_retries=2)
        backend._client = MockClient()

        import asyncio
        async def mock_sleep(seconds):
            pass
        monkeypatch.setattr(asyncio, "sleep", mock_sleep)

        with pytest.raises(LLMRateLimitError, match="Rate limited"):
            await backend.invoke([{"role": "user", "content": "test"}])

    async def test_retries_on_botocore_connect_timeout_error(self, monkeypatch):
        """botocore ConnectTimeoutError should be retried."""
        from app.core.engine.llm_provider import BedrockBackend

        response_payload = {
            "content": [{"type": "text", "text": "OK after connect timeout"}],
            "usage": {"input_tokens": 5, "output_tokens": 3},
            "stop_reason": "end_turn",
        }
        mock_response = self._make_mock_boto3_response(response_payload)
        call_count = 0

        try:
            from botocore.exceptions import ConnectTimeoutError as BotoConnectTimeoutError
            _has_botocore = True
        except ImportError:
            _has_botocore = False

        if not _has_botocore:
            pytest.skip("botocore not installed")

        class MockClient:
            def invoke_model(self, **kwargs):
                nonlocal call_count
                call_count += 1
                if call_count <= 1:
                    raise BotoConnectTimeoutError(endpoint_url="https://bedrock.us-east-1.amazonaws.com")
                return mock_response

        backend = BedrockBackend(model_id="anthropic.claude-test", max_retries=3)
        backend._client = MockClient()

        import asyncio
        async def mock_sleep(seconds):
            pass
        monkeypatch.setattr(asyncio, "sleep", mock_sleep)

        resp = await backend.invoke([{"role": "user", "content": "test"}])
        assert resp.content == "OK after connect timeout"
        assert call_count == 2
