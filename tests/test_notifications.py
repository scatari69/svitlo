from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import SendMessage
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.events.models import PowerStateChanged
from app.formatting import format_duration, ukrainian_plural
from app.models import (
    Device,
    DeviceNotificationChannel,
    NotificationChannel,
    PowerInterval,
    PowerNotificationDelivery,
)
from app.models.enums import ChannelType, PowerState
from app.notifications.formatting import format_power_notification
from app.notifications.service import PowerNotificationHandler
from app.notifications.telegram import send_power_message

NOW = datetime(2026, 10, 2, 12, tzinfo=UTC)


@pytest.mark.parametrize(
    ("number", "word"),
    [
        (0, "хвилин"),
        (1, "хвилина"),
        (2, "хвилини"),
        (4, "хвилини"),
        (5, "хвилин"),
        (11, "хвилин"),
        (12, "хвилин"),
        (14, "хвилин"),
        (21, "хвилина"),
        (22, "хвилини"),
        (25, "хвилин"),
        (101, "хвилина"),
        (111, "хвилин"),
    ],
)
def test_ukrainian_pluralization(number: int, word: str) -> None:
    assert ukrainian_plural(number, "хвилина", "хвилини", "хвилин") == word


@pytest.mark.parametrize(
    ("minutes", "expected"),
    [
        (0, "менше хвилини"),
        (1, "1 хвилина"),
        (2, "2 хвилини"),
        (5, "5 хвилин"),
        (60, "1 година"),
        (120, "2 години"),
        (300, "5 годин"),
        (61, "1 година 1 хвилина"),
        (201, "3 години 21 хвилина"),
        (660, "11 годин"),
        (1440, "24 години"),
        (1500, "25 годин"),
    ],
)
def test_duration_formatting(minutes: int, expected: str) -> None:
    assert format_duration(timedelta(minutes=minutes)) == expected
    assert format_duration(timedelta(minutes=minutes, seconds=59)) == expected


def test_negative_duration_and_plural_count_rejected() -> None:
    with pytest.raises(ValueError):
        format_duration(timedelta(seconds=-1))
    with pytest.raises(ValueError):
        ukrainian_plural(-1, "година", "години", "годин")


def test_outage_notification_format_and_kyiv_time() -> None:
    assert format_power_notification(
        "Дім", PowerState.OFF, NOW, timedelta(hours=3, minutes=42)
    ) == ("🔴 Зникло світло\n\n🏠 Дім\n🕒 15:00\n\nСвітло було:\n3 години 42 хвилини")


def test_restoration_format_and_winter_time() -> None:
    assert format_power_notification(
        "Офіс", PowerState.ON, datetime(2026, 1, 2, 12, tzinfo=UTC), timedelta(hours=2, minutes=36)
    ) == ("🟢 Світло з’явилося\n\n🏠 Офіс\n🕒 14:00\n\nСвітла не було:\n2 години 36 хвилин")


@pytest.mark.parametrize(("hour", "time"), [(0, "02:30"), (1, "04:30")])
def test_dst_changes_only_display_time(hour: int, time: str) -> None:
    text = format_power_notification(
        "Дім", PowerState.ON, datetime(2026, 3, 29, hour, 30, tzinfo=UTC), timedelta(hours=2)
    )
    assert f"🕒 {time}" in text and text.endswith("2 години")


@pytest.fixture
def outage(db_session: Session) -> PowerStateChanged:
    device = db_session.get(Device, 1)
    assert device
    device.name = "Дім <назва>"
    db_session.add_all(
        [
            PowerInterval(
                device_id=1,
                state=PowerState.ON,
                started_at=NOW - timedelta(hours=3, minutes=42),
                ended_at=NOW,
            ),
            PowerInterval(device_id=1, state=PowerState.OFF, started_at=NOW),
            DeviceNotificationChannel(user_id=1, device_id=1, channel_id=1),
            NotificationChannel(
                id=3,
                user_id=1,
                telegram_chat_id=-1234567890123,
                name="Channel",
                channel_type=ChannelType.CHANNEL,
            ),
            NotificationChannel(
                id=4,
                user_id=1,
                telegram_chat_id=-1234567890124,
                name="Disabled",
                channel_type=ChannelType.GROUP,
                enabled=False,
            ),
        ]
    )
    db_session.flush()
    db_session.add_all(
        [
            DeviceNotificationChannel(user_id=1, device_id=1, channel_id=3),
            DeviceNotificationChannel(user_id=1, device_id=1, channel_id=4),
        ]
    )
    db_session.commit()
    return PowerStateChanged(
        user_id=1,
        device_id=1,
        previous_state=PowerState.ON,
        new_state=PowerState.OFF,
        detected_at=NOW,
    )


