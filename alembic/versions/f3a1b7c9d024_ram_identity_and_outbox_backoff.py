"""RAM-aware device identity and pending-alert backoff

Revision ID: f3a1b7c9d024
Revises: b5c9d3a1e2f4
Create Date: 2026-05-25 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f3a1b7c9d024"
down_revision: Union[str, None] = "b5c9d3a1e2f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("device_catalog") as batch:
        batch.drop_constraint("uq_device_identity", type_="unique")
        batch.create_unique_constraint(
            "uq_device_identity",
            ["brand", "model", "storage_gb", "ram_gb"],
        )

    op.add_column(
        "pending_alerts",
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "pending_alerts",
        sa.Column("dead_reason", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_pendingalert_next_attempt",
        "pending_alerts",
        ["next_attempt_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_pendingalert_next_attempt", table_name="pending_alerts")
    op.drop_column("pending_alerts", "dead_reason")
    op.drop_column("pending_alerts", "next_attempt_at")

    with op.batch_alter_table("device_catalog") as batch:
        batch.drop_constraint("uq_device_identity", type_="unique")
        batch.create_unique_constraint(
            "uq_device_identity",
            ["brand", "model", "storage_gb"],
        )
