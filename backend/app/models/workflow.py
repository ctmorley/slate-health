"""WorkflowExecution model — Temporal workflow tracking."""

from sqlalchemy import Enum, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.types import JSONType

from app.models.base import Base, TimestampMixin

WORKFLOW_STATUSES = ("pending", "running", "completed", "failed", "cancelled", "timed_out")


class WorkflowExecution(TimestampMixin, Base):
    __tablename__ = "workflow_executions"

    workflow_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    run_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    agent_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        Enum(*WORKFLOW_STATUSES, name="workflow_status_enum"),
        nullable=False,
        default="pending",
    )
    task_queue: Mapped[str | None] = mapped_column(String(255), nullable=True)
    input_data: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    output_data: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
