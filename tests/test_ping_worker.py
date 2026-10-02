import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.orm import Session

from app.analytics.history import HistoryHandler
from app.events.bus import EventBus
from app.events.models import DomainEvent, PowerStateChanged
from app.models import Device, PingDeviceConfig, PowerInterval
from app.models.enums import MonitorHealth, PowerState
from app.monitoring.base import MonitorResult
from app.monitoring.service import MonitoringService, PingTarget
from app.workers.ping import PingWorker

NOW = datetime(2026, 10, 2, 12, tzinfo=UTC)


@pytest.fixture
def service(sessions: MagicMock, db_session: Session) -> MonitoringService:
    device = db_session.get(Device, 1)
    assert device
    device.created_at = NOW - timedelta(hours=1)
    db_session.add(PingDeviceConfig(device_id=1, host="localhost"))
    db_session.commit()
    bus = EventBus()
    bus.subscribe(PowerStateChanged, HistoryHandler(sessions).handle)
    return MonitoringService(sessions, bus)


async def test_ping_worker_persists_first_failure_recovery_and_idempotent_history(
    service: MonitoringService,
    db_session: Session,
) -> None:
    target = (await service.ping_targets())[0]
    monitor = target.monitor()
    events = AsyncMock()
    service.bus.subscribe(DomainEvent, events)
    with (
        patch(
            "app.monitoring.ping.ping_once",
            side_effect=[True, True, False, False, False, True, True],
        ),
        patch("app.monitoring.ping.datetime") as clock,
    ):
        clock.now.side_effect = [NOW + timedelta(seconds=10 * i) for i in range(7)]
        for _ in range(7):
            result = await monitor.check()
            assert await service.apply_ping_result(target, result)
            # Exact repeated results do not create another interval or event.
            assert await service.apply_ping_result(target, result)
    intervals = list(db_session.scalars(select(PowerInterval).order_by(PowerInterval.id)))
    assert [interval.state for interval in intervals] == [
        PowerState.UNKNOWN,
        PowerState.ON,
        PowerState.OFF,
        PowerState.ON,
    ]
    assert intervals[1].started_at == NOW
    assert intervals[2].started_at == NOW + timedelta(seconds=20)
    assert intervals[3].started_at == NOW + timedelta(seconds=50)
    assert all(a.ended_at == b.started_at for a, b in zip(intervals, intervals[1:], strict=False))
    assert (
        len([call for call in events.call_args_list if isinstance(call.args[0], PowerStateChanged)])
        == 3
    )


async def test_restart_seeds_known_state_and_keeps_existing_open_interval(
    service: MonitoringService,
    db_session: Session,
) -> None:
    device = db_session.get(Device, 1)
    assert device
    device.current_power_state = PowerState.ON
    db_session.add(
        PowerInterval(device_id=1, state=PowerState.ON, started_at=NOW - timedelta(hours=1))
    )
    db_session.commit()
    target = (await service.ping_targets())[0]
    restarted = target.monitor()
    with (
        patch("app.monitoring.ping.ping_once", side_effect=[False, False, False]),
        patch("app.monitoring.ping.datetime") as clock,
    ):
        clock.now.side_effect = [NOW + timedelta(seconds=10 * i) for i in range(3)]
        first = await restarted.check()
        assert first.state == PowerState.ON
        await service.apply_ping_result(target, first)
        await service.apply_ping_result(target, await restarted.check())
        assert len(list(db_session.scalars(select(PowerInterval)))) == 1
        await service.apply_ping_result(target, await restarted.check())
    db_session.expire_all()
    intervals = list(db_session.scalars(select(PowerInterval).order_by(PowerInterval.id)))
    assert [i.state for i in intervals] == [PowerState.ON, PowerState.OFF]
    assert intervals[0].ended_at == first.detected_at
    assert intervals[1].ended_at is None


async def test_disabled_deleted_or_reconfigured_probe_is_discarded(
    service: MonitoringService,
    db_session: Session,
) -> None:
    target = (await service.ping_targets())[0]
    result = MonitorResult(PowerState.ON, MonitorHealth.HEALTHY, NOW, {"responded": True})
    config = db_session.get(PingDeviceConfig, 1)
    assert config
    config.host = "127.0.0.1"
    config.updated_at += timedelta(seconds=1)
    db_session.commit()
    assert not await service.apply_ping_result(target, result)
    target = (await service.ping_targets())[0]
    device = db_session.get(Device, 1)
    assert device
    device.enabled = False
    db_session.commit()
    assert await service.ping_targets() == []
    assert not await service.apply_ping_result(target, result)
    device.enabled = True
    device.deleted_at = NOW
    db_session.commit()
    assert await service.ping_targets() == []
    assert not await service.apply_ping_result(target, result)
    assert list(db_session.scalars(select(PowerInterval))) == []


