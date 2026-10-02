from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    MonitorHealthNotificationState,
    NotificationChannel,
    PowerNotificationDelivery,
    ScheduleNotificationChannel,
    ScheduleNotificationDelivery,
    ScheduleSubscription,
)


class PowerNotificationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def claim(self, interval_id: int, chat_id: int) -> bool:
        result = await self.session.execute(
            insert(PowerNotificationDelivery)
            .values(interval_id=interval_id, telegram_chat_id=chat_id, claimed_at=datetime.now(UTC))
            .on_conflict_do_nothing(
                index_elements=[
                    PowerNotificationDelivery.interval_id,
                    PowerNotificationDelivery.telegram_chat_id,
                ]
            )
            .returning(PowerNotificationDelivery.interval_id)
        )
        return result.scalar_one_or_none() is not None

    async def finish(self, interval_id: int, chat_id: int, status: str) -> None:
        delivery = await self.session.get(PowerNotificationDelivery, (interval_id, chat_id))
        if delivery is None:
            raise LookupError("Notification reservation not found")
        delivery.status = status
        if status == "sent":
            delivery.sent_at = datetime.now(UTC)


class ScheduleNotificationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def chat_ids(self, provider: str, region: str, queue: str) -> list[int]:
        return list(
            (
                await self.session.scalars(
                    select(NotificationChannel.telegram_chat_id)
                    .join(
                        ScheduleNotificationChannel,
                        (ScheduleNotificationChannel.channel_id == NotificationChannel.id)
                        & (ScheduleNotificationChannel.user_id == NotificationChannel.user_id),
                    )
                    .join(
                        ScheduleSubscription,
                        (ScheduleSubscription.id == ScheduleNotificationChannel.subscription_id)
                        & (ScheduleSubscription.user_id == ScheduleNotificationChannel.user_id),
                    )
                    .where(
                        ScheduleSubscription.provider == provider,
                        ScheduleSubscription.region == region,
                        ScheduleSubscription.queue == queue,
                        ScheduleSubscription.enabled.is_(True),
                        NotificationChannel.enabled.is_(True),
                    )
                    .distinct()
                    .order_by(NotificationChannel.telegram_chat_id)
                )
            ).all()
        )

    async def get(self, version_id: int, chat_id: int) -> ScheduleNotificationDelivery | None:
        return await self.session.get(ScheduleNotificationDelivery, (version_id, chat_id))

    async def claim(self, version_id: int, chat_id: int, previous_id: int | None) -> bool:
        result = await self.session.execute(
            insert(ScheduleNotificationDelivery)
            .values(
                version_id=version_id,
                telegram_chat_id=chat_id,
                previous_version_id=previous_id,
                claimed_at=datetime.now(UTC),
            )
            .on_conflict_do_nothing(
                index_elements=[
                    ScheduleNotificationDelivery.version_id,
                    ScheduleNotificationDelivery.telegram_chat_id,
                ]
            )
            .returning(ScheduleNotificationDelivery.version_id)
        )
        return result.scalar_one_or_none() is not None

    async def finish(self, version_id: int, chat_id: int, status: str) -> None:
        delivery = await self.get(version_id, chat_id)
        if delivery is None:
            raise LookupError("Schedule notification reservation not found")
        delivery.status = status
        if status == "sent":
            delivery.sent_at = datetime.now(UTC)


class MonitorHealthNotificationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def claim(
        self,
        device_id: int,
        chat_id: int,
        event_at: datetime,
        *,
        recovery: bool,
        recovery_enabled: bool,
        now: datetime,
        cooldown_seconds: int,
    ) -> bool:
        await self.session.execute(
            insert(MonitorHealthNotificationState)
            .values(device_id=device_id, telegram_chat_id=chat_id)
            .on_conflict_do_nothing()
        )
        row = await self.session.scalar(
            select(MonitorHealthNotificationState)
            .where(
                MonitorHealthNotificationState.device_id == device_id,
                MonitorHealthNotificationState.telegram_chat_id == chat_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        assert row is not None
        if row.last_event_at is not None and event_at <= row.last_event_at:
            return False
        row.last_event_at = event_at
        if recovery:
            send = recovery_enabled and row.warning_sent
            row.warning_sent = False
            if send:
                row.last_recovery_at = now
            return send
        if row.last_warning_at is not None and now < row.last_warning_at + timedelta(
            seconds=cooldown_seconds
        ):
            return False
        row.last_warning_at = now
        row.warning_sent = False
        return True

    async def finish_warning(self, device_id: int, chat_id: int, event_at: datetime) -> None:
        row = await self.session.scalar(
            select(MonitorHealthNotificationState)
            .where(
                MonitorHealthNotificationState.device_id == device_id,
                MonitorHealthNotificationState.telegram_chat_id == chat_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is not None and row.last_event_at == event_at:
            row.warning_sent = True
