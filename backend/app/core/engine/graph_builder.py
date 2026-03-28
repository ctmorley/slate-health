"""Base graph builder — LangGraph StateGraph factory.

Creates a standard agent graph with configurable nodes:
  ingest → reason → decide → execute → audit

Each node is a callable that takes and returns a state dict.
Agents customize behavior by providing their own node implementations
or by extending the default ones.

This module uses the langgraph library for graph compilation and
execution, providing a true StateGraph with conditional routing.

NOTE: Node functions use ``dict`` annotations (not ``BaseAgentState``)
because LangGraph introspects parameter annotations to derive state
channels. Using a ``TypedDict(total=False)`` annotation causes LangGraph
to initialise an empty state, stripping keys supplied by the caller.
"""

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Callable, Awaitable

from langgraph.graph import StateGraph, END

from app.core.engine.state import BaseAgentState, AuditEntry, ToolCall
from app.core.engine.llm_provider import LLMProvider, LLMResponse
from app.core.engine.tool_executor import ToolExecutor

logger = logging.getLogger(__name__)

# Type alias for node functions — uses ``dict`` deliberately; see module
# docstring for the rationale.
NodeFunc = Callable[[dict], Awaitable[dict]]


# ── Structured LLM Output Parsing ──────────────────────────────────


def parse_llm_decision(content: str) -> dict[str, Any]:
    """Parse structured decision output from the LLM response.

    Expects the LLM to return a JSON block (possibly within markdown
    fences) containing at least:
      - confidence: float (0.0-1.0)
      - decision: dict with agent's output
      - tool_calls: list of {tool_name, parameters} (optional)

    If the response is not valid JSON, attempts to extract a JSON block
    from markdown code fences. Falls back to heuristic parsing if no
    JSON is found.

    Returns:
        Dict with keys: confidence, decision, tool_calls.
    """
    # Try direct JSON parse
    parsed = _try_parse_json(content)
    if parsed is not None:
        return _normalize_parsed(parsed)

    # Try extracting from markdown code fences
    fence_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", content, re.DOTALL)
    if fence_match:
        parsed = _try_parse_json(fence_match.group(1).strip())
        if parsed is not None:
            return _normalize_parsed(parsed)

    # Heuristic fallback: try to find an embedded JSON object
    brace_match = re.search(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", content, re.DOTALL)
    if brace_match:
        parsed = _try_parse_json(brace_match.group(0))
        if parsed is not None:
            return _normalize_parsed(parsed)

    # Final fallback: no structured output detected
    return {
        "confidence": 0.0,
        "decision": {"raw_response": content},
        "tool_calls": [],
    }


def _try_parse_json(text: str) -> dict[str, Any] | None:
    """Attempt to parse text as JSON dict, return None on failure."""
    try:
        result = json.loads(text)
        if isinstance(result, dict):
            return result
    except (json.JSONDecodeError, TypeError):
        pass
    return None


def _normalize_parsed(parsed: dict[str, Any]) -> dict[str, Any]:
    """Normalize a parsed JSON dict into the expected structure."""
    confidence = parsed.get("confidence", 0.0)
    if not isinstance(confidence, (int, float)):
        try:
            confidence = float(confidence)
        except (TypeError, ValueError):
            confidence = 0.0
    confidence = max(0.0, min(1.0, float(confidence)))

    decision = parsed.get("decision", {})
    if not isinstance(decision, dict):
        decision = {"value": decision}

    tool_calls_raw = parsed.get("tool_calls", [])
    tool_calls: list[dict[str, Any]] = []
    if isinstance(tool_calls_raw, list):
        for tc in tool_calls_raw:
            if isinstance(tc, dict) and "tool_name" in tc:
                tool_calls.append(
                    ToolCall(
                        tool_name=tc["tool_name"],
                        parameters=tc.get("parameters", {}),
                    )
                )

    return {
        "confidence": confidence,
        "decision": decision,
        "tool_calls": tool_calls,
    }


# ── Default Node Implementations ────────────────────────────────────


async def default_ingest_node(state: dict) -> dict:
    """Default ingest node — validates input and populates context.

    Override in agent-specific graphs to add data fetching (FHIR, etc.).
    """
    state["current_node"] = "ingest"
    state["audit_trail"].append(
        AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            node="ingest",
            action="ingest_started",
            details={"input_keys": list(state.get("input_data", {}).keys())},
        )
    )
    return state


async def default_audit_node(state: dict) -> dict:
    """Default audit node — records final audit entry for the execution."""
    state["current_node"] = "audit"
    state["audit_trail"].append(
        AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            node="audit",
            action="execution_completed",
            details={
                "confidence": state.get("confidence", 0.0),
                "needs_review": state.get("needs_review", False),
                "has_error": state.get("error") is not None,
                "iterations": state.get("iteration", 0),
            },
        )
    )
    return state


