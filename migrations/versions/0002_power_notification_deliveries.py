"""Add durable per-transition Telegram delivery reservations.

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "power_notification_deliveries",
        sa.Column(
            "interval_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False
        ),
        sa.Column("telegram_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(9), server_default="claimed", nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("telegram_chat_id <> 0", name="ck_power_delivery_chat"),
        sa.CheckConstraint(
            "status IN ('claimed', 'sent', 'failed', 'uncertain')", name="ck_power_delivery_status"
        ),
        sa.ForeignKeyConstraint(["interval_id"], ["power_intervals.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("interval_id", "telegram_chat_id"),
    )


def downgrade() -> None:
    # Monitoring history remains intact; only notification reservations are removed.
    op.drop_table("power_notification_deliveries")
