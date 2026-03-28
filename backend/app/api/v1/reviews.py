"""HITL review API routes — list pending, approve, reject, escalate."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy import select

from app.core.auth.jwt import TokenPayload
from app.core.auth.middleware import require_role
from app.core.hitl.review_queue import ReviewNotFoundError, ReviewStateError
from app.dependencies import get_db
from app.models.agent_task import AgentTask
from app.models.hitl_review import HITLReview
from app.schemas.review import ReviewActionRequest, ReviewList, ReviewResponse
from app.services.review_service import ReviewService

router = APIRouter(prefix="/reviews", tags=["reviews"])


def _review_to_response(
    review: HITLReview,
    agent_type: str | None = None,
    patient_id: object | None = None,
) -> ReviewResponse:
    """Convert ORM HITLReview to Pydantic response, avoiding lazy loads.

    ``agent_type`` and ``patient_id`` are supplied by the caller after
    joining to the ``agent_tasks`` table so that the frontend can render
    them as first-class columns without relying on the opaque
    ``agent_decision`` JSON blob.
    """
    from sqlalchemy import inspect as sa_inspect

    d = sa_inspect(review).dict
    return ReviewResponse(
        id=d.get("id", review.id),
        task_id=d.get("task_id"),
        reviewer_id=d.get("reviewer_id"),
        status=d.get("status", "pending"),
        reason=d.get("reason", ""),
        agent_decision=d.get("agent_decision"),
        confidence_score=d.get("confidence_score"),
        reviewer_notes=d.get("reviewer_notes"),
        decided_at=d.get("decided_at"),
        created_at=d.get("created_at"),
        updated_at=d.get("updated_at"),
        agent_type=agent_type,
        patient_id=patient_id,
    )


def _get_review_service(session: AsyncSession = Depends(get_db)) -> ReviewService:
    return ReviewService(session)


@router.get("", response_model=ReviewList)
async def list_reviews(
    status_filter: str | None = None,
    agent_type: str | None = None,
    task_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
    current_user: TokenPayload = Depends(require_role("viewer")),
    service: ReviewService = Depends(_get_review_service),
    session: AsyncSession = Depends(get_db),
) -> ReviewList:
    """List HITL reviews with optional filters and pagination."""
    effective_limit = min(limit, 100)
    reviews, total = await service.list_reviews(
        status=status_filter,
        agent_type=agent_type,
        task_id=task_id,
        limit=effective_limit,
        offset=offset,
    )

    # Batch-fetch linked AgentTask rows so we can populate agent_type / patient_id
    task_ids = [r.task_id for r in reviews]
    task_map: dict[str, AgentTask] = {}
    if task_ids:
        result = await session.execute(
            select(AgentTask).where(AgentTask.id.in_(task_ids))
        )
        for t in result.scalars():
            task_map[str(t.id)] = t

    items: list[ReviewResponse] = []
    for r in reviews:
        task = task_map.get(str(r.task_id))
        items.append(
            _review_to_response(
                r,
                agent_type=task.agent_type if task else None,
                patient_id=task.patient_id if task else None,
            )
        )

    return ReviewList(
        items=items,
        total=total,
        limit=effective_limit,
        offset=offset,
    )


@router.get("/{review_id}", response_model=ReviewResponse)
async def get_review(
    review_id: uuid.UUID,
    current_user: TokenPayload = Depends(require_role("viewer")),
    service: ReviewService = Depends(_get_review_service),
    session: AsyncSession = Depends(get_db),
) -> ReviewResponse:
    """Get a single review by ID."""
    try:
        review = await service.get_review(str(review_id))
    except ReviewNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Review '{review_id}' not found",
        )
    task_result = await session.execute(
        select(AgentTask).where(AgentTask.id == review.task_id)
    )
    task = task_result.scalar_one_or_none()
    return _review_to_response(
        review,
        agent_type=task.agent_type if task else None,
        patient_id=task.patient_id if task else None,
    )


async def _enrich_review(review: HITLReview, session: AsyncSession) -> ReviewResponse:
    """Resolve the linked AgentTask and build a ReviewResponse with first-class fields."""
    task_result = await session.execute(
        select(AgentTask).where(AgentTask.id == review.task_id)
    )
    task = task_result.scalar_one_or_none()
    return _review_to_response(
        review,
        agent_type=task.agent_type if task else None,
        patient_id=task.patient_id if task else None,
    )


@router.post("/{review_id}/approve", response_model=ReviewResponse)
async def approve_review(
    review_id: uuid.UUID,
    body: ReviewActionRequest | None = None,
    current_user: TokenPayload = Depends(require_role("reviewer")),
    service: ReviewService = Depends(_get_review_service),
    session: AsyncSession = Depends(get_db),
) -> ReviewResponse:
    """Approve a pending review."""
    notes = body.notes if body else None
    try:
        review = await service.approve(
            str(review_id),
            reviewer_id=str(current_user.user_id),
            notes=notes,
        )
    except ReviewNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Review '{review_id}' not found",
        )
    except ReviewStateError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    return await _enrich_review(review, session)


@router.post("/{review_id}/reject", response_model=ReviewResponse)
async def reject_review(
    review_id: uuid.UUID,
    body: ReviewActionRequest | None = None,
    current_user: TokenPayload = Depends(require_role("reviewer")),
    service: ReviewService = Depends(_get_review_service),
    session: AsyncSession = Depends(get_db),
) -> ReviewResponse:
    """Reject a pending review."""
    notes = body.notes if body else None
    try:
        review = await service.reject(
            str(review_id),
            reviewer_id=str(current_user.user_id),
            notes=notes,
        )
    except ReviewNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Review '{review_id}' not found",
        )
    except ReviewStateError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    return await _enrich_review(review, session)


@router.post("/{review_id}/escalate", response_model=ReviewResponse)
async def escalate_review(
    review_id: uuid.UUID,
    body: ReviewActionRequest | None = None,
    current_user: TokenPayload = Depends(require_role("reviewer")),
    service: ReviewService = Depends(_get_review_service),
    session: AsyncSession = Depends(get_db),
) -> ReviewResponse:
    """Escalate a review to a supervisor."""
    notes = body.notes if body else None
    try:
        review = await service.escalate(
            str(review_id),
            reviewer_id=str(current_user.user_id),
            notes=notes,
        )
    except ReviewNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Review '{review_id}' not found",
        )
    except ReviewStateError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    return await _enrich_review(review, session)
