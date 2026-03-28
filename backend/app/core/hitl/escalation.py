"""HITL escalation — confidence threshold evaluation and review creation.

Evaluates agent confidence scores against configurable thresholds and
automatically creates HITL review items when thresholds are breached.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit.logger import AuditLogger
from app.core.engine.state import BaseAgentState
from app.core.hitl.review_queue import ReviewQueue
from app.models.agent_task import AgentTask
from app.models.hitl_review import HITLReview

logger = logging.getLogger(__name__)


# ── Default Thresholds Per Agent Type ───────────────────────────────

DEFAULT_THRESHOLDS: dict[str, float] = {
    "eligibility": 0.7,
    "scheduling": 0.7,
    "claims": 0.7,
    "prior_auth": 0.7,
    "credentialing": 0.7,
    "compliance": 0.7,
}


class EscalationConfig:
    """Configuration for HITL escalation behavior.

    Attributes:
        default_threshold: Default confidence threshold (0.0-1.0).
        agent_thresholds: Per-agent-type threshold overrides.
        auto_escalate_errors: Whether to escalate on agent errors.
    """

    def __init__(
        self,
        *,
        default_threshold: float = 0.7,
        agent_thresholds: dict[str, float] | None = None,
        auto_escalate_errors: bool = True,
    ) -> None:
        self.default_threshold = default_threshold
        # Merge DEFAULT_THRESHOLDS as a base; explicit overrides take precedence.
        merged = dict(DEFAULT_THRESHOLDS)
        if agent_thresholds:
            merged.update(agent_thresholds)
        self.agent_thresholds = merged
        self.auto_escalate_errors = auto_escalate_errors

    def get_threshold(self, agent_type: str) -> float:
        """Get the confidence threshold for an agent type."""
        return self.agent_thresholds.get(agent_type, self.default_threshold)


class EscalationManager:
    """Manages HITL escalation based on agent confidence scores.

    Evaluates whether an agent's output needs human review and creates
    the appropriate review records in the database.
    """

    def __init__(
        self,
        session: AsyncSession,
        config: EscalationConfig | None = None,
    ) -> None:
        self._session = session
        self._config = config or EscalationConfig()
        self._audit_logger = AuditLogger(session)
        self._review_queue = ReviewQueue(session)

    def should_escalate(
        self,
        *,
        confidence: float,
        agent_type: str,
        has_error: bool = False,
    ) -> tuple[bool, str]:
        """Determine if an agent result should be escalated for review.

        Args:
            confidence: Agent's confidence score (0.0-1.0).
            agent_type: Type of agent (for threshold lookup).
            has_error: Whether the agent encountered an error.

        Returns:
            Tuple of (should_escalate: bool, reason: str).
        """
        if has_error and self._config.auto_escalate_errors:
            return True, "Agent encountered an error during execution"

        threshold = self._config.get_threshold(agent_type)
        if confidence < threshold:
            return True, (
                f"Confidence {confidence:.2f} below threshold "
                f"{threshold:.2f} for {agent_type}"
            )

        return False, ""

    async def evaluate_and_escalate(
        self,
        *,
        task_id: str,
        agent_type: str,
        confidence: float,
        agent_decision: dict[str, Any] | None = None,
        has_error: bool = False,
        error_message: str | None = None,
    ) -> HITLReview | None:
        """Evaluate confidence and create a review if escalation is needed.

        Args:
            task_id: The agent_task ID.
            agent_type: Type of agent.
            confidence: Agent's confidence score.
            agent_decision: The agent's output/decision data.
            has_error: Whether an error occurred.
            error_message: Error message if applicable.

        Returns:
            HITLReview if escalated, None otherwise.
        """
        should_review, reason = self.should_escalate(
            confidence=confidence,
            agent_type=agent_type,
            has_error=has_error,
        )

        if not should_review:
            return None

        if has_error and error_message:
            reason = f"{reason}: {error_message}"

        review = await self._review_queue.create(
            task_id=task_id,
            reason=reason,
            agent_decision=agent_decision,
            confidence_score=confidence,
        )

        # Log escalation-specific audit entry (separate from the generic
        # "review created" entry that ReviewQueue.create() logs)
        await self._audit_logger.log(
            action="hitl_escalation_created",
            actor_type="agent",
            resource_type="hitl_review",
            resource_id=str(review.id),
            details={
                "task_id": str(task_id),
                "reason": reason,
                "confidence_score": confidence,
            },
        )

        # Update the task status to 'review'
        from sqlalchemy import select

        stmt = select(AgentTask).where(AgentTask.id == task_id)
        result = await self._session.execute(stmt)
        task = result.scalar_one_or_none()
        if task is not None:
            task.status = "review"
            await self._session.flush()

        return review

    async def create_review(
        self,
        *,
        task_id: str,
        reason: str,
        agent_decision: dict[str, Any] | None = None,
        confidence_score: float | None = None,
    ) -> HITLReview:
        """Create a new HITL review record.

        Args:
            task_id: The agent_task ID to review.
            reason: Why review is needed.
            agent_decision: The agent's output/decision.
            confidence_score: Agent's confidence when escalated.

        Returns:
            The created HITLReview record.
        """
        review = HITLReview(
            task_id=task_id,
            status="pending",
            reason=reason,
            agent_decision=agent_decision,
            confidence_score=confidence_score,
        )
        self._session.add(review)
        await self._session.flush()

        # Audit the escalation
        await self._audit_logger.log(
            action="hitl_escalation_created",
            actor_type="agent",
            resource_type="hitl_review",
            resource_id=str(review.id),
            details={
                "task_id": str(task_id),
                "reason": reason,
                "confidence_score": confidence_score,
            },
        )

        logger.info(
            "Created HITL review %s for task %s: %s",
            review.id,
            task_id,
            reason,
        )

        return review

    async def evaluate_state(self, state: BaseAgentState) -> HITLReview | None:
        """Convenience method to evaluate a BaseAgentState for escalation.

        Extracts the relevant fields from the state and calls
        evaluate_and_escalate().  When the state explicitly sets
        ``needs_review=True`` but confidence is above threshold, a review
        is still created (deterministic escalation).
        """
        review = await self.evaluate_and_escalate(
            task_id=state.get("task_id", ""),
            agent_type=state.get("agent_type", ""),
            confidence=state.get("confidence", 0.0),
            agent_decision=state.get("decision"),
            has_error=state.get("error") is not None,
            error_message=state.get("error"),
        )

        # If threshold-based evaluation didn't trigger but the agent
        # explicitly flagged needs_review, force-create the review.
        if review is None and state.get("needs_review", False):
            reason = state.get("review_reason", "Agent flagged needs_review=True")
            review = await self.create_review(
                task_id=state.get("task_id", ""),
                reason=reason,
                agent_decision=state.get("decision"),
                confidence_score=state.get("confidence", 0.0),
            )

            # Update the task status to 'review'
            from sqlalchemy import select as sa_select
            stmt = sa_select(AgentTask).where(
                AgentTask.id == state.get("task_id", "")
            )
            result = await self._session.execute(stmt)
            task = result.scalar_one_or_none()
            if task is not None:
                task.status = "review"
                await self._session.flush()

        return review
