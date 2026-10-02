from datetime import date
from typing import Protocol

from app.schedules.models import DaySchedule, OutageGroup, ProviderResult, Region


class ScheduleProvider(Protocol):
    id: str

    async def get_regions(self) -> ProviderResult[tuple[Region, ...]]: ...
    async def get_queues(self, region: str) -> ProviderResult[tuple[OutageGroup, ...]]: ...
    async def get_schedule(
        self, region: str, queue: str, day: date
    ) -> ProviderResult[DaySchedule]: ...
