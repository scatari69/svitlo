import asyncio
import json
import logging
import sqlite3
from collections.abc import Awaitable
from datetime import UTC, date, datetime, timedelta
from email.utils import format_datetime
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import GetChat, SendMessage
from redis.asyncio import Redis
from redis.asyncio.connection import Connection, ConnectionPool
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.types import Message, Scope

from app.api.middleware import WebhookBodyLimit
from app.bot.app import create_dispatcher, run_polling
from app.bot.transport import TelegramFloodControl, retry_telegram
from app.config import Settings
from app.events.bus import EventBus
from app.events.models import ScheduleChanged
from app.logging import JsonFormatter
from app.main import create_app, stop_polling, stop_telegram
from app.models import ScheduleVersion
from app.schedules.fetching import SharedFetcher
from app.schedules.models import DaySchedule, Freshness, ProviderResult, ScheduleSlot, ScheduleState
from app.schedules.normalizer import normalize_half_hours
from app.schedules.service import ScheduleService
from app.workers.schedules import ScheduleWorker, schedule_hash

NOW = datetime(2026, 10, 2, 12, tzinfo=UTC)
DAY = date(2026, 10, 2)


def test_dynamic_secret_redaction_and_safe_context() -> None:
    secrets = [
        "unknown-community",
        "unknown-access",
        "hidden-password",
        "unknown-webhook",
        "123456:" + "a" * 35,
        "unknown-bearer",
    ]
    record = logging.LogRecord(
        "test",
        logging.ERROR,
        __file__,
        1,
        f"community='{secrets[0]}' access_token={secrets[1]} password={secrets[2]} "
        f"/api/v1/homeassistant/webhook/{secrets[3]} {secrets[4]} "
        f"Authorization: Bearer {secrets[5]}",
        (),
        (ValueError, ValueError("sensitive exception"), None),
    )
    record.device_id = 12
    record.worker = "ping"
    record.access_token = "extra-secret"
    output = JsonFormatter().format(record)
    assert all(secret not in output for secret in secrets + ["extra-secret", "sensitive exception"])
    data = json.loads(output)
    assert data["device_id"] == 12 and data["worker"] == "ping"
    assert data["exception_type"] == "ValueError"


async def test_safe_api_validation_size_and_database_errors(settings: Settings) -> None:
    app = create_app(settings)
    service = AsyncMock()
    app.state.homeassistant_webhooks = service
    token = "secret-token-" + "a" * 35
    path = f"/api/v1/homeassistant/webhook/{token}"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            path, json={"state": "invalid-secret", "token": "secret-extra"}
        )
        assert response.status_code == 422 and response.json() == {"detail": "Invalid request"}
        assert "secret" not in response.text
        response = await client.post(path, content=b"x" * 5000)
        assert response.status_code == 413
        service.receive.assert_not_awaited()
        service.receive.side_effect = SQLAlchemyError("database-secret")
        response = await client.post(path, json={"state": "on"})
        assert response.status_code == 503 and "secret" not in response.text


@pytest.mark.parametrize("kind", ["chunked", "huge_length", "slow"])
async def test_streaming_request_limits(kind: str) -> None:
    app, send = AsyncMock(), AsyncMock()
    scope: Scope = {"type": "http", "path": "/api/v1/homeassistant/webhook/token", "headers": []}
    if kind == "huge_length":
        scope["headers"] = [(b"content-length", b"9" * 5000)]
    count = 0

    async def receive() -> Message:
        nonlocal count
        count += 1
        if kind == "slow":
            await asyncio.Event().wait()
        return {"type": "http.request", "body": b"x" * 3000, "more_body": True}

    await WebhookBodyLimit(app, body_timeout=0.01)(scope, receive, send)
    assert send.call_args_list[0].args[0]["status"] == (408 if kind == "slow" else 413)
    app.assert_not_awaited()
    assert count <= 2


async def test_redis_mutation_not_replayed_and_next_command_recovers() -> None:
    pool = MagicMock(spec=ConnectionPool)
    pool.connection_kwargs = {}
    connection = MagicMock(spec=Connection)
    connection.retry = Retry(NoBackoff(), 0)
    connection.db, connection.host, connection.port = 0, "localhost", 6379
    connection.send_command = AsyncMock(side_effect=[RedisConnectionError("secret"), None])
    connection.read_response = AsyncMock(return_value=b"PONG")
    connection.disconnect = AsyncMock()
    pool.get_connection = AsyncMock(return_value=connection)
    pool.release = AsyncMock()
    redis = Redis(connection_pool=pool)
    with pytest.raises(RedisConnectionError):
        await cast(Awaitable[str], redis.eval("return redis.call('INCR',KEYS[1])", 1, "counter"))
    assert connection.send_command.await_count == 1
    assert await cast(Awaitable[bool], redis.ping())
    assert connection.send_command.await_count == 2
    connection.disconnect.assert_awaited_once()
    assert pool.get_connection.await_count == 2
    await redis.aclose()


