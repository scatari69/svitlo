import json
from hashlib import sha256
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.homeassistant import router
from app.events.bus import EventBus
from app.events.models import DomainEvent, PowerStateChanged
from app.models import Device, HomeAssistantDeviceConfig, PowerInterval
from app.models.enums import HomeAssistantMode, PowerState
from app.services.homeassistant import (
    HomeAssistantWebhookService,
    WebhookNotFound,
    WebhookRateLimited,
)

TOKEN = "a" * 43
DIGEST = sha256(TOKEN.encode()).hexdigest()


@pytest.fixture
def redis() -> MagicMock:
    result = MagicMock(spec=Redis)
    result.eval = AsyncMock(return_value=1)
    result.set = AsyncMock(return_value=False)
    result.get = AsyncMock(return_value=None)
    return result


async def test_duplicate_webhook_records_one_interval_and_one_state_event(
    sessions: MagicMock,
    db_session: Session,
    redis: MagicMock,
) -> None:
    db_session.add(
        HomeAssistantDeviceConfig(
            device_id=3, mode=HomeAssistantMode.WEBHOOK, webhook_token_hash=DIGEST
        )
    )
    db_session.commit()
    handler = AsyncMock()
    bus = EventBus()
    bus.subscribe(DomainEvent, handler)
    service = HomeAssistantWebhookService(sessions, redis, bus)
    await service.receive(TOKEN, PowerState.ON)
    await service.receive(TOKEN, PowerState.ON)
    intervals = list(db_session.scalars(select(PowerInterval)))
    assert len(intervals) == 1 and intervals[0].state == PowerState.ON
    assert (
        len(
            [call for call in handler.call_args_list if isinstance(call.args[0], PowerStateChanged)]
        )
        == 1
    )
    device = db_session.get(Device, 3)
    assert device and device.last_successful_check_at is not None
    await service.receive(TOKEN, PowerState.OFF)
    db_session.expire_all()
    intervals = list(db_session.scalars(select(PowerInterval).order_by(PowerInterval.id)))
    assert [interval.state for interval in intervals] == [PowerState.ON, PowerState.OFF]
    assert intervals[0].ended_at == intervals[1].started_at


async def test_setup_probe_only_accepts_live_token_and_limiter_blocks(
    sessions: MagicMock,
    redis: MagicMock,
) -> None:
    service = HomeAssistantWebhookService(sessions, redis, EventBus())
    with pytest.raises(WebhookNotFound):
        await service.receive(TOKEN, PowerState.ON)
    redis.set.return_value = True
    await service.receive(TOKEN, PowerState.OFF)
    assert redis.set.call_args.args[0] == f"ha:setup:{DIGEST}"
    assert json.loads(redis.set.call_args.args[1])["state"] == "off"
    assert redis.set.call_args.kwargs == {"xx": True, "keepttl": True}
    redis.eval.return_value = 31
    with pytest.raises(WebhookRateLimited):
        await service.receive(TOKEN, PowerState.ON)
    assert TOKEN not in str(redis.mock_calls)


async def test_disabled_or_deleted_device_webhook_cannot_modify_history(
    sessions: MagicMock,
    db_session: Session,
    redis: MagicMock,
) -> None:
    db_session.add(
        HomeAssistantDeviceConfig(
            device_id=3, mode=HomeAssistantMode.WEBHOOK, webhook_token_hash=DIGEST
        )
    )
    device = db_session.get(Device, 3)
    assert device
    redis.set.return_value = True  # A leftover setup key must not resurrect a disabled device.
    device.enabled = False
    db_session.commit()
    with pytest.raises(WebhookNotFound):
        await HomeAssistantWebhookService(sessions, redis, EventBus()).receive(
            TOKEN, PowerState.OFF
        )
    assert list(db_session.scalars(select(PowerInterval))) == []


