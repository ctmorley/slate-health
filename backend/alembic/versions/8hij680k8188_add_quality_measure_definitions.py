"""Add quality_measure_definitions table and seed 5 HEDIS measures.

Revision ID: 8hij680k8188
Revises: 7ghi579j7077
Create Date: 2026-03-27 18:00:00.000000

Creates a database-backed table for quality measure definitions (HEDIS,
MIPS, CMS Stars) and seeds it with 5 HEDIS measure specifications.
Part of Sprint 9: Credentialing & Compliance Agents.
"""

import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "8hij680k8188"
down_revision = "7ghi579j7077"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "quality_measure_definitions",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column("measure_id", sa.String(50), nullable=False, unique=True, index=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("measure_set", sa.String(50), nullable=False, index=True),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("denominator_criteria", sa.JSON, nullable=True),
        sa.Column("numerator_criteria", sa.JSON, nullable=True),
        sa.Column("exclusion_criteria", sa.JSON, nullable=True),
        sa.Column("target_rate", sa.Float, nullable=True),
        sa.Column("version", sa.String(20), nullable=True),
        sa.Column("active", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("(CURRENT_TIMESTAMP)")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("(CURRENT_TIMESTAMP)")),
    )

    # Seed 5 HEDIS measure definitions
    measures = [
        {
            "id": uuid.uuid4(),
            "measure_id": "BCS",
            "name": "Breast Cancer Screening",
            "measure_set": "HEDIS",
            "description": "Percentage of women 50-74 who had a mammogram in the past 2 years",
            "denominator_criteria": {
                "gender": "female",
                "age_min": 50,
                "age_max": 74,
                "continuous_enrollment_months": 12,
            },
            "numerator_criteria": {
                "procedure_codes": ["77067", "77066", "77065"],
                "lookback_months": 24,
            },
            "exclusion_criteria": {
                "diagnosis_codes": ["Z90.11", "Z90.12", "Z90.13"],
            },
            "target_rate": 0.74,
            "version": "MY2025",
            "active": True,
        },
        {
            "id": uuid.uuid4(),
            "measure_id": "CDC-HBA1C",
            "name": "Comprehensive Diabetes Care — HbA1c Testing",
            "measure_set": "HEDIS",
            "description": "Percentage of diabetic patients 18-75 who had HbA1c testing",
            "denominator_criteria": {
                "age_min": 18,
                "age_max": 75,
                "diagnosis_codes": ["E11.65", "E11.9", "E10.65", "E10.9"],
                "continuous_enrollment_months": 12,
            },
            "numerator_criteria": {
                "procedure_codes": ["83036", "83037"],
                "lookback_months": 12,
            },
            "exclusion_criteria": {},
            "target_rate": 0.86,
            "version": "MY2025",
            "active": True,
        },
        {
            "id": uuid.uuid4(),
            "measure_id": "COL",
            "name": "Colorectal Cancer Screening",
            "measure_set": "HEDIS",
            "description": "Percentage of adults 45-75 with appropriate colorectal cancer screening",
            "denominator_criteria": {
                "age_min": 45,
                "age_max": 75,
                "continuous_enrollment_months": 12,
            },
            "numerator_criteria": {
                "procedure_codes": ["45378", "45380", "45381", "45384", "45385", "82270", "81528"],
                "lookback_months": 120,
            },
            "exclusion_criteria": {
                "diagnosis_codes": ["Z90.49"],
            },
            "target_rate": 0.72,
            "version": "MY2025",
            "active": True,
        },
        {
            "id": uuid.uuid4(),
            "measure_id": "CIS-DTaP",
            "name": "Childhood Immunization — DTaP",
            "measure_set": "HEDIS",
            "description": "Percentage of children who turned 2 during measurement year with 4+ DTaP doses",
            "denominator_criteria": {
                "age_min": 2,
                "age_max": 2,
                "continuous_enrollment_months": 12,
            },
            "numerator_criteria": {
                "procedure_codes": ["90700", "90723"],
                "min_doses": 4,
                "lookback_months": 24,
            },
            "exclusion_criteria": {
                "diagnosis_codes": ["D80.0", "D80.1"],
            },
            "target_rate": 0.80,
            "version": "MY2025",
            "active": True,
        },
        {
            "id": uuid.uuid4(),
            "measure_id": "WCV",
            "name": "Well-Child Visits in the First 30 Months of Life",
            "measure_set": "HEDIS",
            "description": "Well-child visits for children in first 30 months",
            "denominator_criteria": {
                "age_min": 0,
                "age_max": 2,
                "continuous_enrollment_months": 12,
            },
            "numerator_criteria": {
                "procedure_codes": ["99381", "99382", "99391", "99392"],
                "min_visits": 6,
                "lookback_months": 30,
            },
            "exclusion_criteria": {},
            "target_rate": 0.70,
            "version": "MY2025",
            "active": True,
        },
    ]

    op.bulk_insert(
        sa.table(
            "quality_measure_definitions",
            sa.column("id", sa.dialects.postgresql.UUID(as_uuid=True)),
            sa.column("measure_id", sa.String),
            sa.column("name", sa.String),
            sa.column("measure_set", sa.String),
            sa.column("description", sa.Text),
            sa.column("denominator_criteria", sa.JSON),
            sa.column("numerator_criteria", sa.JSON),
            sa.column("exclusion_criteria", sa.JSON),
            sa.column("target_rate", sa.Float),
            sa.column("version", sa.String),
            sa.column("active", sa.Boolean),
        ),
        measures,
    )


def downgrade() -> None:
    op.drop_table("quality_measure_definitions")
