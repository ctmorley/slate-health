"""Organization model — healthcare organizations / tenants."""

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.types import JSONType

from app.models.base import Base, TimestampMixin


class Organization(TimestampMixin, Base):
    __tablename__ = "organizations"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    npi: Mapped[str | None] = mapped_column(String(20), nullable=True, unique=True)
    tax_id: Mapped[str | None] = mapped_column(String(20), nullable=True)
    address: Mapped[str | None] = mapped_column(Text, nullable=True)
    settings: Mapped[dict | None] = mapped_column(JSONType, nullable=True, default=dict)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)

    users = relationship("User", back_populates="organization")
    patients = relationship("Patient", back_populates="organization")
