from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.services.time import aware_utc

KYIV = ZoneInfo("Europe/Kyiv")


class ScheduleState(StrEnum):
    ON = "on"
    OFF = "off"
    UNKNOWN = "unknown"


class Freshness(StrEnum):
    FRESH = "fresh"
    CACHED = "cached"
    UNCHANGED = "unchanged"
    STALE = "stale"
    UNAVAILABLE = "unavailable"


class DomainModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Region(DomainModel):
    provider: str = Field(min_length=1, max_length=100)
    id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=255)


class OutageGroup(DomainModel):
    provider: str = Field(min_length=1, max_length=100)
    region: str = Field(min_length=1, max_length=100)
    id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=255)


def day_bounds(day: date) -> tuple[datetime, datetime]:
    return (
        datetime.combine(day, time(), KYIV).astimezone(UTC),
        datetime.combine(day + timedelta(days=1), time(), KYIV).astimezone(UTC),
    )


class ScheduleSlot(DomainModel):
    start: datetime
    end: datetime
    state: ScheduleState

    @field_validator("start", "end")
    @classmethod
    def utc_timestamp(cls, value: datetime) -> datetime:
        return aware_utc(value)

    @model_validator(mode="after")
    def ordered(self) -> "ScheduleSlot":
        if self.end <= self.start:
            raise ValueError("Schedule slots must have positive duration")
        return self

    @property
    def duration(self) -> timedelta:
        return self.end - self.start


class DaySchedule(DomainModel):
    provider: str = Field(min_length=1, max_length=100)
    region: str = Field(min_length=1, max_length=100)
    group: str = Field(min_length=1, max_length=100)
    date: date
    timezone: Literal["Europe/Kyiv"] = "Europe/Kyiv"
    slots: tuple[ScheduleSlot, ...]
    emergency: bool = False

    @model_validator(mode="after")
    def complete_day(self) -> "DaySchedule":
        start, end = day_bounds(self.date)
        cursor = start
        for slot in self.slots:
            if slot.start != cursor:
                raise ValueError("Schedule slots must be contiguous and ordered")
            cursor = slot.end
        if not self.slots or cursor != end:
            raise ValueError("Schedule must cover the complete Kyiv day")
        return self


class SourceDocument(DomainModel):
    regions: tuple[Region, ...]
    groups: tuple[OutageGroup, ...]
    days: tuple[DaySchedule, ...]

    @model_validator(mode="after")
    def unique_catalog(self) -> "SourceDocument":
        regions = {(r.provider, r.id) for r in self.regions}
        groups = {(g.provider, g.region, g.id) for g in self.groups}
        days = {(d.provider, d.region, d.group, d.date) for d in self.days}
        if (
            not regions
            or len(regions) != len(self.regions)
            or len(groups) != len(self.groups)
            or len(days) != len(self.days)
        ):
            raise ValueError("Empty or duplicate schedule catalog")
        if any((g.provider, g.region) not in regions for g in self.groups) or any(
            (d.provider, d.region, d.group) not in groups for d in self.days
        ):
            raise ValueError("Schedule catalog references missing entities")
        return self


@dataclass(frozen=True)
class ProviderResult[T]:
    data: T | None
    freshness: Freshness
    fetched_at: datetime | None = None
    checked_at: datetime | None = None
    error: str | None = None
