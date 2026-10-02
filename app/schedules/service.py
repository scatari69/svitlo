import logging
from collections.abc import Awaitable, Callable, Iterable
from datetime import date, datetime

from app.schedules.base import ScheduleProvider
from app.schedules.models import KYIV, DaySchedule, Freshness, OutageGroup, ProviderResult, Region

logger = logging.getLogger(__name__)


class ScheduleService:
    def __init__(self, providers: Iterable[ScheduleProvider]) -> None:
        self.providers = {provider.id: provider for provider in providers}

    async def _call[T](
        self, provider: str, operation: Callable[[ScheduleProvider], Awaitable[ProviderResult[T]]]
    ) -> ProviderResult[T]:
        source = self.providers.get(provider)
        if source is None:
            return ProviderResult(None, Freshness.UNAVAILABLE, error="provider_not_found")
        try:
            return await operation(source)
        except Exception:
            logger.warning("Schedule provider failed provider=%s", source.id)
            return ProviderResult(None, Freshness.UNAVAILABLE, error="provider_unavailable")

    async def get_regions(self, provider: str) -> ProviderResult[tuple[Region, ...]]:
        return await self._call(provider, lambda source: source.get_regions())

    async def get_queues(
        self, provider: str, region: str
    ) -> ProviderResult[tuple[OutageGroup, ...]]:
        return await self._call(provider, lambda source: source.get_queues(region))

    async def get_schedule(
        self, provider: str, region: str, queue: str, day: date | None = None
    ) -> ProviderResult[DaySchedule]:
        requested = day if day is not None else datetime.now(KYIV).date()
        return await self._call(
            provider, lambda source: source.get_schedule(region, queue, requested)
        )
