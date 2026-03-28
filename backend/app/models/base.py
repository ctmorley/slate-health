"""SQLAlchemy base model with common fields."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.models.types import GUID


class Base(DeclarativeBase):
    """Declarative base for all models."""
    pass


class TimestampMixin:
    """Mixin providing id, created_at, and updated_at columns."""

    id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        primary_key=True,
        default=uuid.uuid4,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
