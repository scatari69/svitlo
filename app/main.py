import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from urllib.parse import unquote, urlsplit

import httpx
from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

from app.analytics.history import HistoryHandler
from app.analytics.service import AnalyticsService
from app.api.health import router
from app.api.homeassistant import router as homeassistant_router
from app.api.middleware import WebhookBodyLimit
from app.bot.app import create_dispatcher, run_polling
from app.bot.transport import TelegramFloodControl
from app.config import Settings
from app.db.session import create_engine, create_session_factory
from app.events.bus import EventBus
from app.events.logging import log_event
from app.events.models import DomainEvent, MonitorHealthChanged, PowerStateChanged, ScheduleChanged
from app.logging import configure_logging
from app.monitoring.service import MonitoringService
from app.notifications.health import MonitorHealthNotificationHandler
from app.notifications.schedules import ScheduleNotificationHandler
from app.notifications.service import PowerNotificationHandler
from app.schedules.fetching import SharedFetcher
from app.schedules.providers.svitlo import SvitloProvider
from app.schedules.service import ScheduleService
from app.services.channels import ChannelManagementService
from app.services.device_setup import DeviceSetupService
from app.services.devices import DeviceManagementService
from app.services.homeassistant import HomeAssistantWebhookService
from app.services.reports import ReportSettingsService
from app.services.subscriptions import SubscriptionManagementService
from app.workers.homeassistant import HomeAssistantWorker
from app.workers.ping import PingWorker
from app.workers.reports import ReportWorker
from app.workers.schedules import ScheduleWorker

logger = logging.getLogger(__name__)


async def stop_polling(task: asyncio.Task[None], grace_seconds: float = 10) -> None:
    task.cancel()
    done, _ = await asyncio.wait({task}, timeout=grace_seconds)
    if not done:
        logger.warning("Task shutdown deadline exceeded", extra={"worker": task.get_name()})
        task.cancel()
    elif not task.cancelled() and task.exception() is not None:
        logger.error("Background task stopped with an error", extra={"worker": task.get_name()})


async def stop_telegram(
    dispatcher: Dispatcher, task: asyncio.Task[None], grace_seconds: float
) -> None:
    if not task.done():
        try:
            async with asyncio.timeout(grace_seconds):
                await dispatcher.stop_polling()
        except Exception:
            logger.warning("Telegram polling shutdown required cancellation")
    await stop_polling(task, grace_seconds)


