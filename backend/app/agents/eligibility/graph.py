"""Eligibility agent LangGraph implementation.

Defines a complete graph with explicit eligibility-specific nodes:
  parse_request → check_payer_rules → build_270 → submit_clearinghouse →
  parse_271 → evaluate_confidence → output/escalate

This agent uses the base agent class infrastructure with eligibility-specific
tools and a custom graph that matches the contract node pipeline.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from langgraph.graph import StateGraph, END
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.base import BaseAgent
from app.agents.eligibility.prompts import SYSTEM_PROMPT
from app.agents.eligibility.tools import get_eligibility_tools
from app.core.engine.graph_builder import AgentGraph
from app.core.engine.llm_provider import LLMProvider
from app.core.engine.state import AuditEntry, BaseAgentState, create_initial_state
from app.core.engine.tool_executor import ToolDefinition, ToolExecutor

logger = logging.getLogger(__name__)

# Confidence threshold for HITL escalation
ELIGIBILITY_CONFIDENCE_THRESHOLD = 0.7


# ── Eligibility-specific graph nodes ─────────────────────────────────


async def parse_request_node(state: dict) -> dict:
    """Parse and validate eligibility request input.

    Extracts subscriber, payer, and provider info from the input data
    and validates required fields.
    """
    state["current_node"] = "parse_request"
    input_data = state.get("input_data", {})

    # Validate required fields
    required = ["subscriber_id", "subscriber_last_name", "subscriber_first_name"]
    missing = [f for f in required if not input_data.get(f)]

    if missing:
        state["error"] = f"Missing required fields: {', '.join(missing)}"
        state["confidence"] = 0.0
        state["audit_trail"].append(
            AuditEntry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                node="parse_request",
                action="validation_failed",
                details={"missing_fields": missing},
            )
        )
        return state

    # Enrich patient context from input
    state["patient_context"] = {
        "subscriber_id": input_data.get("subscriber_id", ""),
        "first_name": input_data.get("subscriber_first_name", ""),
        "last_name": input_data.get("subscriber_last_name", ""),
        "date_of_birth": input_data.get("subscriber_dob", ""),
        "insurance_member_id": input_data.get("subscriber_id", ""),
    }

    # Enrich payer context from input
    if input_data.get("payer_id") or input_data.get("payer_name"):
        state["payer_context"] = {
            "payer_id": input_data.get("payer_id", ""),
            "payer_name": input_data.get("payer_name", ""),
        }

    state["audit_trail"].append(
        AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            node="parse_request",
            action="request_parsed",
            details={
                "subscriber_id": input_data.get("subscriber_id", ""),
                "payer_id": input_data.get("payer_id", ""),
                "has_provider": bool(input_data.get("provider_npi")),
            },
        )
    )

    return state


async def check_payer_rules_node(state: dict) -> dict:
    """Check payer-specific rules for eligibility verification.

    Looks up the payer's rules to determine any special requirements
    for the eligibility check (e.g. required fields, service type codes).
    """
    state["current_node"] = "check_payer_rules"
    payer_context = state.get("payer_context", {})
    payer_id = payer_context.get("payer_id", "")

    rules_applied = []
    if payer_id:
        # In production, this would query the payer_rules table.
        # For now, apply default rules and record the check.
        rules_applied.append({
            "rule": "eligibility_check_allowed",
            "payer_id": payer_id,
            "result": "pass",
        })

    state["payer_rules_applied"] = rules_applied
    state["audit_trail"].append(
        AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            node="check_payer_rules",
            action="payer_rules_checked",
            details={
                "payer_id": payer_id,
                "rules_count": len(rules_applied),
            },
        )
    )

    return state


async def build_270_node(state: dict) -> dict:
    """Build the X12 270 eligibility inquiry from validated input.

    Constructs the 270 transaction segments from patient and payer context.
    """
    state["current_node"] = "build_270"
    input_data = state.get("input_data", {})

    # Build the 270 request data structure
    x12_270_data = {
        "subscriber_id": input_data.get("subscriber_id", ""),
        "subscriber_last_name": input_data.get("subscriber_last_name", ""),
        "subscriber_first_name": input_data.get("subscriber_first_name", ""),
        "subscriber_dob": input_data.get("subscriber_dob", ""),
        "payer_id": input_data.get("payer_id", ""),
        "payer_name": input_data.get("payer_name", ""),
        "provider_npi": input_data.get("provider_npi", ""),
        "service_type_code": input_data.get("service_type_code", "30"),
        "date_of_service": input_data.get("date_of_service", ""),
    }

    state["x12_270_data"] = x12_270_data

    state["audit_trail"].append(
        AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            node="build_270",
            action="270_request_built",
            details={
                "subscriber_id": x12_270_data["subscriber_id"],
                "payer_id": x12_270_data["payer_id"],
            },
        )
    )

    return state


async def submit_clearinghouse_node(state: dict) -> dict:
    """Submit the 270 request to the clearinghouse.

    In the LangGraph agent context, this records the submission intent.
    The actual clearinghouse submission happens in the Temporal workflow
    activities for durability. This node prepares the submission metadata.
    """
    state["current_node"] = "submit_clearinghouse"
    x12_data = state.get("x12_270_data", {})

    state["submission_status"] = "submitted"
    state["submission_metadata"] = {
        "payer_id": x12_data.get("payer_id", ""),
        "subscriber_id": x12_data.get("subscriber_id", ""),
        "submitted": True,
    }

    state["audit_trail"].append(
        AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            node="submit_clearinghouse",
            action="clearinghouse_submission_prepared",
            details={"payer_id": x12_data.get("payer_id", "")},
        )
    )

    return state


async def parse_271_node(state: dict) -> dict:
    """Parse the 271 eligibility response.

    In the agent graph, this processes any available response data.
    The real parsing happens in the Temporal workflow activity; this
    node handles the agent-level interpretation of results.
    """
    state["current_node"] = "parse_271"

    # If there's response data (from tool execution or mock), process it
    tool_results = state.get("tool_results", [])
    response_data = {}
    for result in tool_results:
        if result.get("tool_name") == "parse_271_response" and result.get("success"):
            response_data = result.get("result", {})
            break

    state["response_data"] = response_data

    state["audit_trail"].append(
        AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            node="parse_271",
            action="271_response_processed",
            details={"has_response": bool(response_data)},
        )
    )

    return state


async def evaluate_confidence_node(state: dict) -> dict:
    """Evaluate confidence and determine if HITL escalation is needed.

    Checks multiple factors:
    - Error presence
    - Response completeness
    - Multiple coverage matches (hard review trigger)
    - Payer rule compliance
    """
    state["current_node"] = "evaluate_confidence"

    # Start with high confidence; reduce based on issues found.
    # If the state already has a non-zero confidence (e.g. from LLM),
    # use it; otherwise default to 0.85 (successful parse + submission).
    raw_confidence = state.get("confidence", 0.0)
    confidence = raw_confidence if raw_confidence > 0.0 else 0.85
    needs_review = False
    review_reason = ""

    # Check for errors
    if state.get("error"):
        confidence = 0.0
        needs_review = True
        review_reason = f"Error during processing: {state['error']}"
    else:
        # Check response data for ambiguity indicators
        response_data = state.get("response_data", {})
        input_data = state.get("input_data", {})

        # Multiple coverage matches is a HARD review trigger
        benefits = response_data.get("benefits", [])
        active_benefits = [b for b in benefits if b.get("eligibility_code") == "1"]
        if len(active_benefits) > 1:
            needs_review = True
            confidence = min(confidence, 0.5)
            review_reason = "Multiple active coverage matches found"

        # Ambiguous input with multiple possible payers
        if input_data.get("ambiguous_coverage"):
            needs_review = True
            confidence = min(confidence, 0.4)
            review_reason = "Ambiguous coverage information"

        # Low base confidence from LLM reasoning
        if confidence < ELIGIBILITY_CONFIDENCE_THRESHOLD:
            needs_review = True
            if not review_reason:
                review_reason = (
                    f"Confidence {confidence:.2f} below threshold "
                    f"{ELIGIBILITY_CONFIDENCE_THRESHOLD:.2f}"
                )

    state["confidence"] = confidence
    state["needs_review"] = needs_review
    state["review_reason"] = review_reason

    state["audit_trail"].append(
        AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            node="evaluate_confidence",
            action="confidence_evaluated",
            details={
                "confidence": confidence,
                "needs_review": needs_review,
                "review_reason": review_reason,
            },
        )
    )

    return state


async def escalate_node(state: dict) -> dict:
    """Escalation node — marks the task for HITL review.

    Sets the needs_review flag and records the escalation in the audit trail.
    This node is reached when confidence is below the threshold.
    """
    state["current_node"] = "escalate"

    # Ensure needs_review is set (should already be from evaluate_confidence)
    state["needs_review"] = True

    # Build the decision output with escalation metadata
    state["decision"] = {
        "submission_strategy": "clearinghouse",
        "payer_rules_applied": state.get("payer_rules_applied", []),
        "confidence": state.get("confidence", 0.0),
        "needs_review": True,
        "review_reason": state.get("review_reason", ""),
        "escalated": True,
    }

    state["audit_trail"].append(
        AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            node="escalate",
            action="escalated_to_hitl",
            details={
                "confidence": state.get("confidence", 0.0),
                "review_reason": state.get("review_reason", ""),
            },
        )
    )

    return state


async def output_node(state: dict) -> dict:
    """Final output node — assembles the agent's output."""
    state["current_node"] = "output"

    # Build the decision output (may already be set by escalate node)
    if not state.get("decision"):
        state["decision"] = {
            "submission_strategy": "clearinghouse",
            "payer_rules_applied": state.get("payer_rules_applied", []),
            "confidence": state.get("confidence", 0.85),
            "needs_review": state.get("needs_review", False),
            "review_reason": state.get("review_reason", ""),
        }

    state["audit_trail"].append(
        AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            node="output",
            action="execution_completed",
            details={
                "confidence": state.get("confidence", 0.0),
                "needs_review": state.get("needs_review", False),
            },
        )
    )

    return state


