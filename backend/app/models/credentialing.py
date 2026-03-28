"""Credentialing model — provider credentialing state."""

import uuid
from datetime import date
from typing import Any

from sqlalchemy import Date, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.types import GUID, JSONType

from app.models.base import Base, TimestampMixin


class CredentialingApplication(TimestampMixin, Base):
    __tablename__ = "credentialing_applications"

    task_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("agent_tasks.id"), nullable=False, index=True
    )
    provider_npi: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    provider_name: Mapped[str] = mapped_column(String(255), nullable=False)
    target_organization: Mapped[str | None] = mapped_column(String(255), nullable=True)
    target_payer_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("payers.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="initiated")
    documents_checklist: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    # missing_documents stores {"missing": [...]} dict with a list of document names
    missing_documents: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    # licenses stores a list of license verification dicts
    licenses: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONType, nullable=True)
    sanctions_check: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    application_data: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    submitted_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    approved_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    expiration_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
