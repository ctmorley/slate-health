"""User model — SSO-linked user accounts with roles."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.types import GUID

from app.models.base import Base, TimestampMixin


class User(TimestampMixin, Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(
        Enum("admin", "reviewer", "viewer", name="user_role"),
        nullable=False,
        default="viewer",
    )
    sso_provider: Mapped[str | None] = mapped_column(String(50), nullable=True)
    sso_subject_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(),
        ForeignKey("organizations.id"),
        nullable=True,
    )
    last_login: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)

    organization = relationship("Organization", back_populates="users")
