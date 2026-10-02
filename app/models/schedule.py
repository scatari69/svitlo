from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import UTCDateTime
from app.models.common import Timestamps, id_type


class ScheduleSubscription(Timestamps, Base):
    __tablename__ = "schedule_subscriptions"
    __table_args__ = (
        UniqueConstraint("id", "user_id", name="uq_subscriptions_id_owner"),
        UniqueConstraint("user_id", "creation_key", name="uq_subscription_creation"),
        CheckConstraint(
            "creation_key IS NULL OR length(creation_key) = 32", name="ck_subscription_creation"
        ),
        CheckConstraint("length(trim(name)) > 0", name="ck_subscription_name"),
        CheckConstraint(
            "length(trim(provider)) > 0 AND length(trim(region)) > 0 AND length(trim(queue)) > 0",
            name="ck_subscription_source",
        ),
        Index("ix_subscriptions_owner_enabled", "user_id", "enabled"),
        Index("ix_subscriptions_source", "provider", "region", "queue"),
    )

    id: Mapped[int] = mapped_column(id_type, Identity(), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    provider: Mapped[str] = mapped_column(String(100))
    region: Mapped[str] = mapped_column(String(100))
    queue: Mapped[str] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(255))
    creation_key: Mapped[str | None] = mapped_column(String(32))
    enabled: Mapped[bool] = mapped_column(default=True, server_default=text("true"))


class ScheduleNotificationChannel(Base):
    __tablename__ = "schedule_notification_channels"
    __table_args__ = (
        ForeignKeyConstraint(
            ["subscription_id", "user_id"],
            ["schedule_subscriptions.id", "schedule_subscriptions.user_id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["channel_id", "user_id"],
            ["notification_channels.id", "notification_channels.user_id"],
            ondelete="CASCADE",
        ),
        Index("ix_schedule_channels_channel", "channel_id", "user_id"),
    )

    subscription_id: Mapped[int] = mapped_column(id_type, primary_key=True)
    channel_id: Mapped[int] = mapped_column(id_type, primary_key=True)
    user_id: Mapped[int] = mapped_column(id_type)


class ScheduleVersion(Base):
    """Shared source snapshots, independent of individual subscriptions and devices."""

    __tablename__ = "schedule_versions"
    __table_args__ = (
        CheckConstraint("length(content_hash) = 64", name="ck_schedule_hash_length"),
        CheckConstraint(
            "length(trim(provider)) > 0 AND length(trim(region)) > 0 AND length(trim(queue)) > 0",
            name="ck_schedule_version_source",
        ),
        UniqueConstraint(
            "provider",
            "region",
            "queue",
            "schedule_date",
            "fetched_at",
            name="uq_schedule_version_source_time",
        ),
        Index("ix_schedule_versions_source_hash", "provider", "region", "queue", "content_hash"),
    )

    id: Mapped[int] = mapped_column(id_type, Identity(), primary_key=True)
    provider: Mapped[str] = mapped_column(String(100))
    region: Mapped[str] = mapped_column(String(100))
    queue: Mapped[str] = mapped_column(String(100))
    schedule_date: Mapped[date]
    content_hash: Mapped[str] = mapped_column(String(64))
    normalized_content: Mapped[dict[str, Any]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql")
    )
    source_metadata: Mapped[dict[str, Any] | None] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql")
    )
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime(), server_default=func.now())
