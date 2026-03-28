"""Scheduling request model — appointment scheduling records."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.types import GUID, JSONType

from app.models.base import Base, TimestampMixin


class SchedulingRequest(TimestampMixin, Base):
    __tablename__ = "scheduling_requests"

    task_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("agent_tasks.id"), nullable=False, unique=True, index=True
    )
    patient_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("patients.id"), nullable=True
    )
    request_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    parsed_intent: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    provider_npi: Mapped[str | None] = mapped_column(String(20), nullable=True)
    specialty: Mapped[str | None] = mapped_column(String(100), nullable=True)
    preferred_date_start: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    preferred_date_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="pending")
    appointment_fhir_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    appointment_details: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
