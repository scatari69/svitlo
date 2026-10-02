import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.events.models import PowerStateChanged
from app.models.enums import PowerState
from app.notifications.formatting import format_power_notification
from app.notifications.telegram import send_power_message
from app.repositories.channels import NotificationChannelRepository
from app.repositories.devices import DeviceRepository
from app.repositories.notifications import PowerNotificationRepository
from app.repositories.power import PowerIntervalRepository

logger = logging.getLogger(__name__)


class PowerNotificationHandler:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], bot: Bot) -> None:
        self.sessions, self.bot = sessions, bot

    async def handle(self, event: PowerStateChanged) -> None:
        if (event.previous_state, event.new_state) not in {
            (PowerState.ON, PowerState.OFF),
            (PowerState.OFF, PowerState.ON),
        }:
            return
        async with self.sessions.begin() as session:
            device = await DeviceRepository(session).get(event.user_id, event.device_id)
            if device is None or not device.enabled:
                return
            transition = await PowerIntervalRepository(session).transition_with_previous(
                event.user_id,
                event.device_id,
                event.previous_state,
                event.new_state,
                event.detected_at,
            )
            if transition is None:
                # History must confirm both states before calculating a duration.
                return
            interval, previous = transition
            interval_id = interval.id
            text = format_power_notification(
                device.name, event.new_state, event.detected_at, previous.duration()
            )
            channels = await NotificationChannelRepository(session).for_device(
                event.user_id, event.device_id
            )
            chat_ids = list(dict.fromkeys(channel.telegram_chat_id for channel in channels))
        for chat_id in chat_ids:
            async with self.sessions.begin() as session:
                active = await DeviceRepository(session).get(
                    event.user_id, event.device_id, for_update=True
                )
                if active is None or not active.enabled:
                    continue
                current_channels = await NotificationChannelRepository(session).for_device(
                    event.user_id, event.device_id
                )
                if chat_id not in {channel.telegram_chat_id for channel in current_channels}:
                    continue
                if not await PowerNotificationRepository(session).claim(interval_id, chat_id):
                    continue
            # Commit the reservation before sending; replays cannot resend it.
            status = "sent"
            try:
                await send_power_message(self.bot, chat_id, text)
            except (TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter):
                status = "failed"
                logger.warning(
                    "Power notification rejected device_id=%s chat_id=%s", event.device_id, chat_id
                )
            except Exception:
                status = "uncertain"
                logger.exception(
                    "Power notification unconfirmed device_id=%s chat_id=%s",
                    event.device_id,
                    chat_id,
                )
            try:
                async with self.sessions.begin() as session:
                    await PowerNotificationRepository(session).finish(interval_id, chat_id, status)
            except Exception:
                logger.exception(
                    "Power notification status not saved device_id=%s chat_id=%s",
                    event.device_id,
                    chat_id,
                )
