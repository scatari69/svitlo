"""Persist schedule delivery reservations and their diff baselines.

Revision ID: 0005
Revises: 0004
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    id_type = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
    op.create_table(
        "schedule_notification_deliveries",
        sa.Column("version_id", id_type, nullable=False),
        sa.Column("telegram_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("previous_version_id", id_type, nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(9), server_default="claimed", nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("telegram_chat_id <> 0", name="ck_schedule_delivery_chat"),
        sa.CheckConstraint(
            "status IN ('claimed', 'sent', 'failed', 'uncertain')",
            name="ck_schedule_delivery_status",
        ),
        sa.ForeignKeyConstraint(["version_id"], ["schedule_versions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["previous_version_id"], ["schedule_versions.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("version_id", "telegram_chat_id"),
    )
    op.create_index(
        "ix_schedule_notification_deliveries_previous_version_id",
        "schedule_notification_deliveries",
        ["previous_version_id"],
    )


def downgrade() -> None:
    op.drop_table("schedule_notification_deliveries")
