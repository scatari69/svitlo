import asyncio
import logging
import math
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from hashlib import sha256
from random import uniform
from secrets import token_urlsafe
from typing import cast

import httpx
from pydantic import BaseModel, Field, field_validator
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.schedules.models import Freshness, ProviderResult
from app.services.time import aware_utc
from app.services.validation import validate_url

logger = logging.getLogger(__name__)
PUBLISH = """if redis.call('GET',KEYS[1]) == ARGV[1] then
redis.call('SET',KEYS[2],ARGV[2],'EX',ARGV[3]); redis.call('DEL',KEYS[1]); return 1 end
return 0"""
RELEASE = """if redis.call('GET',KEYS[1]) == ARGV[1] then
return redis.call('DEL',KEYS[1]) end return 0"""


class CacheEntry(BaseModel):
    raw: str | None = Field(default=None, repr=False)
    fetched_at: datetime | None = None
    checked_at: datetime | None = None
    refresh_after: datetime
    etag: str | None = None
    last_modified: str | None = None
    error: str | None = None

    @field_validator("fetched_at", "checked_at", "refresh_after")
    @classmethod
    def aware(cls, value: datetime | None) -> datetime | None:
        return aware_utc(value) if value else None


class FetchFailure(Exception):
    """Fixed internal codes only; upstream exception text is never included."""

    def __init__(self, code: str, *, cooldown: int = 30) -> None:
        self.cooldown = cooldown
        super().__init__(code)