async def test_flood_retry_is_single_owner_and_never_retries_ambiguous_errors() -> None:
    method = SendMessage(chat_id=1, text="test")
    rejected = TelegramRetryAfter(method=method, message="limited", retry_after=1)
    physical = AsyncMock(side_effect=[rejected, True])
    middleware = TelegramFloodControl()
    bot = MagicMock(spec=Bot)
    with patch("app.bot.transport.asyncio.sleep", new_callable=AsyncMock):
        assert await retry_telegram(lambda: middleware(physical, bot, method))
    assert physical.await_count == 2
    physical.reset_mock()
    physical.side_effect = TelegramNetworkError(method=method, message="ambiguous")
    with pytest.raises(TelegramNetworkError):
        await middleware(physical, bot, method)
    physical.assert_awaited_once()
    # Read-only preflight calls still receive bounded retries inside a notification retry owner.
    physical.reset_mock()
    physical.side_effect = [rejected, True]
    with patch("app.bot.transport.asyncio.sleep", new_callable=AsyncMock):
        assert await retry_telegram(lambda: middleware(physical, bot, GetChat(chat_id=1)))
    assert physical.await_count == 2


async def test_polling_restarts_after_unexpected_failure() -> None:
    dispatcher = AsyncMock(spec=Dispatcher)
    dispatcher.start_polling.side_effect = [RuntimeError("private-token"), asyncio.CancelledError]
    with patch("app.bot.app.asyncio.sleep", new_callable=AsyncMock) as sleep:
        with pytest.raises(asyncio.CancelledError):
            await run_polling(dispatcher, MagicMock(spec=Bot))
    assert dispatcher.start_polling.await_count == 2
    sleep.assert_awaited_once_with(1)


async def test_native_polling_shutdown_cancels_child_updates() -> None:
    dispatcher = create_dispatcher()
    bot = Bot("123456:TEST_TOKEN", session=AsyncMock(spec=BaseSession))
    polling_started, polling_stopped, handler_stopped = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )

    async def polling(**kwargs: object) -> None:
        polling_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            polling_stopped.set()

    async def handler() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            handler_stopped.set()

    with patch.object(dispatcher, "_polling", side_effect=polling):
        update = asyncio.create_task(handler())
        dispatcher._handle_update_tasks.add(update)
        task = asyncio.create_task(run_polling(dispatcher, bot))
        async with asyncio.timeout(1):
            await polling_started.wait()
        await stop_telegram(dispatcher, task, 1)
        assert polling_stopped.is_set() and handler_stopped.is_set()
        assert task.done() and update.done()
    await dispatcher.storage.close()
    await bot.session.close()


async def test_task_shutdown_deadline_does_not_block_cleanup() -> None:
    release = asyncio.Event()

    async def resistant() -> None:
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                pass

    task = asyncio.create_task(resistant())
    await asyncio.sleep(0)
    try:
        async with asyncio.timeout(1):
            await stop_polling(task, 0.01)
        assert not task.done()
    finally:
        release.set()
        await task


async def test_retry_after_http_date_respected() -> None:
    count = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        if count == 1:
            return httpx.Response(
                429, headers={"Retry-After": format_datetime(NOW + timedelta(seconds=5))}
            )
        return httpx.Response(200, content=b"{}")

    with (
        patch("app.schedules.fetching.datetime") as clock,
        patch("app.schedules.fetching.asyncio.sleep", new_callable=AsyncMock) as sleep,
    ):
        clock.now.return_value = NOW
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result, _ = await SharedFetcher(AsyncMock(), client)._fetch(
                "https://provider.example", lambda body: body.decode(), None
            )
    assert result.raw == "{}"
    sleep.assert_awaited_once_with(5)


def schedule(off: bool = False) -> DaySchedule:
    return normalize_half_hours(
        "test", "kyiv", "1.2", DAY, {"00:00": ScheduleState.OFF if off else ScheduleState.ON}
    )


def test_schedule_hash_ignores_equivalent_slot_boundaries() -> None:
    original = schedule()
    first, *remaining = original.slots
    split = original.model_copy(
        update={
            "slots": (
                ScheduleSlot(
                    start=first.start, end=first.start + timedelta(minutes=15), state=first.state
                ),
                ScheduleSlot(
                    start=first.start + timedelta(minutes=15), end=first.end, state=first.state
                ),
                *remaining,
            )
        }
    )
    assert schedule_hash(original) == schedule_hash(split)
    assert schedule_hash(original) != schedule_hash(schedule(True))


