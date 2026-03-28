"""Patient and Encounter models."""

import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.types import GUID, JSONType

from app.models.base import Base, TimestampMixin


class Patient(TimestampMixin, Base):
    __tablename__ = "patients"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("organizations.id"), nullable=False
    )
    mrn: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    external_ehr_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    last_name: Mapped[str] = mapped_column(String(100), nullable=False)
    date_of_birth: Mapped[date] = mapped_column(Date, nullable=False)
    gender: Mapped[str | None] = mapped_column(String(20), nullable=True)
    insurance_member_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    insurance_group_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    payer_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("payers.id"), nullable=True
    )

    organization = relationship("Organization", back_populates="patients")
    encounters = relationship("Encounter", back_populates="patient")


class Encounter(TimestampMixin, Base):
    __tablename__ = "encounters"

    patient_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("patients.id"), nullable=False
    )
    encounter_type: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="active")
    encounter_date: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    provider_npi: Mapped[str | None] = mapped_column(String(20), nullable=True)
    facility_npi: Mapped[str | None] = mapped_column(String(20), nullable=True)
    diagnosis_codes: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    procedure_codes: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    patient = relationship("Patient", back_populates="encounters")
