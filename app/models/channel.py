from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.common import Timestamps, enum_type, id_type
from app.models.enums import ChannelType


class NotificationChannel(Timestamps, Base):
    __tablename__ = "notification_channels"
    __table_args__ = (
        UniqueConstraint("user_id", "telegram_chat_id", name="uq_channel_owner_chat"),
        UniqueConstraint("id", "user_id", name="uq_channels_id_owner"),
        CheckConstraint("telegram_chat_id <> 0", name="ck_channel_chat_id"),
        CheckConstraint("length(trim(name)) > 0", name="ck_channel_name"),
    )

    id: Mapped[int] = mapped_column(id_type, Identity(), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    telegram_chat_id: Mapped[int] = mapped_column(BigInteger)
    name: Mapped[str] = mapped_column(String(255))
    channel_type: Mapped[ChannelType] = mapped_column(enum_type(ChannelType, "channel_type"))
    enabled: Mapped[bool] = mapped_column(default=True, server_default=text("true"))


class DeviceNotificationChannel(Base):
    __tablename__ = "device_notification_channels"
    __table_args__ = (
        ForeignKeyConstraint(
            ["device_id", "user_id"], ["devices.id", "devices.user_id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["channel_id", "user_id"],
            ["notification_channels.id", "notification_channels.user_id"],
            ondelete="CASCADE",
        ),
        Index("ix_device_channels_channel", "channel_id", "user_id"),
    )

    device_id: Mapped[int] = mapped_column(id_type, primary_key=True)
    channel_id: Mapped[int] = mapped_column(id_type, primary_key=True)
    user_id: Mapped[int] = mapped_column(id_type)
