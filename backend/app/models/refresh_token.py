"""Refresh token session model for token rotation and revocation tracking."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin
from app.models.types import GUID


class RevokedRefreshToken(Base):
    """Tracks revoked refresh token JTIs to prevent reuse.

    When a refresh token is consumed (exchanged for a new token pair),
    its JTI is recorded here. Any subsequent attempt to use a revoked
    JTI is rejected. This prevents stolen/old refresh tokens from being
    reused after rotation.
    """

    __tablename__ = "revoked_refresh_tokens"

    id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        primary_key=True,
        default=uuid.uuid4,
    )
    jti: Mapped[str] = mapped_column(
        String(255), unique=True, nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    revoked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