async def test_one_failed_device_does_not_stop_other_checks_and_refresh_reloads_settings() -> None:
    target = PingTarget(1, 1, "localhost", 1000, 2, 3, 2, NOW, PowerState.UNKNOWN)
    other = replace(target, device_id=2)
    service = AsyncMock(spec=MonitoringService)
    service.ping_targets.return_value = [target, other]
    service.apply_ping_result.return_value = True
    called = asyncio.Event()
    bad = MagicMock()
    bad.check = AsyncMock(side_effect=RuntimeError("broken device"))
    good = MagicMock(interval_seconds=1000)

    async def check() -> MonitorResult:
        called.set()
        return MonitorResult(PowerState.ON, MonitorHealth.HEALTHY, NOW)

    good.check = AsyncMock(side_effect=check)
    worker = PingWorker(MagicMock(spec=AsyncEngine), service)

    def monitor(target: PingTarget) -> MagicMock:
        return bad if target.device_id == 1 else good

    with patch.object(PingTarget, "monitor", monitor):
        try:
            await worker.refresh()
            async with asyncio.timeout(1):
                await called.wait()
            assert worker._tasks[1][1].done()
            assert service.apply_ping_result.await_count == 2
            results = [call.args[1] for call in service.apply_ping_result.await_args_list]
            assert {result.health for result in results} == {
                MonitorHealth.HEALTHY,
                MonitorHealth.UNAVAILABLE,
            }
            running = worker._tasks[2][1]
            service.ping_targets.return_value = [replace(other, interval_seconds=30)]
            await worker.refresh()
            assert running.cancelled()
            assert worker._tasks[2][0].interval_seconds == 30
            assert 1 not in worker._tasks
            # Persisted power state updates alone must not reset debounce counters.
            running = worker._tasks[2][1]
            service.ping_targets.return_value = [
                replace(other, interval_seconds=30, current_state=PowerState.ON)
            ]
            await worker.refresh()
            assert worker._tasks[2][1] is running
        finally:
            await worker._stop_devices()
    assert worker._tasks == {}


@pytest.mark.parametrize("unlock_fails", [False, True])
async def test_worker_releases_advisory_lock_and_cancels_device_tasks_on_shutdown(
    unlock_fails: bool,
) -> None:
    engine = MagicMock(spec=AsyncEngine)
    connection = AsyncMock(spec=AsyncConnection)
    connection.scalar.return_value = True
    engine.connect.return_value.__aenter__.return_value = connection
    service = AsyncMock(spec=MonitoringService)
    service.ping_targets.return_value = []
    worker = PingWorker(engine, service)
    started = asyncio.Event()
    if unlock_fails:
        connection.execute.side_effect = ConnectionError("Database unavailable")

    async def lead(conn: AsyncConnection) -> None:
        started.set()
        await asyncio.Event().wait()

    with (
        patch.object(worker, "_lead", side_effect=lead),
        patch.object(worker, "_stop_devices") as stop,
    ):
        task = asyncio.create_task(worker.run())
        async with asyncio.timeout(1):
            await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    stop.assert_awaited_once()
    assert "pg_advisory_unlock" in str(connection.execute.call_args.args[0])
    connection.commit.assert_awaited()
    if unlock_fails:
        connection.invalidate.assert_awaited_once()


async def test_follower_does_not_start_checks() -> None:
    engine = MagicMock(spec=AsyncEngine)
    connection = AsyncMock(spec=AsyncConnection)
    connection.scalar.return_value = False
    committed = asyncio.Event()
    connection.commit.side_effect = committed.set
    engine.connect.return_value.__aenter__.return_value = connection
    worker = PingWorker(engine, AsyncMock(spec=MonitoringService), refresh_seconds=0.001)
    with patch.object(worker, "_lead") as lead:
        task = asyncio.create_task(worker.run())
        try:
            async with asyncio.timeout(1):
                await committed.wait()
            lead.assert_not_awaited()
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


async def test_slow_event_consumers_do_not_occupy_network_check_slots() -> None:
    target = PingTarget(1, 1, "localhost", 1000, 2, 3, 2, NOW, PowerState.UNKNOWN)
    service = AsyncMock(spec=MonitoringService)
    worker = PingWorker(MagicMock(spec=AsyncEngine), service)
    worker._checks = asyncio.Semaphore(1)
    monitor = MagicMock(interval_seconds=1000)
    monitor.check = AsyncMock(return_value=MonitorResult(PowerState.ON, MonitorHealth.HEALTHY, NOW))
    applied = asyncio.Event()

    async def apply(*args: object) -> bool:
        applied.set()
        await asyncio.Event().wait()
        return True

    service.apply_ping_result.side_effect = apply
    with patch.object(PingTarget, "monitor", return_value=monitor):
        task = asyncio.create_task(worker._monitor(target))
        try:
            async with asyncio.timeout(1):
                await applied.wait()
                # Another device can check while this device awaits a slow subscriber.
                async with worker._checks:
                    pass
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


async def test_discovery_failure_preserves_leadership_and_other_backends() -> None:
    from app.workers.homeassistant import HomeAssistantWorker

    homeassistant = AsyncMock(spec=HomeAssistantWorker)
    called = asyncio.Event()
    homeassistant.refresh.side_effect = called.set
    worker = PingWorker(
        MagicMock(spec=AsyncEngine), AsyncMock(spec=MonitoringService), homeassistant=homeassistant
    )
    with patch.object(worker, "refresh", side_effect=RuntimeError("Unavailable discovery")):
        task = asyncio.create_task(worker._lead(AsyncMock(spec=AsyncConnection)))
        try:
            async with asyncio.timeout(1):
                await called.wait()
            assert not task.done()
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
