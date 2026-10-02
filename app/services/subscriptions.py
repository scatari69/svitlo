import asyncio
import re
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import ScheduleSubscription
from app.repositories.channels import ChannelView as ChannelView
from app.repositories.channels import NotificationChannelRepository
from app.repositories.schedules import ScheduleRepository
from app.repositories.users import UserRepository
from app.schedules.models import Freshness, OutageGroup, Region
from app.schedules.service import ScheduleService
from app.services.validation import validate_name


class CatalogUnavailable(Exception):
    pass


@dataclass(frozen=True)
class Catalog[T]:
    items: tuple[T, ...]
    stale: bool = False
    partial: bool = False


@dataclass(frozen=True)
class SubscriptionView:
    id: int
    provider: str
    region: str
    region_name: str
    queue: str
    name: str
    enabled: bool


class SubscriptionManagementService:
    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], schedules: ScheduleService
    ) -> None:
        self.sessions, self.schedules = sessions, schedules

    async def regions(self) -> Catalog[Region]:
        results = await asyncio.gather(
            *(self.schedules.get_regions(provider) for provider in self.schedules.providers)
        )
        items = tuple(
            sorted(
                (region for result in results if result.data is not None for region in result.data),
                key=lambda r: (r.name, r.provider, r.id),
            )
        )
        if not items:
            raise CatalogUnavailable
        return Catalog(
            items,
            any(result.freshness == Freshness.STALE for result in results),
            any(result.data is None for result in results),
        )

    async def groups(self, provider: str, region: str) -> Catalog[OutageGroup]:
        result = await self.schedules.get_queues(provider, region)
        if not result.data:
            raise CatalogUnavailable
        return Catalog(result.data, result.freshness == Freshness.STALE)

    async def _views(self, subscriptions: list[ScheduleSubscription]) -> list[SubscriptionView]:
        providers = sorted({subscription.provider for subscription in subscriptions})
        results = await asyncio.gather(
            *(self.schedules.get_regions(provider) for provider in providers)
        )
        names = {
            (r.provider, r.id): r.name
            for result in results
            if result.data is not None
            for r in result.data
        }
        return [
            SubscriptionView(
                s.id,
                s.provider,
                s.region,
                names.get((s.provider, s.region), "Назва регіону тимчасово недоступна"),
                s.queue,
                s.name,
                s.enabled,
            )
            for s in subscriptions
        ]

    async def list_subscriptions(self, telegram_user_id: int) -> list[SubscriptionView]:
        async with self.sessions.begin() as session:
            user = await UserRepository(session).get_or_create(telegram_user_id)
            rows = await ScheduleRepository(session).list_subscriptions(user.id, enabled_only=False)
        return await self._views(rows)

    async def get_subscription(
        self, telegram_user_id: int, subscription_id: int
    ) -> SubscriptionView:
        async with self.sessions.begin() as session:
            user = await UserRepository(session).get_or_create(telegram_user_id)
            row = await ScheduleRepository(session).get(user.id, subscription_id)
            if row is None:
                raise LookupError("Subscription not found")
        return (await self._views([row]))[0]

    async def channels(self, telegram_user_id: int) -> list[ChannelView]:
        async with self.sessions.begin() as session:
            user = await UserRepository(session).get_or_create(telegram_user_id)
            repository = NotificationChannelRepository(session)
            await repository.ensure_private(user.id, telegram_user_id)
            return [
                ChannelView(c.id, c.name, c.enabled) for c in await repository.list_owned(user.id)
            ]

    @staticmethod
    async def _validate_channels(
        session: AsyncSession, user_id: int, channel_ids: set[int]
    ) -> None:
        owned = {c.id for c in await NotificationChannelRepository(session).list_owned(user_id)}
        if not channel_ids <= owned:
            raise LookupError("Channel not found")

    async def create_subscription(
        self,
        telegram_user_id: int,
        provider: str,
        region: str,
        queue: str,
        name: str,
        channel_ids: set[int],
        creation_key: str,
    ) -> SubscriptionView:
        name = validate_name(name)
        if not re.fullmatch(r"[0-9a-f]{32}", creation_key):
            raise ValueError("Invalid subscription creation key")
        async with self.sessions.begin() as session:
            user = await UserRepository(session).get_or_create(telegram_user_id)
            existing = await ScheduleRepository(session).by_creation_key(user.id, creation_key)
        if existing is not None:
            if (existing.provider, existing.region, existing.queue, existing.name) != (
                provider,
                region,
                queue,
                name,
            ):
                raise ValueError("Creation key already used")
            return (await self._views([existing]))[0]
        catalog = await self.groups(provider, region)
        if not any(
            g.provider == provider and g.region == region and g.id == queue for g in catalog.items
        ):
            raise ValueError("Group not in provider catalog")
        async with self.sessions.begin() as session:
            user = await UserRepository(session).get_or_create(telegram_user_id)
            await self._validate_channels(session, user.id, channel_ids)
            repository = ScheduleRepository(session)
            row, created = await repository.create(
                user.id, provider, region, queue, name, creation_key
            )
            if (row.provider, row.region, row.queue, row.name) != (provider, region, queue, name):
                raise ValueError("Creation key already used")
            if created:
                await repository.replace_channels(user.id, row.id, channel_ids)
        return (await self._views([row]))[0]

    async def channel_ids(self, telegram_user_id: int, subscription_id: int) -> list[int]:
        async with self.sessions.begin() as session:
            user = await UserRepository(session).get_or_create(telegram_user_id)
            repository = ScheduleRepository(session)
            if await repository.get(user.id, subscription_id) is None:
                raise LookupError("Subscription not found")
            return await repository.channel_ids(user.id, subscription_id)

    async def update_channels(
        self, telegram_user_id: int, subscription_id: int, channel_ids: set[int]
    ) -> None:
        async with self.sessions.begin() as session:
            user = await UserRepository(session).get_or_create(telegram_user_id)
            repository = ScheduleRepository(session)
            if await repository.get(user.id, subscription_id, for_update=True) is None:
                raise LookupError("Subscription not found")
            await self._validate_channels(session, user.id, channel_ids)
            await repository.replace_channels(user.id, subscription_id, channel_ids)

    async def set_enabled(self, telegram_user_id: int, subscription_id: int, enabled: bool) -> None:
        async with self.sessions.begin() as session:
            user = await UserRepository(session).get_or_create(telegram_user_id)
            row = await ScheduleRepository(session).get(user.id, subscription_id, for_update=True)
            if row is None:
                raise LookupError("Subscription not found")
            row.enabled = enabled

    async def rename(self, telegram_user_id: int, subscription_id: int, name: str) -> None:
        name = validate_name(name)
        async with self.sessions.begin() as session:
            user = await UserRepository(session).get_or_create(telegram_user_id)
            row = await ScheduleRepository(session).get(user.id, subscription_id, for_update=True)
            if row is None:
                raise LookupError("Subscription not found")
            row.name = name

    async def delete(self, telegram_user_id: int, subscription_id: int) -> None:
        async with self.sessions.begin() as session:
            user = await UserRepository(session).get_or_create(telegram_user_id)
            row = await ScheduleRepository(session).get(user.id, subscription_id, for_update=True)
            if row is None:
                raise LookupError("Subscription not found")
            await session.delete(row)  # Only this subscription and its channel links cascade.
