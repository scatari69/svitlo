import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import Settings
from app.main import create_app
from app.services.health import check_health


@pytest.mark.parametrize(
    "database_ok,redis_ok", [(True, True), (False, True), (True, False), (False, False)]
)
async def test_health_and_readiness(settings: Settings, database_ok: bool, redis_ok: bool) -> None:
    settings.schedule_sources = {}
    engine = MagicMock(spec=AsyncEngine)
    connection = AsyncMock()
    engine.connect.return_value.__aenter__ = AsyncMock(return_value=connection)
    engine.connect.return_value.__aexit__ = AsyncMock(return_value=False)
    if not database_ok:
        engine.connect.side_effect = ConnectionError("database-secret")
    redis = AsyncMock(spec=Redis)
    redis.ping = AsyncMock(return_value=True)
    if not redis_ok:
        redis.ping.side_effect = ConnectionError("redis-secret")

    with (
        patch("app.main.create_engine", return_value=engine),
        patch("app.main.Redis.from_url", return_value=redis),
        patch("app.main.configure_logging"),
    ):
        app = create_app(settings)
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        ):
            result = await client.get("/health")
            assert result.status_code == 200
            assert result.json() == {
                "status": "ok" if database_ok and redis_ok else "degraded",
                "process": "ok",
                "database": "ok" if database_ok else "unavailable",
                "redis": "ok" if redis_ok else "unavailable",
            }
            readiness = await client.get("/readiness")
            assert readiness.status_code == (200 if database_ok and redis_ok else 503)
            assert readiness.json() == result.json()
            assert "secret" not in result.text
            if database_ok:
                assert str(connection.execute.call_args.args[0]) == "SELECT 1"
    redis.aclose.assert_awaited_once()
    engine.dispose.assert_awaited_once()


async def test_health_timeout_and_cancellation() -> None:
    engine = MagicMock(spec=AsyncEngine)
    engine.connect.side_effect = ConnectionError
    redis = AsyncMock(spec=Redis)

    async def slow_ping(*args: object) -> bool:
        await asyncio.sleep(60)
        return True

    redis.ping = AsyncMock(side_effect=slow_ping)
    result = await check_health(engine, redis, probe_timeout=0.01)
    assert result.redis == "unavailable"
    redis.ping.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await check_health(engine, redis, probe_timeout=0.01)


async def test_false_redis_ping_is_unavailable() -> None:
    engine = MagicMock(spec=AsyncEngine)
    engine.connect.side_effect = ConnectionError
    redis = AsyncMock(spec=Redis)
    redis.ping = AsyncMock(return_value=False)
    assert (await check_health(engine, redis, probe_timeout=1)).redis == "unavailable"
