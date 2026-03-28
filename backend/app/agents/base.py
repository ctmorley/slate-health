"""Base agent class — abstract interface for all 6 agents.

Each agent implements build_graph(), get_tools(), and optionally overrides
run() to customize execution behavior. The base class provides common
infrastructure: graph building, tool registration, audit trail management,
and HITL escalation integration.
"""

from __future__ import annotations

import logging
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit.logger import AuditLogger
from app.core.engine.graph_builder import AgentGraph, GraphBuilder
from app.core.engine.llm_provider import LLMProvider
from app.core.engine.state import (
    BaseAgentState,
    PatientContext,
    PayerContext,
    ToolCall,
    create_initial_state,
)
from app.core.engine.tool_executor import ToolDefinition, ToolExecutor
from app.core.hitl.escalation import EscalationManager

logger = logging.getLogger(__name__)


class BaseAgent(ABC):
    """Abstract base class for all Slate Health agents.

    Subclasses must implement:
    - build_graph(): Construct the agent's LangGraph workflow
    - get_tools(): Return tool definitions available to the agent

    The base class provides:
    - run(): Execute the agent graph with full lifecycle management
    - _create_audit_logger(): Create an audit logger for the session
    - _register_tools(): Register tools with the executor
    """

    # Subclasses set this to their agent type string
    agent_type: str = ""

    # Default confidence threshold for HITL escalation
    confidence_threshold: float = 0.7

    # Maximum reasoning iterations
    max_iterations: int = 10

    def __init__(
        self,
        *,
        llm_provider: LLMProvider,
        session: AsyncSession | None = None,
    ) -> None:
        self._llm_provider = llm_provider
        self._session = session
        self._tool_executor = ToolExecutor()

        # Register agent-specific tools
        for tool_def in self.get_tools():
            self._tool_executor.register_tool(tool_def)

    @abstractmethod
    def get_tools(self) -> list[ToolDefinition]:
        """Return the list of tools available to this agent.

        Each tool is a ToolDefinition with name, schema, and handler.
        """
        ...

    @abstractmethod
    def build_graph(self) -> AgentGraph:
        """Build and return the agent's execution graph.

        Uses GraphBuilder to create a customized graph with agent-specific
        nodes and edges.
        """
        ...

    def _build_default_graph(self) -> AgentGraph:
        """Build a graph with the standard topology and this agent's config.

        Convenience method for agents that use the default node implementations
        with custom tools and confidence threshold.
        """
        builder = GraphBuilder(
            llm_provider=self._llm_provider,
            tool_executor=self._tool_executor,
            confidence_threshold=self.confidence_threshold,
        )
        return builder.build()

    async def run(
        self,
        *,
        task_id: str | None = None,
        input_data: dict[str, Any] | None = None,
        patient_context: PatientContext | None = None,
        payer_context: PayerContext | None = None,
    ) -> BaseAgentState:
        """Execute the agent graph and return the final state.

        This is the main entry point for running an agent. It:
        1. Creates initial state from inputs
        2. Builds the agent graph
        3. Runs the graph to completion
        4. Logs audit entries to the database (if session available)

        Args:
            task_id: The agent_task ID for this execution.
            input_data: Raw input data for the agent.
            patient_context: Patient demographics and insurance info.
            payer_context: Payer details and applicable rules.

        Returns:
            Final BaseAgentState after graph execution.
        """
        effective_task_id = task_id or str(uuid.uuid4())

        state = create_initial_state(
            task_id=effective_task_id,
            agent_type=self.agent_type,
            input_data=input_data,
            patient_context=patient_context,
            payer_context=payer_context,
            max_iterations=self.max_iterations,
        )

        # Expose db_session in state so graph nodes can use it
        # (e.g. payer rule engine lookups in check_pa_required_node)
        if self._session is not None:
            state["db_session"] = self._session

        # Build and run graph
        graph = self.build_graph()

        try:
            state = await graph.run(state)
        except Exception as exc:
            logger.error(
                "Agent '%s' task '%s' failed: %s",
                self.agent_type,
                effective_task_id,
                exc,
            )
            state["error"] = str(exc)

        # Persist audit trail and handle HITL escalation if we have a DB session
        if self._session is not None:
            await self._persist_audit_trail(state)
            await self._evaluate_escalation(state)

        return state

    async def _persist_audit_trail(self, state: BaseAgentState) -> None:
        """Write audit trail entries from the state to the database."""
        try:
            audit_logger = AuditLogger(self._session)
            for entry in state.get("audit_trail", []):
                await audit_logger.log(
                    action=f"agent:{self.agent_type}:{entry.get('action', 'unknown')}",
                    actor_type="agent",
                    resource_type="agent_task",
                    resource_id=state.get("task_id", ""),
                    details={
                        "node": entry.get("node", ""),
                        **entry.get("details", {}),
                    },
                )
        except Exception as exc:
            logger.error("Failed to persist audit trail: %s", exc)

    async def _evaluate_escalation(self, state: BaseAgentState) -> None:
        """Evaluate the agent state for HITL escalation and create a review if needed.

        Called automatically after graph execution when a database session is
        available.  If the state indicates low confidence or an error, an
        EscalationManager creates a HITLReview record and updates the task
        status to 'review'.
        """
        needs_review = state.get("needs_review", False)
        has_error = state.get("error") is not None

        if not needs_review and not has_error:
            return

        try:
            escalation_mgr = EscalationManager(self._session)
            review = await escalation_mgr.evaluate_state(state)
            if review is not None:
                logger.info(
                    "Agent '%s' task '%s' escalated to HITL review '%s'",
                    self.agent_type,
                    state.get("task_id", ""),
                    review.id,
                )
        except Exception as exc:
            logger.error("Failed to evaluate escalation: %s", exc)
