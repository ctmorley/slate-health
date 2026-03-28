"""Audit models — immutable audit logs and PHI access tracking."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.types import GUID, JSONType

from app.models.base import Base


class AuditLog(Base):
    """Immutable append-only audit log entry."""

    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        GUID(), primary_key=True, default=uuid.uuid4
    )
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), nullable=True
    )
    actor_type: Mapped[str] = mapped_column(
        String(50), nullable=False, default="system"
    )
    action: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    resource_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    resource_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    details: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    phi_accessed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)


class PHIAccessLog(Base):
    """Specific PHI access tracking for HIPAA compliance."""

    __tablename__ = "phi_access_log"

    id: Mapped[uuid.UUID] = mapped_column(
        GUID(), primary_key=True, default=uuid.uuid4
    )
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), nullable=False, index=True
    )
    patient_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), nullable=True
    )
    access_type: Mapped[str] = mapped_column(String(50), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    phi_fields_accessed: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
