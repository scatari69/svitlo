import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

from app.analytics.history import HistoryHandler
from app.events.bus import EventBus
from app.events.logging import log_event
from app.events.models import DomainEvent, MonitorHealthChanged, PowerStateChanged, ScheduleChanged
from app.models import Device, PowerInterval
from app.models.enums import MonitorHealth, PowerState

NOW = datetime(2026, 10, 2, 12, tzinfo=UTC)


def power_event(previous: PowerState, new: PowerState, *, minute: int = 0) -> PowerStateChanged:
    return PowerStateChanged(
        user_id=1,
        device_id=1,
        previous_state=previous,
        new_state=new,
        detected_at=NOW + timedelta(minutes=minute),
    )


def create_history_bus(db_session: Session) -> EventBus:
    @asynccontextmanager
    async def transaction() -> AsyncIterator[AsyncMock]:
        # New ORM session per delivery exercises persisted state rather than a handler cache.
        with Session(db_session.get_bind(), expire_on_commit=False) as persisted:
            with persisted.begin():
                session = AsyncMock(spec=AsyncSession)
                session.scalar.side_effect = persisted.scalar
                session.scalars.side_effect = persisted.scalars
                session.flush.side_effect = persisted.flush
                session.add.side_effect = persisted.add
                yield session

    factory = MagicMock(spec=async_sessionmaker)
    factory.begin.side_effect = transaction
    bus = EventBus()
    bus.subscribe(PowerStateChanged, HistoryHandler(factory).handle)
    return bus


@pytest.fixture
def event_bus(db_session: Session) -> EventBus:
    return create_history_bus(db_session)


@pytest.mark.parametrize(
    ("previous", "new"), [(PowerState.ON, PowerState.OFF), (PowerState.OFF, PowerState.ON)]
)
async def test_confirmed_transitions_close_and_open_intervals(
    event_bus: EventBus, db_session: Session, previous: PowerState, new: PowerState
) -> None:
    await event_bus.publish(power_event(PowerState.UNKNOWN, previous))
    await event_bus.publish(power_event(previous, new, minute=3))
    intervals = list(db_session.scalars(select(PowerInterval).order_by(PowerInterval.id)))
    assert len(intervals) == 2
    assert intervals[0].state == previous
    assert intervals[0].started_at == NOW
    assert intervals[0].ended_at == NOW + timedelta(minutes=3)
    assert intervals[1].state == new and intervals[1].ended_at is None
    assert intervals[1].started_at == intervals[0].ended_at
    device = db_session.get(Device, 1)
    assert device and device.current_power_state == new
    assert device.last_state_changed_at == intervals[1].started_at


@pytest.mark.parametrize("state", [PowerState.ON, PowerState.OFF, PowerState.UNKNOWN])
async def test_repeated_states_leave_history_unchanged(
    event_bus: EventBus, db_session: Session, state: PowerState
) -> None:
    await event_bus.publish(power_event(PowerState.UNKNOWN, state))
    await event_bus.publish(power_event(state, state, minute=2))
    intervals = list(db_session.scalars(select(PowerInterval)))
    assert len(intervals) == 1
    assert intervals[0].state == state
    assert intervals[0].started_at == NOW and intervals[0].ended_at is None
    device = db_session.get(Device, 1)
    assert device and device.last_state_changed_at == NOW


async def test_unknown_time_is_neither_on_nor_off(event_bus: EventBus, db_session: Session) -> None:
    states = [PowerState.ON, PowerState.UNKNOWN, PowerState.OFF, PowerState.UNKNOWN, PowerState.ON]
    previous = PowerState.UNKNOWN
    for minute, state in enumerate(states):
        await event_bus.publish(power_event(previous, state, minute=minute * 3))
        previous = state
    intervals = list(db_session.scalars(select(PowerInterval).order_by(PowerInterval.id)))
    assert [interval.state for interval in intervals] == states
    durations = {
        state: sum(
            (
                interval.duration(until=NOW + timedelta(minutes=15))
                for interval in intervals
                if interval.state == state
            ),
            timedelta(),
        )
        for state in PowerState
    }
    assert durations == {
        PowerState.ON: timedelta(minutes=6),
        PowerState.OFF: timedelta(minutes=3),
        PowerState.UNKNOWN: timedelta(minutes=6),
    }


async def test_duplicate_delivery_and_delayed_replay_do_not_change_history(
    event_bus: EventBus, db_session: Session
) -> None:
    await event_bus.publish(power_event(PowerState.UNKNOWN, PowerState.ON))
    outage = power_event(PowerState.ON, PowerState.OFF, minute=1)
    await event_bus.publish(outage)
    await event_bus.publish(outage)
    await event_bus.publish(power_event(PowerState.OFF, PowerState.ON, minute=2))
    await event_bus.publish(outage)
    # Even a newly constructed duplicate has no effect; UUIDs are not the source of correctness.
    await event_bus.publish(power_event(PowerState.ON, PowerState.OFF, minute=1))
    intervals = list(db_session.scalars(select(PowerInterval).order_by(PowerInterval.id)))
    assert [interval.state for interval in intervals] == [
        PowerState.ON,
        PowerState.OFF,
        PowerState.ON,
    ]
    assert intervals[-1].ended_at is None
    device = db_session.get(Device, 1)
    assert device and device.current_power_state == PowerState.ON
    assert device.last_state_changed_at == NOW + timedelta(minutes=2)


