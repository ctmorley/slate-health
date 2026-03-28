"""Compliance report model — HEDIS/MIPS/CMS Stars records."""

import uuid
from typing import Any

from sqlalchemy import Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.types import GUID, JSONType

from app.models.base import Base, TimestampMixin


class ComplianceReport(TimestampMixin, Base):
    __tablename__ = "compliance_reports"

    task_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("agent_tasks.id"), nullable=False, index=True
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("organizations.id"), nullable=False
    )
    measure_set: Mapped[str] = mapped_column(String(50), nullable=False)
    reporting_period_start: Mapped[str] = mapped_column(String(10), nullable=False)
    reporting_period_end: Mapped[str] = mapped_column(String(10), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="pending")
    measure_scores: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    overall_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    gaps_identified: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # gap_details stores a list of gap records (one per patient per measure)
    gap_details: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONType, nullable=True)
    # recommendations stores a list of remediation recommendation dicts
    recommendations: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONType, nullable=True)
    report_data: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
