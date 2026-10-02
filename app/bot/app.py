import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.redis import RedisEventIsolation, RedisStorage
from aiogram.types import ErrorEvent, Message
from redis.asyncio import Redis

from app.analytics.service import AnalyticsService
from app.bot.handlers.channels import create_router as create_channel_router
from app.bot.handlers.devices import create_router as create_device_router
from app.bot.handlers.reports import create_router as create_report_router
from app.bot.handlers.schedule_notifications import (
    create_router as create_schedule_notification_router,
)
from app.bot.handlers.start import create_router
from app.bot.handlers.statistics import create_router as create_statistics_router
from app.bot.handlers.subscriptions import create_router as create_subscription_router
from app.notifications.schedules import ScheduleNotificationHandler
from app.services.channels import ChannelManagementService
from app.services.devices import DeviceManagementService
from app.services.reports import ReportSettingsService
from app.services.subscriptions import SubscriptionManagementService

logger = logging.getLogger(__name__)


def create_dispatcher(
    device_service: DeviceManagementService | None = None,
    redis: Redis | None = None,
    subscription_service: SubscriptionManagementService | None = None,
    schedule_notifications: ScheduleNotificationHandler | None = None,
    analytics_service: AnalyticsService | None = None,
    report_service: ReportSettingsService | None = None,
    channel_service: ChannelManagementService | None = None,
) -> Dispatcher:
    dispatcher = Dispatcher(
        storage=RedisStorage(redis, state_ttl=1800, data_ttl=1800) if redis is not None else None,
        events_isolation=RedisEventIsolation(redis, lock_kwargs={"timeout": 180})
        if redis is not None
        else None,
    )

    async def close_updates() -> None:
        # aiogram tracks handler tasks but does not drain them when polling stops.
        tasks = list(dispatcher._handle_update_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    dispatcher.shutdown.register(close_updates)
    dispatcher.errors.register(handle_error)
    dispatcher.include_router(create_router())
    if channel_service is not None:
        dispatcher["channel_service"] = channel_service
        dispatcher.include_router(create_channel_router())
    if device_service is not None:
        dispatcher["device_service"] = device_service
        dispatcher.include_router(create_device_router())
    if subscription_service is not None:
        dispatcher["subscription_service"] = subscription_service
        dispatcher.include_router(create_subscription_router())
    if schedule_notifications is not None:
        dispatcher["schedule_notifications"] = schedule_notifications
        dispatcher.include_router(create_schedule_notification_router())
    if analytics_service is not None and device_service is not None:
        dispatcher["analytics_service"] = analytics_service
        dispatcher.include_router(create_statistics_router())
    if report_service is not None and device_service is not None:
        dispatcher["report_service"] = report_service
        dispatcher.include_router(create_report_router())
    return dispatcher


async def run_polling(dispatcher: Dispatcher, bot: Bot) -> None:
    delay = 1
    while True:
        try:
            await dispatcher.start_polling(
                bot, handle_signals=False, close_bot_session=False, tasks_concurrency_limit=32
            )
            return
        except Exception:
            logger.warning("Telegram polling restarting", extra={"worker": "telegram"})
        await asyncio.sleep(delay)
        delay = min(delay * 2, 30)


async def handle_error(event: ErrorEvent) -> bool:
    logger.error(
        "Telegram interaction failed", extra={"exception_type": type(event.exception).__name__}
    )
    try:
        if event.update.callback_query is not None:
            await event.update.callback_query.answer(
                "❌ Сервіс тимчасово недоступний. Спробуйте пізніше.", show_alert=True
            )
        elif (
            isinstance(event.update.message, Message)
            and event.update.message.chat.type == "private"
        ):
            await event.update.message.answer(
                "❌ Сервіс тимчасово недоступний. Спробуйте пізніше.", parse_mode=None
            )
    except Exception:
        logger.warning("Telegram error response unavailable")
    return True
