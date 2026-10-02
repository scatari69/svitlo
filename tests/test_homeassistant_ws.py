import asyncio
from collections.abc import AsyncGenerator
from dataclasses import replace
from datetime import UTC
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.events.bus import EventBus
from app.events.models import PowerStateChanged
from app.models import Device, HomeAssistantDeviceConfig, PowerInterval
from app.models.enums import HomeAssistantMode, MonitorHealth, PowerState
from app.monitoring.base import MonitorResult
from app.monitoring.homeassistant_ws import HomeAssistantWebSocketMonitor, reject_redirect
from app.monitoring.service import HomeAssistantTarget, MonitoringService
from app.workers.homeassistant import HomeAssistantWorker

ENTITY = "binary_sensor.power"


def initial(state: str = "on") -> list[object]:
    return [
        {"type": "auth_required"},
        {"type": "auth_ok"},
        {"id": 1, "type": "result", "success": True},
        {
            "id": 2,
            "type": "result",
            "success": True,
            "result": [{"entity_id": ENTITY, "state": state}],
        },
    ]


def event(entity: str, state: object) -> dict[str, object]:
    return {
        "id": 1,
        "type": "event",
        "event": {
            "event_type": "state_changed",
            "data": {"entity_id": entity, "new_state": {"state": state}},
        },
    }


@pytest.fixture
def socket() -> MagicMock:
    socket = MagicMock()
    socket.receive_json = AsyncMock(side_effect=initial())
    socket.send_json = AsyncMock()
    return socket


def session_for(socket: MagicMock) -> MagicMock:
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.ws_connect.return_value.__aenter__ = AsyncMock(return_value=socket)
    client.ws_connect.return_value.__aexit__ = AsyncMock(return_value=False)
    return client


async def test_auth_subscription_snapshot_and_entity_filtering(socket: MagicMock) -> None:
    socket.receive_json.side_effect = initial() + [
        event("sensor.other", "off"),
        event(ENTITY, "off"),
        event(ENTITY, "unavailable"),
    ]
    client = session_for(socket)
    monitor = HomeAssistantWebSocketMonitor("https://ha.local", "private-token", ENTITY)
    with patch("app.monitoring.homeassistant_ws.aiohttp.ClientSession", return_value=client):
        stream = monitor.stream()
        results = [await anext(stream) for _ in range(3)]
        await stream.aclose()
    assert [item.state for item in results] == [PowerState.ON, PowerState.OFF, PowerState.UNKNOWN]
    assert results[-1].health == MonitorHealth.DEGRADED
    assert "private-token" not in repr(results) + repr(monitor)
    assert socket.send_json.await_args_list[0].args[0] == {
        "type": "auth",
        "access_token": "private-token",
    }
    assert socket.send_json.await_args_list[1].args[0] == {
        "id": 1,
        "type": "subscribe_events",
        "event_type": "state_changed",
    }
    client.ws_connect.return_value.__aexit__.assert_awaited_once()


@pytest.mark.parametrize(
    ("value", "state", "health"),
    [
        ("powered", PowerState.ON, MonitorHealth.HEALTHY),
        ("down", PowerState.OFF, MonitorHealth.HEALTHY),
        ("on", PowerState.UNKNOWN, MonitorHealth.DEGRADED),
        ("unknown", PowerState.UNKNOWN, MonitorHealth.DEGRADED),
        ("unavailable", PowerState.UNKNOWN, MonitorHealth.DEGRADED),
        (None, PowerState.UNKNOWN, MonitorHealth.DEGRADED),
        ([], PowerState.UNKNOWN, MonitorHealth.DEGRADED),
    ],
)
def test_custom_mapping(value: object, state: PowerState, health: MonitorHealth) -> None:
    monitor = HomeAssistantWebSocketMonitor("http://ha.local", "secret", ENTITY, "powered", "down")
    result = monitor.result(value)
    assert result.state == state and result.health == health
    assert result.detected_at.tzinfo == UTC


