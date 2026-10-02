import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import NotificationChannel, ReportSettings
from app.repositories.channels import ChannelView, NotificationChannelRepository
from app.repositories.devices import DeviceRepository
from app.repositories.reports import ReportRepository
from app.repositories.users import UserRepository


class ReportKind(StrEnum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


WEEKDAYS = ("Понеділок", "Вівторок", "Середа", "Четвер", "П’ятниця", "Субота", "Неділя")


@dataclass(frozen=True)
class ReportSettingsView:
    device_id: int
    channel_id: int
    device_name: str
    channel_name: str
    channel_enabled: bool
    daily_enabled: bool
    weekly_enabled: bool
    monthly_enabled: bool
    daily_time: int
    weekly_time: int
    monthly_time: int
    weekly_weekday: int


def clock_label(minutes: int) -> str:
    return f"{minutes // 60:02}:{minutes % 60:02}"


def settings_text(view: ReportSettingsView) -> str:
    def flag(enabled: bool) -> str:
        return "✅" if enabled else "❌"

    return (
        f"📊 Автоматичні звіти\n\n🏠 {view.device_name}\n🔔 {view.channel_name}\n\n"
        f"Щоденний: {flag(view.daily_enabled)} — {clock_label(view.daily_time)}\n"
        f"Щотижневий: {flag(view.weekly_enabled)} — {WEEKDAYS[view.weekly_weekday]}, "
        f"{clock_label(view.weekly_time)}\n"
        f"Щомісячний: {flag(view.monthly_enabled)} — 1 числа, {clock_label(view.monthly_time)}\n\n"
        "Час: Київ. Звіти містять лише завершений день, тиждень або місяць.\n"
        "Канал підключено до сповіщень пристрою."
        + ("\n\n⚠️ Канал вимкнено. Звіти не надсилатимуться." if not view.channel_enabled else "")
    )


class ReportSettingsService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def channels(self, telegram_user_id: int, device_id: int) -> list[ChannelView]:
        async with self.sessions.begin() as session:
            user = await UserRepository(session).get_or_create(telegram_user_id)
            if await DeviceRepository(session).get(user.id, device_id) is None:
                raise LookupError("Device not found")
            repository = NotificationChannelRepository(session)
            await repository.ensure_private(user.id, telegram_user_id)
            channels = await repository.list_owned(user.id)
            return [ChannelView(c.id, c.name, c.enabled) for c in channels]

    async def settings(
        self,
        telegram_user_id: int,
        device_id: int,
        channel_id: int,
        *,
        kind: ReportKind | None = None,
        enabled: bool | None = None,
        local_time: str | None = None,
        weekday: int | None = None,
    ) -> ReportSettingsView:
        if kind is not None:
            kind = ReportKind(kind)
        elif enabled is not None or local_time is not None or weekday is not None:
            raise ValueError("Report kind required")
        minutes = None
        if local_time is not None:
            if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", local_time):
                raise ValueError("Invalid local time")
            hour, minute = map(int, local_time.split(":"))
            minutes = hour * 60 + minute
        if weekday is not None and (kind != ReportKind.WEEKLY or not 0 <= weekday <= 6):
            raise ValueError("Invalid weekly weekday")
        async with self.sessions.begin() as session:
            user = await UserRepository(session).get_or_create(telegram_user_id)
            device = await DeviceRepository(session).get(user.id, device_id, for_update=True)
            channel = await session.get(NotificationChannel, channel_id)
            if device is None or channel is None or channel.user_id != user.id:
                raise LookupError("Device or channel not found")
            row = await ReportRepository(session).ensure(user.id, device_id, channel_id)
            if kind is not None:
                if enabled is not None:
                    if enabled and not getattr(row, f"{kind}_enabled"):
                        setattr(row, f"{kind}_enabled_at", datetime.now(UTC))
                    setattr(row, f"{kind}_enabled", enabled)
                if minutes is not None:
                    setattr(row, f"{kind}_time", minutes)
                if weekday is not None:
                    row.weekly_weekday = weekday
            await session.flush()
            return self._view(row, device.name, channel.name, channel.enabled)

    @staticmethod
    def _view(
        row: ReportSettings, device_name: str, channel_name: str, channel_enabled: bool
    ) -> ReportSettingsView:
        return ReportSettingsView(
            row.device_id,
            row.channel_id,
            device_name,
            channel_name,
            channel_enabled,
            row.daily_enabled,
            row.weekly_enabled,
            row.monthly_enabled,
            row.daily_time,
            row.weekly_time,
            row.monthly_time,
            row.weekly_weekday,
        )
