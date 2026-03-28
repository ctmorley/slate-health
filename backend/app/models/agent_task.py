"""AgentTask model — individual agent task instances."""

import uuid

from sqlalchemy import Enum, Float, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.types import GUID, JSONType

from app.models.base import Base, TimestampMixin

AGENT_TYPES = (
    "eligibility",
    "scheduling",
    "claims",
    "prior_auth",
    "credentialing",
    "compliance",
)

TASK_STATUSES = ("pending", "running", "completed", "failed", "cancelled", "review")


class AgentTask(TimestampMixin, Base):
    __tablename__ = "agent_tasks"

    agent_type: Mapped[str] = mapped_column(
        Enum(*AGENT_TYPES, name="agent_type_enum"),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(
        Enum(*TASK_STATUSES, name="task_status_enum"),
        nullable=False,
        default="pending",
        index=True,
    )
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("organizations.id"), nullable=True
    )
    patient_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("patients.id"), nullable=True
    )
    input_data: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    output_data: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    workflow_execution_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(),
        ForeignKey("workflow_executions.id"),
        nullable=True,
    )
