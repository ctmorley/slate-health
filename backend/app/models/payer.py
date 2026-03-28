"""Payer models — payer directory, rules, and clearinghouse configs."""

import uuid
from datetime import date

from sqlalchemy import Date, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.types import GUID, JSONType

from app.models.base import Base, TimestampMixin


class Payer(TimestampMixin, Base):
    __tablename__ = "payers"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    payer_id_code: Mapped[str] = mapped_column(
        String(50), nullable=False, unique=True, index=True
    )
    payer_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    address: Mapped[str | None] = mapped_column(Text, nullable=True)
    phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    electronic_payer_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)

    rules = relationship("PayerRule", back_populates="payer")


class PayerRule(TimestampMixin, Base):
    __tablename__ = "payer_rules"

    payer_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("payers.id"), nullable=False, index=True
    )
    agent_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    rule_type: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    conditions: Mapped[dict] = mapped_column(JSONType, nullable=False)
    actions: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    termination_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)

    payer = relationship("Payer", back_populates="rules")


class ClearinghouseConfig(TimestampMixin, Base):
    __tablename__ = "clearinghouse_configs"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("organizations.id"), nullable=False, index=True
    )
    clearinghouse_name: Mapped[str] = mapped_column(String(100), nullable=False)
    api_endpoint: Mapped[str] = mapped_column(String(500), nullable=False)
    credentials: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
    supported_transactions: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
