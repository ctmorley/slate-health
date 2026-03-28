"""Make patient_id nullable on claims and scheduling_requests tables.

Revision ID: 6fgh468i6966
Revises: 5efg357h5855
Create Date: 2026-03-27 14:00:00.000000

Removes the NOT NULL constraint on patient_id for claims and
scheduling_requests tables. This prevents FK integrity failures when
agent workflows create records before a patient has been resolved
in the system. Patient identity can be linked later.
Part of Sprint 7: Scheduling & Claims Agents (Iteration 4).
"""
from alembic import op
import sqlalchemy as sa
import app.models.types

# revision identifiers, used by Alembic.
revision = '6fgh468i6966'
down_revision = '5efg357h5855'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Use batch mode for SQLite compatibility (ALTER COLUMN not supported)
    with op.batch_alter_table('claims') as batch_op:
        batch_op.alter_column('patient_id',
                              existing_type=app.models.types.GUID(),
                              nullable=True)
    with op.batch_alter_table('scheduling_requests') as batch_op:
        batch_op.alter_column('patient_id',
                              existing_type=app.models.types.GUID(),
                              nullable=True)


def downgrade() -> None:
    with op.batch_alter_table('scheduling_requests') as batch_op:
        batch_op.alter_column('patient_id',
                              existing_type=app.models.types.GUID(),
                              nullable=False)
    with op.batch_alter_table('claims') as batch_op:
        batch_op.alter_column('patient_id',
                              existing_type=app.models.types.GUID(),
                              nullable=False)
