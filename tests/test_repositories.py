from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Dialect
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.models import (
    Device,
    DeviceNotificationChannel,
    PowerInterval,
    ScheduleSubscription,
    ScheduleVersion,
    User,
)
from app.models.enums import PowerState
from app.repositories.channels import NotificationChannelRepository
from app.repositories.devices import DeviceRepository
from app.repositories.power import PowerIntervalRepository
from app.repositories.schedules import ScheduleRepository
from app.repositories.users import UserRepository

NOW = datetime(2026, 10, 2, 12, tzinfo=UTC)


@pytest.fixture
def async_session(db_session: Session) -> AsyncMock:
    """Mock async transport while exercising real SQL and constraints in SQLite.

    SQLite does not simulate PostgreSQL row locks; those statements are compiled separately.
    """
    session = AsyncMock(spec=AsyncSession)
    session.execute.side_effect = db_session.execute
    session.scalar.side_effect = db_session.scalar
    session.scalars.side_effect = db_session.scalars
    session.flush.side_effect = db_session.flush
    session.add.side_effect = db_session.add
    return session


async def test_user_upsert_is_idempotent_and_transaction_owned_by_caller(
    async_session: AsyncMock, db_session: Session
) -> None:
    repository = UserRepository(async_session)
    first = await repository.get_or_create(2**45)
    second = await repository.get_or_create(2**45)
    assert first.id == second.id
    assert first.telegram_user_id == 2**45
    assert len(db_session.scalars(select(User).where(User.telegram_user_id == 2**45)).all()) == 1
    async_session.commit.assert_not_called()
    db_session.rollback()
    assert db_session.scalar(select(User).where(User.telegram_user_id == 2**45)) is None
    for invalid in [0, -1, 2**63]:
        with pytest.raises(ValueError):
            await repository.get_or_create(invalid)


async def test_device_queries_are_scoped_and_exclude_deleted(
    async_session: AsyncMock, postgres_dialect: Dialect
) -> None:
    repository = DeviceRepository(async_session)
    assert await repository.get(1, 4) is None
    device = await repository.get(1, 1, for_update=True)
    assert device
    sql = str(async_session.scalar.call_args.args[0].compile(dialect=postgres_dialect))
    assert "FOR UPDATE" in sql and "devices.user_id" in sql
    device.enabled = False
    assert [device.id for device in await repository.list_owned(1, enabled_only=True)] == [2, 3]
    device.deleted_at = NOW
    assert await repository.get(1, 1) is None
    assert [device.id for device in await repository.list_owned(1)] == [2, 3]


async def test_interval_transitions_are_idempotent_and_preserve_unknown(
    async_session: AsyncMock, db_session: Session, postgres_dialect: Dialect
) -> None:
    repository = PowerIntervalRepository(async_session)
    initial = await repository.record_transition(1, 1, PowerState.ON, NOW)
    flush_count = async_session.flush.await_count
    duplicate = await repository.record_transition(1, 1, PowerState.ON, NOW + timedelta(minutes=1))
    assert duplicate.id == initial.id
    assert async_session.flush.await_count == flush_count
    outage = await repository.record_transition(1, 1, PowerState.OFF, NOW + timedelta(minutes=2))
    unknown = await repository.record_transition(
        1, 1, PowerState.UNKNOWN, NOW + timedelta(minutes=4)
    )
    assert initial.duration() == timedelta(minutes=2)
    assert outage.duration() == timedelta(minutes=2)
    assert unknown.ended_at is None and unknown.state == PowerState.UNKNOWN
    assert len(db_session.scalars(select(PowerInterval)).all()) == 3
    device = db_session.get(Device, 1)
    assert device and device.current_power_state == PowerState.UNKNOWN
    assert device.last_state_changed_at == NOW + timedelta(minutes=4)
    lock_query = async_session.scalar.call_args_list[0].args[0]
    assert "FOR UPDATE" in str(lock_query.compile(dialect=postgres_dialect))
    async_session.commit.assert_not_called()


async def test_transition_validation_prevents_history_corruption(async_session: AsyncMock) -> None:
    repository = PowerIntervalRepository(async_session)
    with pytest.raises(LookupError):
        await repository.record_transition(1, 4, PowerState.OFF, NOW)
    with pytest.raises(ValueError, match="timezone-aware"):
        await repository.record_transition(1, 1, PowerState.ON, NOW.replace(tzinfo=None))
    await repository.record_transition(1, 1, PowerState.ON, NOW)
    with pytest.raises(ValueError, match="precede"):
        await repository.record_transition(1, 1, PowerState.OFF, NOW - timedelta(seconds=1))
    current = await repository.record_transition(1, 1, PowerState.ON, NOW)
    assert current.ended_at is None


async def test_overlap_queries_include_open_and_cross_boundary_history(
    async_session: AsyncMock, db_session: Session
) -> None:
    repository = PowerIntervalRepository(async_session)
    await repository.record_transition(1, 1, PowerState.ON, NOW)
    await repository.record_transition(1, 1, PowerState.OFF, NOW + timedelta(hours=1))
    await repository.record_transition(1, 1, PowerState.UNKNOWN, NOW + timedelta(hours=2))
    device = db_session.get(Device, 1)
    assert device
    device.deleted_at = NOW + timedelta(hours=3)
    assert [
        interval.state
        for interval in await repository.list_overlapping(
            1, 1, NOW + timedelta(minutes=30), NOW + timedelta(hours=3)
        )
    ] == [PowerState.ON, PowerState.OFF, PowerState.UNKNOWN]
    assert [
        interval.state
        for interval in await repository.list_overlapping(
            1, 1, NOW + timedelta(hours=1), NOW + timedelta(hours=2)
        )
    ] == [PowerState.OFF]
    assert await repository.list_overlapping(2, 1, NOW, NOW + timedelta(hours=3)) == []
    with pytest.raises(ValueError):
        await repository.list_overlapping(1, 1, NOW, NOW)


async def test_channels_are_owner_scoped_and_respect_enabled_state(
    async_session: AsyncMock, db_session: Session
) -> None:
    repository = NotificationChannelRepository(async_session)
    assert [channel.id for channel in await repository.list_owned(1)] == [1]
    db_session.add(DeviceNotificationChannel(device_id=1, channel_id=1, user_id=1))
    db_session.flush()
    channels = await repository.for_device(1, 1)
    assert [channel.id for channel in channels] == [1]
    assert await repository.for_device(2, 1) == []
    channels[0].enabled = False
    assert await repository.for_device(1, 1) == []


async def test_schedule_queries_share_source_versions_without_devices(
    async_session: AsyncMock, db_session: Session
) -> None:
    repository = ScheduleRepository(async_session)
    assert [subscription.id for subscription in await repository.list_subscriptions(1)] == [1]
    assert await repository.list_subscriptions(2) == []
    subscription = db_session.get(ScheduleSubscription, 1)
    assert subscription
    subscription.enabled = False
    assert await repository.list_subscriptions(1) == []
    day = date(2026, 10, 2)
    for offset, content in enumerate(["a", "b", "a"]):
        db_session.add(
            ScheduleVersion(
                provider="test",
                region="kyiv",
                queue="1.2",
                schedule_date=day,
                content_hash=content * 64,
                normalized_content={"slots": []},
                fetched_at=NOW + timedelta(seconds=offset),
            )
        )
    db_session.flush()
    latest = await repository.latest_version("test", "kyiv", "1.2", day)
    assert latest and latest.content_hash == "a" * 64
    assert latest.fetched_at == NOW + timedelta(seconds=2)
    assert await repository.latest_version("test", "kyiv", "other", day) is None
