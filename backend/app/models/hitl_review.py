"""HITLReview model — human-in-the-loop review queue."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, Float, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.types import GUID, JSONType

from app.models.base import Base, TimestampMixin

REVIEW_STATUSES = ("pending", "approved", "rejected", "escalated")


class HITLReview(TimestampMixin, Base):
    __tablename__ = "hitl_reviews"

    task_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("agent_tasks.id"), nullable=False, index=True
    )
    reviewer_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(
        Enum(*REVIEW_STATUSES, name="review_status_enum"),
        nullable=False,
        default="pending",
    )
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    agent_decision: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    confidence_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    reviewer_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
