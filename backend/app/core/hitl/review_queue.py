"""Review queue — HITL review lifecycle management.

Supports the full review lifecycle: create → assign → approve/reject/escalate
with audit trail logging for every state transition.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, and_, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit.logger import AuditLogger
from app.models.agent_task import AgentTask
from app.models.hitl_review import HITLReview

logger = logging.getLogger(__name__)


class ReviewNotFoundError(Exception):
    """Raised when a review is not found."""
    pass


class ReviewStateError(Exception):
    """Raised when a review state transition is invalid."""
    pass


class ReviewQueue:
    """Manages the HITL review queue with full lifecycle support.

    Provides methods for listing, filtering, assigning, approving,
    rejecting, and escalating reviews. Every state change is
    audit-logged for compliance.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._audit_logger = AuditLogger(session)

    async def create(
        self,
        *,
        task_id: str,
        reason: str,
        agent_decision: dict[str, Any] | None = None,
        confidence_score: float | None = None,
    ) -> HITLReview:
        """Create a new HITL review record in the queue.

        Idempotency guard: if a pending review already exists for the
        given ``task_id``, the existing review is returned instead of
        creating a duplicate.  This prevents the double-escalation issue
        where both the agent layer and the workflow service layer attempt
        to create a review for the same task.

        Args:
            task_id: The agent_task ID to review.
            reason: Why review is needed.
            agent_decision: The agent's output/decision data.
            confidence_score: Agent's confidence when escalated.

        Returns:
            The created (or existing) HITLReview record.
        """
        # Idempotency: check for an existing pending review for this task
        existing_stmt = select(HITLReview).where(
            and_(
                HITLReview.task_id == task_id,
                HITLReview.status.in_(["pending", "escalated"]),
            )
        )
        existing_result = await self._session.execute(existing_stmt)
        existing_review = existing_result.scalar_one_or_none()
        if existing_review is not None:
            logger.info(
                "Returning existing pending review %s for task %s (idempotency guard)",
                existing_review.id,
                task_id,
            )
            return existing_review

        review = HITLReview(
            task_id=task_id,
            status="pending",
            reason=reason,
            agent_decision=agent_decision,
            confidence_score=confidence_score,
        )
        self._session.add(review)
        await self._session.flush()

        await self._audit_logger.log(
            action="hitl_review_created",
            actor_type="system",
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

    async def list_reviews(
        self,
        *,
        status: str | None = None,
        agent_type: str | None = None,
        reviewer_id: str | None = None,
        task_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[HITLReview]:
        """List reviews with optional filters.

        Args:
            status: Filter by review status (pending, approved, rejected, escalated).
            agent_type: Filter by the associated task's agent type.
            reviewer_id: Filter by assigned reviewer.
            task_id: Filter by associated agent task ID.
            limit: Maximum results to return.
            offset: Number of results to skip.

        Returns:
            List of matching HITLReview records.
        """
        stmt = select(HITLReview).order_by(HITLReview.created_at.asc())

        if status is not None:
            stmt = stmt.where(HITLReview.status == status)
        if reviewer_id is not None:
            stmt = stmt.where(HITLReview.reviewer_id == reviewer_id)
        if task_id is not None:
            stmt = stmt.where(HITLReview.task_id == task_id)

        # Filter by agent_type requires joining with AgentTask
        if agent_type is not None:
            stmt = stmt.join(
                AgentTask, AgentTask.id == HITLReview.task_id
            ).where(AgentTask.agent_type == agent_type)

        stmt = stmt.offset(offset).limit(limit)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def get_review(self, review_id: str) -> HITLReview:
        """Get a single review by ID."""
        stmt = select(HITLReview).where(HITLReview.id == review_id)
        result = await self._session.execute(stmt)
        review = result.scalar_one_or_none()

        if review is None:
            raise ReviewNotFoundError(f"Review '{review_id}' not found")

        return review

    async def get_pending_count(self, agent_type: str | None = None) -> int:
        """Get count of pending reviews, optionally by agent type."""
        stmt = select(func.count(HITLReview.id)).where(
            HITLReview.status == "pending"
        )
        if agent_type is not None:
            stmt = stmt.join(
                AgentTask, AgentTask.id == HITLReview.task_id
            ).where(AgentTask.agent_type == agent_type)

        result = await self._session.execute(stmt)
        return result.scalar() or 0

    async def assign_reviewer(
        self,
        review_id: str,
        reviewer_id: str,
    ) -> HITLReview:
        """Assign a reviewer to a pending review.

        Args:
            review_id: The review to assign.
            reviewer_id: UUID of the reviewer user.

        Returns:
            Updated HITLReview record.
        """
        review = await self.get_review(review_id)

        if review.status != "pending":
            raise ReviewStateError(
                f"Cannot assign reviewer: review is '{review.status}', expected 'pending'"
            )

        review.reviewer_id = reviewer_id
        await self._session.flush()

        await self._audit_logger.log(
            action="hitl_reviewer_assigned",
            actor_id=reviewer_id,
            actor_type="user",
            resource_type="hitl_review",
            resource_id=str(review_id),
            details={"reviewer_id": str(reviewer_id)},
        )

        return review

    async def approve(
        self,
        review_id: str,
        *,
        reviewer_id: str,
        notes: str | None = None,
    ) -> HITLReview:
        """Approve a review, marking the associated task as completed.

        Args:
            review_id: The review to approve.
            reviewer_id: UUID of the approving reviewer.
            notes: Optional reviewer notes.

        Returns:
            Updated HITLReview record.
        """
        review = await self.get_review(review_id)

        if review.status not in ("pending", "escalated"):
            raise ReviewStateError(
                f"Cannot approve: review is '{review.status}', "
                f"expected 'pending' or 'escalated'"
            )

        now = datetime.now(timezone.utc)
        review.status = "approved"
        review.reviewer_id = reviewer_id
        review.reviewer_notes = notes
        review.decided_at = now
        await self._session.flush()

        # Update associated task status
        await self._update_task_status(str(review.task_id), "completed")

        await self._audit_logger.log(
            action="hitl_review_approved",
            actor_id=reviewer_id,
            actor_type="user",
            resource_type="hitl_review",
            resource_id=str(review_id),
            details={
                "task_id": str(review.task_id),
                "notes": notes,
            },
        )

        logger.info("Review %s approved by %s", review_id, reviewer_id)
        return review

    async def reject(
        self,
        review_id: str,
        *,
        reviewer_id: str,
        notes: str | None = None,
    ) -> HITLReview:
        """Reject a review, marking the associated task as failed.

        Args:
            review_id: The review to reject.
            reviewer_id: UUID of the rejecting reviewer.
            notes: Optional reviewer notes explaining rejection.

        Returns:
            Updated HITLReview record.
        """
        review = await self.get_review(review_id)

        if review.status not in ("pending", "escalated"):
            raise ReviewStateError(
                f"Cannot reject: review is '{review.status}', "
                f"expected 'pending' or 'escalated'"
            )

        now = datetime.now(timezone.utc)
        review.status = "rejected"
        review.reviewer_id = reviewer_id
        review.reviewer_notes = notes
        review.decided_at = now
        await self._session.flush()

        # Update associated task status
        await self._update_task_status(str(review.task_id), "failed")

        await self._audit_logger.log(
            action="hitl_review_rejected",
            actor_id=reviewer_id,
            actor_type="user",
            resource_type="hitl_review",
            resource_id=str(review_id),
            details={
                "task_id": str(review.task_id),
                "notes": notes,
            },
        )

        logger.info("Review %s rejected by %s", review_id, reviewer_id)
        return review

    async def escalate(
        self,
        review_id: str,
        *,
        reviewer_id: str,
        notes: str | None = None,
    ) -> HITLReview:
        """Escalate a review to a supervisor.

        Args:
            review_id: The review to escalate.
            reviewer_id: UUID of the escalating reviewer.
            notes: Optional notes explaining escalation reason.

        Returns:
            Updated HITLReview record.
        """
        review = await self.get_review(review_id)

        if review.status != "pending":
            raise ReviewStateError(
                f"Cannot escalate: review is '{review.status}', expected 'pending'"
            )

        review.status = "escalated"
        review.reviewer_id = reviewer_id
        review.reviewer_notes = notes
        await self._session.flush()

        await self._audit_logger.log(
            action="hitl_review_escalated",
            actor_id=reviewer_id,
            actor_type="user",
            resource_type="hitl_review",
            resource_id=str(review_id),
            details={
                "task_id": str(review.task_id),
                "notes": notes,
            },
        )

        logger.info("Review %s escalated by %s", review_id, reviewer_id)
        return review

    async def _update_task_status(self, task_id: str, status: str) -> None:
        """Update the status of the associated agent task."""
        stmt = select(AgentTask).where(AgentTask.id == task_id)
        result = await self._session.execute(stmt)
        task = result.scalar_one_or_none()
        if task is not None:
            task.status = status
            await self._session.flush()
