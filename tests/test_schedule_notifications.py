import asyncio
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import SendMessage
from aiogram.types import CallbackQuery, Chat, Message, User
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.bot.app import create_dispatcher
from app.bot.handlers.schedule_notifications import show_diff
from app.events.bus import EventBus
from app.events.models import ScheduleChanged
from app.models import (
    NotificationChannel,
    ScheduleNotificationChannel,
    ScheduleNotificationDelivery,
    ScheduleSubscription,
    ScheduleVersion,
)
from app.models.enums import ChannelType
from app.notifications.schedule_formatting import (
    diff_messages,
    format_schedule_diff,
    format_schedule_notification,
    interval_label,
)
from app.notifications.schedules import ScheduleNotificationHandler
from app.schedules.diff import schedule_diff
from app.schedules.models import DaySchedule, Freshness, ProviderResult, Region, ScheduleState
from app.schedules.normalizer import normalize_half_hours
from app.schedules.service import ScheduleService

DAY = date(2026, 10, 2)
NOW = datetime(2026, 10, 2, 12, tzinfo=UTC)


def schedule(*periods: tuple[int, int, ScheduleState], day: date = DAY) -> DaySchedule:
    values = {f"{n // 2:02}:{(n % 2) * 30:02}": ScheduleState.ON for n in range(48)}
    for start, end, state in periods:
        for n in range(start, end):
            values[f"{n // 2:02}:{(n % 2) * 30:02}"] = state
    return normalize_half_hours("test", "kyiv", "1.2", day, values)


def test_message_matches_example_and_midnight() -> None:
    day = schedule(
        (12, 19, ScheduleState.OFF), (28, 36, ScheduleState.OFF), (45, 48, ScheduleState.OFF)
    )
    # The example's three ranges total nine hours, rather than ten.
    assert format_schedule_notification(day, "Київська область", now=NOW) == (
        "⚠️ Графік відключень змінено\n\n📍 Київська область\nГрупа 1.2\n\n"
        "Сьогодні, 2 жовтня\n\n🔻 06:00 — 09:30\n🔻 14:00 — 18:00\n"
        "🔻 22:30 — 24:00\n\nЗагалом без світла:\n9 годин"
    )


@pytest.mark.parametrize(("old", "new"), [(34, 36), (36, 34)])
def test_expanded_and_shortened_intervals(old: int, new: int) -> None:
    before = schedule((28, old, ScheduleState.OFF), (6, 8, ScheduleState.OFF))
    after = schedule((28, new, ScheduleState.OFF), (6, 8, ScheduleState.OFF))
    text = format_schedule_diff(before, after)
    assert f"Було:\n14:00–{old // 2}:00" in text
    assert f"Стало:\n14:00–{new // 2}:00" in text
    assert "03:00" not in text  # Unchanged intervals are excluded.


@pytest.mark.parametrize("removed", [False, True])
def test_added_and_removed_outages(removed: bool) -> None:
    on, off = schedule(), schedule((28, 34, ScheduleState.OFF))
    before, after = (off, on) if removed else (on, off)
    text = format_schedule_diff(before, after)
    assert (
        "Було:\n14:00–17:00\n\nСтало:\nНемає" if removed else "Було:\nНемає\n\nСтало:\n14:00–17:00"
    ) in text


def test_unknown_diff_and_total_are_separate_from_outages() -> None:
    unknown = schedule((28, 34, ScheduleState.UNKNOWN))
    text = format_schedule_notification(unknown, "Київська область", now=NOW)
    assert "Загалом без світла:\n0 хвилин" in text
    assert "⚪ Невідомі періоди:\n14:00–17:00" in text
    assert "⚪ Невідомі періоди" in format_schedule_diff(schedule(), unknown)
    assert "🔻 Відключення" not in format_schedule_diff(schedule(), unknown)
    restored = format_schedule_diff(unknown, schedule((28, 34, ScheduleState.OFF)))
    assert "⚪ Невідомі періоди" in restored and "🔻 Відключення" in restored


def test_semantic_equivalence_ignores_adjacent_slot_boundaries() -> None:
    original = schedule((28, 36, ScheduleState.OFF))
    parts = []
    for slot in original.slots:
        midpoint = slot.start + slot.duration / 2
        parts.extend(
            [slot.model_copy(update={"end": midpoint}), slot.model_copy(update={"start": midpoint})]
        )
    split = original.model_copy(update={"slots": tuple(parts)})
    assert schedule_diff(original, split) == ()
    assert format_schedule_diff(original, split) == "Графік не змінився."