def make_reason_node(llm_provider: LLMProvider) -> NodeFunc:
    """Create a reason node that uses the LLM to analyze the situation.

    The reason node constructs a prompt from the current state, sends it
    to the LLM, and parses the structured response into confidence,
    decision, and tool_calls fields on the state.
    """

    async def reason_node(state: dict) -> dict:
        state["current_node"] = "reason"
        iteration = state.get("iteration", 0) + 1
        state["iteration"] = iteration

        # Build messages for the LLM
        messages = list(state.get("messages", []))
        if not messages:
            # Initial message with context
            user_content = (
                f"Agent type: {state.get('agent_type', 'unknown')}\n"
                f"Input data: {state.get('input_data', {})}\n"
                f"Patient context: {state.get('patient_context', {})}\n"
                f"Payer context: {state.get('payer_context', {})}\n"
                f"Tool results: {state.get('tool_results', [])}\n\n"
                "Analyze the situation and decide on the next action.\n\n"
                "You MUST respond with a JSON object containing:\n"
                '- "confidence": a float between 0.0 and 1.0\n'
                '- "decision": an object with your decision details\n'
                '- "tool_calls": a list of tool calls (each with "tool_name" '
                'and "parameters"), or empty list\n'
            )
            messages.append({"role": "user", "content": user_content})

        # Add tool results as assistant context if available
        tool_results = state.get("tool_results", [])
        if tool_results and iteration > 1:
            results_text = "\n".join(
                f"Tool '{r.get('tool_name', 'unknown')}': "
                f"{'success' if r.get('success') else 'failed'} — "
                f"{r.get('result', r.get('error', ''))}"
                for r in tool_results
            )
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"Tool results:\n{results_text}\n\n"
                        "Respond with an updated JSON decision object."
                    ),
                }
            )

        response: LLMResponse = await llm_provider.send(
            messages,
            system_prompt=(
                "You are a healthcare AI agent. Analyze the input and determine "
                "the appropriate action. You MUST respond ONLY with a JSON object "
                "containing: confidence (float 0-1), decision (object), and "
                "tool_calls (array of {tool_name, parameters})."
            ),
        )

        # Parse structured output from LLM
        parsed = parse_llm_decision(response.content)
        state["confidence"] = parsed["confidence"]
        state["decision"] = parsed["decision"]
        state["tool_calls"] = parsed["tool_calls"]

        state["messages"] = messages + [
            {"role": "assistant", "content": response.content}
        ]

        state["audit_trail"].append(
            AuditEntry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                node="reason",
                action="llm_reasoning",
                details={
                    "iteration": iteration,
                    "input_tokens": response.input_tokens,
                    "output_tokens": response.output_tokens,
                    "model": response.model,
                    "confidence": parsed["confidence"],
                    "tool_call_count": len(parsed["tool_calls"]),
                },
            )
        )

        return state

    return reason_node


def make_decide_node(confidence_threshold: float = 0.7) -> NodeFunc:
    """Create a decide node that evaluates confidence and routes.

    The decide node examines the agent's confidence score and determines
    whether to proceed to execution or escalate for human review.
    """

    async def decide_node(state: dict) -> dict:
        state["current_node"] = "decide"

        confidence = state.get("confidence", 0.0)
        if confidence < confidence_threshold:
            state["needs_review"] = True
            state["review_reason"] = (
                f"Confidence {confidence:.2f} below threshold "
                f"{confidence_threshold:.2f}"
            )
        else:
            state["needs_review"] = False

        state["audit_trail"].append(
            AuditEntry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                node="decide",
                action="decision_made",
                details={
                    "confidence": confidence,
                    "threshold": confidence_threshold,
                    "needs_review": state["needs_review"],
                },
            )
        )

        return state

    return decide_node


def make_execute_node(tool_executor: ToolExecutor) -> NodeFunc:
    """Create an execute node that runs tool calls from the reasoning step."""

    async def execute_node(state: dict) -> dict:
        state["current_node"] = "execute"

        tool_calls = state.get("tool_calls", [])
        if not tool_calls:
            state["audit_trail"].append(
                AuditEntry(
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    node="execute",
                    action="no_tools_to_execute",
                    details={},
                )
            )
            return state

        results = await tool_executor.execute_many(tool_calls)
        state["tool_results"] = results

        state["audit_trail"].append(
            AuditEntry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                node="execute",
                action="tools_executed",
                details={
                    "tool_count": len(tool_calls),
                    "success_count": sum(
                        1 for r in results if r.get("success")
                    ),
                    "failure_count": sum(
                        1 for r in results if not r.get("success")
                    ),
                },
            )
        )

        return state

    return execute_node


# ── Router Functions ────────────────────────────────────────────────


def decide_router(state: dict) -> str:
    """Route from decide node: to execute or audit (via HITL)."""
    if state.get("needs_review", False):
        return "audit"
    return "execute"


def execute_router(state: dict) -> str:
    """Route from execute node: back to reason or to audit."""
    iteration = state.get("iteration", 0)
    max_iterations = state.get("max_iterations", 10)

    # If we have pending tool calls that need re-evaluation, loop back
    tool_results = state.get("tool_results", [])
    has_failures = any(not r.get("success") for r in tool_results)

    if has_failures and iteration < max_iterations:
        return "reason"

    return "audit"


