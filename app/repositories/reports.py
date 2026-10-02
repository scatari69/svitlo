from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics.service import ReportingWindow
from app.models import (
    Device,
    DeviceNotificationChannel,
    NotificationChannel,
    ReportDelivery,
    ReportSettings,
)


class ReportRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def settings(
        self, device_id: int, channel_id: int, *, lock: bool = False
    ) -> ReportSettings | None:
        query = select(ReportSettings).where(
            ReportSettings.device_id == device_id, ReportSettings.channel_id == channel_id
        )
        if lock:
            query = query.with_for_update().execution_options(populate_existing=True)
        return await self.session.scalar(query)

    async def ensure(self, user_id: int, device_id: int, channel_id: int) -> ReportSettings:
        await self.session.execute(
            insert(DeviceNotificationChannel)
            .values(user_id=user_id, device_id=device_id, channel_id=channel_id)
            .on_conflict_do_nothing()
        )
        await self.session.execute(
            insert(ReportSettings)
            .values(device_id=device_id, channel_id=channel_id)
            .on_conflict_do_nothing()
        )
        settings = await self.settings(device_id, channel_id, lock=True)
        if settings is None:
            raise RuntimeError("Report settings missing after insert")
        return settings

    async def active(self) -> list[tuple[ReportSettings, Device, NotificationChannel]]:
        result = await self.session.execute(
            select(ReportSettings, Device, NotificationChannel)
            .join(Device, Device.id == ReportSettings.device_id)
            .join(NotificationChannel, NotificationChannel.id == ReportSettings.channel_id)
            .where(
                Device.deleted_at.is_(None),
                NotificationChannel.enabled.is_(True),
                Device.user_id == NotificationChannel.user_id,
                or_(
                    ReportSettings.daily_enabled.is_(True),
                    ReportSettings.weekly_enabled.is_(True),
                    ReportSettings.monthly_enabled.is_(True),
                ),
            )
            .order_by(ReportSettings.device_id, ReportSettings.channel_id)
        )
        return [(row[0], row[1], row[2]) for row in result.all()]

    async def claim(
        self, device_id: int, chat_id: int, kind: str, window: ReportingWindow, now: datetime
    ) -> bool:
        result = await self.session.execute(
            insert(ReportDelivery)
            .values(
                device_id=device_id,
                telegram_chat_id=chat_id,
                kind=kind,
                period_start=window.start,
                period_end=window.end,
                claimed_at=now,
            )
            .on_conflict_do_nothing()
            .returning(ReportDelivery.device_id)
        )
        return result.scalar_one_or_none() is not None

    async def finish(
        self, device_id: int, chat_id: int, kind: str, window: ReportingWindow, status: str
    ) -> None:
        row = await self.session.get(ReportDelivery, (device_id, chat_id, kind, window.end))
        if row is None:
            raise LookupError("Report delivery not found")
        row.status = status
        if status == "sent":
            row.sent_at = datetime.now(UTC)

    async def reserved(
        self, device_id: int, chat_id: int, kind: str, window: ReportingWindow
    ) -> bool:
        return (
            await self.session.get(ReportDelivery, (device_id, chat_id, kind, window.end))
            is not None
        )
