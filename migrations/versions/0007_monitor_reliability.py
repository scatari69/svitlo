"""Persist monitor failure streaks and health warning cooldowns."""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("devices") as batch:
        batch.add_column(
            sa.Column("monitor_failure_count", sa.Integer(), server_default="0", nullable=False)
        )
        for name in (
            "monitor_failure_started_at",
            "monitor_health_changed_at",
            "last_monitor_observation_at",
        ):
            batch.add_column(sa.Column(name, sa.DateTime(timezone=True), nullable=True))
        batch.create_check_constraint("ck_devices_monitor_failures", "monitor_failure_count >= 0")
    op.create_table(
        "monitor_health_notification_states",
        sa.Column(
            "device_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False
        ),
        sa.Column("telegram_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("last_event_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_warning_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_recovery_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("warning_sent", sa.Boolean(), server_default="false", nullable=False),
        sa.CheckConstraint("telegram_chat_id <> 0", name="ck_monitor_health_notification_chat"),
        sa.ForeignKeyConstraint(["device_id"], ["devices.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("device_id", "telegram_chat_id"),
    )


def downgrade() -> None:
    op.drop_table("monitor_health_notification_states")
    with op.batch_alter_table("devices") as batch:
        batch.drop_constraint("ck_devices_monitor_failures", type_="check")
        for name in (
            "last_monitor_observation_at",
            "monitor_health_changed_at",
            "monitor_failure_started_at",
            "monitor_failure_count",
        ):
            batch.drop_column(name)
