from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analytics.service import ReportingWindow, calculate_statistics
from app.events.bus import EventBus
from app.events.models import MonitorHealthChanged, PowerStateChanged
from app.models import Device, DeviceNotificationChannel, PingDeviceConfig, PowerInterval
from app.models.enums import MonitorHealth, PowerState
from app.monitoring.base import MonitorResult
from app.monitoring.service import MonitoringService
from app.notifications.health import MonitorHealthNotificationHandler, format_health_notification

NOW = datetime(2026, 10, 2, 12, tzinfo=UTC)


@pytest.fixture
def monitoring(sessions: MagicMock, db_session: Session) -> MonitoringService:
    device = db_session.get(Device, 1)
    assert device is not None
    device.created_at = NOW - timedelta(hours=1)
    device.current_power_state = PowerState.ON
    device.monitor_health = MonitorHealth.HEALTHY
    device.last_successful_check_at = NOW - timedelta(minutes=10)
    db_session.add(PingDeviceConfig(device_id=1, host="localhost"))
    db_session.add(PowerInterval(device_id=1, state=PowerState.ON, started_at=device.created_at))
    db_session.commit()
    return MonitoringService(sessions, EventBus())


def failure(at: datetime) -> MonitorResult:
    return MonitorResult(
        PowerState.UNKNOWN,
        MonitorHealth.UNAVAILABLE,
        at,
        {"reason": "connection_unavailable", "confirmed": False},
    )


async def test_failure_streak_restart_duplicates_and_unknown_analytics(
    monitoring: MonitoringService, sessions: MagicMock, db_session: Session
) -> None:
    target = (await monitoring.ping_targets())[0]
    health_events, power_events = AsyncMock(), AsyncMock()
    monitoring.bus.subscribe(MonitorHealthChanged, health_events)
    monitoring.bus.subscribe(PowerStateChanged, power_events)
    first = failure(NOW)
    await monitoring.apply_ping_result(target, first)
    await monitoring.apply_ping_result(target, first)
    db_session.expire_all()
    device = db_session.get(Device, 1)
    assert device and device.current_power_state == PowerState.ON
    assert device.monitor_health == MonitorHealth.DEGRADED and device.monitor_failure_count == 1
    assert len(db_session.scalars(select(PowerInterval)).all()) == 1
    # A new service instance continues the persisted streak.
    restarted = MonitoringService(sessions, monitoring.bus)
    await restarted.apply_ping_result(target, failure(NOW + timedelta(seconds=10)))
    await restarted.apply_ping_result(target, failure(NOW + timedelta(seconds=20)))
    db_session.expire_all()
    device = db_session.get(Device, 1)
    assert device is not None
    assert device.current_power_state == PowerState.UNKNOWN
    assert device.monitor_health == MonitorHealth.UNAVAILABLE
    assert device.last_successful_check_at == NOW - timedelta(minutes=10)
    history = db_session.scalars(select(PowerInterval).order_by(PowerInterval.id)).all()
    assert [row.state for row in history] == [PowerState.ON, PowerState.UNKNOWN]
    assert history[1].started_at == NOW and history[0].ended_at == NOW
    assert health_events.await_count == 2 and power_events.await_count == 1
    stats = calculate_statistics(
        history,
        ReportingWindow(NOW - timedelta(hours=1), NOW + timedelta(hours=1)),
        now=NOW + timedelta(hours=1),
    )
    assert stats.off_duration == timedelta(0)
    assert stats.unknown_duration == timedelta(hours=1)
    assert stats.outage_count == 0


async def test_transient_failure_and_stale_sample_do_not_change_power(
    monitoring: MonitoringService, db_session: Session
) -> None:
    target = (await monitoring.ping_targets())[0]
    await monitoring.apply_ping_result(target, failure(NOW))
    success = MonitorResult(
        PowerState.ON,
        MonitorHealth.HEALTHY,
        NOW + timedelta(seconds=10),
        {"responded": True, "confirmed": True},
    )
    await monitoring.apply_ping_result(target, success)
    await monitoring.apply_ping_result(target, failure(NOW + timedelta(seconds=5)))
    db_session.expire_all()
    device = db_session.get(Device, 1)
    assert device and device.monitor_failure_count == 0
    assert (
        device.monitor_health == MonitorHealth.HEALTHY
        and device.current_power_state == PowerState.ON
    )
    assert device.last_successful_check_at == success.observed_at
    assert len(db_session.scalars(select(PowerInterval)).all()) == 1


