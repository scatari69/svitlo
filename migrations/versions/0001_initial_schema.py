"""Initial monitoring, notification, and planned schedule schema.

Revision ID: 0001
Revises: None
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "schedule_versions",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            sa.Identity(always=False),
            nullable=False,
        ),
        sa.Column("provider", sa.String(length=100), nullable=False),
        sa.Column("region", sa.String(length=100), nullable=False),
        sa.Column("queue", sa.String(length=100), nullable=False),
        sa.Column("schedule_date", sa.Date(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "normalized_content",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "source_metadata",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=True,
        ),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("length(content_hash) = 64", name="ck_schedule_hash_length"),
        sa.CheckConstraint(
            "length(trim(provider)) > 0 AND length(trim(region)) > 0 AND length(trim(queue)) > 0",
            name="ck_schedule_version_source",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider",
            "region",
            "queue",
            "schedule_date",
            "fetched_at",
            name="uq_schedule_version_source_time",
        ),
    )
    op.create_index(
        "ix_schedule_versions_source_hash",
        "schedule_versions",
        ["provider", "region", "queue", "content_hash"],
        unique=False,
    )
    op.create_table(
        "users",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            sa.Identity(always=False),
            nullable=False,
        ),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("telegram_user_id > 0", name="ck_users_telegram_id"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("telegram_user_id"),
    )
    op.create_table(
        "devices",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            sa.Identity(always=False),
            nullable=False,
        ),
        sa.Column("user_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column(
            "monitoring_type",
            sa.Enum(
                "ping",
                "snmp",
                "home_assistant",
                name="monitoring_type",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "current_power_state",
            sa.Enum(
                "on",
                "off",
                "unknown",
                name="device_power_state",
                native_enum=False,
                create_constraint=True,
            ),
            server_default="unknown",
            nullable=False,
        ),
        sa.Column(
            "monitor_health",
            sa.Enum(
                "healthy",
                "degraded",
                "unavailable",
                name="monitor_health",
                native_enum=False,
                create_constraint=True,
            ),
            server_default="unavailable",
            nullable=False,
        ),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_successful_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_state_changed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("length(trim(name)) > 0", name="ck_devices_name"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "monitoring_type", name="uq_devices_id_backend"),
        sa.UniqueConstraint("id", "user_id", name="uq_devices_id_owner"),
    )
    op.create_index(
        "ix_devices_owner_active", "devices", ["user_id", "deleted_at", "enabled"], unique=False
    )
    op.create_table(
        "notification_channels",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            sa.Identity(always=False),
            nullable=False,
        ),
        sa.Column("user_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("telegram_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column(
            "channel_type",
            sa.Enum(
                "private",
                "group",
                "supergroup",
                "channel",
                name="channel_type",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("length(trim(name)) > 0", name="ck_channel_name"),
        sa.CheckConstraint("telegram_chat_id <> 0", name="ck_channel_chat_id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "user_id", name="uq_channels_id_owner"),
        sa.UniqueConstraint("user_id", "telegram_chat_id", name="uq_channel_owner_chat"),
    )
    op.create_table(
        "schedule_subscriptions",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            sa.Identity(always=False),
            nullable=False,
        ),
        sa.Column("user_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("provider", sa.String(length=100), nullable=False),
        sa.Column("region", sa.String(length=100), nullable=False),
        sa.Column("queue", sa.String(length=100), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("length(trim(name)) > 0", name="ck_subscription_name"),
        sa.CheckConstraint(
            "length(trim(provider)) > 0 AND length(trim(region)) > 0 AND length(trim(queue)) > 0",
            name="ck_subscription_source",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "user_id", name="uq_subscriptions_id_owner"),
    )
    op.create_index(
        "ix_subscriptions_owner_enabled",
        "schedule_subscriptions",
        ["user_id", "enabled"],
        unique=False,
    )
    op.create_index(
        "ix_subscriptions_source",
        "schedule_subscriptions",
        ["provider", "region", "queue"],
        unique=False,
    )
    op.create_table(
        "device_notification_channels",
        sa.Column(
            "device_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False
        ),
        sa.Column(
            "channel_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False
        ),
        sa.Column("user_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.ForeignKeyConstraint(
            ["channel_id", "user_id"],
            ["notification_channels.id", "notification_channels.user_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["device_id", "user_id"], ["devices.id", "devices.user_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("device_id", "channel_id"),
    )
    op.create_index(
        "ix_device_channels_channel",
        "device_notification_channels",
        ["channel_id", "user_id"],
        unique=False,
    )
    op.create_table(
        "homeassistant_device_configs",
        sa.Column(
            "device_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False
        ),
        sa.Column(
            "monitoring_type", sa.String(length=14), server_default="home_assistant", nullable=False
        ),
        sa.Column(
            "mode",
            sa.Enum("webhook", "api", name="ha_mode", native_enum=False, create_constraint=True),
            nullable=False,
        ),
        sa.Column("webhook_token_hash", sa.String(length=64), nullable=True),
        sa.Column("url", sa.String(length=2048), nullable=True),
        sa.Column("access_token_encrypted", sa.LargeBinary(), nullable=True),
        sa.Column("entity_id", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(mode = 'webhook' AND webhook_token_hash IS NOT NULL AND "
            "length(webhook_token_hash) = 64 AND url IS NULL AND access_token_encrypted "
            "IS NULL AND entity_id IS NULL) OR (mode = 'api' AND webhook_token_hash IS "
            "NULL AND url IS NOT NULL AND (url LIKE 'http://%' OR url LIKE 'https://%') "
            "AND access_token_encrypted IS NOT NULL AND length(access_token_encrypted) > "
            "0 AND entity_id IS NOT NULL AND length(trim(entity_id)) > 0)",
            name="ck_ha_mode_config",
        ),
        sa.CheckConstraint("monitoring_type = 'home_assistant'", name="ck_ha_backend"),
        sa.ForeignKeyConstraint(
            ["device_id", "monitoring_type"],
            ["devices.id", "devices.monitoring_type"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("device_id"),
        sa.UniqueConstraint("webhook_token_hash"),
    )
    op.create_table(
        "ping_device_configs",
        sa.Column(
            "device_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False
        ),
        sa.Column("monitoring_type", sa.String(length=14), server_default="ping", nullable=False),
        sa.Column("host", sa.String(length=255), nullable=False),
        sa.Column("interval_seconds", sa.Double(), server_default="10", nullable=False),
        sa.Column("timeout_seconds", sa.Double(), server_default="2", nullable=False),
        sa.Column("failure_threshold", sa.Integer(), server_default="3", nullable=False),
        sa.Column("recovery_threshold", sa.Integer(), server_default="2", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("monitoring_type = 'ping'", name="ck_ping_backend"),
        sa.CheckConstraint(
            "failure_threshold > 1 AND recovery_threshold > 0", name="ck_ping_thresholds"
        ),
        sa.CheckConstraint("interval_seconds > 0 AND timeout_seconds > 0", name="ck_ping_timing"),
        sa.CheckConstraint("length(trim(host)) > 0", name="ck_ping_host"),
        sa.ForeignKeyConstraint(
            ["device_id", "monitoring_type"],
            ["devices.id", "devices.monitoring_type"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("device_id"),
    )
    op.create_table(
        "power_intervals",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            sa.Identity(always=False),
            nullable=False,
        ),
        sa.Column(
            "device_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False
        ),
        sa.Column(
            "state",
            sa.Enum(
                "on",
                "off",
                "unknown",
                name="interval_power_state",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("ended_at IS NULL OR ended_at >= started_at", name="ck_interval_order"),
        sa.ForeignKeyConstraint(["device_id"], ["devices.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_power_intervals_device_start",
        "power_intervals",
        ["device_id", "started_at"],
        unique=False,
    )
    op.create_index(
        "uq_power_intervals_open_device",
        "power_intervals",
        ["device_id"],
        unique=True,
        postgresql_where=sa.text("ended_at IS NULL"),
        sqlite_where=sa.text("ended_at IS NULL"),
    )
    op.create_table(
        "schedule_notification_channels",
        sa.Column(
            "subscription_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False
        ),
        sa.Column(
            "channel_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False
        ),
        sa.Column("user_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.ForeignKeyConstraint(
            ["channel_id", "user_id"],
            ["notification_channels.id", "notification_channels.user_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["subscription_id", "user_id"],
            ["schedule_subscriptions.id", "schedule_subscriptions.user_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("subscription_id", "channel_id"),
    )
    op.create_index(
        "ix_schedule_channels_channel",
        "schedule_notification_channels",
        ["channel_id", "user_id"],
        unique=False,
    )
    op.create_table(
        "snmp_device_configs",
        sa.Column(
            "device_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False
        ),
        sa.Column("monitoring_type", sa.String(length=14), server_default="snmp", nullable=False),
        sa.Column("host", sa.String(length=255), nullable=False),
        sa.Column("port", sa.Integer(), server_default="161", nullable=False),
        sa.Column("community_encrypted", sa.LargeBinary(), nullable=False),
        sa.Column("polling_interval_seconds", sa.Double(), server_default="10", nullable=False),
        sa.Column(
            "mode",
            sa.Enum(
                "interface",
                "custom_oid",
                name="snmp_mode",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("interface_index", sa.Integer(), nullable=True),
        sa.Column("oid", sa.String(length=255), nullable=True),
        sa.Column("on_value", sa.String(length=255), nullable=True),
        sa.Column("off_value", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(mode = 'interface' AND interface_index IS NOT NULL AND interface_index > 0 "
            "AND oid IS NULL AND on_value IS NULL AND off_value IS NULL) OR (mode = "
            "'custom_oid' AND interface_index IS NULL AND oid IS NOT NULL AND "
            "length(trim(oid)) > 0 AND on_value IS NOT NULL AND off_value IS NOT NULL "
            "AND on_value <> off_value)",
            name="ck_snmp_mode_config",
        ),
        sa.CheckConstraint("monitoring_type = 'snmp'", name="ck_snmp_backend"),
        sa.CheckConstraint("length(community_encrypted) > 0", name="ck_snmp_community"),
        sa.CheckConstraint("length(trim(host)) > 0", name="ck_snmp_host"),
        sa.CheckConstraint("polling_interval_seconds > 0", name="ck_snmp_interval"),
        sa.CheckConstraint("port BETWEEN 1 AND 65535", name="ck_snmp_port"),
        sa.ForeignKeyConstraint(
            ["device_id", "monitoring_type"],
            ["devices.id", "devices.monitoring_type"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("device_id"),
    )
    op.create_table(
        "report_settings",
        sa.Column(
            "device_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False
        ),
        sa.Column(
            "channel_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False
        ),
        sa.Column("daily_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("weekly_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("monthly_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["device_id", "channel_id"],
            ["device_notification_channels.device_id", "device_notification_channels.channel_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("device_id", "channel_id"),
    )
    op.create_index(
        op.f("ix_report_settings_channel_id"), "report_settings", ["channel_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_report_settings_channel_id"), table_name="report_settings")
    op.drop_table("report_settings")
    op.drop_table("snmp_device_configs")
    op.drop_index("ix_schedule_channels_channel", table_name="schedule_notification_channels")
    op.drop_table("schedule_notification_channels")
    op.drop_index(
        "uq_power_intervals_open_device",
        table_name="power_intervals",
        postgresql_where=sa.text("ended_at IS NULL"),
        sqlite_where=sa.text("ended_at IS NULL"),
    )
    op.drop_index("ix_power_intervals_device_start", table_name="power_intervals")
    op.drop_table("power_intervals")
    op.drop_table("ping_device_configs")
    op.drop_table("homeassistant_device_configs")
    op.drop_index("ix_device_channels_channel", table_name="device_notification_channels")
    op.drop_table("device_notification_channels")
    op.drop_index("ix_subscriptions_source", table_name="schedule_subscriptions")
    op.drop_index("ix_subscriptions_owner_enabled", table_name="schedule_subscriptions")
    op.drop_table("schedule_subscriptions")
    op.drop_table("notification_channels")
    op.drop_index("ix_devices_owner_active", table_name="devices")
    op.drop_table("devices")
    op.drop_table("users")
    op.drop_index("ix_schedule_versions_source_hash", table_name="schedule_versions")
    op.drop_table("schedule_versions")