def test_first_version_and_emergency_change() -> None:
    original = schedule()
    assert "перша збережена версія" in format_schedule_diff(None, original)
    emergency = original.model_copy(update={"emergency": True})
    assert "Увімкнено" in format_schedule_diff(original, emergency)
    assert "екстрені відключення" in format_schedule_notification(emergency, "Регіон", now=NOW)


@pytest.mark.parametrize(("day", "hours"), [(date(2026, 3, 29), 23), (date(2026, 10, 25), 25)])
def test_dst_elapsed_total(day: date, hours: int) -> None:
    full = schedule((0, 48, ScheduleState.OFF), day=day)
    assert ("23 години" if hours == 23 else "25 годин") in format_schedule_notification(
        full, "Регіон", now=NOW
    )
    assert interval_label(full.slots[0], full) == "00:00–24:00"


def test_kyiv_today_boundary_and_long_diff_chunks() -> None:
    text = format_schedule_notification(
        schedule(), "Регіон", now=datetime(2026, 10, 1, 21, 30, tzinfo=UTC)
    )
    assert "Сьогодні, 2 жовтня" in text
    long = "\n".join("14:00–17:00" for _ in range(1000))
    messages = diff_messages(long)
    assert len(messages) > 1 and all(len(message) <= 3900 for message in messages)
    assert "\n".join(messages) == long


@pytest.fixture
def change(db_session: Session) -> ScheduleChanged:
    for version_id, data in enumerate(
        [schedule((28, 34, ScheduleState.OFF)), schedule((28, 36, ScheduleState.OFF))], start=1
    ):
        db_session.add(
            ScheduleVersion(
                id=version_id,
                provider="test",
                region="kyiv",
                queue="1.2",
                schedule_date=DAY,
                content_hash=str(version_id) * 64,
                normalized_content=data.model_dump(mode="json"),
                fetched_at=NOW + timedelta(seconds=version_id),
            )
        )
    db_session.add_all(
        [
            NotificationChannel(
                id=3,
                user_id=1,
                telegram_chat_id=-1001234567890,
                name="Channel",
                channel_type=ChannelType.CHANNEL,
            ),
            NotificationChannel(
                id=4,
                user_id=1,
                telegram_chat_id=-1001234567891,
                name="Disabled",
                channel_type=ChannelType.CHANNEL,
                enabled=False,
            ),
            ScheduleSubscription(
                id=2,
                user_id=1,
                provider="test",
                region="kyiv",
                queue="1.2",
                name="Duplicate source",
            ),
            ScheduleSubscription(
                id=3,
                user_id=2,
                provider="test",
                region="kyiv",
                queue="1.2",
                name="Disabled subscription",
                enabled=False,
            ),
            ScheduleSubscription(
                id=4, user_id=2, provider="test", region="other", queue="1.2", name="Other region"
            ),
        ]
    )
    db_session.flush()
    for subscription_id, channel_id, owner in [
        (1, 1, 1),
        (1, 3, 1),
        (1, 4, 1),
        (2, 1, 1),
        (3, 2, 2),
        (4, 2, 2),
    ]:
        db_session.add(
            ScheduleNotificationChannel(
                subscription_id=subscription_id, channel_id=channel_id, user_id=owner
            )
        )
    db_session.commit()
    return ScheduleChanged(
        provider="test",
        region="kyiv",
        queue="1.2",
        schedule_date=DAY,
        previous_version_id=1,
        new_version_id=2,
        detected_at=NOW,
    )


@pytest.fixture
def catalog() -> AsyncMock:
    service = AsyncMock(spec=ScheduleService)
    service.get_regions.return_value = ProviderResult(
        (Region(provider="test", id="kyiv", name="Київська область"),), Freshness.CACHED
    )
    return service


async def test_event_consumer_channels_replay_new_uuid_restart_and_button(
    sessions: MagicMock,
    db_session: Session,
    change: ScheduleChanged,
    catalog: AsyncMock,
) -> None:
    bot = AsyncMock(spec=Bot)
    handler = ScheduleNotificationHandler(sessions, bot, catalog)
    bus = EventBus()
    bus.subscribe(ScheduleChanged, handler.handle)
    await bus.publish(change)
    await bus.publish(change)
    await ScheduleNotificationHandler(sessions, bot, catalog).handle(
        replace(change, event_id=uuid4())
    )
    assert bot.send_message.await_count == 2
    assert {call.args[0] for call in bot.send_message.call_args_list} == {-(2**40), -1001234567890}
    assert all("📍 Київська область" in call.args[1] for call in bot.send_message.call_args_list)
    keyboard = bot.send_message.call_args.kwargs["reply_markup"]
    assert keyboard.inline_keyboard[0][0].text == "🔎 Що змінилося?"
    assert keyboard.inline_keyboard[0][0].callback_data == "schedule_diff:2"
    records = list(db_session.scalars(select(ScheduleNotificationDelivery)))
    assert len(records) == 2 and all(row.status == "sent" and row.sent_at for row in records)
    assert all(row.previous_version_id == 1 for row in records)
    assert "Стало:\n14:00–18:00" in (await handler.diff_for_chat(2, -(2**40)) or "")
    assert await handler.diff_for_chat(2, 2**40) is None