def create_app(settings: Settings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        config = settings or Settings()
        database_url = config.database_url.get_secret_value()
        redis_url = config.redis_url.get_secret_value()
        token = config.telegram_bot_token.get_secret_value() if config.telegram_bot_token else ""
        configure_logging(
            config.log_level,
            (
                database_url,
                redis_url,
                token,
                config.encryption_key.get_secret_value() if config.encryption_key else "",
                make_url(database_url).password or "",
                unquote(urlsplit(redis_url).password or ""),
            ),
        )
        async with AsyncExitStack() as stack:
            engine = create_engine(config)
            stack.push_async_callback(engine.dispose)
            redis = Redis.from_url(
                redis_url,
                socket_connect_timeout=config.health_timeout,
                socket_timeout=config.health_timeout,
                decode_responses=True,
                health_check_interval=30,
                socket_keepalive=True,
                # Reconnect on subsequent commands; do not replay ambiguous EVAL/SET mutations.
                retry=Retry(NoBackoff(), 0),
                retry_on_error=[],
            )
            stack.push_async_callback(redis.aclose)
            application.state.worker_tasks = {}
            application.state.accepting_requests = False
            application.state.settings = config
            application.state.engine = engine
            application.state.session_factory = create_session_factory(engine)
            application.state.analytics = AnalyticsService(application.state.session_factory)
            application.state.report_service = ReportSettingsService(
                application.state.session_factory
            )
            application.state.redis = redis
            schedule_client = await stack.enter_async_context(
                httpx.AsyncClient(follow_redirects=False, trust_env=False)
            )
            schedule_fetcher = SharedFetcher(
                redis,
                schedule_client,
                timeout=config.schedule_timeout,
                fresh_seconds=config.schedule_cache_seconds,
                retries=config.schedule_retries,
            )
            application.state.schedules = ScheduleService(
                SvitloProvider(provider, url, schedule_fetcher)
                for provider, url in config.schedule_sources.items()
            )
            event_bus = EventBus()
            history = HistoryHandler(application.state.session_factory)
            event_bus.subscribe(PowerStateChanged, history.handle)
            event_bus.subscribe(DomainEvent, log_event)
            application.state.event_bus = event_bus
            setup = DeviceSetupService(config.encryption_key, redis, config.public_base_url)
            application.state.subscription_service = SubscriptionManagementService(
                application.state.session_factory, application.state.schedules
            )
            application.state.device_service = DeviceManagementService(
                application.state.session_factory, setup
            )
            application.state.homeassistant_webhooks = HomeAssistantWebhookService(
                application.state.session_factory,
                redis,
                event_bus,
            )
            bot: Bot | None = None
            if config.bot_enabled:
                bot = Bot(token=token, session=AiohttpSession(timeout=15))
                bot.session.middleware.register(TelegramFloodControl())
                stack.push_async_callback(bot.session.close)
                notifications = PowerNotificationHandler(application.state.session_factory, bot)
                event_bus.subscribe(PowerStateChanged, notifications.handle)
                health_notifications = MonitorHealthNotificationHandler(
                    application.state.session_factory,
                    bot,
                    warnings_enabled=config.monitor_health_warnings_enabled,
                    recovery_enabled=config.monitor_health_recovery_enabled,
                    cooldown_seconds=config.monitor_health_warning_cooldown_seconds,
                )
                event_bus.subscribe(MonitorHealthChanged, health_notifications.handle)
                schedule_notifications = ScheduleNotificationHandler(
                    application.state.session_factory, bot, application.state.schedules
                )
                event_bus.subscribe(ScheduleChanged, schedule_notifications.handle)
                report_worker = ReportWorker(application.state.session_factory, bot)
                application.state.report_worker = report_worker
                report_task = asyncio.create_task(report_worker.run(), name="report-scheduler")
                application.state.worker_tasks["reports"] = report_task
                stack.push_async_callback(stop_polling, report_task, config.shutdown_timeout)
            if config.monitoring_enabled:
                monitoring = MonitoringService(
                    application.state.session_factory,
                    event_bus,
                    failure_threshold=config.monitor_failure_threshold,
                )
                ha_worker = HomeAssistantWorker(monitoring, config.encryption_key)
                application.state.homeassistant_worker = ha_worker
                worker = PingWorker(
                    engine,
                    monitoring,
                    homeassistant=ha_worker,
                    encryption_key=config.encryption_key,
                )
                application.state.ping_worker = worker
                monitoring_task = asyncio.create_task(worker.run(), name="ping-scheduler")
                application.state.worker_tasks["monitoring"] = monitoring_task
                stack.push_async_callback(stop_polling, monitoring_task, config.shutdown_timeout)
            if bot is not None:
                dispatcher = create_dispatcher(
                    application.state.device_service,
                    redis,
                    application.state.subscription_service,
                    schedule_notifications,
                    application.state.analytics,
                    application.state.report_service,
                    ChannelManagementService(application.state.session_factory, bot),
                )
                task = asyncio.create_task(run_polling(dispatcher, bot), name="telegram-polling")
                application.state.worker_tasks["telegram"] = task
                stack.push_async_callback(stop_telegram, dispatcher, task, config.shutdown_timeout)
            if config.schedule_sources:
                schedule_worker = ScheduleWorker(
                    application.state.session_factory, application.state.schedules, event_bus
                )
                schedule_task = asyncio.create_task(schedule_worker.run(), name="schedule-refresh")
                application.state.worker_tasks["schedules"] = schedule_task
                stack.push_async_callback(stop_polling, schedule_task, config.shutdown_timeout)
            application.state.accepting_requests = True
            logger.info("Application started")
            try:
                yield
            finally:
                application.state.accepting_requests = False
                logger.info("Application stopping")
        logger.info("Application stopped")

    application = FastAPI(title="Svitlo", lifespan=lifespan)
    application.add_middleware(WebhookBodyLimit)

    @application.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, error: RequestValidationError) -> JSONResponse:
        # Pydantic error dictionaries may contain submitted tokens/credentials and raw input.
        return JSONResponse(status_code=422, content={"detail": "Invalid request"})

    @application.exception_handler(SQLAlchemyError)
    async def database_unavailable(request: Request, error: SQLAlchemyError) -> JSONResponse:
        logger.error(
            "Database operation unavailable", extra={"exception_type": type(error).__name__}
        )
        return JSONResponse(status_code=503, content={"detail": "Service unavailable"})

    application.include_router(router)
    application.include_router(homeassistant_router)
    return application


app = create_app()
