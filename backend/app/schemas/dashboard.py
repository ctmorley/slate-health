"""Pydantic schemas for dashboard summary and metrics."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.schemas.agent import AgentStatsResponse


class RecentTaskSummary(BaseModel):
    """Lightweight task summary for the dashboard activity feed."""

    id: str
    task_id: str
    agent_type: str
    status: str
    confidence_score: float | None = None
    created_at: str | None = None
    updated_at: str | None = None


class DashboardSummary(BaseModel):
    """Aggregate dashboard summary across all agents."""

    total_tasks: int = 0
    pending: int = 0
    running: int = 0
    completed: int = 0
    failed: int = 0
    in_review: int = 0
    cancelled: int = 0
    agents: list[AgentStatsResponse] = Field(default_factory=list)
    recent_tasks: list[RecentTaskSummary] = Field(default_factory=list)


class AgentMetrics(BaseModel):
    """Per-agent metrics for a given time window."""

    agent_type: str
    total_tasks: int = 0
    completed: int = 0
    failed: int = 0
    avg_confidence: float | None = None
    avg_processing_time_seconds: float | None = None
    tasks_by_day: list[dict[str, Any]] = Field(default_factory=list)
