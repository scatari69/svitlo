import base64
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Device, HomeAssistantDeviceConfig, PingDeviceConfig, SnmpDeviceConfig
from app.models.enums import MonitorHealth, MonitoringType, PowerState
from app.monitoring.base import MonitorResult
from app.repositories.channels import NotificationChannelRepository
from app.repositories.devices import DeviceRepository
from app.repositories.power import PowerIntervalRepository
from app.repositories.users import UserRepository
from app.services.device_setup import CONFIG_CLASSES, DeviceSetupService
from app.services.validation import validate_name


@dataclass(frozen=True)
class DeviceView:
    id: int
    name: str
    monitoring_type: MonitoringType
    current_power_state: PowerState
    enabled: bool
    last_state_changed_at: datetime | None
    last_successful_check_at: datetime | None
    monitor_health: MonitorHealth = MonitorHealth.UNAVAILABLE

    @classmethod
    def from_device(cls, device: Device) -> "DeviceView":
        return cls(
            id=device.id,
            name=device.name,
            monitoring_type=device.monitoring_type,
            current_power_state=device.current_power_state,
            enabled=device.enabled,
            last_state_changed_at=device.last_state_changed_at,
            last_successful_check_at=device.last_successful_check_at,
            monitor_health=device.monitor_health,
        )