async def test_failure_isolated_and_uncertain_send_not_retried(
    sessions: MagicMock,
    db_session: Session,
    change: ScheduleChanged,
    catalog: AsyncMock,
) -> None:
    bot = AsyncMock(spec=Bot)
    bot.send_message.side_effect = [
        TelegramNetworkError(
            method=SendMessage(chat_id=1, text=""), message="secret upstream details"
        ),
        None,
    ]
    handler = ScheduleNotificationHandler(sessions, bot, catalog)
    await handler.handle(change)
    await handler.handle(change)
    assert bot.send_message.await_count == 2
    assert {row.status for row in db_session.scalars(select(ScheduleNotificationDelivery))} == {
        "sent",
        "uncertain",
    }


@pytest.mark.parametrize("case", ["missing", "source", "old_source", "malformed", "same", "time"])
async def test_invalid_and_unchanged_versions_cannot_notify(
    sessions: MagicMock,
    db_session: Session,
    change: ScheduleChanged,
    catalog: AsyncMock,
    case: str,
) -> None:
    old, current = db_session.get(ScheduleVersion, 1), db_session.get(ScheduleVersion, 2)
    assert old and current
    if case == "missing":
        change = replace(change, previous_version_id=99)
    elif case == "source":
        change = replace(change, region="other")
    elif case == "old_source":
        old.queue = "2.1"
    elif case == "malformed":
        current.normalized_content = {"slots": []}
    elif case == "same":
        current.normalized_content = old.normalized_content
    else:
        old.fetched_at = current.fetched_at + timedelta(seconds=1)
    db_session.commit()
    bot = AsyncMock(spec=Bot)
    await ScheduleNotificationHandler(sessions, bot, catalog).handle(change)
    bot.send_message.assert_not_awaited()
    assert list(db_session.scalars(select(ScheduleNotificationDelivery))) == []


async def test_provider_failure_uses_safe_fallback_and_does_not_block_delivery(
    sessions: MagicMock,
    change: ScheduleChanged,
    catalog: AsyncMock,
) -> None:
    catalog.get_regions.return_value = ProviderResult(None, Freshness.UNAVAILABLE)
    bot = AsyncMock(spec=Bot)
    await ScheduleNotificationHandler(sessions, bot, catalog).handle(change)
    assert bot.send_message.await_count == 2
    assert "Регіон недоступний" in bot.send_message.call_args.args[1]
    catalog.get_regions.assert_awaited_once_with("test")


async def test_concurrent_duplicate_delivery_reserved_before_send(
    sessions: MagicMock,
    change: ScheduleChanged,
    catalog: AsyncMock,
) -> None:
    bot = AsyncMock(spec=Bot)
    started, release = asyncio.Event(), asyncio.Event()

    async def send(chat_id: int, text: str, **options: object) -> None:
        started.set()
        await release.wait()

    bot.send_message.side_effect = send
    first = asyncio.create_task(ScheduleNotificationHandler(sessions, bot, catalog).handle(change))
    try:
        async with asyncio.timeout(1):
            await started.wait()
            second = asyncio.create_task(
                ScheduleNotificationHandler(sessions, bot, catalog).handle(change)
            )
            await asyncio.sleep(0)
            release.set()
            await asyncio.gather(first, second)
    finally:
        release.set()
        await first
    assert bot.send_message.await_count == 2


async def test_delivery_retains_versions_after_subscription_delete(
    sessions: MagicMock,
    db_session: Session,
    change: ScheduleChanged,
    catalog: AsyncMock,
) -> None:
    handler = ScheduleNotificationHandler(sessions, AsyncMock(spec=Bot), catalog)
    await handler.handle(change)
    db_session.execute(delete(ScheduleSubscription).where(ScheduleSubscription.id.in_([1, 2])))
    db_session.commit()
    with pytest.raises(IntegrityError):
        db_session.execute(delete(ScheduleVersion).where(ScheduleVersion.id == 1))
    db_session.rollback()
    assert "Було:\n14:00–17:00" in (await handler.diff_for_chat(2, -(2**40)) or "")