async def test_restart_uses_existing_open_interval(db_session: Session) -> None:
    device = db_session.get(Device, 1)
    assert device
    device.current_power_state = PowerState.ON
    device.last_state_changed_at = NOW
    db_session.add(PowerInterval(device_id=1, state=PowerState.ON, started_at=NOW))
    db_session.commit()
    db_session.expunge_all()
    # Reconstruct both bus and subscriber with a fresh session factory after restart.
    bus = create_history_bus(db_session)
    await bus.publish(power_event(PowerState.ON, PowerState.ON, minute=1))
    await bus.publish(power_event(PowerState.ON, PowerState.OFF, minute=3))
    intervals = list(db_session.scalars(select(PowerInterval).order_by(PowerInterval.id)))
    assert len(intervals) == 2
    assert intervals[0].started_at == NOW
    assert intervals[0].ended_at == NOW + timedelta(minutes=3)
    assert intervals[1].state == PowerState.OFF and intervals[1].ended_at is None


async def test_invalid_event_rolls_back_and_surfaces_failure(
    event_bus: EventBus, db_session: Session
) -> None:
    await event_bus.publish(power_event(PowerState.UNKNOWN, PowerState.ON))
    with pytest.raises(ExceptionGroup) as error:
        await event_bus.publish(power_event(PowerState.OFF, PowerState.UNKNOWN, minute=2))
    assert isinstance(error.value.exceptions[0], ValueError)
    with pytest.raises(ExceptionGroup):
        await event_bus.publish(power_event(PowerState.ON, PowerState.OFF))
    intervals = list(db_session.scalars(select(PowerInterval)))
    assert len(intervals) == 1 and intervals[0].ended_at is None


async def test_bus_multiple_handlers_routing_order_and_failure_isolation() -> None:
    bus = EventBus()
    calls: list[str] = []

    async def first(event: PowerStateChanged) -> None:
        calls.append("first")
        raise RuntimeError("Handler failed")

    async def second(event: DomainEvent) -> None:
        calls.append(type(event).__name__)

    bus.subscribe(PowerStateChanged, first)
    bus.subscribe(DomainEvent, second)
    bus.subscribe(DomainEvent, second)
    with pytest.raises(ExceptionGroup) as error:
        await bus.publish(power_event(PowerState.ON, PowerState.OFF))
    assert len(error.value.exceptions) == 1
    assert calls == ["first", "PowerStateChanged"]
    calls.clear()
    await bus.publish(
        MonitorHealthChanged(
            user_id=1,
            device_id=1,
            previous_health=MonitorHealth.HEALTHY,
            new_health=MonitorHealth.DEGRADED,
            detected_at=NOW,
        )
    )
    assert calls == ["MonitorHealthChanged"]
    calls.clear()
    await bus.publish(
        ScheduleChanged(
            provider="test",
            region="kyiv",
            queue="1.2",
            schedule_date=date(2026, 10, 2),
            previous_version_id=None,
            new_version_id=1,
            detected_at=NOW,
        )
    )
    assert calls == ["ScheduleChanged"]


async def test_bus_cancellation_is_not_swallowed() -> None:
    bus = EventBus()
    handler = AsyncMock(side_effect=asyncio.CancelledError)
    later = AsyncMock()
    bus.subscribe(PowerStateChanged, handler)
    bus.subscribe(PowerStateChanged, later)
    with pytest.raises(asyncio.CancelledError):
        await bus.publish(power_event(PowerState.ON, PowerState.OFF))
    later.assert_not_awaited()


async def test_logging_handler(caplog: pytest.LogCaptureFixture) -> None:
    bus = EventBus()
    bus.subscribe(DomainEvent, log_event)
    with caplog.at_level(logging.DEBUG):
        await bus.publish(power_event(PowerState.ON, PowerState.OFF))
    assert "device_id=1" in caplog.text
    assert "previous_state=on new_state=off" in caplog.text


def test_events_are_immutable_and_timestamps_are_utc() -> None:
    event = power_event(PowerState.ON, PowerState.OFF)
    attribute = "detected_at"
    with pytest.raises(FrozenInstanceError):
        setattr(event, attribute, NOW + timedelta(minutes=1))
    localized = PowerStateChanged(
        user_id=1,
        device_id=1,
        previous_state=PowerState.ON,
        new_state=PowerState.OFF,
        detected_at=NOW.astimezone(ZoneInfo("Europe/Kyiv")),
    )
    assert localized.detected_at == NOW and localized.detected_at.tzinfo == UTC
    with pytest.raises(ValueError, match="timezone-aware"):
        PowerStateChanged(
            user_id=1,
            device_id=1,
            previous_state=PowerState.ON,
            new_state=PowerState.OFF,
            detected_at=NOW.replace(tzinfo=None),
        )
