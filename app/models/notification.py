from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import UTCDateTime
from app.models.common import id_type


class PowerNotificationDelivery(Base):
    """Durable at-most-once reservation, independent of event UUIDs or channel recreation."""

    __tablename__ = "power_notification_deliveries"
    __table_args__ = (
        CheckConstraint("telegram_chat_id <> 0", name="ck_power_delivery_chat"),
        CheckConstraint(
            "status IN ('claimed', 'sent', 'failed', 'uncertain')", name="ck_power_delivery_status"
        ),
    )
    interval_id: Mapped[int] = mapped_column(
        id_type, ForeignKey("power_intervals.id", ondelete="RESTRICT"), primary_key=True
    )
    telegram_chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    claimed_at: Mapped[datetime] = mapped_column(UTCDateTime())
    status: Mapped[str] = mapped_column(String(9), default="claimed", server_default="claimed")
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class ScheduleNotificationDelivery(Base):
    """Reserve each shared version once per destination and retain its diff baseline."""

    __tablename__ = "schedule_notification_deliveries"
    __table_args__ = (
        CheckConstraint("telegram_chat_id <> 0", name="ck_schedule_delivery_chat"),
        CheckConstraint(
            "status IN ('claimed', 'sent', 'failed', 'uncertain')",
            name="ck_schedule_delivery_status",
        ),
    )
    version_id: Mapped[int] = mapped_column(
        id_type, ForeignKey("schedule_versions.id", ondelete="RESTRICT"), primary_key=True
    )
    telegram_chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    previous_version_id: Mapped[int | None] = mapped_column(
        id_type, ForeignKey("schedule_versions.id", ondelete="RESTRICT"), index=True
    )
    claimed_at: Mapped[datetime] = mapped_column(UTCDateTime())
    status: Mapped[str] = mapped_column(String(9), default="claimed", server_default="claimed")
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class MonitorHealthNotificationState(Base):
    """Durable cooldown and episode state, preserved when a channel is recreated."""

    __tablename__ = "monitor_health_notification_states"
    __table_args__ = (
        CheckConstraint("telegram_chat_id <> 0", name="ck_monitor_health_notification_chat"),
    )
    device_id: Mapped[int] = mapped_column(
        id_type, ForeignKey("devices.id", ondelete="RESTRICT"), primary_key=True
    )
    telegram_chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    last_event_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_warning_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_recovery_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    warning_sent: Mapped[bool] = mapped_column(default=False, server_default="false")
