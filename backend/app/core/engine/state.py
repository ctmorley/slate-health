"""Agent state definitions — TypedDict-based LangGraph state.

Provides the common state structure used by all agent graphs. Each agent
extends BaseAgentState with agent-specific fields while inheriting the
shared context fields (patient, payer, confidence, decision, audit trail).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, TypedDict


class PatientContext(TypedDict, total=False):
    """Patient information available to the agent."""

    patient_id: str
    mrn: str
    first_name: str
    last_name: str
    date_of_birth: str
    gender: str
    insurance_member_id: str
    insurance_group_id: str
    payer_id: str
    payer_name: str


class PayerContext(TypedDict, total=False):
    """Payer information and applicable rules."""

    payer_id: str
    payer_name: str
    payer_id_code: str
    electronic_payer_id: str
    applicable_rules: list[dict[str, Any]]


class AuditEntry(TypedDict, total=False):
    """Single audit trail entry recorded during agent execution."""

    timestamp: str
    node: str
    action: str
    details: dict[str, Any]


class ToolCall(TypedDict, total=False):
    """Structured tool call request from the LLM."""

    tool_name: str
    parameters: dict[str, Any]


class ToolResult(TypedDict, total=False):
    """Result from executing a tool call."""

    tool_name: str
    success: bool
    result: Any
    error: str | None


class BaseAgentState(TypedDict, total=False):
    """Common state shared by all agent graphs.

    Fields:
        task_id: The agent_task ID this execution belongs to.
        agent_type: Which agent is running (eligibility, claims, etc.).
        patient_context: Patient demographics and insurance info.
        payer_context: Payer details and applicable rules.
        input_data: Raw input submitted by the user/system.
        confidence: Agent's confidence in its decision (0.0-1.0).
        decision: The agent's output decision/recommendation.
        needs_review: Whether HITL review is required.
        review_reason: Why HITL review was triggered.
        error: Error message if execution failed.
        messages: LLM conversation messages for reasoning.
        tool_calls: Tool calls requested by the LLM.
        tool_results: Results from executed tool calls.
        audit_trail: Ordered list of audit entries for this execution.
        current_node: The graph node currently executing.
        iteration: Number of reasoning iterations completed.
        max_iterations: Maximum allowed reasoning iterations.
    """

    task_id: str
    agent_type: str
    patient_context: PatientContext
    payer_context: PayerContext
    input_data: dict[str, Any]
    confidence: float
    decision: dict[str, Any]
    needs_review: bool
    review_reason: str
    error: str | None
    messages: list[dict[str, Any]]
    tool_calls: list[ToolCall]
    tool_results: list[ToolResult]
    audit_trail: list[AuditEntry]
    current_node: str
    iteration: int
    max_iterations: int


def create_initial_state(
    *,
    task_id: str,
    agent_type: str,
    input_data: dict[str, Any] | None = None,
    patient_context: PatientContext | None = None,
    payer_context: PayerContext | None = None,
    max_iterations: int = 10,
) -> BaseAgentState:
    """Create a fresh initial state for an agent execution.

    Returns a BaseAgentState with all fields set to sensible defaults.
    """
    return BaseAgentState(
        task_id=task_id,
        agent_type=agent_type,
        patient_context=patient_context or PatientContext(),
        payer_context=payer_context or PayerContext(),
        input_data=input_data or {},
        confidence=0.0,
        decision={},
        needs_review=False,
        review_reason="",
        error=None,
        messages=[],
        tool_calls=[],
        tool_results=[],
        audit_trail=[],
        current_node="start",
        iteration=0,
        max_iterations=max_iterations,
    )
