from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import UTCDateTime
from app.models.common import Timestamps, id_type


class ReportSettings(Timestamps, Base):
    __tablename__ = "report_settings"
    __table_args__ = (
        ForeignKeyConstraint(
            ["device_id", "channel_id"],
            ["device_notification_channels.device_id", "device_notification_channels.channel_id"],
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "daily_time BETWEEN 0 AND 1439 AND weekly_time BETWEEN 0 AND 1439 "
            "AND monthly_time BETWEEN 0 AND 1439",
            name="ck_report_times",
        ),
        CheckConstraint("weekly_weekday BETWEEN 0 AND 6", name="ck_report_weekday"),
        Index(
            "ix_report_settings_active",
            "device_id",
            "channel_id",
            postgresql_where=text(
                "daily_enabled IS TRUE OR weekly_enabled IS TRUE OR monthly_enabled IS TRUE"
            ),
            sqlite_where=text(
                "daily_enabled IS TRUE OR weekly_enabled IS TRUE OR monthly_enabled IS TRUE"
            ),
        ),
    )
    device_id: Mapped[int] = mapped_column(id_type, primary_key=True)
    channel_id: Mapped[int] = mapped_column(id_type, primary_key=True, index=True)
    daily_enabled: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    weekly_enabled: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    monthly_enabled: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    daily_time: Mapped[int] = mapped_column(default=540, server_default="540")
    weekly_time: Mapped[int] = mapped_column(default=540, server_default="540")
    monthly_time: Mapped[int] = mapped_column(default=540, server_default="540")
    weekly_weekday: Mapped[int] = mapped_column(default=0, server_default="0")
    daily_enabled_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    weekly_enabled_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    monthly_enabled_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class ReportDelivery(Base):
    """Durable reservations survive report disablement and channel recreation."""

    __tablename__ = "report_deliveries"
    __table_args__ = (
        CheckConstraint("kind IN ('daily', 'weekly', 'monthly')", name="ck_report_delivery_kind"),
        CheckConstraint("telegram_chat_id <> 0", name="ck_report_delivery_chat"),
        CheckConstraint("period_end > period_start", name="ck_report_delivery_period"),
        CheckConstraint(
            "status IN ('claimed', 'sent', 'failed', 'uncertain')", name="ck_report_delivery_status"
        ),
    )
    device_id: Mapped[int] = mapped_column(
        id_type, ForeignKey("devices.id", ondelete="RESTRICT"), primary_key=True
    )
    telegram_chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(String(7), primary_key=True)
    period_end: Mapped[datetime] = mapped_column(UTCDateTime(), primary_key=True)
    period_start: Mapped[datetime] = mapped_column(UTCDateTime())
    claimed_at: Mapped[datetime] = mapped_column(UTCDateTime())
    status: Mapped[str] = mapped_column(String(9), default="claimed", server_default="claimed")
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