async def test_multiple_channels_new_uuid_replay_and_restart_are_deduplicated(
    sessions: MagicMock,
    db_session: Session,
    outage: PowerStateChanged,
) -> None:
    bot = AsyncMock(spec=Bot)
    handler = PowerNotificationHandler(sessions, bot)
    await handler.handle(outage)
    await handler.handle(outage)
    await PowerNotificationHandler(sessions, bot).handle(
        PowerStateChanged(
            user_id=1,
            device_id=1,
            previous_state=PowerState.ON,
            new_state=PowerState.OFF,
            detected_at=NOW,
        )
    )
    assert bot.send_message.await_count == 2
    assert {call.args[0] for call in bot.send_message.call_args_list} == {-(2**40), -1234567890123}
    assert all(call.kwargs["parse_mode"] is None for call in bot.send_message.call_args_list)
    assert all("🏠 Дім <назва>" in call.args[1] for call in bot.send_message.call_args_list)
    deliveries = list(db_session.scalars(select(PowerNotificationDelivery)))
    assert len(deliveries) == 2 and all(d.status == "sent" and d.sent_at for d in deliveries)


async def test_one_channel_failure_does_not_stop_others_or_retry_ambiguous_delivery(
    sessions: MagicMock,
    db_session: Session,
    outage: PowerStateChanged,
) -> None:
    bot = AsyncMock(spec=Bot)
    bot.send_message.side_effect = [
        TelegramNetworkError(
            method=SendMessage(chat_id=1, text=""), message="secret upstream details"
        ),
        None,
    ]
    handler = PowerNotificationHandler(sessions, bot)
    await handler.handle(outage)
    await handler.handle(outage)
    assert bot.send_message.await_count == 2
    deliveries = list(
        db_session.scalars(
            select(PowerNotificationDelivery).order_by(PowerNotificationDelivery.telegram_chat_id)
        )
    )
    assert {d.status for d in deliveries} == {"sent", "uncertain"}


async def test_forbidden_channel_is_recorded_without_blocking_other_channels(
    sessions: MagicMock,
    db_session: Session,
    outage: PowerStateChanged,
) -> None:
    bot = AsyncMock(spec=Bot)
    bot.send_message.side_effect = [
        TelegramForbiddenError(method=SendMessage(chat_id=1, text=""), message="Forbidden"),
        None,
    ]
    await PowerNotificationHandler(sessions, bot).handle(outage)
    assert bot.send_message.await_count == 2
    assert {d.status for d in db_session.scalars(select(PowerNotificationDelivery))} == {
        "sent",
        "failed",
    }


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (PowerState.ON, PowerState.ON),
        (PowerState.OFF, PowerState.OFF),
        (PowerState.UNKNOWN, PowerState.ON),
        (PowerState.ON, PowerState.UNKNOWN),
        (PowerState.UNKNOWN, PowerState.OFF),
    ],
)
async def test_unknown_and_unchanged_events_do_not_send(
    sessions: MagicMock,
    old: PowerState,
    new: PowerState,
) -> None:
    bot = AsyncMock(spec=Bot)
    await PowerNotificationHandler(sessions, bot).handle(
        PowerStateChanged(
            user_id=1, device_id=1, previous_state=old, new_state=new, detected_at=NOW
        )
    )
    bot.send_message.assert_not_awaited()
    sessions.begin.assert_not_called()


async def test_unpersisted_or_wrong_owner_event_cannot_send(
    sessions: MagicMock, outage: PowerStateChanged
) -> None:
    bot = AsyncMock(spec=Bot)
    handler = PowerNotificationHandler(sessions, bot)
    await handler.handle(
        PowerStateChanged(
            user_id=2,
            device_id=1,
            previous_state=PowerState.ON,
            new_state=PowerState.OFF,
            detected_at=NOW,
        )
    )
    await handler.handle(
        PowerStateChanged(
            user_id=1,
            device_id=1,
            previous_state=PowerState.ON,
            new_state=PowerState.OFF,
            detected_at=NOW + timedelta(minutes=1),
        )
    )
    bot.send_message.assert_not_awaited()


async def test_rate_limit_retries_only_known_rejection() -> None:
    bot = AsyncMock(spec=Bot)
    bot.send_message.side_effect = [
        TelegramRetryAfter(
            method=SendMessage(chat_id=1, text=""), message="Slow down", retry_after=2
        ),
        None,
    ]
    with patch("app.bot.transport.asyncio.sleep") as sleep:
        await send_power_message(bot, 1, "Текст")
    sleep.assert_awaited_once_with(2)
    assert bot.send_message.await_count == 2


