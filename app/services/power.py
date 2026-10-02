from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.events.models import MonitorHealthChanged, PowerStateChanged
from app.models import Device
from app.models.enums import MonitorHealth, PowerState
from app.repositories.power import PowerIntervalRepository


async def record_observation(
    session: AsyncSession,
    device: Device,
    *,
    state: PowerState,
    health: MonitorHealth,
    detected_at: datetime,
    observed_at: datetime,
    checked_at: datetime,
    responded: bool,
    confirmed: bool,
) -> list[PowerStateChanged | MonitorHealthChanged]:
    """Apply normalized history and health inside the caller's locked device transaction."""
    events: list[PowerStateChanged | MonitorHealthChanged] = []
    previous_state, previous_health = device.current_power_state, device.monitor_health
    history = PowerIntervalRepository(session)
    if previous_state != state:
        await history.record_transition(device.user_id, device.id, state, detected_at)
        events.append(
            PowerStateChanged(
                user_id=device.user_id,
                device_id=device.id,
                previous_state=previous_state,
                new_state=state,
                detected_at=detected_at,
            )
        )
    elif await history.get_open(device.id) is None:
        if state == PowerState.UNKNOWN or confirmed:
            await history.record_transition(
                device.user_id,
                device.id,
                state,
                device.created_at if state == PowerState.UNKNOWN else detected_at,
            )
    device.last_monitor_observation_at = observed_at
    device.last_checked_at = checked_at
    if responded:
        device.last_successful_check_at = observed_at
    device.monitor_health = health
    if previous_health != health:
        device.monitor_health_changed_at = checked_at
        events.append(
            MonitorHealthChanged(
                user_id=device.user_id,
                device_id=device.id,
                previous_health=previous_health,
                new_health=health,
                detected_at=checked_at,
            )
        )
    return events
