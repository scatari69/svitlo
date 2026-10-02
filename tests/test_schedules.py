import asyncio
import json
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError

from app.schedules.fetching import PUBLISH, CacheEntry, SharedFetcher
from app.schedules.models import (
    DaySchedule,
    Freshness,
    ProviderResult,
    ScheduleSlot,
    ScheduleState,
    day_bounds,
)
from app.schedules.normalizer import normalize_half_hours
from app.schedules.providers.svitlo import SvitloProvider, decode_document
from app.schedules.service import ScheduleService

DAY = date(2026, 10, 2)
NOW = datetime(2026, 10, 2, 12, tzinfo=UTC)
URL = "https://provider.example/schedules"


class MemoryRedis:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.data.get(key)

    async def set(self, key: str, value: str, *, ex: int, nx: bool = False) -> bool:
        if nx and key in self.data:
            return False
        self.data[key] = value
        return True

    async def eval(self, script: str, count: int, *args: str | int) -> int:
        lock, token = str(args[0]), str(args[count])
        if self.data.get(lock) != token:
            return 0
        if script == PUBLISH:
            self.data[str(args[1])] = str(args[count + 1])
        del self.data[lock]
        return 1


@pytest.fixture
def redis() -> MemoryRedis:
    return MemoryRedis()


@pytest.fixture
def clock() -> Iterator[MagicMock]:
    with patch("app.schedules.fetching.datetime") as clock:
        clock.now.return_value = NOW
        yield clock


def payload() -> dict[str, object]:
    return {
        "date_today": DAY.isoformat(),
        "updated_at": "irrelevant",
        "regions": [
            {
                "cpu": "kyiv",
                "name_ua": "Київ",
                "emergency": False,
                "schedule": {
                    "1.1": {DAY.isoformat(): {"00:00": 1, "00:30": 2, "01:00": 99}},
                    "1.2": {DAY.isoformat(): {"00:00": 2}},
                },
            }
        ],
    }


def provider(redis: MemoryRedis, client: httpx.AsyncClient, **options: object) -> SvitloProvider:
    return SvitloProvider("test", URL, SharedFetcher(cast(Redis, redis), client, **options))  # type: ignore[arg-type]


async def test_catalog_groups_and_many_users_share_one_bulk_request(redis: MemoryRedis) -> None:
    requests = 0

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        await asyncio.sleep(0.01)
        return httpx.Response(200, json=payload())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        first, second = provider(redis, client), provider(redis, client)
        results = await asyncio.gather(
            *[
                (first if index % 2 else second).get_schedule(
                    "kyiv", "1.1" if index % 2 else "1.2", DAY
                )
                for index in range(100)
            ]
        )
        assert all(result.data is not None for result in results)
        assert requests == 1
        regions = await first.get_regions()
        queues = await second.get_queues("kyiv")
        assert regions.data and regions.data[0].name == "Київ"
        assert queues.data and [group.id for group in queues.data] == ["1.1", "1.2"]
        assert requests == 1
        assert (await first.get_schedule("kyiv", "missing", DAY)).data is None
        assert (await first.get_queues("missing")).error == "region_not_found"


async def test_conditional_fetch_preserves_payload_and_download_timestamp(
    redis: MemoryRedis, clock: MagicMock
) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                200,
                json=payload(),
                headers={"ETag": '"v1"', "Last-Modified": "Fri, 02 Oct 2026 11:00:00 GMT"},
            )
        assert request.headers["If-None-Match"] == '"v1"'
        assert request.headers["If-Modified-Since"] == "Fri, 02 Oct 2026 11:00:00 GMT"
        return httpx.Response(304)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        source = provider(redis, client)
        original = await source.get_schedule("kyiv", "1.1", DAY)
        clock.now.return_value = NOW + timedelta(seconds=601)
        unchanged = await source.get_schedule("kyiv", "1.1", DAY)
        cached = await source.get_schedule("kyiv", "1.1", DAY)
    assert original.freshness == Freshness.FRESH
    assert unchanged.freshness == Freshness.UNCHANGED and unchanged.data == original.data
    assert unchanged.fetched_at == original.fetched_at == NOW
    assert unchanged.checked_at == NOW + timedelta(seconds=601)
    assert cached.freshness == Freshness.CACHED and len(requests) == 2


