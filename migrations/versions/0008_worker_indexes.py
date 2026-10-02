"""Index active monitoring targets and enabled report settings."""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    active = sa.text("enabled IS TRUE AND deleted_at IS NULL")
    reports = sa.text("daily_enabled IS TRUE OR weekly_enabled IS TRUE OR monthly_enabled IS TRUE")
    op.create_index(
        "ix_devices_monitoring_enabled",
        "devices",
        ["monitoring_type", "id"],
        postgresql_where=active,
        sqlite_where=active,
    )
    op.create_index(
        "ix_report_settings_active",
        "report_settings",
        ["device_id", "channel_id"],
        postgresql_where=reports,
        sqlite_where=reports,
    )


def downgrade() -> None:
    op.drop_index("ix_report_settings_active", table_name="report_settings")
    op.drop_index("ix_devices_monitoring_enabled", table_name="devices")