@pytest.mark.parametrize(
    ("on", "off"), [("on", "on"), ("", "off"), ("unknown", "off"), ("on", "unavailable")]
)
def test_invalid_state_mapping(on: str, off: str) -> None:
    with pytest.raises(ValueError):
        HomeAssistantWebSocketMonitor("http://ha.local", "secret", ENTITY, on, off)


@pytest.mark.parametrize(
    "messages",
    [
        [{"type": "auth_required"}, {"type": "auth_invalid", "message": "secret"}],
        [{"type": "invalid"}],
        [
            {"type": "auth_required"},
            {"type": "auth_ok"},
            {"id": 1, "type": "result", "success": False},
        ],
        initial()[:-1] + [{"id": 2, "type": "result", "success": True, "result": []}],
        initial()[:-1] + [{"id": 2, "type": "result", "success": True, "result": "bad"}],
        [[]],
    ],
)
async def test_bad_protocol_is_unavailable_and_connection_is_closed(
    messages: list[object], socket: MagicMock
) -> None:
    socket.receive_json.side_effect = messages
    client = session_for(socket)
    with patch("app.monitoring.homeassistant_ws.aiohttp.ClientSession", return_value=client):
        result = await HomeAssistantWebSocketMonitor("http://ha.local", "secret", ENTITY).check()
    assert result.state == PowerState.UNKNOWN and result.health == MonitorHealth.UNAVAILABLE
    assert "secret" not in repr(result)
    client.ws_connect.return_value.__aexit__.assert_awaited_once()


async def test_redirects_and_timeouts_do_not_expose_token(socket: MagicMock) -> None:
    with pytest.raises(ConnectionError, match="redirects"):
        await reject_redirect(None, None, None)
    socket.receive_json.side_effect = TimeoutError("secret")
    with patch(
        "app.monitoring.homeassistant_ws.aiohttp.ClientSession", return_value=session_for(socket)
    ):
        result = await HomeAssistantWebSocketMonitor("http://ha.local", "secret", ENTITY).check()
    assert result.health == MonitorHealth.UNAVAILABLE
    assert "secret" not in repr(result)


@pytest.fixture
def service(sessions: MagicMock, db_session: Session) -> MonitoringService:
    db_session.add(
        HomeAssistantDeviceConfig(
            device_id=3,
            mode=HomeAssistantMode.API,
            url="http://ha.local",
            entity_id=ENTITY,
            access_token_encrypted=b"ciphertext",
        )
    )
    db_session.commit()
    return MonitoringService(sessions, EventBus())


async def test_runtime_history_health_duplicate_and_stale_configuration(
    service: MonitoringService, db_session: Session
) -> None:
    target = (await service.homeassistant_targets())[0]
    handler = AsyncMock()
    service.bus.subscribe(PowerStateChanged, handler)
    monitor = HomeAssistantWebSocketMonitor(target.url, "secret", ENTITY)
    on = monitor.result("on")
    assert await service.apply_homeassistant_result(target, on)
    assert await service.apply_homeassistant_result(target, on)
    for _ in range(3):
        assert await service.apply_homeassistant_result(target, monitor.unavailable())
    db_session.expire_all()
    device = db_session.get(Device, 3)
    assert device and device.monitor_health == MonitorHealth.UNAVAILABLE
    assert device.current_power_state == PowerState.UNKNOWN
    assert await service.apply_homeassistant_result(target, monitor.result("off"))
    intervals = list(db_session.scalars(select(PowerInterval).order_by(PowerInterval.id)))
    assert [item.state for item in intervals] == [PowerState.ON, PowerState.UNKNOWN, PowerState.OFF]
    assert handler.await_count == 3
    assert "ciphertext" not in repr(target)
    assert not await service.apply_homeassistant_result(replace(target, on_state="different"), on)
    device.enabled = False
    db_session.commit()
    assert not await service.apply_homeassistant_result(target, on)
    assert await service.homeassistant_targets() == []


