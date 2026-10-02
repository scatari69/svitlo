from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Device, HomeAssistantDeviceConfig, PingDeviceConfig, SnmpDeviceConfig
from app.models.enums import HomeAssistantMode, MonitoringType


class DeviceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, user_id: int, device_id: int, *, for_update: bool = False) -> Device | None:
        query = select(Device).where(
            Device.id == device_id, Device.user_id == user_id, Device.deleted_at.is_(None)
        )
        if for_update:
            query = query.with_for_update().execution_options(populate_existing=True)
        return await self.session.scalar(query)

    async def list_owned(self, user_id: int, *, enabled_only: bool = False) -> list[Device]:
        query = select(Device).where(Device.user_id == user_id, Device.deleted_at.is_(None))
        if enabled_only:
            query = query.where(Device.enabled.is_(True))
        return list((await self.session.scalars(query.order_by(Device.id))).all())

    async def list_enabled_ping(self) -> list[tuple[Device, PingDeviceConfig]]:
        result = await self.session.execute(
            select(Device, PingDeviceConfig)
            .join(PingDeviceConfig, PingDeviceConfig.device_id == Device.id)
            .where(
                Device.enabled.is_(True),
                Device.deleted_at.is_(None),
                Device.monitoring_type == MonitoringType.PING,
            )
            .order_by(Device.id)
        )
        return [(row[0], row[1]) for row in result.all()]

    async def list_enabled_homeassistant(self) -> list[tuple[Device, HomeAssistantDeviceConfig]]:
        result = await self.session.execute(
            select(Device, HomeAssistantDeviceConfig)
            .join(HomeAssistantDeviceConfig, HomeAssistantDeviceConfig.device_id == Device.id)
            .where(
                Device.enabled.is_(True),
                Device.deleted_at.is_(None),
                HomeAssistantDeviceConfig.mode == HomeAssistantMode.API,
            )
            .order_by(Device.id)
        )
        return [(row[0], row[1]) for row in result.all()]

    async def list_enabled_snmp(self) -> list[tuple[Device, SnmpDeviceConfig]]:
        result = await self.session.execute(
            select(Device, SnmpDeviceConfig)
            .join(SnmpDeviceConfig, SnmpDeviceConfig.device_id == Device.id)
            .where(
                Device.enabled.is_(True),
                Device.deleted_at.is_(None),
                Device.monitoring_type == MonitoringType.SNMP,
            )
            .order_by(Device.id)
        )
        return [(row[0], row[1]) for row in result.all()]