async def test_concurrent_duplicate_subscribers_claim_each_chat_once(
    sessions: MagicMock,
    outage: PowerStateChanged,
) -> None:
    import asyncio

    bot = AsyncMock(spec=Bot)
    started, release = asyncio.Event(), asyncio.Event()

    async def send(chat_id: int, text: str, **options: object) -> None:
        if chat_id == -(2**40):
            started.set()
            await release.wait()

    bot.send_message.side_effect = send
    first = asyncio.create_task(PowerNotificationHandler(sessions, bot).handle(outage))
    try:
        async with asyncio.timeout(1):
            await started.wait()
            await PowerNotificationHandler(sessions, bot).handle(outage)
        assert bot.send_message.await_count == 2
    finally:
        release.set()
        async with asyncio.timeout(1):
            await first
    assert bot.send_message.await_count == 2


async def test_interrupted_send_remains_reserved_but_other_channels_can_deliver_on_replay(
    sessions: MagicMock,
    outage: PowerStateChanged,
    db_session: Session,
) -> None:
    import asyncio

    bot = AsyncMock(spec=Bot)
    bot.send_message.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await PowerNotificationHandler(sessions, bot).handle(outage)
    assert len(list(db_session.scalars(select(PowerNotificationDelivery)))) == 1
    bot.send_message.side_effect = None
    await PowerNotificationHandler(sessions, bot).handle(outage)
    assert bot.send_message.await_count == 2
    assert bot.send_message.call_args.args[0] == -1234567890123
    db_session.expire_all()
    assert {row.status for row in db_session.scalars(select(PowerNotificationDelivery))} == {
        "claimed",
        "sent",
    }


@pytest.mark.parametrize("deleted", [False, True])
async def test_disabled_or_deleted_device_has_no_notifications(
    sessions: MagicMock,
    outage: PowerStateChanged,
    db_session: Session,
    deleted: bool,
) -> None:
    device = db_session.get(Device, 1)
    assert device
    if deleted:
        device.deleted_at = NOW
    else:
        device.enabled = False
    db_session.commit()
    bot = AsyncMock(spec=Bot)
    await PowerNotificationHandler(sessions, bot).handle(outage)
    bot.send_message.assert_not_awaited()


async def test_event_subscription_uses_history_persisted_by_previous_subscriber(
    sessions: MagicMock,
    db_session: Session,
) -> None:
    from app.analytics.history import HistoryHandler
    from app.events.bus import EventBus

    device = db_session.get(Device, 1)
    assert device
    device.current_power_state = PowerState.ON
    db_session.add_all(
        [
            PowerInterval(device_id=1, state=PowerState.ON, started_at=NOW - timedelta(hours=2)),
            DeviceNotificationChannel(user_id=1, device_id=1, channel_id=1),
        ]
    )
    db_session.commit()
    bot = AsyncMock(spec=Bot)
    bus = EventBus()
    bus.subscribe(PowerStateChanged, HistoryHandler(sessions).handle)
    bus.subscribe(PowerStateChanged, PowerNotificationHandler(sessions, bot).handle)
    await bus.publish(
        PowerStateChanged(
            user_id=1,
            device_id=1,
            previous_state=PowerState.ON,
            new_state=PowerState.OFF,
            detected_at=NOW,
        )
    )
    bot.send_message.assert_awaited_once()
    assert bot.send_message.call_args.args[1].endswith("Світло було:\n2 години")


@pytest.fixture(autouse=True)
def verified_destination(monkeypatch: pytest.MonkeyPatch) -> None:
    # Access checks have dedicated transport tests; these tests isolate delivery semantics.
    monkeypatch.setattr("app.notifications.telegram.require_destination", AsyncMock())


async def test_channel_revoked_before_reservation_is_not_sent(
    sessions: MagicMock, db_session: Session
) -> None:
    from app.repositories.channels import NotificationChannelRepository

    start = NOW - timedelta(hours=1)
    db_session.add_all(
        [
            PowerInterval(id=101, device_id=1, state=PowerState.ON, started_at=start, ended_at=NOW),
            PowerInterval(id=102, device_id=1, state=PowerState.OFF, started_at=NOW),
            DeviceNotificationChannel(user_id=1, device_id=1, channel_id=1),
        ]
    )
    db_session.commit()
    channel = db_session.get(NotificationChannel, 1)
    assert channel is not None
    bot = AsyncMock(spec=Bot)
    with patch.object(NotificationChannelRepository, "for_device", side_effect=[[channel], []]):
        await PowerNotificationHandler(sessions, bot).handle(
            PowerStateChanged(
                user_id=1,
                device_id=1,
                previous_state=PowerState.ON,
                new_state=PowerState.OFF,
                detected_at=NOW,
            )
        )
    bot.send_message.assert_not_awaited()
    assert not db_session.scalars(select(PowerNotificationDelivery)).all()
