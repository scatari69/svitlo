import asyncio
from collections.abc import Awaitable, Callable
from typing import Literal, cast

from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


class HealthStatus(BaseModel):
    status: Literal["ok", "degraded"]
    process: Literal["ok", "stopping"] = "ok"
    database: Literal["ok", "unavailable"]
    redis: Literal["ok", "unavailable"]
    workers: dict[str, Literal["ok", "unavailable"]] | None = None


async def check_health(engine: AsyncEngine, redis: Redis, probe_timeout: float) -> HealthStatus:
    async def database_ping() -> None:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    async def redis_ping() -> None:
        if not await cast(Awaitable[bool], redis.ping()):
            raise ConnectionError("Redis ping failed")

    async def probe(check: Callable[[], Awaitable[None]]) -> Literal["ok", "unavailable"]:
        try:
            async with asyncio.timeout(probe_timeout):
                await check()
        except Exception:
            return "unavailable"
        return "ok"

    database, cache = await asyncio.gather(probe(database_ping), probe(redis_ping))
    return HealthStatus(
        status="ok" if database == cache == "ok" else "degraded", database=database, redis=cache
    )
