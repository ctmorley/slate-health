"""Prior authorization models — PA requests and appeals."""

import uuid
from datetime import date

from sqlalchemy import Date, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.types import GUID, JSONType

from app.models.base import Base, TimestampMixin


class PriorAuthRequest(TimestampMixin, Base):
    __tablename__ = "prior_auth_requests"

    task_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("agent_tasks.id"), nullable=False, index=True
    )
    patient_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("patients.id"), nullable=False
    )
    payer_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("payers.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="pending")
    procedure_code: Mapped[str] = mapped_column(String(20), nullable=False)
    diagnosis_codes: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    clinical_info: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    submission_channel: Mapped[str | None] = mapped_column(String(50), nullable=True)
    auth_number: Mapped[str | None] = mapped_column(String(100), nullable=True)
    determination: Mapped[str | None] = mapped_column(String(30), nullable=True)
    effective_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    expiration_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    request_data_278: Mapped[dict | None] = mapped_column(JSONType, nullable=True)


class PriorAuthAppeal(TimestampMixin, Base):
    __tablename__ = "prior_auth_appeals"

    prior_auth_id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        ForeignKey("prior_auth_requests.id"),
        nullable=False,
        index=True,
    )
    appeal_level: Mapped[int] = mapped_column(nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="draft")
    appeal_letter: Mapped[str | None] = mapped_column(Text, nullable=True)
    clinical_evidence: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(30), nullable=True)
    outcome_details: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
