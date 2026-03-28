"""Dashboard API routes — summary stats and per-agent metrics."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.jwt import TokenPayload
from app.core.auth.middleware import require_role
from app.dependencies import get_db
from app.models.agent_task import AGENT_TYPES
from app.schemas.dashboard import AgentMetrics, DashboardSummary
from app.services.dashboard_service import DashboardService

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


def _get_dashboard_service(
    session: AsyncSession = Depends(get_db),
) -> DashboardService:
    return DashboardService(session)


@router.get("/summary", response_model=DashboardSummary)
async def get_summary(
    current_user: TokenPayload = Depends(require_role("viewer")),
    service: DashboardService = Depends(_get_dashboard_service),
) -> DashboardSummary:
    """Get aggregate dashboard summary across all agent types."""
    data = await service.get_summary()
    return DashboardSummary(**data)


@router.get("/agents/{agent_type}/metrics", response_model=AgentMetrics)
async def get_agent_metrics(
    agent_type: str,
    current_user: TokenPayload = Depends(require_role("viewer")),
    service: DashboardService = Depends(_get_dashboard_service),
) -> AgentMetrics:
    """Get detailed metrics for a specific agent type."""
    if agent_type not in AGENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid agent_type '{agent_type}'.",
        )
    data = await service.get_agent_metrics(agent_type)
    return AgentMetrics(**data)
