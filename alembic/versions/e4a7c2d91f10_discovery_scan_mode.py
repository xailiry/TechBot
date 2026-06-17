"""discovery scan mode

Revision ID: e4a7c2d91f10
Revises: ca481baf9b42
Create Date: 2026-06-15 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e4a7c2d91f10"
down_revision: Union[str, None] = "ca481baf9b42"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(
            sa.Column(
                "discovery_scan_mode",
                sa.String(length=16),
                server_default="fast",
                nullable=False,
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("discovery_scan_mode")