async def test_webhook_api_validation_and_safe_errors() -> None:
    app = FastAPI()
    app.include_router(router)
    service = AsyncMock(spec=HomeAssistantWebhookService)
    app.state.homeassistant_webhooks = service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        path = f"/api/v1/homeassistant/webhook/{TOKEN}"
        response = await client.post(path, json={"state": "on"})
        assert response.status_code == 204
        service.receive.assert_awaited_once_with(TOKEN, PowerState.ON)
        assert (await client.post(path, json={"state": "unknown"})).status_code == 422
        service.receive.side_effect = WebhookNotFound
        response = await client.post(path, json={"state": "off"})
        assert response.status_code == 404 and TOKEN not in response.text
        service.receive.side_effect = WebhookRateLimited
        assert (await client.post(path, json={"state": "on"})).status_code == 429


@pytest.mark.parametrize("state", [PowerState.ON, PowerState.OFF])
async def test_valid_webhook_endpoint_persists_normalized_state(
    state: PowerState, sessions: MagicMock, db_session: Session, redis: MagicMock
) -> None:
    db_session.add(
        HomeAssistantDeviceConfig(
            device_id=3, mode=HomeAssistantMode.WEBHOOK, webhook_token_hash=DIGEST
        )
    )
    db_session.commit()
    handler = AsyncMock()
    bus = EventBus()
    bus.subscribe(PowerStateChanged, handler)
    app = FastAPI()
    app.include_router(router)
    app.state.homeassistant_webhooks = HomeAssistantWebhookService(sessions, redis, bus)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for _ in range(2):
            response = await client.post(
                f"/api/v1/homeassistant/webhook/{TOKEN}", json={"state": state.value}
            )
            assert response.status_code == 204 and response.content == b""
    db_session.expire_all()
    device = db_session.get(Device, 3)
    assert device and device.current_power_state == state
    intervals = list(db_session.scalars(select(PowerInterval)))
    assert len(intervals) == 1 and intervals[0].state == state
    handler.assert_awaited_once()


@pytest.mark.parametrize(
    "payload",
    [
        {"state": "UNKNOWN"},
        {"state": "ON"},
        {"state": True},
        {},
        {"state": "on", "extra": "value"},
        {"state": None},
        ["on"],
    ],
)
async def test_invalid_payload_never_calls_domain_service(payload: object) -> None:
    app = FastAPI()
    app.include_router(router)
    app.state.homeassistant_webhooks = AsyncMock(spec=HomeAssistantWebhookService)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(f"/api/v1/homeassistant/webhook/{TOKEN}", json=payload)
    assert response.status_code == 422
    assert TOKEN not in response.text
    app.state.homeassistant_webhooks.receive.assert_not_awaited()


@pytest.mark.parametrize("token", ["short", "x" * 129, "а" * 43, "x" * 42 + "!"])
async def test_malformed_tokens_are_rejected_before_redis_or_database(
    token: str, sessions: MagicMock, redis: MagicMock
) -> None:
    with pytest.raises(WebhookNotFound):
        await HomeAssistantWebhookService(sessions, redis, EventBus()).receive(token, PowerState.ON)
    redis.eval.assert_not_awaited()
    sessions.begin.assert_not_called()


async def test_invalid_token_endpoint_and_redis_outage(
    sessions: MagicMock, redis: MagicMock
) -> None:
    from redis.exceptions import ConnectionError as RedisConnectionError

    app = FastAPI()
    app.include_router(router)
    app.state.homeassistant_webhooks = HomeAssistantWebhookService(sessions, redis, EventBus())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        path = f"/api/v1/homeassistant/webhook/{TOKEN}"
        response = await client.post(path, json={"state": "on"})
        assert response.status_code == 404 and TOKEN not in response.text
        redis.eval.side_effect = RedisConnectionError("private credentials")
        response = await client.post(path, json={"state": "on"})
        assert response.status_code == 503
        assert "private credentials" not in response.text
