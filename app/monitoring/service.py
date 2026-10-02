from dataclasses import dataclass, field
from datetime import UTC, datetime

from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.events.bus import EventBus
from app.events.models import MonitorHealthChanged, PowerStateChanged
from app.models import Device, HomeAssistantDeviceConfig, PingDeviceConfig, SnmpDeviceConfig
from app.models.enums import HomeAssistantMode, MonitorHealth, MonitoringType, PowerState, SnmpMode
from app.monitoring.base import MonitorResult
from app.monitoring.ping import PingMonitor
from app.monitoring.snmp import SnmpMonitor
from app.repositories.devices import DeviceRepository
from app.services.power import record_observation
from app.services.secrets import decrypt_secret


@dataclass(frozen=True)
class PingTarget:
    device_id: int
    user_id: int
    host: str
    interval_seconds: float
    timeout_seconds: float
    failure_threshold: int
    recovery_threshold: int
    config_updated_at: datetime
    current_state: PowerState = field(compare=False)

    @classmethod
    def from_device(cls, device: Device, config: PingDeviceConfig) -> "PingTarget":
        return cls(
            device.id,
            device.user_id,
            config.host,
            config.interval_seconds,
            config.timeout_seconds,
            config.failure_threshold,
            config.recovery_threshold,
            config.updated_at,
            device.current_power_state,
        )

    def monitor(self) -> PingMonitor:
        return PingMonitor(
            self.host,
            self.timeout_seconds,
            self.failure_threshold,
            self.recovery_threshold,
            interval_seconds=self.interval_seconds,
            initial_state=self.current_state,
        )


@dataclass(frozen=True)
class HomeAssistantTarget:
    device_id: int
    user_id: int
    url: str
    access_token_encrypted: bytes = field(repr=False)
    entity_id: str
    on_state: str
    off_state: str
    config_updated_at: datetime

    @classmethod
    def from_device(
        cls, device: Device, config: HomeAssistantDeviceConfig
    ) -> "HomeAssistantTarget":
        if config.url is None or config.access_token_encrypted is None or config.entity_id is None:
            raise ValueError("Invalid Home Assistant API configuration")
        return cls(
            device.id,
            device.user_id,
            config.url,
            config.access_token_encrypted,
            config.entity_id,
            config.on_state,
            config.off_state,
            config.updated_at,
        )


@dataclass(frozen=True)
class SnmpTarget:
    device_id: int
    user_id: int
    host: str
    port: int
    community_encrypted: bytes = field(repr=False)
    interval_seconds: float
    mode: SnmpMode
    interface_index: int | None
    oid: str | None
    on_value: str | None
    off_value: str | None
    config_updated_at: datetime

    @classmethod
    def from_device(cls, device: Device, config: SnmpDeviceConfig) -> "SnmpTarget":
        return cls(
            device.id,
            device.user_id,
            config.host,
            config.port,
            config.community_encrypted,
            config.polling_interval_seconds,
            config.mode,
            config.interface_index,
            config.oid,
            config.on_value,
            config.off_value,
            config.updated_at,
        )

    def monitor(self, encryption_key: SecretStr | None) -> SnmpMonitor:
        return SnmpMonitor(
            self.host,
            self.port,
            decrypt_secret(encryption_key, self.community_encrypted),
            self.mode,
            polling_interval_seconds=self.interval_seconds,
            interface_index=self.interface_index,
            oid=self.oid,
            on_value=self.on_value,
            off_value=self.off_value,
        )


class MonitoringService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        bus: EventBus,
        *,
        failure_threshold: int = 3,
    ) -> None:
        if not 2 <= failure_threshold <= 20:
            raise ValueError("Invalid monitor failure threshold")
        self.sessions, self.bus = sessions, bus
        self.failure_threshold = failure_threshold

    async def ping_targets(self) -> list[PingTarget]:
        async with self.sessions.begin() as session:
            return [
                PingTarget.from_device(device, config)
                for device, config in await DeviceRepository(session).list_enabled_ping()
            ]

    async def homeassistant_targets(self) -> list[HomeAssistantTarget]:
        async with self.sessions.begin() as session:
            return [
                HomeAssistantTarget.from_device(device, config)
                for device, config in await DeviceRepository(session).list_enabled_homeassistant()
            ]

    async def snmp_targets(self) -> list[SnmpTarget]:
        async with self.sessions.begin() as session:
            return [
                SnmpTarget.from_device(device, config)
                for device, config in await DeviceRepository(session).list_enabled_snmp()
            ]

    async def apply_snmp_result(self, target: SnmpTarget, result: MonitorResult) -> bool:
        return await self._apply_result(target, result)

    async def apply_ping_result(self, target: PingTarget, result: MonitorResult) -> bool:
        return await self._apply_result(target, result)

    async def apply_homeassistant_result(
        self, target: HomeAssistantTarget, result: MonitorResult
    ) -> bool:
        return await self._apply_result(target, result)

    async def _apply_result(
        self, target: PingTarget | HomeAssistantTarget | SnmpTarget, result: MonitorResult
    ) -> bool:
        """Persist under the device lock; discard probes for retired or changed configurations."""
        events: list[PowerStateChanged | MonitorHealthChanged] = []
        async with self.sessions.begin() as session:
            device = await DeviceRepository(session).get(
                target.user_id, target.device_id, for_update=True
            )
            backend = (
                MonitoringType.PING
                if isinstance(target, PingTarget)
                else MonitoringType.SNMP
                if isinstance(target, SnmpTarget)
                else MonitoringType.HOME_ASSISTANT
            )
            if device is None or not device.enabled or device.monitoring_type != backend:
                return False
            if isinstance(target, PingTarget):
                config = await session.get(PingDeviceConfig, target.device_id)
                if config is None or PingTarget.from_device(device, config) != target:
                    return False
            elif isinstance(target, SnmpTarget):
                snmp_config = await session.get(SnmpDeviceConfig, target.device_id)
                if snmp_config is None or SnmpTarget.from_device(device, snmp_config) != target:
                    return False
            else:
                ha_config = await session.get(HomeAssistantDeviceConfig, target.device_id)
                if (
                    ha_config is None
                    or ha_config.mode != HomeAssistantMode.API
                    or HomeAssistantTarget.from_device(device, ha_config) != target
                ):
                    return False
            assert result.observed_at is not None
            if (
                device.last_monitor_observation_at is not None
                and result.observed_at <= device.last_monitor_observation_at
            ):
                return True  # Duplicate/stale samples do not advance a failure streak.
            now = datetime.now(UTC)
            state, health, detected_at = result.state, result.health, result.detected_at
            if health == MonitorHealth.UNAVAILABLE:
                device.monitor_failure_count += 1
                if device.monitor_failure_started_at is None:
                    device.monitor_failure_started_at = result.observed_at
                if device.monitor_failure_count < self.failure_threshold:
                    state, health = device.current_power_state, MonitorHealth.DEGRADED
                else:
                    state = PowerState.UNKNOWN
                    detected_at = device.monitor_failure_started_at
            else:
                device.monitor_failure_count = 0
                device.monitor_failure_started_at = None
                if isinstance(target, PingTarget) and result.metadata.get("confirmed") is False:
                    state = device.current_power_state
            events = await record_observation(
                session,
                device,
                state=state,
                health=health,
                detected_at=detected_at,
                observed_at=result.observed_at,
                checked_at=now,
                responded=result.metadata.get("responded") is True,
                confirmed=result.metadata.get("confirmed") is True,
            )
        # State/history commits before dispatch; the history consumer can safely replay the event.
        for event in events:
            await self.bus.publish(event)
        return True
