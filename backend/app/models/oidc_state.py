"""OIDC state/nonce entries for CSRF protection across restarts and workers.

Stores OIDC authorization flow state parameters in the database so they
survive process restarts and are accessible across multiple workers,
replacing the previous in-memory store.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, Index
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.types import GUID


class OIDCStateEntry(Base):
    """Persisted OIDC state+nonce for CSRF and replay protection.

    Created when a login flow is initiated (POST /login with provider=oidc),
    consumed (deleted) when the callback validates the state parameter.
    Entries have a TTL and are cleaned up on access.
    """

    __tablename__ = "oidc_state_entries"

    id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        primary_key=True,
        default=uuid.uuid4,
    )
    state: Mapped[str] = mapped_column(
        String(255), unique=True, nullable=False, index=True
    )
    nonce: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        Index("ix_oidc_state_entries_created_at", "created_at"),
    )
