"""Add unique constraint on task_id for claims and scheduling_requests.

Revision ID: 7ghi579j7077
Revises: 6fgh468i6966
Create Date: 2026-03-27 16:00:00.000000

Prevents duplicate records per task on retries/replays by enforcing
a unique constraint on task_id for one-record-per-task tables.
Part of Sprint 7: Scheduling & Claims Agents (Iteration 7).
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = '7ghi579j7077'
down_revision = '6fgh468i6966'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('claims') as batch_op:
        batch_op.create_unique_constraint('uq_claims_task_id', ['task_id'])
    with op.batch_alter_table('scheduling_requests') as batch_op:
        batch_op.create_unique_constraint('uq_scheduling_requests_task_id', ['task_id'])


def downgrade() -> None:
    with op.batch_alter_table('scheduling_requests') as batch_op:
        batch_op.drop_constraint('uq_scheduling_requests_task_id', type_='unique')
    with op.batch_alter_table('claims') as batch_op:
        batch_op.drop_constraint('uq_claims_task_id', type_='unique')
