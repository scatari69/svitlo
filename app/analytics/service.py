"""Statistics derived exclusively from actual device intervals."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import PowerInterval
from app.models.enums import PowerState
from app.repositories.devices import DeviceRepository
from app.repositories.power import PowerIntervalRepository
from app.repositories.users import UserRepository
from app.services.time import aware_utc

KYIV = ZoneInfo("Europe/Kyiv")


class AnalyticsPeriod(StrEnum):
    TODAY = "today"
    YESTERDAY = "yesterday"
    LAST_7_DAYS = "last_7_days"
    CALENDAR_WEEK = "calendar_week"
    CURRENT_MONTH = "current_month"
    PREVIOUS_MONTH = "previous_month"


@dataclass(frozen=True)
class ReportingWindow:
    """Half-open UTC bounds determined from Kyiv calendar boundaries."""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "start", aware_utc(self.start))
        object.__setattr__(self, "end", aware_utc(self.end))
        if self.end <= self.start:
            raise ValueError("Period end must follow start")


def period_window(period: AnalyticsPeriod, *, now: datetime) -> ReportingWindow:
    today = aware_utc(now).astimezone(KYIV).date()
    period = AnalyticsPeriod(period)
    if period == AnalyticsPeriod.TODAY:
        start, end = today, today + timedelta(days=1)
    elif period == AnalyticsPeriod.YESTERDAY:
        start, end = today - timedelta(days=1), today
    elif period == AnalyticsPeriod.LAST_7_DAYS:
        start, end = today - timedelta(days=6), today + timedelta(days=1)
    elif period == AnalyticsPeriod.CALENDAR_WEEK:
        start = today - timedelta(days=today.weekday())
        end = start + timedelta(days=7)
    else:
        first = today.replace(day=1)
        if period == AnalyticsPeriod.PREVIOUS_MONTH:
            start, end = (first - timedelta(days=1)).replace(day=1), first
        else:
            start = first
            end = date(first.year + first.month // 12, first.month % 12 + 1, 1)
    return ReportingWindow(
        datetime.combine(start, time(), KYIV), datetime.combine(end, time(), KYIV)
    )


@dataclass(frozen=True)
class PowerStatistics:
    window: ReportingWindow
    measured_until: datetime
    on_duration: timedelta
    off_duration: timedelta
    unknown_duration: timedelta
    outage_count: int
    longest_outage: timedelta
    average_outage_duration: timedelta

    @property
    def known_duration(self) -> timedelta:
        return self.on_duration + self.off_duration

    @property
    def availability_percentage(self) -> float | None:
        """Percentage of known time; undefined when no confirmed history exists."""
        return 100 * self.on_duration / self.known_duration if self.known_duration else None


def calculate_statistics(
    records: Iterable[PowerInterval], window: ReportingWindow, *, now: datetime
) -> PowerStatistics:
    """Clip actual history to elapsed time; gaps are UNKNOWN, never implicit ON or OFF.

    Outages are contiguous OFF episodes overlapping the window, including ongoing
    episodes and ones beginning before the window. Lengths refer only to the portion
    inside the window, so totals, longest and average use the same denominator.
    Invalid overlapping history is rejected rather than silently double-counted.
    """
    until = min(window.end, aware_utc(now))
    if until < window.start:
        raise ValueError("Reporting window starts in the future")
    rows = []
    device_ids = set()
    for record in records:
        start = aware_utc(record.started_at)
        end = aware_utc(record.ended_at) if record.ended_at is not None else until
        if record.ended_at is not None and end < start:
            raise ValueError("Interval end precedes start")
        state = PowerState(record.state)
        start, end = max(start, window.start), min(end, until)
        if start < end:
            rows.append((start, end, state))
            device_ids.add(record.device_id)
    if len(device_ids) > 1:
        raise ValueError("Statistics require a single device")
    rows.sort(key=lambda row: row[0])
    on, off = timedelta(), timedelta()
    outages: list[timedelta] = []
    cursor = window.start
    previous_state: PowerState | None = None
    for start, end, state in rows:
        if start < cursor:
            raise ValueError("Overlapping power history")
        duration = end - start
        if state == PowerState.ON:
            on += duration
        elif state == PowerState.OFF:
            off += duration
            if previous_state == PowerState.OFF and start == cursor:
                outages[-1] += duration
            else:
                outages.append(duration)
        previous_state, cursor = state, end
    return PowerStatistics(
        window=window,
        measured_until=until,
        on_duration=on,
        off_duration=off,
        unknown_duration=until - window.start - on - off,
        outage_count=len(outages),
        longest_outage=max(outages, default=timedelta()),
        average_outage_duration=off / len(outages) if outages else timedelta(),
    )


class AnalyticsService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def get_statistics(
        self,
        user_id: int,
        device_id: int,
        period: AnalyticsPeriod,
        *,
        now: datetime | None = None,
    ) -> PowerStatistics:
        observed_at = aware_utc(now if now is not None else datetime.now(UTC))
        window = period_window(period, now=observed_at)
        async with self.sessions.begin() as session:
            if await DeviceRepository(session).get(user_id, device_id) is None:
                raise LookupError("Device not found")
            if window.start == observed_at:
                records = []
            else:
                records = await PowerIntervalRepository(session).list_overlapping(
                    user_id, device_id, window.start, min(window.end, observed_at)
                )
            return calculate_statistics(records, window, now=observed_at)

    async def get_for_telegram(
        self,
        telegram_user_id: int,
        device_id: int,
        period: AnalyticsPeriod,
        *,
        now: datetime | None = None,
    ) -> PowerStatistics:
        async with self.sessions.begin() as session:
            user = await UserRepository(session).get_or_create(telegram_user_id)
            user_id = user.id
        return await self.get_statistics(user_id, device_id, period, now=now)
