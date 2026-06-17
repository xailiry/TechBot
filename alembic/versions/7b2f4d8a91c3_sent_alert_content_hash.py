"""sent alert content hash

Revision ID: 7b2f4d8a91c3
Revises: e4a7c2d91f10
Create Date: 2026-06-15 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "7b2f4d8a91c3"
down_revision: Union[str, None] = "e4a7c2d91f10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "sent_alerts",
        sa.Column("content_hash", sa.String(length=64), nullable=True),
    )
    op.execute(
        """
        UPDATE sent_alerts
        SET content_hash = (
            SELECT listings.content_hash
            FROM listings
            WHERE listings.id = sent_alerts.listing_id
        )
        WHERE content_hash IS NULL
        """
    )
    op.create_index(
        "ix_sentalert_tg_content",
        "sent_alerts",
        ["tg_id", "content_hash"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_sentalert_tg_content", table_name="sent_alerts")
    op.drop_column("sent_alerts", "content_hash")