async def test_ping_executor_failure_then_unconfirmed_recovery_keeps_power(
    monitoring: MonitoringService, db_session: Session
) -> None:
    target = (await monitoring.ping_targets())[0]
    monitor = target.monitor()
    with (
        patch("app.monitoring.ping.ping_once", side_effect=[OSError("secret"), True, True]),
        patch("app.monitoring.ping.datetime") as clock,
    ):
        clock.now.side_effect = [NOW + timedelta(seconds=10 * index) for index in range(3)]
        for _ in range(3):
            await monitoring.apply_ping_result(target, await monitor.check())
    db_session.expire_all()
    device = db_session.get(Device, 1)
    assert device and device.current_power_state == PowerState.ON
    assert device.monitor_health == MonitorHealth.HEALTHY
    assert len(db_session.scalars(select(PowerInterval)).all()) == 1


@pytest.fixture
def bot(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    monkeypatch.setattr("app.notifications.telegram.require_destination", AsyncMock())
    return AsyncMock()


@pytest.fixture
def linked(db_session: Session) -> None:
    db_session.add(DeviceNotificationChannel(device_id=1, channel_id=1, user_id=1))
    db_session.commit()


def health_event(db_session: Session, state: MonitorHealth, at: datetime) -> MonitorHealthChanged:
    device = db_session.get(Device, 1)
    assert device is not None
    previous = device.monitor_health
    device.monitor_health = state
    device.monitor_health_changed_at = at
    device.last_successful_check_at = NOW - timedelta(minutes=28)
    db_session.commit()
    return MonitorHealthChanged(
        user_id=1, device_id=1, previous_health=previous, new_health=state, detected_at=at
    )


@pytest.mark.parametrize("recovery_enabled", [False, True])
async def test_warning_dedup_restart_and_optional_recovery(
    sessions: MagicMock, db_session: Session, bot: AsyncMock, linked: None, recovery_enabled: bool
) -> None:
    handler = MonitorHealthNotificationHandler(
        sessions, bot, warnings_enabled=True, recovery_enabled=recovery_enabled
    )
    event = health_event(db_session, MonitorHealth.UNAVAILABLE, NOW)
    await handler.handle(event)
    await handler.handle(event)
    restarted = MonitorHealthNotificationHandler(
        sessions, bot, warnings_enabled=True, recovery_enabled=recovery_enabled
    )
    await restarted.handle(event)
    assert bot.send_message.await_count == 1
    assert "⚠️ Не вдалося перевірити стан пристрою" in bot.send_message.call_args.args[1]
    assert "14:32" in bot.send_message.call_args.args[1]
    recovered = health_event(db_session, MonitorHealth.HEALTHY, NOW + timedelta(minutes=5))
    await restarted.handle(recovered)
    await restarted.handle(recovered)
    assert bot.send_message.await_count == (2 if recovery_enabled else 1)


async def test_flapping_cooldown_and_no_unmatched_recovery(
    sessions: MagicMock, db_session: Session, bot: AsyncMock, linked: None
) -> None:
    handler = MonitorHealthNotificationHandler(
        sessions, bot, warnings_enabled=True, recovery_enabled=True
    )
    with patch("app.notifications.health.datetime") as clock:
        clock.now.return_value = NOW
        await handler.handle(health_event(db_session, MonitorHealth.UNAVAILABLE, NOW))
        clock.now.return_value = NOW + timedelta(minutes=1)
        await handler.handle(
            health_event(db_session, MonitorHealth.HEALTHY, clock.now.return_value)
        )
        clock.now.return_value = NOW + timedelta(minutes=2)
        await handler.handle(
            health_event(db_session, MonitorHealth.UNAVAILABLE, clock.now.return_value)
        )
        clock.now.return_value = NOW + timedelta(minutes=3)
        await handler.handle(
            health_event(db_session, MonitorHealth.HEALTHY, clock.now.return_value)
        )
        assert bot.send_message.await_count == 2
        clock.now.return_value = NOW + timedelta(hours=2)
        event = health_event(db_session, MonitorHealth.UNAVAILABLE, clock.now.return_value)
        await handler.handle(event)
        assert bot.send_message.await_count == 3
        clock.now.return_value += timedelta(days=1)
        await handler.handle(event)  # Cooldown expiration cannot replay an old event.
        assert bot.send_message.await_count == 3


async def test_warnings_opt_in_and_stale_events_ignored(
    sessions: MagicMock, db_session: Session, bot: AsyncMock, linked: None
) -> None:
    event = health_event(db_session, MonitorHealth.UNAVAILABLE, NOW)
    await MonitorHealthNotificationHandler(sessions, bot).handle(event)
    bot.send_message.assert_not_awaited()
    handler = MonitorHealthNotificationHandler(sessions, bot, warnings_enabled=True)
    health_event(db_session, MonitorHealth.HEALTHY, NOW + timedelta(seconds=1))
    await handler.handle(event)
    bot.send_message.assert_not_awaited()


async def test_failed_warning_is_reserved_without_recovery(
    sessions: MagicMock, db_session: Session, bot: AsyncMock, linked: None
) -> None:
    handler = MonitorHealthNotificationHandler(
        sessions, bot, warnings_enabled=True, recovery_enabled=True
    )
    event = health_event(db_session, MonitorHealth.UNAVAILABLE, NOW)
    bot.send_message.side_effect = TimeoutError("secret")
    await handler.handle(event)
    await handler.handle(event)
    bot.send_message.side_effect = None
    await handler.handle(
        health_event(db_session, MonitorHealth.HEALTHY, NOW + timedelta(seconds=5))
    )
    assert bot.send_message.await_count == 1


def test_health_formatting_missing_success_and_recovery() -> None:
    assert "Ще не було" in format_health_notification("Дім", None, recovery=False)
    assert format_health_notification("Дім", None, recovery=True) == (
        "✅ Перевірку стану пристрою відновлено\n\n🏠 Дім"
    )


async def test_snmp_communication_failure_and_unexpected_state(
    sessions: MagicMock, db_session: Session
) -> None:
    from app.models import SnmpDeviceConfig
    from app.models.enums import SnmpMode
    from app.monitoring.snmp import SnmpMonitor

    device = db_session.get(Device, 2)
    assert device is not None
    device.current_power_state = PowerState.ON
    device.monitor_health = MonitorHealth.HEALTHY
    db_session.add(
        SnmpDeviceConfig(
            device_id=2,
            host="localhost",
            port=161,
            community_encrypted=b"ciphertext",
            mode=SnmpMode.INTERFACE,
            interface_index=1,
        )
    )
    db_session.add(
        PowerInterval(device_id=2, state=PowerState.ON, started_at=NOW - timedelta(hours=1))
    )
    db_session.commit()
    service = MonitoringService(sessions, EventBus())
    target = (await service.snmp_targets())[0]
    assert "ciphertext" not in repr(target)
    for index in range(3):
        await service.apply_snmp_result(target, failure(NOW + timedelta(seconds=index * 10)))
    db_session.expire_all()
    assert device.current_power_state == PowerState.UNKNOWN
    assert device.monitor_health == MonitorHealth.UNAVAILABLE
    monitor = SnmpMonitor("localhost", 161, "secret", SnmpMode.INTERFACE, interface_index=1)
    with patch("app.monitoring.snmp.snmp_get", return_value="7"):
        unexpected = await monitor.check()
    await service.apply_snmp_result(target, unexpected)
    db_session.expire_all()
    device = db_session.get(Device, 2)
    assert device is not None
    assert device.current_power_state == PowerState.UNKNOWN
    assert device.monitor_health == MonitorHealth.DEGRADED
    assert device.monitor_failure_count == 0
    assert device.last_successful_check_at == unexpected.observed_at


async def test_snmp_worker_initialization_failure_is_persisted(
    sessions: MagicMock, db_session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from app.models import SnmpDeviceConfig
    from app.models.enums import SnmpMode
    from app.workers.ping import PingWorker

    db_session.add(
        SnmpDeviceConfig(
            device_id=2,
            host="localhost",
            community_encrypted=b"secret",
            mode=SnmpMode.INTERFACE,
            interface_index=1,
        )
    )
    db_session.commit()
    service = MonitoringService(sessions, EventBus())
    target = (await service.snmp_targets())[0]
    worker = PingWorker(MagicMock(spec=AsyncEngine), service)
    with patch("app.monitoring.service.decrypt_secret", side_effect=ValueError("secret")):
        for _ in range(3):
            await worker._monitor(target)
    db_session.expire_all()
    device = db_session.get(Device, 2)
    assert device and device.monitor_failure_count == 3
    assert device.monitor_health == MonitorHealth.UNAVAILABLE
    assert "secret" not in caplog.text


async def test_disabled_channel_and_degraded_health_are_quiet(
    sessions: MagicMock, db_session: Session, bot: AsyncMock, linked: None
) -> None:
    from app.models import NotificationChannel

    handler = MonitorHealthNotificationHandler(sessions, bot, warnings_enabled=True)
    await handler.handle(health_event(db_session, MonitorHealth.DEGRADED, NOW))
    bot.send_message.assert_not_awaited()
    channel = db_session.get(NotificationChannel, 1)
    assert channel is not None
    channel.enabled = False
    db_session.commit()
    await handler.handle(
        health_event(db_session, MonitorHealth.UNAVAILABLE, NOW + timedelta(seconds=5))
    )
    bot.send_message.assert_not_awaited()


def test_observation_timestamp_validation() -> None:
    with pytest.raises(ValueError, match="Detection"):
        MonitorResult(
            PowerState.ON, MonitorHealth.HEALTHY, NOW, observed_at=NOW - timedelta(seconds=1)
        )


async def test_degraded_reconnect_does_not_lose_pending_recovery(
    sessions: MagicMock, db_session: Session, bot: AsyncMock, linked: None
) -> None:
    handler = MonitorHealthNotificationHandler(
        sessions, bot, warnings_enabled=True, recovery_enabled=True
    )
    with patch("app.notifications.health.datetime") as clock:
        clock.now.return_value = NOW
        await handler.handle(health_event(db_session, MonitorHealth.UNAVAILABLE, NOW))
        clock.now.return_value += timedelta(minutes=1)
        await handler.handle(
            health_event(db_session, MonitorHealth.DEGRADED, clock.now.return_value)
        )
        clock.now.return_value += timedelta(minutes=1)
        await handler.handle(
            health_event(db_session, MonitorHealth.UNAVAILABLE, clock.now.return_value)
        )
        clock.now.return_value += timedelta(minutes=1)
        await handler.handle(
            health_event(db_session, MonitorHealth.HEALTHY, clock.now.return_value)
        )
    assert bot.send_message.await_count == 2
    assert "відновлено" in bot.send_message.call_args.args[1]


async def test_persistence_failure_is_not_a_network_failure(
    monitoring: MonitoringService,
) -> None:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from app.monitoring.service import PingTarget
    from app.workers.ping import PingWorker

    target = (await monitoring.ping_targets())[0]
    monitor = MagicMock()
    monitor.check = AsyncMock(return_value=MonitorResult(PowerState.ON, MonitorHealth.HEALTHY, NOW))
    worker = PingWorker(MagicMock(spec=AsyncEngine), monitoring)
    with (
        patch.object(PingTarget, "monitor", return_value=monitor),
        patch.object(
            monitoring, "apply_ping_result", side_effect=RuntimeError("database")
        ) as apply,
    ):
        await worker._monitor(target)
    apply.assert_awaited_once()
    assert apply.call_args.args[1].health == MonitorHealth.HEALTHY


async def test_snmp_worker_schedules_and_stops_independently() -> None:
    import asyncio

    from sqlalchemy.ext.asyncio import AsyncEngine

    from app.models.enums import SnmpMode
    from app.monitoring.service import SnmpTarget
    from app.workers.ping import PingWorker

    target = SnmpTarget(
        2, 1, "localhost", 161, b"secret", 1000, SnmpMode.INTERFACE, 1, None, None, None, NOW
    )
    service = AsyncMock(spec=MonitoringService)
    service.ping_targets.return_value = []
    service.snmp_targets.return_value = [target]
    service.apply_snmp_result.return_value = True
    checked = asyncio.Event()

    async def check() -> MonitorResult:
        checked.set()
        return MonitorResult(PowerState.ON, MonitorHealth.HEALTHY, NOW, {"responded": True})

    monitor = MagicMock(interval_seconds=1000)
    monitor.check = AsyncMock(side_effect=check)
    worker = PingWorker(MagicMock(spec=AsyncEngine), service)
    with patch.object(SnmpTarget, "monitor", return_value=monitor):
        try:
            await worker.refresh()
            async with asyncio.timeout(1):
                await checked.wait()
            service.apply_snmp_result.assert_awaited_once()
            service.apply_ping_result.assert_not_awaited()
        finally:
            await worker._stop_devices()
    assert not worker._tasks


async def test_ha_persistence_failure_does_not_change_monitor_health() -> None:
    import asyncio

    from app.monitoring.service import HomeAssistantTarget
    from app.workers.homeassistant import HomeAssistantWorker

    service = AsyncMock(spec=MonitoringService)
    service.apply_homeassistant_result.side_effect = RuntimeError("database")
    target = HomeAssistantTarget(
        3, 1, "http://ha.local", b"secret", "sensor.power", "on", "off", NOW
    )
    worker = HomeAssistantWorker(service, None)

    async def stream(_monitor: object) -> AsyncGenerator[MonitorResult]:
        yield MonitorResult(PowerState.ON, MonitorHealth.HEALTHY, NOW, {"responded": True})

    with (
        patch("app.workers.homeassistant.decrypt_secret", return_value="secret"),
        patch("app.workers.homeassistant.HomeAssistantWebSocketMonitor.stream", stream),
        patch("app.workers.homeassistant.asyncio.sleep", side_effect=asyncio.CancelledError),
    ):
        with pytest.raises(asyncio.CancelledError):
            await worker._monitor(target)
    service.apply_homeassistant_result.assert_awaited_once()
    assert service.apply_homeassistant_result.call_args.args[1].health == MonitorHealth.HEALTHY
