from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Device, PingDeviceConfig, PowerInterval
from app.models.enums import MonitorHealth, MonitoringType, PowerState
from app.monitoring.base import MonitorResult
from app.services.device_setup import DeviceSetupService
from app.services.devices import DeviceManagementService

PING = {
    "host": "localhost",
    "interval_seconds": "10",
    "timeout_seconds": "2",
    "failure_threshold": "3",
    "recovery_threshold": "2",
}


@pytest.fixture
def device_service(sessions: MagicMock) -> Iterator[DeviceManagementService]:
    setup = DeviceSetupService(None)
    with patch.object(
        setup,
        "test",
        return_value=MonitorResult(
            PowerState.ON,
            MonitorHealth.HEALTHY,
            datetime.now(UTC),
        ),
    ):
        yield DeviceManagementService(sessions, setup)


async def test_save_persists_tested_config_without_fabricating_power_history(
    device_service: DeviceManagementService,
    db_session: Session,
) -> None:
    device = await device_service.save_setup(2**40, "Дім", MonitoringType.PING, PING)
    assert device.enabled and device.current_power_state == PowerState.UNKNOWN
    assert device.last_successful_check_at is not None
    config = db_session.get(PingDeviceConfig, device.id)
    assert config and config.host == "localhost" and config.failure_threshold == 3
    assert list(db_session.scalars(select(PowerInterval))) == []
    assert [d.id for d in await device_service.list_devices(2**40 + 1)] == [4]


async def test_failed_probe_cannot_save(
    device_service: DeviceManagementService, db_session: Session
) -> None:
    with patch.object(
        device_service.setup,
        "test",
        return_value=MonitorResult(
            PowerState.UNKNOWN,
            MonitorHealth.UNAVAILABLE,
            datetime.now(UTC),
        ),
    ):
        with pytest.raises(ConnectionError):
            await device_service.save_setup(2**40, "Дім", MonitoringType.PING, PING)
    assert len(list(db_session.scalars(select(Device)))) == 4


async def test_reconfigure_and_rename_are_owner_scoped(
    device_service: DeviceManagementService,
    db_session: Session,
) -> None:
    with pytest.raises(LookupError):
        await device_service.save_setup(2**40 + 1, "Дім", MonitoringType.PING, PING, 1)
    with pytest.raises(LookupError):
        await device_service.rename(2**40 + 1, 1, "Чужий")
    await device_service.save_setup(2**40, "Дім", MonitoringType.PING, PING, 1)
    await device_service.save_setup(
        2**40, "Дім", MonitoringType.PING, {**PING, "host": "127.0.0.1"}, 1
    )
    assert (await device_service.rename(2**40, 1, "Офіс")).name == "Офіс"
    config = db_session.get(PingDeviceConfig, 1)
    assert config and config.host == "127.0.0.1"
    assert len(list(db_session.scalars(select(PingDeviceConfig)))) == 1


async def test_soft_delete_closes_interval_retains_history_and_denies_other_owner(
    device_service: DeviceManagementService,
    db_session: Session,
) -> None:
    db_session.add(
        PowerInterval(
            device_id=1, state=PowerState.ON, started_at=datetime.now(UTC) - timedelta(hours=1)
        )
    )
    db_session.commit()
    with pytest.raises(LookupError):
        await device_service.delete(2**40 + 1, 1)
    await device_service.delete(2**40, 1)
    interval = db_session.scalar(select(PowerInterval))
    device = db_session.get(Device, 1)
    assert interval and interval.ended_at is not None
    assert device and not device.enabled and device.deleted_at == interval.ended_at
    assert 1 not in [d.id for d in await device_service.list_devices(2**40)]
    with pytest.raises(LookupError):
        await device_service.check(2**40, 1)


async def test_diagnostic_check_cannot_change_history(
    device_service: DeviceManagementService,
    db_session: Session,
) -> None:
    await device_service.save_setup(2**40, "Дім", MonitoringType.PING, PING, 1)
    result = await device_service.check(2**40, 1)
    assert result.state == PowerState.ON
    assert list(db_session.scalars(select(PowerInterval))) == []
    assert (await device_service.get_device(2**40, 1)).current_power_state == PowerState.UNKNOWN


@pytest.mark.parametrize("health", [MonitorHealth.HEALTHY, MonitorHealth.UNAVAILABLE])
async def test_diagnostic_check_preserves_worker_health_and_observation_ordering(
    device_service: DeviceManagementService, db_session: Session, health: MonitorHealth
) -> None:
    await device_service.save_setup(2**40, "Дім", MonitoringType.PING, PING, 1)
    observed = datetime.now(UTC)
    device = db_session.get(Device, 1)
    assert device
    device.monitor_health = MonitorHealth.DEGRADED
    device.monitor_health_changed_at = observed - timedelta(minutes=1)
    device.monitor_failure_count = 2
    device.monitor_failure_started_at = observed - timedelta(seconds=10)
    device.last_monitor_observation_at = observed
    device.last_successful_check_at = observed
    db_session.commit()
    with patch.object(
        device_service.setup,
        "test",
        return_value=MonitorResult(PowerState.UNKNOWN, health, observed - timedelta(seconds=1)),
    ):
        await device_service.check(2**40, 1)
    db_session.refresh(device)
    assert device.monitor_health == MonitorHealth.DEGRADED
    assert device.monitor_health_changed_at == observed - timedelta(minutes=1)
    assert device.monitor_failure_count == 2
    assert device.monitor_failure_started_at == observed - timedelta(seconds=10)
    assert device.last_monitor_observation_at == observed
    assert device.last_successful_check_at == observed