async def test_worker_reconnect_backoff_safe_logs(
    service: MonitoringService, caplog: pytest.LogCaptureFixture
) -> None:
    target = (await service.homeassistant_targets())[0]
    worker = HomeAssistantWorker(service, None)
    with (
        patch(
            "app.workers.homeassistant.decrypt_secret", side_effect=RuntimeError("private-token")
        ),
        patch(
            "app.workers.homeassistant.asyncio.sleep",
            side_effect=[None] * 8 + [asyncio.CancelledError],
        ) as sleep,
    ):
        with pytest.raises(asyncio.CancelledError):
            await worker._monitor(target)
    assert [call.args[0] for call in sleep.await_args_list] == [1, 2, 4, 8, 16, 32, 60, 60, 60]
    assert "private-token" not in caplog.text


async def test_worker_connections_are_independent_and_shutdown_closes_streams(
    service: MonitoringService,
) -> None:
    target = (await service.homeassistant_targets())[0]
    started, closed = asyncio.Event(), asyncio.Event()
    monitor = HomeAssistantWebSocketMonitor(target.url, "secret", ENTITY)

    async def stream() -> AsyncGenerator[MonitorResult]:
        try:
            yield monitor.result("on")
            started.set()
            await asyncio.Event().wait()
        finally:
            closed.set()

    worker = HomeAssistantWorker(service, None)
    with (
        patch.object(
            service, "homeassistant_targets", return_value=[target, replace(target, device_id=30)]
        ),
        patch.object(service, "apply_homeassistant_result", return_value=True),
        patch(
            "app.workers.homeassistant.decrypt_secret",
            side_effect=[RuntimeError("offline"), "secret"],
        ),
        patch("app.workers.homeassistant.HomeAssistantWebSocketMonitor.stream", side_effect=stream),
    ):
        await worker.refresh()
        async with asyncio.timeout(1):
            await started.wait()
        assert len(worker._tasks) == 2
        await worker.stop()
    assert closed.is_set() and worker._tasks == {}


async def test_connection_test_confirms_subscription_and_closes_socket(socket: MagicMock) -> None:
    client = session_for(socket)
    with patch("app.monitoring.homeassistant_ws.aiohttp.ClientSession", return_value=client):
        result = await HomeAssistantWebSocketMonitor("http://ha.local", "secret", ENTITY).check()
    assert result.state == PowerState.ON and result.health == MonitorHealth.HEALTHY
    assert socket.send_json.await_count == 3
    client.ws_connect.return_value.__aexit__.assert_awaited_once()


async def test_switching_to_webhook_rejects_old_subscription_results(
    service: MonitoringService, db_session: Session
) -> None:
    target = (await service.homeassistant_targets())[0]
    config = db_session.get(HomeAssistantDeviceConfig, 3)
    assert config
    config.mode = HomeAssistantMode.WEBHOOK
    config.url = config.entity_id = config.access_token_encrypted = None
    config.webhook_token_hash = "a" * 64
    db_session.commit()
    assert not await service.apply_homeassistant_result(
        target, HomeAssistantWebSocketMonitor.unavailable()
    )
    assert await service.homeassistant_targets() == []


async def test_configuration_refresh_replaces_and_cancels_subscriptions(
    service: MonitoringService,
) -> None:
    target = (await service.homeassistant_targets())[0]
    worker = HomeAssistantWorker(service, None)
    cancelled: list[str] = []
    started = asyncio.Queue[str]()

    async def monitor(target: HomeAssistantTarget) -> None:
        try:
            await started.put(target.entity_id)
            await asyncio.Event().wait()
        finally:
            cancelled.append(target.entity_id)

    with (
        patch.object(worker, "_monitor", side_effect=monitor),
        patch.object(
            service,
            "homeassistant_targets",
            side_effect=[[target], [replace(target, entity_id="sensor.power")], []],
        ),
    ):
        await worker.refresh()
        assert await asyncio.wait_for(started.get(), 1) == ENTITY
        await worker.refresh()
        assert await asyncio.wait_for(started.get(), 1) == "sensor.power"
        await worker.refresh()
    assert cancelled == [ENTITY, "sensor.power"] and worker._tasks == {}
