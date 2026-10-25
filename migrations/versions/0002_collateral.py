"""collateral kind and value

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-25 19:00:00
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('applications', sa.Column('collateral_kind', sa.String(length=20), nullable=True))
    op.add_column('applications', sa.Column('collateral_value', sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column('applications', 'collateral_value')
    op.drop_column('applications', 'collateral_kind')