@pytest.mark.parametrize(
    "bad",
    [
        {"regions": []},
        {"regions": None},
        {"regions": [{"cpu": "kyiv", "name_ua": "Київ", "schedule": {}}]},
        {
            "regions": [
                {"cpu": "kyiv", "name_ua": "Київ", "schedule": {"1.1": {DAY.isoformat(): {}}}}
            ]
        },
    ],
)
async def test_malformed_data_retains_last_good_schedule_and_cools_down(
    bad: object, redis: MemoryRedis, clock: MagicMock
) -> None:
    count = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        return httpx.Response(
            200,
            json=payload() if count == 1 else bad,
            headers={"ETag": '"good"' if count == 1 else '"bad"'},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        source = provider(redis, client)
        good = await source.get_schedule("kyiv", "1.1", DAY)
        clock.now.return_value = NOW + timedelta(seconds=601)
        stale = await source.get_schedule("kyiv", "1.1", DAY)
        repeated = await source.get_schedule("kyiv", "1.1", DAY)
    assert stale.data == good.data == repeated.data
    assert stale.freshness == repeated.freshness == Freshness.STALE
    assert stale.checked_at == NOW and stale.error == "invalid_payload"
    assert count == 2
    entry = CacheEntry.model_validate_json(
        next(value for key, value in redis.data.items() if key.endswith(":data"))
    )
    assert entry.etag == '"good"'


async def test_retry_backoff_and_timeout_are_bounded(redis: MemoryRedis) -> None:
    count = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        assert request.extensions["timeout"]["read"] == 2
        if count == 1:
            raise httpx.ReadTimeout("private provider details")
        if count == 2:
            return httpx.Response(503)
        return httpx.Response(200, json=payload())

    with patch("app.schedules.fetching.asyncio.sleep", new_callable=AsyncMock) as sleep:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result = await provider(redis, client, timeout=2).get_schedule("kyiv", "1.1", DAY)
    assert result.data and count == 3
    delays = [call.args[0] for call in sleep.await_args_list]
    assert len(delays) == 2 and 1 <= delays[0] <= 1.25 and 2 <= delays[1] <= 2.25


async def test_redis_failure_never_bypasses_shared_fetching() -> None:
    redis = MagicMock(spec=Redis)
    redis.get = AsyncMock(side_effect=RedisConnectionError("private credentials"))
    request = MagicMock(side_effect=AssertionError("Must not request upstream"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(request)) as client:
        source = SvitloProvider("test", URL, SharedFetcher(redis, client))
        result = await source.get_regions()
    assert result.freshness == Freshness.UNAVAILABLE and result.data is None
    assert "private credentials" not in repr(result)
    request.assert_not_called()


@pytest.mark.parametrize("status", [302, 404, 304])
async def test_unusable_http_response_is_negative_cached(status: int, redis: MemoryRedis) -> None:
    requests = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(status, headers={"Location": "https://other.example"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        source = provider(redis, client)
        assert (await source.get_regions()).freshness == Freshness.UNAVAILABLE
        assert (await source.get_regions()).data is None
    assert requests == 1


async def test_retry_after_long_delay_is_respected(redis: MemoryRedis, clock: MagicMock) -> None:
    count = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        return httpx.Response(429, headers={"Retry-After": "120"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        source = provider(redis, client)
        assert (await source.get_regions()).error == "rate_limited"
        clock.now.return_value = NOW + timedelta(seconds=31)
        assert (await source.get_regions()).error == "rate_limited"
    assert count == 1


@pytest.mark.parametrize(
    ("day", "hours"), [(date(2026, 3, 29), 23), (date(2026, 10, 25), 25), (DAY, 24)]
)
def test_normalization_spans_actual_kyiv_day(day: date, hours: int) -> None:
    values = {
        f"{hour:02d}:{minute:02d}": ScheduleState.ON for hour in range(24) for minute in (0, 30)
    }
    schedule = normalize_half_hours("test", "kyiv", "1", day, values)
    assert len(schedule.slots) == 1
    assert schedule.slots[0].duration == timedelta(hours=hours)
    assert schedule.slots[0].start.tzinfo == UTC


def test_autumn_repeated_hour_and_spring_missing_hour() -> None:
    autumn = normalize_half_hours(
        "test",
        "kyiv",
        "1",
        date(2026, 10, 25),
        {"03:00": ScheduleState.OFF, "03:30": ScheduleState.OFF},
    )
    spring = normalize_half_hours(
        "test",
        "kyiv",
        "1",
        date(2026, 3, 29),
        {"03:00": ScheduleState.OFF, "03:30": ScheduleState.OFF},
    )
    assert sum(
        (slot.duration for slot in autumn.slots if slot.state == ScheduleState.OFF), timedelta()
    ) == timedelta(hours=2)
    assert all(slot.state == ScheduleState.UNKNOWN for slot in spring.slots)


def test_unknown_codes_and_missing_labels_are_never_off() -> None:
    document = decode_document(json.dumps(payload()).encode(), "test")
    schedule = document.days[0]
    assert [slot.state for slot in schedule.slots] == [
        ScheduleState.ON,
        ScheduleState.OFF,
        ScheduleState.UNKNOWN,
    ]
    assert schedule.slots[-1].duration == timedelta(hours=23)
    wrapped = decode_document(
        json.dumps({"body": json.dumps(payload()), "statusCode": 200}).encode(), "test"
    )
    assert wrapped == document


@pytest.mark.parametrize("label", ["24:00", "12:15", "9:00", "03:60"])
def test_invalid_half_hour_labels(label: str) -> None:
    with pytest.raises(ValueError):
        normalize_half_hours("test", "kyiv", "1", DAY, {label: ScheduleState.ON})


def test_domain_rejects_naive_and_incomplete_intervals() -> None:
    with pytest.raises(ValidationError):
        ScheduleSlot(start=datetime(2026, 10, 2), end=NOW, state=ScheduleState.ON)
    start, end = day_bounds(DAY)
    with pytest.raises(ValidationError):
        DaySchedule(
            provider="test",
            region="kyiv",
            group="1",
            date=DAY,
            slots=(
                ScheduleSlot(start=start + timedelta(hours=1), end=end, state=ScheduleState.ON),
            ),
        )


async def test_provider_failures_are_isolated_and_unknown_provider_is_safe() -> None:
    failing, healthy = MagicMock(), MagicMock()
    failing.id, healthy.id = "failed", "healthy"
    failing.get_regions = AsyncMock(side_effect=RuntimeError("private URL"))
    healthy.get_regions = AsyncMock(return_value=ProviderResult((), Freshness.FRESH))
    service = ScheduleService([failing, healthy])
    broken, working = await asyncio.gather(
        service.get_regions("failed"), service.get_regions("healthy")
    )
    assert broken.data is None and broken.error == "provider_unavailable"
    assert working.data == ()
    assert (await service.get_regions("missing")).error == "provider_not_found"


async def test_cache_is_rechecked_after_acquiring_lease(redis: MemoryRedis) -> None:
    cached = CacheEntry(
        raw=json.dumps(payload()),
        fetched_at=NOW,
        checked_at=NOW,
        refresh_after=NOW + timedelta(seconds=600),
    )
    real_set = redis.set

    async def racing_set(key: str, value: str, *, ex: int, nx: bool = False) -> bool:
        # Another process published after this request read the cache, before NX.
        redis.data[key.removesuffix(":lock") + ":data"] = cached.model_dump_json()
        return await real_set(key, value, ex=ex, nx=nx)

    request = MagicMock(side_effect=AssertionError("Cache should prevent HTTP request"))
    with (
        patch("app.schedules.fetching.datetime") as clock,
        patch.object(redis, "set", side_effect=racing_set),
    ):
        clock.now.return_value = NOW
        async with httpx.AsyncClient(transport=httpx.MockTransport(request)) as client:
            result = await provider(redis, client).get_regions()
    assert result.data and result.freshness == Freshness.CACHED
    request.assert_not_called()
    assert not any(key.endswith(":lock") for key in redis.data)


async def test_expired_owner_cannot_overwrite_new_owner_cache(redis: MemoryRedis) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        lock = next(key for key in redis.data if key.endswith(":lock"))
        redis.data[lock] = "replacement-owner"
        return httpx.Response(200, json=payload())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await provider(redis, client).get_regions()
    assert result.data is None
    assert list(redis.data.values()) == ["replacement-owner"]


async def test_trailing_slash_endpoint_is_preserved(redis: MemoryRedis) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/schedules/"
        return httpx.Response(200, json=payload())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        source = SvitloProvider("test", URL + "/", SharedFetcher(cast(Redis, redis), client))
        assert (await source.get_regions()).data


async def test_cancelled_fetch_releases_lease(redis: MemoryRedis) -> None:
    started = asyncio.Event()

    async def respond(request: httpx.Request) -> httpx.Response:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("Unreachable")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        task = asyncio.create_task(provider(redis, client).get_regions())
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert redis.data == {}


async def test_retention_limit_never_relabels_ancient_data_fresh(
    redis: MemoryRedis, clock: MagicMock
) -> None:
    count = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        if count == 1:
            return httpx.Response(200, json=payload())
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        source = provider(redis, client)
        assert (await source.get_regions()).data
        clock.now.return_value = NOW + timedelta(days=2)
        result = await source.get_regions()
    assert result.data is None and result.freshness == Freshness.UNAVAILABLE


@pytest.mark.parametrize("body", [b"{", b"\xff", b"x" * (2 * 1024 * 1024 + 1)])
async def test_invalid_json_and_oversized_payloads_are_safe(
    body: bytes, redis: MemoryRedis
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    ) as client:
        result = await provider(redis, client).get_regions()
    assert result.data is None and result.freshness == Freshness.UNAVAILABLE
    assert result.error in {"invalid_payload", "payload_too_large"}


async def test_default_schedule_date_uses_kyiv_after_utc_midnight_boundary() -> None:
    from app.schedules.models import KYIV

    source = MagicMock()
    source.id = "test"
    source.get_schedule = AsyncMock(return_value=ProviderResult(None, Freshness.UNAVAILABLE))
    with patch("app.schedules.service.datetime") as clock:
        clock.now.return_value = datetime(2026, 10, 2, 22, tzinfo=UTC).astimezone(KYIV)
        await ScheduleService([source]).get_schedule("test", "kyiv", "1")
        clock.now.assert_called_once_with(KYIV)
    source.get_schedule.assert_awaited_once_with("kyiv", "1", date(2026, 10, 3))