# ── LangGraph StateGraph Compilation ───────────────────────────────


class AgentGraph:
    """Compiled agent graph that wraps a LangGraph StateGraph.

    This is the compiled result of a GraphBuilder. It wraps a
    langgraph ``StateGraph`` compiled into a runnable, providing
    the standard agent execution topology.
    """

    def __init__(
        self,
        compiled_graph: Any,
        node_names: list[str],
    ) -> None:
        self._compiled = compiled_graph
        self._node_names = node_names

    @property
    def node_names(self) -> list[str]:
        return list(self._node_names)

    @property
    def compiled(self) -> Any:
        """Access the underlying compiled LangGraph for inspection."""
        return self._compiled

    async def run(self, state: dict) -> dict:
        """Execute the graph starting from the entry point.

        Uses the compiled LangGraph StateGraph to process the state
        through all nodes following defined edges and conditional routing.
        """
        try:
            result = await self._compiled.ainvoke(state)
            return result
        except Exception as exc:
            logger.error("Graph execution failed: %s", exc)
            state["error"] = f"Graph execution failed: {exc}"
            return state


class GraphBuilder:
    """Factory for building agent graphs with standard topology.

    Creates a LangGraph ``StateGraph`` with the standard nodes
    (ingest → reason → decide → execute → audit) using configurable
    node implementations and routing.
    """

    def __init__(
        self,
        *,
        llm_provider: LLMProvider | None = None,
        tool_executor: ToolExecutor | None = None,
        confidence_threshold: float = 0.7,
    ) -> None:
        self._llm_provider = llm_provider
        self._tool_executor = tool_executor or ToolExecutor()
        self._confidence_threshold = confidence_threshold
        self._custom_nodes: dict[str, NodeFunc] = {}
        self._custom_edges: dict[str, str | Callable] = {}

    def set_node(self, name: str, func: NodeFunc) -> "GraphBuilder":
        """Override a standard node with a custom implementation."""
        self._custom_nodes[name] = func
        return self

    def set_edge(
        self, from_node: str, to: "str | Callable"
    ) -> "GraphBuilder":
        """Override or add an edge."""
        self._custom_edges[from_node] = to
        return self

    def build(self) -> AgentGraph:
        """Build and return the compiled LangGraph agent graph.

        Standard topology::

            ingest → reason → decide → [execute | audit]
                                         execute → [reason | audit]

        Any node or edge can be overridden via ``set_node``/``set_edge``.
        """
        # Build default nodes
        nodes: dict[str, NodeFunc] = {
            "ingest": default_ingest_node,
            "audit": default_audit_node,
        }

        # Reason node requires LLM provider
        if self._llm_provider is not None:
            nodes["reason"] = make_reason_node(self._llm_provider)
        else:
            # Passthrough reason node when no LLM configured
            async def passthrough_reason(state: dict) -> dict:
                state["current_node"] = "reason"
                state["audit_trail"].append(
                    AuditEntry(
                        timestamp=datetime.now(timezone.utc).isoformat(),
                        node="reason",
                        action="passthrough_no_llm",
                        details={},
                    )
                )
                return state

            nodes["reason"] = passthrough_reason

        nodes["decide"] = make_decide_node(self._confidence_threshold)
        nodes["execute"] = make_execute_node(self._tool_executor)

        # Apply custom node overrides
        nodes.update(self._custom_nodes)

        # Build default edges
        edges: dict[str, Any] = {
            "ingest": "reason",
            "reason": "decide",
            "decide": decide_router,
            "execute": execute_router,
            # "audit" terminates → goes to END
        }

        # Apply custom edge overrides
        edges.update(self._custom_edges)

        # Construct LangGraph StateGraph
        graph = StateGraph(dict)

        # Add all nodes
        for name, func in nodes.items():
            graph.add_node(name, func)

        # Set entry point
        graph.set_entry_point("ingest")

        # Add edges
        for from_node, target in edges.items():
            if callable(target):
                # Conditional edge: the callable returns a node name string
                possible_targets = self._get_conditional_targets(
                    from_node, target
                )
                graph.add_conditional_edges(
                    from_node, target, possible_targets
                )
            else:
                graph.add_edge(from_node, target)

        # Terminal node: audit goes to END
        graph.add_edge("audit", END)

        # Compile the graph
        compiled = graph.compile()

        return AgentGraph(
            compiled_graph=compiled,
            node_names=list(nodes.keys()),
        )

    @staticmethod
    def _get_conditional_targets(
        from_node: str,
        router: Callable,
    ) -> dict[str, str]:
        """Determine possible routing targets for conditional edges.

        Maps the string return values of router functions to node names.
        """
        if router is decide_router:
            return {"audit": "audit", "execute": "execute"}
        elif router is execute_router:
            return {"reason": "reason", "audit": "audit"}
        else:
            # For custom routers, assume they can target any standard node
            return {
                "ingest": "ingest",
                "reason": "reason",
                "decide": "decide",
                "execute": "execute",
                "audit": "audit",
            }
