"""Quality measure definition model — HEDIS/MIPS/CMS Stars measure specs."""

import uuid

from sqlalchemy import Float, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.types import GUID, JSONType
from app.models.base import Base, TimestampMixin


class QualityMeasureDefinition(TimestampMixin, Base):
    """Database-backed quality measure definition.

    Stores the specification for a quality measure (HEDIS, MIPS, CMS Stars)
    including denominator/numerator criteria, exclusions, and target rates.
    Seeded with 5 HEDIS measures via migration.
    """

    __tablename__ = "quality_measure_definitions"

    measure_id: Mapped[str] = mapped_column(
        String(50), nullable=False, unique=True, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    measure_set: Mapped[str] = mapped_column(
        String(50), nullable=False, index=True
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    denominator_criteria: Mapped[dict | None] = mapped_column(
        JSONType, nullable=True
    )
    numerator_criteria: Mapped[dict | None] = mapped_column(
        JSONType, nullable=True
    )
    exclusion_criteria: Mapped[dict | None] = mapped_column(
        JSONType, nullable=True
    )
    target_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    version: Mapped[str | None] = mapped_column(String(20), nullable=True)
    active: Mapped[bool] = mapped_column(default=True, nullable=False)
