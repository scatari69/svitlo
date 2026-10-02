import json
from datetime import date

from pydantic import BaseModel, Field, StrictInt

from app.schedules.fetching import SharedFetcher
from app.schedules.models import (
    DaySchedule,
    Freshness,
    OutageGroup,
    ProviderResult,
    Region,
    ScheduleState,
    SourceDocument,
)
from app.schedules.normalizer import normalize_half_hours


class RawRegion(BaseModel):
    cpu: str
    name_ua: str
    emergency: bool = False
    schedule: dict[str, dict[str, dict[str, StrictInt]]] | None = None


class RawDocument(BaseModel):
    regions: tuple[RawRegion, ...] = Field(min_length=1)


def decode_document(raw: bytes, provider: str) -> SourceDocument:
    payload = json.loads(raw)
    if isinstance(payload, dict) and isinstance(payload.get("body"), str):
        if payload.get("statusCode", 200) != 200:
            raise ValueError("Invalid wrapped response")
        payload = json.loads(payload["body"])
    source = RawDocument.model_validate(payload)
    regions: list[Region] = []
    groups: list[OutageGroup] = []
    days: list[DaySchedule] = []
    for region in source.regions:
        if region.schedule is None:
            continue
        if not region.schedule:
            raise ValueError("Empty region schedule")
        regions.append(Region(provider=provider, id=region.cpu, name=region.name_ua))
        for queue, schedules in region.schedule.items():
            if not schedules:
                raise ValueError("Empty group schedule")
            groups.append(
                OutageGroup(provider=provider, region=region.cpu, id=queue, name=f"Група {queue}")
            )
            for day_key, labels in schedules.items():
                day = date.fromisoformat(day_key)
                if day.isoformat() != day_key or not labels:
                    raise ValueError("Invalid or empty day schedule")
                values = {
                    label: {1: ScheduleState.ON, 2: ScheduleState.OFF}.get(
                        code, ScheduleState.UNKNOWN
                    )
                    for label, code in labels.items()
                }
                days.append(
                    normalize_half_hours(
                        provider, region.cpu, queue, day, values, emergency=region.emergency
                    )
                )
    return SourceDocument(regions=tuple(regions), groups=tuple(groups), days=tuple(days))


class SvitloProvider:
    """One bulk source serves every region, group, date and subscriber."""

    def __init__(self, id: str, url: str, fetcher: SharedFetcher) -> None:
        self.id, self.url, self.fetcher = id, url, fetcher

    async def document(self) -> ProviderResult[SourceDocument]:
        return await self.fetcher.get(self.url, lambda raw: decode_document(raw, self.id))

    async def get_regions(self) -> ProviderResult[tuple[Region, ...]]:
        result = await self.document()
        return ProviderResult(
            tuple(sorted(result.data.regions, key=lambda r: r.name)) if result.data else None,
            result.freshness,
            result.fetched_at,
            result.checked_at,
            result.error,
        )

    async def get_queues(self, region: str) -> ProviderResult[tuple[OutageGroup, ...]]:
        result = await self.document()
        if result.data and not any(r.id == region for r in result.data.regions):
            return ProviderResult(None, Freshness.UNAVAILABLE, error="region_not_found")
        return ProviderResult(
            tuple(g for g in result.data.groups if g.region == region) if result.data else None,
            result.freshness,
            result.fetched_at,
            result.checked_at,
            result.error,
        )

    async def get_schedule(self, region: str, queue: str, day: date) -> ProviderResult[DaySchedule]:
        result = await self.document()
        schedule = (
            next(
                (
                    d
                    for d in result.data.days
                    if d.region == region and d.group == queue and d.date == day
                ),
                None,
            )
            if result.data
            else None
        )
        return ProviderResult(
            schedule,
            result.freshness if schedule else Freshness.UNAVAILABLE,
            result.fetched_at,
            result.checked_at,
            result.error if schedule else result.error or "schedule_not_found",
        )
