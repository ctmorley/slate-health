"""Add oidc_state_entries table for DB-backed OIDC CSRF state.

Revision ID: 5efg357h5855
Revises: 4def246g4744
Create Date: 2026-03-27 12:00:00.000000

Persists OIDC authorization flow state/nonce to the database so they
survive process restarts and are accessible across multiple workers,
replacing the previous in-memory-only store.
Part of Sprint 6: SSO Authentication hardening (Iteration 4).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import app.models.types


# revision identifiers, used by Alembic.
revision: str = '5efg357h5855'
down_revision: Union[str, None] = '4def246g4744'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('oidc_state_entries',
        sa.Column('id', app.models.types.GUID(), nullable=False),
        sa.Column('state', sa.String(length=255), nullable=False),
        sa.Column('nonce', sa.String(length=255), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_oidc_state_entries_state'), 'oidc_state_entries', ['state'], unique=True)
    op.create_index('ix_oidc_state_entries_created_at', 'oidc_state_entries', ['created_at'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_oidc_state_entries_created_at', table_name='oidc_state_entries')
    op.drop_index(op.f('ix_oidc_state_entries_state'), table_name='oidc_state_entries')
    op.drop_table('oidc_state_entries')
