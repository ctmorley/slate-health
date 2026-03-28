"""Review service — business logic for HITL review operations.

Wraps the ReviewQueue with additional functionality for the API layer
including counting and filtering.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.hitl.review_queue import ReviewNotFoundError, ReviewQueue, ReviewStateError
from app.models.agent_task import AgentTask
from app.models.hitl_review import HITLReview

logger = logging.getLogger(__name__)


class ReviewService:
    """Service for managing HITL reviews via the API."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._queue = ReviewQueue(session)

    async def list_reviews(
        self,
        *,
        status: str | None = None,
        agent_type: str | None = None,
        task_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[HITLReview], int]:
        """List reviews with pagination and optional filters."""
        reviews = await self._queue.list_reviews(
            status=status,
            agent_type=agent_type,
            task_id=task_id,
            limit=limit,
            offset=offset,
        )

        # Count total matching reviews
        count_query = select(func.count()).select_from(HITLReview)
        if status:
            count_query = count_query.where(HITLReview.status == status)
        if agent_type:
            count_query = count_query.join(
                AgentTask, AgentTask.id == HITLReview.task_id
            ).where(AgentTask.agent_type == agent_type)
        if task_id:
            count_query = count_query.where(HITLReview.task_id == task_id)

        count_result = await self._session.execute(count_query)
        total = count_result.scalar() or 0

        return reviews, total

    async def get_review(self, review_id: str) -> HITLReview:
        """Get a single review by ID. Raises ReviewNotFoundError if not found."""
        return await self._queue.get_review(review_id)

    async def approve(
        self,
        review_id: str,
        *,
        reviewer_id: str,
        notes: str | None = None,
    ) -> HITLReview:
        """Approve a review."""
        return await self._queue.approve(
            review_id,
            reviewer_id=reviewer_id,
            notes=notes,
        )

    async def reject(
        self,
        review_id: str,
        *,
        reviewer_id: str,
        notes: str | None = None,
    ) -> HITLReview:
        """Reject a review."""
        return await self._queue.reject(
            review_id,
            reviewer_id=reviewer_id,
            notes=notes,
        )

    async def escalate(
        self,
        review_id: str,
        *,
        reviewer_id: str,
        notes: str | None = None,
    ) -> HITLReview:
        """Escalate a review to supervisor."""
        return await self._queue.escalate(
            review_id,
            reviewer_id=reviewer_id,
            notes=notes,
        )