async def test_diff_callback_chat_authorization_and_dispatcher_registration() -> None:
    service = AsyncMock(spec=ScheduleNotificationHandler)
    service.diff_for_chat.return_value = "Було:\n14:00–17:00\n\nСтало:\n14:00–18:00"
    callback = CallbackQuery(
        id="query",
        from_user=User(id=42, is_bot=False, first_name="User"),
        chat_instance="instance",
        data="schedule_diff:2",
        message=Message(message_id=1, date=NOW, chat=Chat(id=-123, type="supergroup")),
    )
    with pytest.MonkeyPatch.context() as monkeypatch:
        answer, message_answer = AsyncMock(), AsyncMock()
        monkeypatch.setattr(CallbackQuery, "answer", answer)
        monkeypatch.setattr(Message, "answer", message_answer)
        await show_diff(callback, service)
        service.diff_for_chat.assert_awaited_once_with(2, -123)
        message_answer.assert_awaited_once_with(service.diff_for_chat.return_value, parse_mode=None)
        service.diff_for_chat.return_value = None
        message_answer.reset_mock()
        await show_diff(callback, service)
        message_answer.assert_not_awaited()
        assert answer.call_args.args == ("Зміни цього графіка недоступні.",)
        service.diff_for_chat.reset_mock()
        await show_diff(
            callback.model_copy(update={"data": "schedule_diff:999999999999999999999"}), service
        )
        service.diff_for_chat.assert_not_awaited()
    dispatcher = create_dispatcher(schedule_notifications=service)
    assert dispatcher["schedule_notifications"] is service
    assert "schedule_notifications" in {router.name for router in dispatcher.sub_routers}


async def test_first_version_is_sent_without_a_fabricated_diff(
    sessions: MagicMock,
    change: ScheduleChanged,
    catalog: AsyncMock,
) -> None:
    handler = ScheduleNotificationHandler(sessions, AsyncMock(spec=Bot), catalog)
    await handler.handle(replace(change, previous_version_id=None))
    assert "перша збережена версія" in (await handler.diff_for_chat(2, -(2**40)) or "")


async def test_same_chat_owned_by_multiple_users_receives_one_message(
    sessions: MagicMock,
    db_session: Session,
    change: ScheduleChanged,
    catalog: AsyncMock,
) -> None:
    channel = db_session.get(NotificationChannel, 2)
    subscription = db_session.get(ScheduleSubscription, 3)
    assert channel and subscription
    channel.telegram_chat_id = -(2**40)
    subscription.enabled = True
    db_session.commit()
    bot = AsyncMock(spec=Bot)
    await ScheduleNotificationHandler(sessions, bot, catalog).handle(change)
    assert bot.send_message.await_count == 2


async def test_cancellation_keeps_reservation_and_replay_can_send_remaining_chat(
    sessions: MagicMock,
    db_session: Session,
    change: ScheduleChanged,
    catalog: AsyncMock,
) -> None:
    bot = AsyncMock(spec=Bot)
    bot.send_message.side_effect = asyncio.CancelledError
    handler = ScheduleNotificationHandler(sessions, bot, catalog)
    with pytest.raises(asyncio.CancelledError):
        await handler.handle(change)
    bot.send_message.side_effect = None
    await handler.handle(change)
    assert bot.send_message.await_count == 2
    assert {row.status for row in db_session.scalars(select(ScheduleNotificationDelivery))} == {
        "claimed",
        "sent",
    }


def test_split_and_merged_outages_are_interval_level() -> None:
    full = schedule((28, 36, ScheduleState.OFF))
    split = schedule((28, 31, ScheduleState.OFF), (32, 36, ScheduleState.OFF))
    text = format_schedule_diff(full, split)
    assert "Було:\n14:00–18:00" in text
    assert "Стало:\n14:00–15:30\n16:00–18:00" in text
    assert "Було:\n14:00–15:30\n16:00–18:00" in format_schedule_diff(split, full)


def test_unrelated_day_comparison_is_rejected() -> None:
    with pytest.raises(ValueError, match="unrelated"):
        schedule_diff(schedule(), schedule(day=DAY + timedelta(days=1)))


@pytest.fixture(autouse=True)
def verified_destination(monkeypatch: pytest.MonkeyPatch) -> None:
    # Access checks have dedicated transport tests; these tests isolate delivery semantics.
    monkeypatch.setattr("app.notifications.telegram.require_destination", AsyncMock())
