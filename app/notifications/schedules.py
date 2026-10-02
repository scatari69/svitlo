import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.events.models import ScheduleChanged
from app.models import ScheduleVersion
from app.notifications.schedule_formatting import format_schedule_diff, format_schedule_notification
from app.notifications.telegram import send_power_message
from app.repositories.notifications import ScheduleNotificationRepository
from app.repositories.schedules import ScheduleRepository
from app.schedules.diff import schedule_diff
from app.schedules.models import DaySchedule
from app.schedules.service import ScheduleService

logger = logging.getLogger(__name__)


def load_schedule(version: ScheduleVersion) -> DaySchedule:
    schedule = DaySchedule.model_validate(version.normalized_content)
    if (schedule.provider, schedule.region, schedule.group, schedule.date) != (
        version.provider,
        version.region,
        version.queue,
        version.schedule_date,
    ):
        raise ValueError("Schedule snapshot source mismatch")
    return schedule


class ScheduleNotificationHandler:
    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], bot: Bot, schedules: ScheduleService
    ) -> None:
        self.sessions, self.bot, self.schedules = sessions, bot, schedules

    async def handle(self, event: ScheduleChanged) -> None:
        async with self.sessions.begin() as session:
            repository = ScheduleRepository(session)
            version = await repository.version(event.new_version_id)
            if version is None or (
                version.provider,
                version.region,
                version.queue,
                version.schedule_date,
            ) != (event.provider, event.region, event.queue, event.schedule_date):
                return
            previous = None
            try:
                current = load_schedule(version)
                if event.previous_version_id is not None:
                    old = await repository.version(event.previous_version_id)
                    if old is None or old.fetched_at >= version.fetched_at:
                        return
                    previous = load_schedule(old)
                    changes = schedule_diff(previous, current)
                    if not changes and previous.emergency == current.emergency:
                        return
            except ValueError:
                logger.warning("Invalid schedule notification snapshot version_id=%s", version.id)
                return
            chats = await ScheduleNotificationRepository(session).chat_ids(
                event.provider, event.region, event.queue
            )
        if not chats:
            return
        # Resolve the shared catalog once for the event, never once per recipient.
        regions = await self.schedules.get_regions(event.provider)
        region_name = next(
            (region.name for region in regions.data or () if region.id == event.region),
            "Регіон недоступний",
        )
        text = format_schedule_notification(current, region_name)
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🔎 Що змінилося?", callback_data=f"schedule_diff:{version.id}"
                    )
                ]
            ]
        )
        for chat_id in chats:
            async with self.sessions.begin() as session:
                current_chats = await ScheduleNotificationRepository(session).chat_ids(
                    event.provider, event.region, event.queue
                )
                if chat_id not in current_chats:
                    continue
                if not await ScheduleNotificationRepository(session).claim(
                    version.id, chat_id, event.previous_version_id
                ):
                    continue
            status = "sent"
            try:
                await send_power_message(self.bot, chat_id, text, markup)
            except (TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter):
                status = "failed"
                logger.warning(
                    "Schedule notification rejected version_id=%s chat_id=%s", version.id, chat_id
                )
            except Exception:
                status = "uncertain"
                logger.exception(
                    "Schedule notification unconfirmed version_id=%s chat_id=%s",
                    version.id,
                    chat_id,
                )
            try:
                async with self.sessions.begin() as session:
                    await ScheduleNotificationRepository(session).finish(
                        version.id, chat_id, status
                    )
            except Exception:
                logger.exception(
                    "Schedule notification status not saved version_id=%s chat_id=%s",
                    version.id,
                    chat_id,
                )

    async def diff_for_chat(self, version_id: int, chat_id: int) -> str | None:
        async with self.sessions.begin() as session:
            delivery = await ScheduleNotificationRepository(session).get(version_id, chat_id)
            if delivery is None:
                return None
            repository = ScheduleRepository(session)
            version = await repository.version(version_id)
            old = (
                await repository.version(delivery.previous_version_id)
                if (delivery.previous_version_id is not None)
                else None
            )
            if version is None or (delivery.previous_version_id is not None and old is None):
                return None
            try:
                return format_schedule_diff(
                    load_schedule(old) if old else None, load_schedule(version)
                )
            except ValueError:
                logger.warning("Invalid schedule diff snapshot version_id=%s", version_id)
                return None