async def test_schedule_worker_versions_changes_stale_and_duplicate_samples(
    sessions: MagicMock, db_session: Session
) -> None:
    from sqlalchemy import select

    # SQLite executes all real repository statements; only PostgreSQL's lock primitive is replaced.
    connection = db_session.connection().connection.driver_connection
    assert isinstance(connection, sqlite3.Connection)
    connection.create_function("pg_advisory_xact_lock", 1, lambda key: 1)
    service = AsyncMock(spec=ScheduleService)
    service.get_schedule.side_effect = [
        ProviderResult(schedule(), Freshness.FRESH, checked_at=NOW),
        ProviderResult(schedule(), Freshness.CACHED, checked_at=NOW),
        ProviderResult(schedule(True), Freshness.STALE, checked_at=NOW + timedelta(seconds=1)),
        ProviderResult(schedule(True), Freshness.FRESH, checked_at=NOW + timedelta(seconds=2)),
    ]
    bus, handler = EventBus(), AsyncMock()
    bus.subscribe(ScheduleChanged, handler)
    worker = ScheduleWorker(sessions, service, bus)
    for _ in range(4):
        await worker._refresh_source("test", "kyiv", "1.2", DAY)
    versions = db_session.scalars(select(ScheduleVersion).order_by(ScheduleVersion.id)).all()
    assert len(versions) == 2
    handler.assert_awaited_once()
    assert handler.call_args.args[0].previous_version_id == versions[0].id
    assert handler.call_args.args[0].new_version_id == versions[1].id


async def test_readiness_detects_dead_workers_and_shutdown(settings: Settings) -> None:
    settings.schedule_sources = {}
    engine, redis = MagicMock(), AsyncMock()
    engine.dispose = AsyncMock()
    engine.connect.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
    engine.connect.return_value.__aexit__ = AsyncMock()
    redis.ping.return_value = True
    with (
        patch("app.main.create_engine", return_value=engine),
        patch("app.main.Redis.from_url", return_value=redis) as redis_factory,
        patch("app.main.configure_logging"),
    ):
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                dead = asyncio.create_task(asyncio.sleep(0))
                await dead
                app.state.worker_tasks["monitoring"] = dead
                response = await client.get("/readiness")
                assert response.status_code == 503
                assert response.json()["workers"] == {"monitoring": "unavailable"}
                assert (await client.get("/health")).status_code == 200
                app.state.worker_tasks = {}
                app.state.accepting_requests = False
                assert (await client.get("/readiness")).json()["process"] == "stopping"
        # Mutation commands are deliberately not retried after ambiguous transport failures.
        assert redis_factory.call_args.kwargs["retry"].get_retries() == 0
        assert redis_factory.call_args.kwargs["health_check_interval"] == 30


def test_json_secret_fields_are_redacted() -> None:
    record = logging.LogRecord(
        "test",
        logging.ERROR,
        __file__,
        1,
        json.dumps({"community": 'hidden"community', "access_token": "hidden-token"}),
        (),
        None,
    )
    output = JsonFormatter().format(record)
    assert "hidden" not in output


def test_hardcoded_telegram_responses_are_ukrainian() -> None:
    import ast
    import re
    from pathlib import Path

    technical = re.compile(r"Home Assistant|SNMP|Ping|Telegram|WebSocket|OID|IP")
    for path in Path("app/bot").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call):
                continue
            text = None
            if isinstance(node.func, ast.Attribute) and node.func.attr == "answer" and node.args:
                text = node.args[0]
            elif isinstance(node.func, ast.Name) and node.func.id in {
                "InlineKeyboardButton",
                "KeyboardButton",
            }:
                text = next((item.value for item in node.keywords if item.arg == "text"), None)
            if isinstance(text, ast.Constant) and isinstance(text.value, str):
                assert not re.search(r"[A-Za-z]", technical.sub("", text.value)), (
                    path,
                    text.lineno,
                )


async def test_schedule_refresh_bounds_queued_source_tasks(
    sessions: MagicMock, db_session: Session
) -> None:
    from app.models import ScheduleSubscription

    db_session.add_all(
        ScheduleSubscription(user_id=1, provider="test", region="kyiv", queue=str(i), name="Дім")
        for i in range(40)
    )
    db_session.commit()
    worker = ScheduleWorker(sessions, MagicMock(spec=ScheduleService), EventBus())
    active, peak, completed = 0, 0, 0

    async def refresh(*args: object) -> None:
        nonlocal active, peak, completed
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        active -= 1
        completed += 1

    with patch.object(worker, "_refresh_source", side_effect=refresh):
        await worker.refresh()
    assert completed == 82  # Forty new sources plus the seeded subscription, two days each.
    assert peak == 8
