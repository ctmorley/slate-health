"""Add revoked_refresh_tokens table for token rotation and revocation.

Revision ID: 4def246g4744
Revises: 3bcd135f3633
Create Date: 2026-03-27 10:00:00.000000

Tracks consumed refresh token JTIs to prevent reuse after rotation.
Part of Sprint 6: SSO Authentication refresh token invalidation.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import app.models.types


# revision identifiers, used by Alembic.
revision: str = '4def246g4744'
down_revision: Union[str, None] = '3bcd135f3633'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('revoked_refresh_tokens',
        sa.Column('id', app.models.types.GUID(), nullable=False),
        sa.Column('jti', sa.String(length=255), nullable=False),
        sa.Column('user_id', app.models.types.GUID(), nullable=False),
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_revoked_refresh_tokens_jti'), 'revoked_refresh_tokens', ['jti'], unique=True)
    op.create_index(op.f('ix_revoked_refresh_tokens_user_id'), 'revoked_refresh_tokens', ['user_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_revoked_refresh_tokens_user_id'), table_name='revoked_refresh_tokens')
    op.drop_index(op.f('ix_revoked_refresh_tokens_jti'), table_name='revoked_refresh_tokens')
    op.drop_table('revoked_refresh_tokens')
