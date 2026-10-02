from datetime import date

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ScheduleNotificationChannel, ScheduleSubscription, ScheduleVersion


class ScheduleRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_subscriptions(
        self, user_id: int, *, enabled_only: bool = True
    ) -> list[ScheduleSubscription]:
        query = (
            select(ScheduleSubscription)
            .where(ScheduleSubscription.user_id == user_id)
            .order_by(ScheduleSubscription.id)
        )
        if enabled_only:
            query = query.where(ScheduleSubscription.enabled.is_(True))
        return list((await self.session.scalars(query)).all())

    async def get(
        self, user_id: int, subscription_id: int, *, for_update: bool = False
    ) -> ScheduleSubscription | None:
        query = select(ScheduleSubscription).where(
            ScheduleSubscription.user_id == user_id, ScheduleSubscription.id == subscription_id
        )
        if for_update:
            query = query.with_for_update().execution_options(populate_existing=True)
        return await self.session.scalar(query)

    async def by_creation_key(self, user_id: int, creation_key: str) -> ScheduleSubscription | None:
        return await self.session.scalar(
            select(ScheduleSubscription).where(
                ScheduleSubscription.user_id == user_id,
                ScheduleSubscription.creation_key == creation_key,
            )
        )

    async def create(
        self, user_id: int, provider: str, region: str, queue: str, name: str, creation_key: str
    ) -> tuple[ScheduleSubscription, bool]:
        created_id = await self.session.scalar(
            insert(ScheduleSubscription)
            .values(
                user_id=user_id,
                provider=provider,
                region=region,
                queue=queue,
                name=name,
                creation_key=creation_key,
            )
            .on_conflict_do_nothing(
                index_elements=[ScheduleSubscription.user_id, ScheduleSubscription.creation_key]
            )
            .returning(ScheduleSubscription.id)
        )
        subscription = await self.by_creation_key(user_id, creation_key)
        if subscription is None:
            raise RuntimeError("Subscription not found after insert")
        return subscription, created_id is not None

    async def channel_ids(self, user_id: int, subscription_id: int) -> list[int]:
        return list(
            (
                await self.session.scalars(
                    select(ScheduleNotificationChannel.channel_id)
                    .where(
                        ScheduleNotificationChannel.user_id == user_id,
                        ScheduleNotificationChannel.subscription_id == subscription_id,
                    )
                    .order_by(ScheduleNotificationChannel.channel_id)
                )
            ).all()
        )

    async def replace_channels(
        self, user_id: int, subscription_id: int, channel_ids: set[int]
    ) -> None:
        await self.session.execute(
            delete(ScheduleNotificationChannel).where(
                ScheduleNotificationChannel.user_id == user_id,
                ScheduleNotificationChannel.subscription_id == subscription_id,
            )
        )
        for channel_id in sorted(channel_ids):
            self.session.add(
                ScheduleNotificationChannel(
                    user_id=user_id, subscription_id=subscription_id, channel_id=channel_id
                )
            )

    async def latest_version(
        self, provider: str, region: str, queue: str, schedule_date: date
    ) -> ScheduleVersion | None:
        query = (
            select(ScheduleVersion)
            .where(
                ScheduleVersion.provider == provider,
                ScheduleVersion.region == region,
                ScheduleVersion.queue == queue,
                ScheduleVersion.schedule_date == schedule_date,
            )
            .order_by(ScheduleVersion.fetched_at.desc(), ScheduleVersion.id.desc())
            .limit(1)
        )
        return await self.session.scalar(query)

    async def version(self, version_id: int) -> ScheduleVersion | None:
        return await self.session.get(ScheduleVersion, version_id)
