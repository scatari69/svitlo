import asyncio
import logging
from datetime import UTC, datetime
from itertools import batched

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analytics.charts import render_report_chart
from app.analytics.report_schedule import DueReport, due_reports
from app.analytics.reports import format_statistics
from app.analytics.service import calculate_statistics
from app.models import Device, NotificationChannel
from app.notifications.telegram import send_report_photo
from app.repositories.devices import DeviceRepository
from app.repositories.power import PowerIntervalRepository
from app.repositories.reports import ReportRepository
from app.services.time import aware_utc

logger = logging.getLogger(__name__)


class ReportWorker:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], bot: Bot) -> None:
        self.sessions, self.bot = sessions, bot
        self._limit = asyncio.Semaphore(8)

    async def run(self) -> None:
        while True:
            try:
                await self.refresh()
            except Exception:
                logger.exception("Report discovery unavailable worker=reports")
            await asyncio.sleep(30)

    async def refresh(self, *, now: datetime | None = None) -> None:
        observed = aware_utc(now if now is not None else datetime.now(UTC))
        async with self.sessions.begin() as session:
            targets = await ReportRepository(session).active()
        deliveries = (
            (device, channel.id, due)
            for settings, device, channel in targets
            for due in due_reports(settings, observed)
        )
        for batch in batched(deliveries, 8, strict=False):
            await asyncio.gather(
                *(
                    self._deliver(device, channel_id, due, observed)
                    for device, channel_id, due in batch
                )
            )

    async def _current(
        self, session: AsyncSession, device: Device, channel_id: int, due: DueReport, now: datetime
    ) -> NotificationChannel | None:
        # Match channel assignment/settings writers: device before report settings.
        active_device = await DeviceRepository(session).get(
            device.user_id, device.id, for_update=True
        )
        if active_device is None:
            return None
        settings = await ReportRepository(session).settings(device.id, channel_id, lock=True)
        channel = await session.get(NotificationChannel, channel_id)
        if (
            settings is None
            or channel is None
            or not channel.enabled
            or channel.user_id != device.user_id
            or due not in due_reports(settings, now)
        ):
            return None
        if await ReportRepository(session).reserved(
            device.id, channel.telegram_chat_id, due.kind.value, due.window
        ):
            return None
        return channel

    async def _deliver(
        self, device: Device, channel_id: int, due: DueReport, now: datetime
    ) -> None:
        async with self._limit:
            try:
                async with self.sessions.begin() as session:
                    if await self._current(session, device, channel_id, due, now) is None:
                        return
                    history = await PowerIntervalRepository(session).list_overlapping(
                        device.user_id, device.id, due.window.start, due.window.end
                    )
                    statistics = calculate_statistics(history, due.window, now=now)
                # Pillow work stays off the event loop and outside the database transaction.
                image = await asyncio.to_thread(
                    render_report_chart, statistics, history, device.name
                )
                caption = format_statistics(statistics, device.name)
                async with self.sessions.begin() as session:
                    channel = await self._current(session, device, channel_id, due, now)
                    if channel is None:
                        return
                    chat_id = channel.telegram_chat_id
                    if not await ReportRepository(session).claim(
                        device.id, chat_id, due.kind.value, due.window, now
                    ):
                        return
                status = "sent"
                try:
                    await self._send_photo(chat_id, image, caption)
                except (TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter):
                    status = "failed"
                    logger.warning(
                        "Report rejected device_id=%s channel_id=%s kind=%s",
                        device.id,
                        channel_id,
                        due.kind.value,
                    )
                except Exception:
                    status = "uncertain"
                    logger.exception(
                        "Report unconfirmed device_id=%s channel_id=%s kind=%s",
                        device.id,
                        channel_id,
                        due.kind.value,
                    )
                async with self.sessions.begin() as session:
                    await ReportRepository(session).finish(
                        device.id, chat_id, due.kind.value, due.window, status
                    )
            except Exception:
                logger.exception(
                    "Report failed device_id=%s channel_id=%s kind=%s",
                    device.id,
                    channel_id,
                    due.kind.value,
                )

    async def _send_photo(self, chat_id: int, image: bytes, caption: str) -> None:
        await send_report_photo(self.bot, chat_id, image, caption)
