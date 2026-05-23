"""Add Discovery Mode columns to users

Revision ID: 9dd9ac04dcf2
Revises: d7b7df492eef
Create Date: 2026-05-21 15:52:02.014313

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9dd9ac04dcf2'
down_revision: Union[str, None] = 'd7b7df492eef'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('discovery_enabled', sa.Integer(), server_default='0', nullable=False))
        batch_op.add_column(sa.Column('discovery_min_profit_rub', sa.Integer(), server_default='7000', nullable=False))
        batch_op.add_column(sa.Column('discovery_min_profit_ratio', sa.Float(), server_default='0.2', nullable=False))
        batch_op.add_column(sa.Column('discovery_city_slug', sa.String(length=64), server_default="'rossiya'", nullable=False))


def downgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('discovery_city_slug')
        batch_op.drop_column('discovery_min_profit_ratio')
        batch_op.drop_column('discovery_min_profit_rub')
        batch_op.drop_column('discovery_enabled')
