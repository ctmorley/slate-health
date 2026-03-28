"""Eligibility check model — 270/271 transaction records."""

import uuid

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.types import GUID, JSONType

from app.models.base import Base, TimestampMixin


class EligibilityCheck(TimestampMixin, Base):
    __tablename__ = "eligibility_checks"

    task_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("agent_tasks.id"), nullable=False, index=True
    )
    patient_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("patients.id"), nullable=True
    )
    payer_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("payers.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="pending")
    request_data: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    response_data: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    coverage_active: Mapped[bool | None] = mapped_column(nullable=True)
    coverage_details: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    transaction_id_270: Mapped[str | None] = mapped_column(String(100), nullable=True)
    transaction_id_271: Mapped[str | None] = mapped_column(String(100), nullable=True)