class SharedFetcher:
    def __init__(
        self,
        redis: Redis,
        client: httpx.AsyncClient,
        *,
        timeout: float = 8,
        fresh_seconds: int = 600,
        retain_seconds: int = 86400,
        retries: int = 2,
    ) -> None:
        if (
            not math.isfinite(timeout)
            or timeout <= 0
            or not 0 <= retries <= 5
            or not 0 < fresh_seconds <= retain_seconds
        ):
            raise ValueError("Invalid schedule fetching configuration")
        self.redis, self.client = redis, client
        self.timeout, self.fresh_seconds, self.retain_seconds, self.retries = (
            timeout,
            fresh_seconds,
            retain_seconds,
            retries,
        )
        self.lock_seconds = math.ceil(timeout * (retries + 1) + 30 * retries + 5)

    async def _read(self, key: str) -> CacheEntry | None:
        raw = await self.redis.get(key)
        if raw is None:
            return None
        try:
            entry = CacheEntry.model_validate_json(raw)
            if (
                entry.checked_at
                and (datetime.now(UTC) - entry.checked_at).total_seconds() >= self.retain_seconds
            ):
                return None
            return entry
        except ValueError:
            return None

    @staticmethod
    def _result[T](
        entry: CacheEntry, decode: Callable[[bytes], T], freshness: Freshness
    ) -> ProviderResult[T]:
        data = decode(entry.raw.encode()) if entry.raw is not None else None
        return ProviderResult(
            data,
            freshness if data is not None else Freshness.UNAVAILABLE,
            entry.fetched_at,
            entry.checked_at,
            entry.error,
        )

    async def get[T](self, url: str, decode: Callable[[bytes], T]) -> ProviderResult[T]:
        validate_url(url)
        url = str(httpx.URL(url.strip()))
        digest = sha256(url.encode()).hexdigest()
        key, lock = f"schedules:{{{digest}}}:data", f"schedules:{{{digest}}}:lock"
        token = token_urlsafe(16)
        entry: CacheEntry | None = None
        acquired = False
        try:
            async with asyncio.timeout(self.lock_seconds + 1):
                while True:
                    entry = await self._read(key)
                    if entry is not None:
                        try:
                            cached = self._result(
                                entry, decode, Freshness.STALE if entry.error else Freshness.CACHED
                            )
                        except ValueError:
                            entry = None
                        else:
                            if entry.refresh_after > datetime.now(UTC):
                                return cached
                    acquired = bool(
                        await self.redis.set(lock, token, ex=self.lock_seconds, nx=True)
                    )
                    if acquired:
                        break
                    # Other processes share the lease; never bypass Redis to fetch.
                    await asyncio.sleep(0.05)
                # A refresh may finish between our first read and lease acquisition.
                latest = await self._read(key)
                if latest is not None:
                    try:
                        cached = self._result(
                            latest, decode, Freshness.STALE if latest.error else Freshness.CACHED
                        )
                    except Exception:
                        entry = None
                    else:
                        entry = latest
                        if latest.refresh_after > datetime.now(UTC):
                            return cached
                try:
                    updated, freshness = await self._fetch(url, decode, entry)
                except FetchFailure as failure:
                    now = datetime.now(UTC)
                    updated = entry.model_copy() if entry else CacheEntry(refresh_after=now)
                    updated.refresh_after = now + timedelta(seconds=failure.cooldown)
                    updated.error = str(failure)
                    freshness = Freshness.STALE
                    logger.warning(
                        "Schedule source unavailable source_id=%s reason=%s", digest, updated.error
                    )
                remaining = self.retain_seconds
                if updated.checked_at:
                    remaining -= int((datetime.now(UTC) - updated.checked_at).total_seconds())
                if not await cast(
                    Awaitable[str],
                    self.redis.eval(
                        PUBLISH, 2, lock, key, token, updated.model_dump_json(), max(1, remaining)
                    ),
                ):
                    raise FetchFailure("cache_busy")
                return self._result(updated, decode, freshness)
        except (RedisError, TimeoutError, FetchFailure):
            if entry is not None:
                try:
                    return self._result(
                        entry.model_copy(update={"error": "cache_unavailable"}),
                        decode,
                        Freshness.STALE,
                    )
                except ValueError:
                    pass
            return ProviderResult(None, Freshness.UNAVAILABLE, error="cache_unavailable")
        finally:
            if acquired:
                try:
                    await cast(Awaitable[str], self.redis.eval(RELEASE, 1, lock, token))
                except (RedisError, TimeoutError, ConnectionError):
                    pass  # Lease expires; a Redis outage never grants permission to fetch again.

    async def _fetch[T](
        self, url: str, decode: Callable[[bytes], T], entry: CacheEntry | None
    ) -> tuple[CacheEntry, Freshness]:
        headers = {}
        if entry and entry.raw is not None:
            if entry.etag:
                headers["If-None-Match"] = entry.etag
            if entry.last_modified:
                headers["If-Modified-Since"] = entry.last_modified
        for attempt in range(self.retries + 1):
            delay = float(2**attempt) + uniform(0, 0.25)
            try:
                async with asyncio.timeout(self.timeout):
                    async with self.client.stream(
                        "GET", url, headers=headers, timeout=self.timeout, follow_redirects=False
                    ) as response:
                        now = datetime.now(UTC)
                        if response.status_code == 304:
                            if entry is None or entry.raw is None:
                                raise FetchFailure("invalid_not_modified")
                            decode(entry.raw.encode())
                            return entry.model_copy(
                                update={
                                    "checked_at": now,
                                    "refresh_after": now + timedelta(seconds=self.fresh_seconds),
                                    "error": None,
                                    "etag": response.headers.get("ETag", entry.etag),
                                    "last_modified": response.headers.get(
                                        "Last-Modified", entry.last_modified
                                    ),
                                }
                            ), Freshness.UNCHANGED
                        if response.status_code == 429 or response.status_code >= 500:
                            retry_after = response.headers.get("Retry-After", "")
                            seconds = 0.0
                            try:
                                seconds = (
                                    (86400.0 if len(retry_after) > 10 else float(retry_after))
                                    if retry_after.isdecimal()
                                    else (
                                        aware_utc(parsedate_to_datetime(retry_after)) - now
                                    ).total_seconds()
                                )
                            except (ValueError, TypeError, OverflowError):
                                pass
                            if seconds > 30:
                                raise FetchFailure(
                                    "rate_limited", cooldown=min(math.ceil(seconds), 86400)
                                )
                            delay = max(delay, seconds)
                            if attempt == self.retries:
                                raise FetchFailure("http_unavailable")
                        elif response.status_code != 200:
                            raise FetchFailure("http_error")
                        else:
                            raw = bytearray()
                            async for chunk in response.aiter_bytes():
                                raw.extend(chunk)
                                if len(raw) > 2 * 1024 * 1024:
                                    raise FetchFailure("payload_too_large")
                            try:
                                decode(bytes(raw))
                                text = raw.decode("utf-8")
                            except Exception:
                                raise FetchFailure("invalid_payload") from None
                            return CacheEntry(
                                raw=text,
                                fetched_at=now,
                                checked_at=now,
                                refresh_after=now + timedelta(seconds=self.fresh_seconds),
                                etag=response.headers.get("ETag"),
                                last_modified=response.headers.get("Last-Modified"),
                            ), Freshness.FRESH
            except (httpx.RequestError, TimeoutError):
                if attempt == self.retries:
                    raise FetchFailure("transport_unavailable") from None
            await asyncio.sleep(delay)
        raise FetchFailure("http_unavailable")
