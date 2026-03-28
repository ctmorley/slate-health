"""Unit tests for the Eligibility Verification Agent.

Tests cover: agent graph construction, node execution, tool definitions,
request parsing, 270 building, 271 parsing, and confidence evaluation.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.agents.eligibility.graph import (
    EligibilityAgent,
    parse_request_node,
    check_payer_rules_node,
    build_270_node,
    submit_clearinghouse_node,
    parse_271_node,
    evaluate_confidence_node,
    output_node,
)
from app.agents.eligibility.tools import (
    build_270_request,
    check_payer_rules,
    get_eligibility_tools,
    parse_271_response,
    validate_subscriber_info,
)
from app.core.engine.llm_provider import LLMProvider, MockLLMBackend
from app.core.engine.state import create_initial_state


# ── Tool Tests ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_validate_subscriber_valid():
    """validate_subscriber_info returns valid=True for complete info."""
    result = await validate_subscriber_info(
        subscriber_id="INS-12345",
        subscriber_first_name="Jane",
        subscriber_last_name="Doe",
        subscriber_dob="19850615",
    )
    assert result["valid"] is True
    assert result["issues"] == []
    assert result["subscriber_name"] == "Doe, Jane"


@pytest.mark.asyncio
async def test_validate_subscriber_missing_fields():
    """validate_subscriber_info returns issues for missing fields."""
    result = await validate_subscriber_info(
        subscriber_id="",
        subscriber_first_name="",
        subscriber_last_name="Doe",
    )
    assert result["valid"] is False
    assert len(result["issues"]) >= 2


@pytest.mark.asyncio
async def test_build_270_request_success():
    """build_270_request returns success with X12 payload."""
    result = await build_270_request(
        subscriber_id="INS-12345",
        subscriber_first_name="Jane",
        subscriber_last_name="Doe",
        subscriber_dob="19850615",
        payer_id="BCBS01",
        payer_name="Blue Cross Blue Shield",
        provider_npi="1234567890",
        provider_last_name="Smith",
    )
    assert result["success"] is True
    assert "x12_270" in result
    assert "control_number" in result
    # X12 270 is a string containing segments
    assert isinstance(result["x12_270"], str)
    assert "ISA" in result["x12_270"]
    assert "270" in result["x12_270"]


@pytest.mark.asyncio
async def test_parse_271_response_empty():
    """parse_271_response handles empty response gracefully."""
    result = await parse_271_response("")
    # Should either succeed with empty data or fail gracefully
    assert "success" in result


@pytest.mark.asyncio
async def test_check_payer_rules():
    """check_payer_rules returns default rules for known payer."""
    result = await check_payer_rules(payer_id="BCBS01")
    assert result["payer_id"] == "BCBS01"
    assert result["submission_method"] == "clearinghouse"


@pytest.mark.asyncio
async def test_get_eligibility_tools_definitions():
    """get_eligibility_tools returns all 4 tool definitions."""
    tools = get_eligibility_tools()
    assert len(tools) == 4
    names = {t.name for t in tools}
    assert "validate_subscriber" in names
    assert "build_270" in names
    assert "parse_271" in names
    assert "check_payer_rules" in names


# ── Graph/Node Tests ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_parse_request_node_valid_input():
    """parse_request_node enriches state with patient context."""
    state = create_initial_state(
        task_id=str(uuid.uuid4()),
        agent_type="eligibility",
        input_data={
            "subscriber_id": "INS-12345",
            "subscriber_first_name": "Jane",
            "subscriber_last_name": "Doe",
            "subscriber_dob": "19850615",
            "payer_id": "BCBS01",
            "payer_name": "Blue Cross",
        },
    )
    result = await parse_request_node(dict(state))
    assert result["current_node"] == "parse_request"
    assert result["patient_context"]["subscriber_id"] == "INS-12345"
    assert result["payer_context"]["payer_id"] == "BCBS01"
    assert result.get("error") is None


@pytest.mark.asyncio
async def test_parse_request_node_missing_fields():
    """parse_request_node sets error for missing required fields."""
    state = create_initial_state(
        task_id=str(uuid.uuid4()),
        agent_type="eligibility",
        input_data={"payer_id": "BCBS01"},  # missing subscriber info
    )
    result = await parse_request_node(dict(state))
    assert result.get("error") is not None
    assert "Missing required fields" in result["error"]
    assert result["confidence"] == 0.0


@pytest.mark.asyncio
async def test_check_payer_rules_node():
    """check_payer_rules_node records rules check in state."""
    state = create_initial_state(
        task_id=str(uuid.uuid4()),
        agent_type="eligibility",
        input_data={"subscriber_id": "INS-001"},
        payer_context={"payer_id": "BCBS01", "payer_name": "BCBS"},
    )
    result = await check_payer_rules_node(dict(state))
    assert result["current_node"] == "check_payer_rules"
    assert len(result["payer_rules_applied"]) >= 1


@pytest.mark.asyncio
async def test_build_270_node():
    """build_270_node creates x12_270_data in state."""
    state = create_initial_state(
        task_id=str(uuid.uuid4()),
        agent_type="eligibility",
        input_data={
            "subscriber_id": "INS-12345",
            "subscriber_first_name": "Jane",
            "subscriber_last_name": "Doe",
            "payer_id": "BCBS01",
        },
    )
    result = await build_270_node(dict(state))
    assert result["current_node"] == "build_270"
    assert "x12_270_data" in result
    assert result["x12_270_data"]["subscriber_id"] == "INS-12345"


@pytest.mark.asyncio
async def test_evaluate_confidence_node_high():
    """evaluate_confidence_node with no issues keeps confidence high."""
    state = create_initial_state(
        task_id=str(uuid.uuid4()),
        agent_type="eligibility",
        input_data={"subscriber_id": "INS-001"},
    )
    state_dict = dict(state)
    state_dict["confidence"] = 0.9
    state_dict["response_data"] = {}
    result = await evaluate_confidence_node(state_dict)
    assert result["needs_review"] is False
    assert result["confidence"] >= 0.7


@pytest.mark.asyncio
async def test_evaluate_confidence_node_multiple_active_benefits():
    """evaluate_confidence_node triggers review for multiple active coverage matches."""
    state = create_initial_state(
        task_id=str(uuid.uuid4()),
        agent_type="eligibility",
        input_data={"subscriber_id": "INS-001"},
    )
    state_dict = dict(state)
    state_dict["confidence"] = 0.9
    state_dict["response_data"] = {
        "benefits": [
            {"eligibility_code": "1", "plan": "PPO"},
            {"eligibility_code": "1", "plan": "HMO"},
        ]
    }
    result = await evaluate_confidence_node(state_dict)
    assert result["needs_review"] is True
    assert "Multiple active coverage" in result["review_reason"]


@pytest.mark.asyncio
async def test_evaluate_confidence_node_error():
    """evaluate_confidence_node triggers review on error."""
    state = create_initial_state(
        task_id=str(uuid.uuid4()),
        agent_type="eligibility",
        input_data={"subscriber_id": "INS-001"},
    )
    state_dict = dict(state)
    state_dict["error"] = "Something went wrong"
    state_dict["response_data"] = {}
    result = await evaluate_confidence_node(state_dict)
    assert result["needs_review"] is True
    assert result["confidence"] == 0.0


@pytest.mark.asyncio
async def test_eligibility_agent_build_graph():
    """EligibilityAgent builds a compilable graph with contract-required nodes."""
    mock_backend = MockLLMBackend(responses=['{"confidence": 0.9, "decision": {"coverage": "active"}, "tool_calls": []}'])
    llm_provider = LLMProvider(primary=mock_backend, phi_safe=False)

    agent = EligibilityAgent(llm_provider=llm_provider)
    graph = agent.build_graph()

    assert graph is not None
    # Verify the contract-required eligibility-specific nodes
    assert "parse_request" in graph.node_names
    assert "check_payer_rules" in graph.node_names
    assert "build_270" in graph.node_names
    assert "submit_clearinghouse" in graph.node_names
    assert "parse_271" in graph.node_names
    assert "evaluate_confidence" in graph.node_names
    assert "output" in graph.node_names


@pytest.mark.asyncio
async def test_eligibility_agent_run_full_cycle():
    """EligibilityAgent.run() executes full graph with mock LLM."""
    mock_backend = MockLLMBackend(
        responses=['{"confidence": 0.85, "decision": {"coverage_active": true, "plan": "PPO"}, "tool_calls": []}']
    )
    llm_provider = LLMProvider(primary=mock_backend, phi_safe=False)

    agent = EligibilityAgent(llm_provider=llm_provider)
    state = await agent.run(
        task_id=str(uuid.uuid4()),
        input_data={
            "subscriber_id": "INS-12345",
            "subscriber_first_name": "Jane",
            "subscriber_last_name": "Doe",
        },
    )

    # Agent should have run through the graph
    assert state["agent_type"] == "eligibility"
    assert len(state.get("audit_trail", [])) > 0
    # Verify it went through the eligibility-specific nodes
    node_actions = [e.get("node", "") for e in state.get("audit_trail", [])]
    assert "parse_request" in node_actions
    assert "output" in node_actions


@pytest.mark.asyncio
async def test_eligibility_agent_low_confidence_triggers_review():
    """EligibilityAgent with ambiguous coverage sets needs_review=True."""
    mock_backend = MockLLMBackend(
        responses=['{"confidence": 0.3, "decision": {"ambiguous": true}, "tool_calls": []}']
    )
    llm_provider = LLMProvider(primary=mock_backend, phi_safe=False)

    agent = EligibilityAgent(llm_provider=llm_provider)
    state = await agent.run(
        task_id=str(uuid.uuid4()),
        input_data={
            "subscriber_id": "INS-12345",
            "subscriber_first_name": "Jane",
            "subscriber_last_name": "Doe",
            "ambiguous_coverage": True,  # Triggers HITL review
        },
    )

    assert state.get("needs_review") is True


@pytest.mark.asyncio
async def test_eligibility_agent_high_confidence_no_review():
    """EligibilityAgent with high confidence does not need review."""
    mock_backend = MockLLMBackend(
        responses=['{"confidence": 0.95, "decision": {"coverage_active": true}, "tool_calls": []}']
    )
    llm_provider = LLMProvider(primary=mock_backend, phi_safe=False)

    agent = EligibilityAgent(llm_provider=llm_provider)
    state = await agent.run(
        task_id=str(uuid.uuid4()),
        input_data={
            "subscriber_id": "INS-12345",
            "subscriber_first_name": "Jane",
            "subscriber_last_name": "Doe",
        },
    )

    assert state.get("needs_review") is False
