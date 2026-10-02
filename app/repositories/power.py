from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models import Device, PowerInterval
from app.models.enums import PowerState
from app.repositories.devices import DeviceRepository
from app.services.time import aware_utc


class PowerIntervalRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def record_transition(
        self,
        user_id: int,
        device_id: int,
        state: PowerState,
        detected_at: datetime,
        *,
        previous_state: PowerState | None = None,
    ) -> PowerInterval:
        detected_at = aware_utc(detected_at)
        device = await DeviceRepository(self.session).get(user_id, device_id, for_update=True)
        if device is None:
            raise LookupError("Device not found")
        # A replay can arrive after further transitions, even after a process restart.
        recorded = await self.session.scalar(
            select(PowerInterval)
            .where(
                PowerInterval.device_id == device_id,
                PowerInterval.started_at == detected_at,
                PowerInterval.state == state,
            )
            .order_by(PowerInterval.id)
            .limit(1)
        )
        if recorded is not None:
            return recorded
        if previous_state is not None and device.current_power_state != previous_state:
            # An unchanged observation is also harmless without requiring a matching predecessor.
            if device.current_power_state != state:
                raise ValueError("Event previous state does not match the device state")
        current = await self.get_open(device_id)
        if current is not None:
            if detected_at < current.started_at:
                raise ValueError("State transitions cannot precede the current interval")
            if current.state == state:
                return current
            if detected_at == current.started_at:
                raise ValueError("Different states must have distinct timestamps")
            current.ended_at = detected_at
            # Close before inserting: the partial unique index is checked on each statement.
            await self.session.flush()
        interval = PowerInterval(device_id=device_id, state=state, started_at=detected_at)
        self.session.add(interval)
        device.current_power_state = state
        device.last_state_changed_at = detected_at
        await self.session.flush()
        return interval

    async def get_open(self, device_id: int) -> PowerInterval | None:
        return await self.session.scalar(
            select(PowerInterval).where(
                PowerInterval.device_id == device_id, PowerInterval.ended_at.is_(None)
            )
        )

    async def list_overlapping(
        self, user_id: int, device_id: int, start: datetime, end: datetime
    ) -> list[PowerInterval]:
        start, end = aware_utc(start), aware_utc(end)
        if end <= start:
            raise ValueError("Period end must follow start")
        query = (
            select(PowerInterval)
            .join(Device, Device.id == PowerInterval.device_id)
            .where(
                Device.user_id == user_id,
                Device.id == device_id,
                PowerInterval.started_at < end,
                or_(PowerInterval.ended_at.is_(None), PowerInterval.ended_at > start),
            )
            .order_by(PowerInterval.started_at, PowerInterval.id)
        )
        return list((await self.session.scalars(query)).all())

    async def transition_with_previous(
        self,
        user_id: int,
        device_id: int,
        previous_state: PowerState,
        new_state: PowerState,
        detected_at: datetime,
    ) -> tuple[PowerInterval, PowerInterval] | None:
        previous = aliased(PowerInterval)
        row = (
            await self.session.execute(
                select(PowerInterval, previous)
                .join(Device, Device.id == PowerInterval.device_id)
                .join(
                    previous,
                    (previous.device_id == PowerInterval.device_id)
                    & (previous.ended_at == PowerInterval.started_at),
                )
                .where(
                    Device.user_id == user_id,
                    Device.id == device_id,
                    PowerInterval.state == new_state,
                    PowerInterval.started_at == aware_utc(detected_at),
                    previous.state == previous_state,
                )
                .order_by(previous.id.desc())
                .limit(1)
            )
        ).first()
        return (row[0], row[1]) if row is not None else None
