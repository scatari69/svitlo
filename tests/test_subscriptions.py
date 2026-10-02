from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    NotificationChannel,
    ScheduleNotificationChannel,
    ScheduleSubscription,
    ScheduleVersion,
)
from app.schedules.models import Freshness, OutageGroup, ProviderResult, Region
from app.schedules.service import ScheduleService
from app.services.subscriptions import CatalogUnavailable, SubscriptionManagementService

OWNER = 2**40
OTHER = OWNER + 1


@pytest.fixture
def service(sessions: MagicMock) -> SubscriptionManagementService:
    provider = MagicMock()
    provider.id = "test"
    provider.get_regions = AsyncMock(
        return_value=ProviderResult(
            (Region(provider="test", id="kyiv", name="Київська область"),), Freshness.FRESH
        )
    )
    provider.get_queues = AsyncMock(
        return_value=ProviderResult(
            tuple(
                OutageGroup(provider="test", region="kyiv", id=queue, name=f"Група {queue}")
                for queue in ("1.2", "3.1")
            ),
            Freshness.FRESH,
        )
    )
    return SubscriptionManagementService(sessions, ScheduleService([provider]))


async def test_create_is_persisted_and_idempotent(
    service: SubscriptionManagementService, db_session: Session
) -> None:
    key = uuid4().hex
    item = await service.create_subscription(OWNER, "test", "kyiv", "3.1", "Батьки", {1}, key)
    repeated = await service.create_subscription(OWNER, "test", "kyiv", "3.1", "Батьки", {1}, key)
    assert repeated == item
    assert item.region_name == "Київська область" and item.enabled
    assert len(list(db_session.scalars(select(ScheduleSubscription)))) == 2
    assert await service.channel_ids(OWNER, item.id) == [1]
    assert len(list(db_session.scalars(select(ScheduleNotificationChannel)))) == 1
    with pytest.raises(ValueError, match="already used"):
        await service.create_subscription(OWNER, "test", "kyiv", "3.1", "Інша назва", {1}, key)


async def test_mutations_and_disabled_list_preserve_links_and_source_history(
    service: SubscriptionManagementService, db_session: Session
) -> None:
    item = await service.create_subscription(OWNER, "test", "kyiv", "1.2", "Дім", {1}, uuid4().hex)
    channels = await service.channels(OWNER)
    private = next(channel for channel in channels if channel.name == "Особисті повідомлення")
    await service.update_channels(OWNER, item.id, {1, private.id})
    await service.set_enabled(OWNER, item.id, False)
    await service.set_enabled(OWNER, item.id, False)
    views = await service.list_subscriptions(OWNER)
    assert next(view for view in views if view.id == item.id).enabled is False
    assert await service.channel_ids(OWNER, item.id) == [1, private.id]
    await service.rename(OWNER, item.id, "Дача")
    await service.set_enabled(OWNER, item.id, True)
    view = await service.get_subscription(OWNER, item.id)
    assert view.name == "Дача" and view.enabled
    await service.update_channels(OWNER, item.id, set())
    assert await service.channel_ids(OWNER, item.id) == []


@pytest.mark.parametrize(
    "operation",
    ["get_subscription", "channel_ids", "delete", "set_enabled", "update_channels", "rename"],
)
async def test_cross_user_access_is_rejected(
    operation: str, service: SubscriptionManagementService
) -> None:
    arguments: tuple[object, ...] = ()
    if operation == "set_enabled":
        arguments = (False,)
    elif operation == "update_channels":
        arguments = ({2},)
    elif operation == "rename":
        arguments = ("Чужа назва",)
    with pytest.raises(LookupError):
        await getattr(service, operation)(OTHER, 1, *arguments)
    assert (await service.get_subscription(OWNER, 1)).name == "Home"


async def test_foreign_channels_and_forged_catalog_choice_cannot_be_saved(
    service: SubscriptionManagementService, db_session: Session
) -> None:
    with pytest.raises(LookupError):
        await service.create_subscription(OWNER, "test", "kyiv", "1.2", "Дім", {2}, uuid4().hex)
    with pytest.raises(ValueError, match="catalog"):
        await service.create_subscription(
            OWNER, "test", "kyiv", "forged", "Дім", set(), uuid4().hex
        )
    with pytest.raises(LookupError):
        await service.update_channels(OWNER, 1, {2})
    assert len(list(db_session.scalars(select(ScheduleSubscription)))) == 1
    assert list(db_session.scalars(select(ScheduleNotificationChannel))) == []


async def test_private_channel_is_owner_scoped_and_created_once(
    service: SubscriptionManagementService, db_session: Session
) -> None:
    first = await service.channels(OWNER)
    second = await service.channels(OWNER)
    assert first == second
    assert 2 not in {channel.id for channel in first}
    private = db_session.scalar(
        select(NotificationChannel).where(
            NotificationChannel.telegram_chat_id == OWNER, NotificationChannel.user_id == 1
        )
    )
    assert private and private.name == "Особисті повідомлення"
    assert len(first) == 2


async def test_delete_removes_only_subscription_links(
    service: SubscriptionManagementService, db_session: Session
) -> None:
    from datetime import date

    db_session.add(
        ScheduleVersion(
            provider="test",
            region="kyiv",
            queue="1.2",
            schedule_date=date(2026, 10, 2),
            content_hash="a" * 64,
            normalized_content={"slots": []},
        )
    )
    db_session.commit()
    await service.update_channels(OWNER, 1, {1})
    await service.delete(OWNER, 1)
    db_session.expire_all()
    assert db_session.get(ScheduleSubscription, 1) is None
    assert list(db_session.scalars(select(ScheduleNotificationChannel))) == []
    assert db_session.get(NotificationChannel, 1) is not None
    assert db_session.scalar(select(ScheduleVersion)) is not None


async def test_catalog_freshness_and_unavailability_are_visible(
    service: SubscriptionManagementService,
) -> None:
    catalog = await service.regions()
    assert catalog.items[0].name == "Київська область"
    provider = service.schedules.providers["test"]
    provider.get_regions = AsyncMock(return_value=ProviderResult(catalog.items, Freshness.STALE))  # type: ignore[method-assign]
    assert (await service.regions()).stale
    provider.get_regions = AsyncMock(return_value=ProviderResult(None, Freshness.UNAVAILABLE))  # type: ignore[method-assign]
    assert (
        await service.get_subscription(OWNER, 1)
    ).region_name == "Назва регіону тимчасово недоступна"
    with pytest.raises(CatalogUnavailable):
        await service.regions()