class DeviceManagementService:
    """Owner-scoped device management; transport and monitoring adapters stay outside handlers."""

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], setup: DeviceSetupService
    ) -> None:
        self.session_factory = session_factory
        self.setup = setup

    async def list_devices(self, telegram_user_id: int) -> list[DeviceView]:
        async with self.session_factory.begin() as session:
            user = await UserRepository(session).get_or_create(telegram_user_id)
            devices = await DeviceRepository(session).list_owned(user.id)
            return [DeviceView.from_device(device) for device in devices]

    async def get_device(self, telegram_user_id: int, device_id: int) -> DeviceView:
        async with self.session_factory.begin() as session:
            user = await UserRepository(session).get_or_create(telegram_user_id)
            device = await DeviceRepository(session).get(user.id, device_id)
            if device is None:
                raise LookupError("Device not found")
            return DeviceView.from_device(device)

    async def save_setup(
        self,
        telegram_user_id: int,
        name: str,
        method: MonitoringType,
        values: dict[str, str],
        device_id: int = 0,
    ) -> DeviceView:
        name = self.validate_name(name)
        # Check authorization before making network requests for an existing device.
        if device_id:
            existing = await self.get_device(telegram_user_id, device_id)
            if existing.monitoring_type != method:
                raise ValueError("Changing a monitoring backend requires a new device")
        result = await self.setup.test(method, values)
        if result.health != MonitorHealth.HEALTHY or result.state == PowerState.UNKNOWN:
            raise ConnectionError("Connection test failed")
        config_values = self.setup.config_values(method, values)
        async with self.session_factory.begin() as session:
            user = await UserRepository(session).get_or_create(telegram_user_id)
            if device_id:
                device = await DeviceRepository(session).get(user.id, device_id, for_update=True)
                if device is None:
                    raise LookupError("Device not found")
                old = await session.get(CONFIG_CLASSES[method], device_id)
                if old is not None:
                    await session.delete(old)
                    await session.flush()
            else:
                device = Device(user_id=user.id, name=name, monitoring_type=method)
                session.add(device)
                await session.flush()
            device.name = name
            device.enabled = True
            device.last_checked_at = datetime.now(UTC)
            device.last_successful_check_at = (
                result.detected_at if values.get("mode") == "webhook" else device.last_checked_at
            )
            device.monitor_health = result.health
            device.monitor_health_changed_at = device.last_checked_at
            device.monitor_failure_count = 0
            device.monitor_failure_started_at = None
            device.last_monitor_observation_at = None
            # A setup probe validates connectivity, not the start of an observed power interval.
            session.add(CONFIG_CLASSES[method](device_id=device.id, **config_values))
            await session.flush()
            return DeviceView.from_device(device)

    async def rename(self, telegram_user_id: int, device_id: int, name: str) -> DeviceView:
        name = self.validate_name(name)
        async with self.session_factory.begin() as session:
            user = await UserRepository(session).get_or_create(telegram_user_id)
            device = await DeviceRepository(session).get(user.id, device_id, for_update=True)
            if device is None:
                raise LookupError("Device not found")
            device.name = name
            await session.flush()
            return DeviceView.from_device(device)

    async def delete(self, telegram_user_id: int, device_id: int) -> None:
        async with self.session_factory.begin() as session:
            user = await UserRepository(session).get_or_create(telegram_user_id)
            device = await DeviceRepository(session).get(user.id, device_id, for_update=True)
            if device is None:
                raise LookupError("Device not found")
            deleted_at = datetime.now(UTC)
            current = await PowerIntervalRepository(session).get_open(device_id)
            if current is not None:
                if current.started_at > deleted_at:
                    raise ValueError("Cannot close an interval starting in the future")
                current.ended_at = deleted_at
            device.deleted_at = deleted_at
            device.enabled = False
            await session.flush()

    async def channel_names(self, telegram_user_id: int, device_id: int) -> list[str]:
        async with self.session_factory.begin() as session:
            user = await UserRepository(session).get_or_create(telegram_user_id)
            if await DeviceRepository(session).get(user.id, device_id) is None:
                raise LookupError("Device not found")
            channels = await NotificationChannelRepository(session).for_device(user.id, device_id)
            return [channel.name for channel in channels]

    async def check(self, telegram_user_id: int, device_id: int) -> MonitorResult:
        async with self.session_factory.begin() as session:
            user = await UserRepository(session).get_or_create(telegram_user_id)
            device = await DeviceRepository(session).get(user.id, device_id)
            if device is None:
                raise LookupError("Device not found")
            method = device.monitoring_type
            config = cast(
                PingDeviceConfig | SnmpDeviceConfig | HomeAssistantDeviceConfig | None,
                await session.get(CONFIG_CLASSES[method], device_id),
            )
            if config is None:
                raise ValueError("Device has no monitoring configuration")
            config_updated_at = config.updated_at
            values = {}
            for column in config.__table__.columns:
                value = getattr(config, column.name)
                if value is not None:
                    values[column.name] = (
                        base64.urlsafe_b64encode(value).decode()
                        if isinstance(value, bytes)
                        else str(value)
                    )
        if method == MonitoringType.HOME_ASSISTANT and values.get("mode") == "webhook":
            # A webhook has no remote endpoint to poll: report its last received check honestly.
            return MonitorResult(
                device.current_power_state,
                device.monitor_health,
                device.last_successful_check_at or datetime.now(UTC),
            )
        result = await self.setup.test(method, values)
        async with self.session_factory.begin() as session:
            user = await UserRepository(session).get_or_create(telegram_user_id)
            device = await DeviceRepository(session).get(user.id, device_id, for_update=True)
            if device is None:
                raise LookupError("Device not found")
            current_config = cast(
                PingDeviceConfig | SnmpDeviceConfig | HomeAssistantDeviceConfig | None,
                await session.get(CONFIG_CLASSES[method], device_id),
            )
            if current_config is None or current_config.updated_at != config_updated_at:
                raise ValueError("Configuration changed during the check; retry")
            device.last_checked_at = datetime.now(UTC)
            # Diagnostics must not reset worker debounce, observation ordering or health events.
            if result.health == MonitorHealth.HEALTHY:
                assert result.observed_at is not None
                device.last_successful_check_at = max(
                    device.last_successful_check_at or result.observed_at, result.observed_at
                )
        return result

    @staticmethod
    def validate_name(name: str) -> str:
        return validate_name(name)
