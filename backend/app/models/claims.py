"""Claims models — claim records and denial tracking."""

import uuid
from decimal import Decimal

from sqlalchemy import ForeignKey, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.types import GUID, JSONType

from app.models.base import Base, TimestampMixin


class Claim(TimestampMixin, Base):
    __tablename__ = "claims"

    task_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("agent_tasks.id"), nullable=False, unique=True, index=True
    )
    patient_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("patients.id"), nullable=True
    )
    encounter_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("encounters.id"), nullable=True
    )
    payer_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("payers.id"), nullable=True
    )
    claim_type: Mapped[str] = mapped_column(String(10), nullable=False)  # 837P or 837I
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="draft")
    claim_number: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    total_charge: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    total_paid: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    patient_responsibility: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    diagnosis_codes: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    procedure_codes: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    submission_data: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    remittance_data: Mapped[dict | None] = mapped_column(JSONType, nullable=True)


class ClaimDenial(TimestampMixin, Base):
    __tablename__ = "claim_denials"

    claim_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("claims.id"), nullable=False, index=True
    )
    denial_code: Mapped[str] = mapped_column(String(20), nullable=False)
    denial_reason: Mapped[str] = mapped_column(Text, nullable=False)
    denial_category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    recommended_action: Mapped[str | None] = mapped_column(Text, nullable=True)
    appeal_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    appeal_details: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
