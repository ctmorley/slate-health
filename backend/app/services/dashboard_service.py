"""Dashboard service — aggregate stats and per-agent metrics.

Provides summary data for the monitoring dashboard, including task counts
across all agents and per-agent performance metrics.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import cast, Date, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_task import AGENT_TYPES, AgentTask

logger = logging.getLogger(__name__)


class DashboardService:
    """Service for computing dashboard summary and agent metrics."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_summary(self) -> dict[str, Any]:
        """Get aggregate dashboard summary across all agent types."""
        # Total counts by status
        status_counts: dict[str, int] = {}
        for status_val in ("pending", "running", "completed", "failed", "review", "cancelled"):
            result = await self._session.execute(
                select(func.count()).select_from(AgentTask).where(
                    AgentTask.status == status_val
                )
            )
            status_counts[status_val] = result.scalar() or 0

        total_result = await self._session.execute(
            select(func.count()).select_from(AgentTask)
        )
        total = total_result.scalar() or 0

        # Per-agent stats
        agents = []
        for agent_type in AGENT_TYPES:
            agent_total_result = await self._session.execute(
                select(func.count()).select_from(AgentTask).where(
                    AgentTask.agent_type == agent_type
                )
            )
            agent_total = agent_total_result.scalar() or 0

            agent_stats: dict[str, int] = {}
            for status_val in ("pending", "running", "completed", "failed", "review", "cancelled"):
                r = await self._session.execute(
                    select(func.count()).select_from(AgentTask).where(
                        AgentTask.agent_type == agent_type,
                        AgentTask.status == status_val,
                    )
                )
                agent_stats[status_val] = r.scalar() or 0

            avg_result = await self._session.execute(
                select(func.avg(AgentTask.confidence_score)).where(
                    AgentTask.agent_type == agent_type,
                    AgentTask.status == "completed",
                    AgentTask.confidence_score.isnot(None),
                )
            )
            avg_conf = avg_result.scalar()

            agents.append({
                "agent_type": agent_type,
                "total_tasks": agent_total,
                "pending": agent_stats.get("pending", 0),
                "running": agent_stats.get("running", 0),
                "completed": agent_stats.get("completed", 0),
                "failed": agent_stats.get("failed", 0),
                "in_review": agent_stats.get("review", 0),
                "cancelled": agent_stats.get("cancelled", 0),
                "avg_confidence": round(avg_conf, 3) if avg_conf is not None else None,
            })

        # Fetch 20 most recent tasks for the activity feed
        recent_query = (
            select(AgentTask)
            .order_by(AgentTask.updated_at.desc().nullslast(), AgentTask.created_at.desc())
            .limit(20)
        )
        recent_result = await self._session.execute(recent_query)
        recent_rows = recent_result.scalars().all()
        recent_tasks = [
            {
                "id": str(task.id),
                "task_id": str(task.id),
                "agent_type": task.agent_type,
                "status": task.status,
                "confidence_score": task.confidence_score,
                "created_at": task.created_at.isoformat() if task.created_at else None,
                "updated_at": task.updated_at.isoformat() if task.updated_at else None,
            }
            for task in recent_rows
        ]

        return {
            "total_tasks": total,
            "pending": status_counts.get("pending", 0),
            "running": status_counts.get("running", 0),
            "completed": status_counts.get("completed", 0),
            "failed": status_counts.get("failed", 0),
            "in_review": status_counts.get("review", 0),
            "cancelled": status_counts.get("cancelled", 0),
            "agents": agents,
            "recent_tasks": recent_tasks,
        }

    async def get_agent_metrics(self, agent_type: str) -> dict[str, Any]:
        """Get detailed metrics for a specific agent type."""
        total_result = await self._session.execute(
            select(func.count()).select_from(AgentTask).where(
                AgentTask.agent_type == agent_type
            )
        )
        total = total_result.scalar() or 0

        completed_result = await self._session.execute(
            select(func.count()).select_from(AgentTask).where(
                AgentTask.agent_type == agent_type,
                AgentTask.status == "completed",
            )
        )
        completed = completed_result.scalar() or 0

        failed_result = await self._session.execute(
            select(func.count()).select_from(AgentTask).where(
                AgentTask.agent_type == agent_type,
                AgentTask.status == "failed",
            )
        )
        failed = failed_result.scalar() or 0

        avg_result = await self._session.execute(
            select(func.avg(AgentTask.confidence_score)).where(
                AgentTask.agent_type == agent_type,
                AgentTask.status == "completed",
                AgentTask.confidence_score.isnot(None),
            )
        )
        avg_conf = avg_result.scalar()

        # Aggregate tasks per day for the last 7 days
        today = date.today()
        seven_days_ago = today - timedelta(days=6)
        day_query = (
            select(
                cast(AgentTask.created_at, Date).label("task_date"),
                func.count().label("task_count"),
            )
            .where(
                AgentTask.agent_type == agent_type,
                cast(AgentTask.created_at, Date) >= seven_days_ago,
            )
            .group_by(cast(AgentTask.created_at, Date))
            .order_by(cast(AgentTask.created_at, Date))
        )
        day_result = await self._session.execute(day_query)
        day_rows = {row.task_date: row.task_count for row in day_result}

        # Build complete 7-day series (fill in zeros for missing days)
        tasks_by_day: list[dict[str, Any]] = []
        for i in range(7):
            d = seven_days_ago + timedelta(days=i)
            tasks_by_day.append({
                "date": d.isoformat(),
                "count": day_rows.get(d, 0),
            })

        # Compute average processing time for completed tasks
        avg_time_result = await self._session.execute(
            select(
                func.avg(
                    func.extract("epoch", AgentTask.updated_at)
                    - func.extract("epoch", AgentTask.created_at)
                )
            ).where(
                AgentTask.agent_type == agent_type,
                AgentTask.status == "completed",
                AgentTask.updated_at.isnot(None),
            )
        )
        avg_time = avg_time_result.scalar()

        return {
            "agent_type": agent_type,
            "total_tasks": total,
            "completed": completed,
            "failed": failed,
            "avg_confidence": round(avg_conf, 3) if avg_conf is not None else None,
            "avg_processing_time_seconds": round(avg_time, 2) if avg_time is not None else None,
            "tasks_by_day": tasks_by_day,
        }
