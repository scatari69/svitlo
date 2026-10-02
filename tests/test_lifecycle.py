import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import SecretStr
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from app.analytics.service import AnalyticsService
from app.config import Settings
from app.main import create_app


async def test_shutdown_cancels_polling_and_closes_resources(settings: Settings) -> None:
    settings.bot_enabled = True
    settings.telegram_bot_token = SecretStr("123456:TEST_TOKEN")
    engine = MagicMock(spec=AsyncEngine)
    redis = AsyncMock(spec=Redis)
    bot = MagicMock()
    bot.session.close = AsyncMock()
    dispatcher = MagicMock()
    dispatcher.storage.close = AsyncMock()
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def polling(*args: object) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    with (
        patch("app.main.create_engine", return_value=engine),
        patch("app.main.Redis.from_url", return_value=redis),
        patch("app.main.Bot", return_value=bot),
        patch("app.main.create_dispatcher", return_value=dispatcher),
        patch("app.main.run_polling", side_effect=polling),
        patch("app.main.configure_logging"),
    ):
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            async with asyncio.timeout(1):
                await started.wait()
        assert cancelled.is_set()
    bot.session.close.assert_awaited_once()
    dispatcher.storage.close.assert_not_awaited()
    redis.aclose.assert_awaited_once()
    engine.dispose.assert_awaited_once()


async def test_startup_failure_closes_already_created_resources(settings: Settings) -> None:
    engine = MagicMock(spec=AsyncEngine)
    with (
        patch("app.main.create_engine", return_value=engine),
        patch("app.main.Redis.from_url", side_effect=RuntimeError("Initialization failed")),
        patch("app.main.configure_logging"),
    ):
        app = create_app(settings)
        with pytest.raises(RuntimeError, match="Initialization failed"):
            async with app.router.lifespan_context(app):
                pytest.fail("Startup must fail")
    engine.dispose.assert_awaited_once()


async def test_shutdown_cancels_ping_worker_before_closing_resources(settings: Settings) -> None:
    settings.monitoring_enabled = True
    engine = MagicMock(spec=AsyncEngine)
    redis = AsyncMock(spec=Redis)
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def run() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def dispose() -> None:
        assert cancelled.is_set()

    engine.dispose.side_effect = dispose
    with (
        patch("app.main.create_engine", return_value=engine),
        patch("app.main.Redis.from_url", return_value=redis),
        patch("app.main.PingWorker.run", side_effect=run),
        patch("app.main.configure_logging"),
    ):
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            async with asyncio.timeout(1):
                await started.wait()
            assert app.state.ping_worker is not None
        assert cancelled.is_set()
    redis.aclose.assert_awaited_once()
    engine.dispose.assert_awaited_once()


async def test_schedule_transport_is_lazy_and_closes_on_shutdown(settings: Settings) -> None:
    engine = MagicMock(spec=AsyncEngine)
    redis = AsyncMock(spec=Redis)
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    with (
        patch("app.main.create_engine", return_value=engine),
        patch("app.main.Redis.from_url", return_value=redis),
        patch("app.main.httpx.AsyncClient", return_value=client),
        patch("app.main.configure_logging"),
    ):
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            assert isinstance(app.state.analytics, AnalyticsService)
            assert set(app.state.schedules.providers) == {"svitlo_live", "dtek"}
            client.stream.assert_not_called()
        client.__aexit__.assert_awaited_once()


async def test_report_worker_runs_without_monitoring_and_stops_before_bot_closes(
    settings: Settings,
) -> None:
    settings.bot_enabled = True
    settings.monitoring_enabled = False
    settings.telegram_bot_token = SecretStr("123456:TEST_TOKEN")
    engine = MagicMock(spec=AsyncEngine)
    redis = AsyncMock(spec=Redis)
    bot = MagicMock()
    bot.session.close = AsyncMock()
    dispatcher = MagicMock()
    dispatcher.storage.close = AsyncMock()
    started, stopped = asyncio.Event(), asyncio.Event()

    async def run() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    async def close() -> None:
        assert stopped.is_set()

    bot.session.close.side_effect = close
    with (
        patch("app.main.create_engine", return_value=engine),
        patch("app.main.Redis.from_url", return_value=redis),
        patch("app.main.Bot", return_value=bot),
        patch("app.main.create_dispatcher", return_value=dispatcher),
        patch("app.main.run_polling", new=AsyncMock()),
        patch("app.main.ReportWorker.run", side_effect=run),
        patch("app.main.configure_logging"),
    ):
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            async with asyncio.timeout(1):
                await started.wait()
            assert app.state.report_worker is not None
            assert app.state.report_service is not None
        assert stopped.is_set()
    bot.session.close.assert_awaited_once()
