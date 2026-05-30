"""feedback reasons and card report snapshot

Revision ID: 58d12c0f68aa
Revises: f3a1b7c9d024
Create Date: 2026-05-26 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "58d12c0f68aa"
down_revision: Union[str, None] = "f3a1b7c9d024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("card_state", sa.Column("report_json", sa.Text(), nullable=True))
    op.add_column("deal_feedback", sa.Column("reason", sa.String(length=32), nullable=True))
    op.add_column("deal_feedback", sa.Column("feature_json", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("deal_feedback", "feature_json")
    op.drop_column("deal_feedback", "reason")
    op.drop_column("card_state", "report_json")
