"""subscription paused

Revision ID: c3f5a9b8e210
Revises: 7b2f4d8a91c3
Create Date: 2026-06-16 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c3f5a9b8e210"
down_revision: Union[str, None] = "7b2f4d8a91c3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("subscriptions") as batch_op:
        batch_op.add_column(
            sa.Column(
                "paused",
                sa.Integer(),
                server_default="0",
                nullable=False,
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("subscriptions") as batch_op:
        batch_op.drop_column("paused")
