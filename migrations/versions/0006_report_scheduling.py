"""Add local report schedules, activation cutoffs and durable delivery reservations."""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("report_settings") as batch:
        for kind in ("daily", "weekly", "monthly"):
            batch.add_column(
                sa.Column(f"{kind}_time", sa.Integer(), server_default="540", nullable=False)
            )
            batch.add_column(
                sa.Column(f"{kind}_enabled_at", sa.DateTime(timezone=True), nullable=True)
            )
        batch.add_column(
            sa.Column("weekly_weekday", sa.Integer(), server_default="0", nullable=False)
        )
        batch.create_check_constraint(
            "ck_report_times",
            "daily_time BETWEEN 0 AND 1439 AND weekly_time BETWEEN 0 AND 1439 "
            "AND monthly_time BETWEEN 0 AND 1439",
        )
        batch.create_check_constraint("ck_report_weekday", "weekly_weekday BETWEEN 0 AND 6")
    op.create_table(
        "report_deliveries",
        sa.Column(
            "device_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False
        ),
        sa.Column("telegram_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(7), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(9), server_default="claimed", nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "kind IN ('daily', 'weekly', 'monthly')", name="ck_report_delivery_kind"
        ),
        sa.CheckConstraint("telegram_chat_id <> 0", name="ck_report_delivery_chat"),
        sa.CheckConstraint("period_end > period_start", name="ck_report_delivery_period"),
        sa.CheckConstraint(
            "status IN ('claimed', 'sent', 'failed', 'uncertain')", name="ck_report_delivery_status"
        ),
        sa.ForeignKeyConstraint(["device_id"], ["devices.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("device_id", "telegram_chat_id", "kind", "period_end"),
    )


def downgrade() -> None:
    op.drop_table("report_deliveries")
    with op.batch_alter_table("report_settings") as batch:
        batch.drop_constraint("ck_report_times", type_="check")
        batch.drop_constraint("ck_report_weekday", type_="check")
        batch.drop_column("weekly_weekday")
        for kind in ("daily", "weekly", "monthly"):
            batch.drop_column(f"{kind}_time")
            batch.drop_column(f"{kind}_enabled_at")
