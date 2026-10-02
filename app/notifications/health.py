import logging
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.events.models import MonitorHealthChanged
from app.models.enums import MonitorHealth
from app.notifications.telegram import send_power_message
from app.repositories.channels import NotificationChannelRepository
from app.repositories.devices import DeviceRepository
from app.repositories.notifications import MonitorHealthNotificationRepository
from app.services.time import aware_utc

logger = logging.getLogger(__name__)


def format_health_notification(name: str, last_success: datetime | None, *, recovery: bool) -> str:
    if recovery:
        return f"✅ Перевірку стану пристрою відновлено\n\n🏠 {name}"
    checked = (
        aware_utc(last_success).astimezone(ZoneInfo("Europe/Kyiv")).strftime("%H:%M")
        if last_success is not None
        else "Ще не було"
    )
    return (
        f"⚠️ Не вдалося перевірити стан пристрою\n\n🏠 {name}\n\n"
        f"Остання успішна перевірка:\n{checked}"
    )


class MonitorHealthNotificationHandler:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        bot: Bot,
        *,
        warnings_enabled: bool = False,
        recovery_enabled: bool = False,
        cooldown_seconds: int = 3600,
    ) -> None:
        if not 60 <= cooldown_seconds <= 604800:
            raise ValueError("Invalid monitor warning cooldown")
        self.sessions, self.bot = sessions, bot
        self.warnings_enabled, self.recovery_enabled = warnings_enabled, recovery_enabled
        self.cooldown_seconds = cooldown_seconds

    async def handle(self, event: MonitorHealthChanged) -> None:
        if not self.warnings_enabled and not self.recovery_enabled:
            return
        recovery = event.new_health == MonitorHealth.HEALTHY
        if event.new_health not in {MonitorHealth.UNAVAILABLE, MonitorHealth.HEALTHY}:
            return
        if not recovery and not self.warnings_enabled:
            return
        async with self.sessions.begin() as session:
            channels = await NotificationChannelRepository(session).for_device(
                event.user_id, event.device_id
            )
            chat_ids = list(dict.fromkeys(channel.telegram_chat_id for channel in channels))
        for chat_id in chat_ids:
            try:
                async with self.sessions.begin() as session:
                    device = await DeviceRepository(session).get(
                        event.user_id, event.device_id, for_update=True
                    )
                    if (
                        device is None
                        or not device.enabled
                        or device.monitor_health != event.new_health
                        or device.monitor_health_changed_at != event.detected_at
                    ):
                        continue
                    # Recheck assignments/enablement at reservation time.
                    channels = await NotificationChannelRepository(session).for_device(
                        event.user_id, event.device_id
                    )
                    if chat_id not in {channel.telegram_chat_id for channel in channels}:
                        continue
                    if not await MonitorHealthNotificationRepository(session).claim(
                        event.device_id,
                        chat_id,
                        event.detected_at,
                        recovery=recovery,
                        recovery_enabled=self.recovery_enabled,
                        now=datetime.now(UTC),
                        cooldown_seconds=self.cooldown_seconds,
                    ):
                        continue
                    text = format_health_notification(
                        device.name, device.last_successful_check_at, recovery=recovery
                    )
                # Durable reservation precedes delivery, including ambiguous transport failures.
                await send_power_message(self.bot, chat_id, text)
                if not recovery:
                    async with self.sessions.begin() as session:
                        await MonitorHealthNotificationRepository(session).finish_warning(
                            event.device_id, chat_id, event.detected_at
                        )
            except Exception:
                # Exception text can include Telegram credentials; use only safe identifiers.
                logger.warning(
                    "Monitor health notification unavailable device_id=%s chat_id=%s",
                    event.device_id,
                    chat_id,
                )
