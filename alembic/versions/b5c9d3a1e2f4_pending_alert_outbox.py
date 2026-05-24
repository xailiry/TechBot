"""Add pending alert outbox

Revision ID: b5c9d3a1e2f4
Revises: 9dd9ac04dcf2
Create Date: 2026-05-24 23:22:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b5c9d3a1e2f4"
down_revision: Union[str, None] = "9dd9ac04dcf2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "pending_alerts",
        sa.Column("tg_id", sa.BigInteger(), nullable=False),
        sa.Column("listing_id", sa.String(length=64), nullable=False),
        sa.Column("item_json", sa.Text(), nullable=False),
        sa.Column("report_json", sa.Text(), nullable=False),
        sa.Column("valuation_json", sa.Text(), nullable=False),
        sa.Column("sub_query", sa.String(length=256), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("tg_id", "listing_id"),
    )
    op.create_index(
        "ix_pendingalert_created_at",
        "pending_alerts",
        ["created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_pendingalert_created_at", table_name="pending_alerts")
    op.drop_table("pending_alerts")