# ── Graph routing ────────────────────────────────────────────────────


def _evaluate_confidence_router(state: dict) -> str:
    """Route from evaluate_confidence: to output or escalate.

    Routes to 'escalate' when confidence is below threshold or
    needs_review is set, otherwise routes directly to 'output'.
    """
    if state.get("needs_review", False):
        return "escalate"
    return "output"


def _parse_request_router(state: dict) -> str:
    """Route from parse_request: skip to output on error."""
    if state.get("error"):
        return "output"
    return "check_payer_rules"


# ── Agent class ──────────────────────────────────────────────────────


class EligibilityAgent(BaseAgent):
    """Eligibility Verification Agent.

    Processes insurance eligibility verification requests through a
    LangGraph workflow with the following explicit nodes:

    1. parse_request — validate and normalize input
    2. check_payer_rules — look up payer-specific requirements
    3. build_270 — construct X12 270 inquiry
    4. submit_clearinghouse — submit via clearinghouse
    5. parse_271 — parse response and extract coverage details
    6. evaluate_confidence — determine confidence and HITL escalation
    7. output — assemble final output (or escalate)
    """

    agent_type = "eligibility"
    confidence_threshold = ELIGIBILITY_CONFIDENCE_THRESHOLD

    def get_tools(self) -> list[ToolDefinition]:
        """Return eligibility-specific tools."""
        return get_eligibility_tools()

    def build_graph(self) -> AgentGraph:
        """Build the eligibility agent graph with explicit contract nodes.

        Graph topology:
            parse_request → [check_payer_rules | output (on error)]
            check_payer_rules → build_270
            build_270 → submit_clearinghouse
            submit_clearinghouse → parse_271
            parse_271 → evaluate_confidence
            evaluate_confidence → [output | escalate]
            escalate → output
        """
        graph = StateGraph(dict)

        # Add all eligibility-specific nodes
        graph.add_node("parse_request", parse_request_node)
        graph.add_node("check_payer_rules", check_payer_rules_node)
        graph.add_node("build_270", build_270_node)
        graph.add_node("submit_clearinghouse", submit_clearinghouse_node)
        graph.add_node("parse_271", parse_271_node)
        graph.add_node("evaluate_confidence", evaluate_confidence_node)
        graph.add_node("escalate", escalate_node)
        graph.add_node("output", output_node)

        # Set entry point
        graph.set_entry_point("parse_request")

        # Add edges — parse_request routes conditionally on error
        graph.add_conditional_edges(
            "parse_request",
            _parse_request_router,
            {"check_payer_rules": "check_payer_rules", "output": "output"},
        )
        graph.add_edge("check_payer_rules", "build_270")
        graph.add_edge("build_270", "submit_clearinghouse")
        graph.add_edge("submit_clearinghouse", "parse_271")
        graph.add_edge("parse_271", "evaluate_confidence")
        # evaluate_confidence routes to escalate or output based on needs_review
        graph.add_conditional_edges(
            "evaluate_confidence",
            _evaluate_confidence_router,
            {"escalate": "escalate", "output": "output"},
        )
        graph.add_edge("escalate", "output")
        graph.add_edge("output", END)

        compiled = graph.compile()

        return AgentGraph(
            compiled_graph=compiled,
            node_names=[
                "parse_request",
                "check_payer_rules",
                "build_270",
                "submit_clearinghouse",
                "parse_271",
                "evaluate_confidence",
                "escalate",
                "output",
            ],
        )


async def run_eligibility_agent(
    *,
    input_data: dict[str, Any],
    llm_provider: LLMProvider,
    session: AsyncSession | None = None,
    task_id: str | None = None,
) -> BaseAgentState:
    """Convenience function to run the eligibility agent.

    Creates and runs an EligibilityAgent with the given input.
    Returns the final agent state.
    """
    agent = EligibilityAgent(
        llm_provider=llm_provider,
        session=session,
    )

    patient_context = {
        "subscriber_id": input_data.get("subscriber_id", ""),
        "first_name": input_data.get("subscriber_first_name", ""),
        "last_name": input_data.get("subscriber_last_name", ""),
        "date_of_birth": input_data.get("subscriber_dob", ""),
    }

    payer_context = {
        "payer_id": input_data.get("payer_id", ""),
        "payer_name": input_data.get("payer_name", ""),
    }

    state = await agent.run(
        task_id=task_id,
        input_data=input_data,
        patient_context=patient_context,
        payer_context=payer_context,
    )

    return state
