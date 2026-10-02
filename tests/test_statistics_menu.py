from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery, SendMessage
from aiogram.types import CallbackQuery, Chat, InlineKeyboardMarkup, Message, User
from sqlalchemy.orm import Session

from app.analytics.reports import format_statistics
from app.analytics.service import (
    KYIV,
    AnalyticsPeriod,
    AnalyticsService,
    ReportingWindow,
    calculate_statistics,
)
from app.bot.app import create_dispatcher
from app.bot.handlers.statistics import action, open_menu
from app.bot.keyboards.statistics import PERIODS, StatisticsAction, device_list
from app.models import PowerInterval
from app.models.enums import MonitoringType, PowerState
from app.services.devices import DeviceManagementService, DeviceView

START = datetime(2026, 10, 2, tzinfo=KYIV)


@pytest.fixture
def device_service() -> AsyncMock:
    service = AsyncMock(spec=DeviceManagementService)
    device = DeviceView(7, "Дім <назва>", MonitoringType.PING, PowerState.ON, True, None, None)
    service.list_devices.return_value = [device]
    service.get_device.return_value = device
    return service


@pytest.fixture
def analytics() -> AsyncMock:
    service = AsyncMock(spec=AnalyticsService)
    service.get_for_telegram.return_value = calculate_statistics(
        [
            PowerInterval(
                device_id=7,
                state=PowerState.ON,
                started_at=START,
                ended_at=START + timedelta(hours=17, minutes=32),
            ),
            PowerInterval(
                device_id=7,
                state=PowerState.OFF,
                started_at=START + timedelta(hours=17, minutes=32),
                ended_at=None,
            ),
        ],
        ReportingWindow(START, START + timedelta(days=1)),
        now=START + timedelta(days=1),
    )
    return service


@pytest.fixture
def fsm() -> FSMContext:
    return FSMContext(MemoryStorage(), StorageKey(bot_id=123456, user_id=2**40, chat_id=2**40))


def query(chat_type: str = "private") -> CallbackQuery:
    bot = Bot("123456:TEST_TOKEN", session=AsyncMock(spec=BaseSession))
    message = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=2**40 if chat_type == "private" else -1, type=chat_type),
    ).as_(bot)
    return CallbackQuery(
        id="query",
        from_user=User(id=2**40, is_bot=False, first_name="User"),
        chat_instance="chat",
        message=message,
    ).as_(bot)


def messages(callback: CallbackQuery) -> list[SendMessage]:
    assert callback.bot
    session = callback.bot.session
    assert isinstance(session, AsyncMock)
    return [
        call.args[1] for call in session.call_args_list if isinstance(call.args[1], SendMessage)
    ]


async def test_menu_clears_setup_state_and_lists_owned_devices(
    fsm: FSMContext,
    device_service: AsyncMock,
    analytics: AsyncMock,
) -> None:
    await fsm.set_state("setup")
    await fsm.set_data({"secret": "setup data"})
    callback = query()
    await open_menu(callback, fsm, device_service, analytics)
    device_service.list_devices.assert_awaited_once_with(2**40)
    assert await fsm.get_state() is None and await fsm.get_data() == {}
    sent = messages(callback)[-1]
    assert sent.text == "📊 Статистика\n\nОберіть пристрій:"
    assert isinstance(sent.reply_markup, InlineKeyboardMarkup)
    assert sent.reply_markup.inline_keyboard[0][0].text == "Дім <назва>"
    assert (
        StatisticsAction.unpack(
            sent.reply_markup.inline_keyboard[0][0].callback_data or ""
        ).device_id
        == 7
    )
    analytics.get_for_telegram.assert_not_awaited()


async def test_select_device_shows_exact_period_buttons(
    fsm: FSMContext,
    device_service: AsyncMock,
    analytics: AsyncMock,
) -> None:
    callback = query()
    await action(
        callback, StatisticsAction(action="device", device_id=7), fsm, device_service, analytics
    )
    device_service.get_device.assert_awaited_once_with(2**40, 7)
    sent = messages(callback)[-1]
    assert "Оберіть період:" in sent.text
    assert isinstance(sent.reply_markup, InlineKeyboardMarkup)
    buttons = [button for row in sent.reply_markup.inline_keyboard for button in row]
    assert [button.text for button in buttons[:5]] == [
        "📅 Сьогодні",
        "↩️ Вчора",
        "7️⃣ 7 днів",
        "🗓 Тиждень",
        "📆 Місяць",
    ]
    assert all(
        StatisticsAction.unpack(button.callback_data or "").device_id == 7 for button in buttons[:5]
    )
    analytics.get_for_telegram.assert_not_awaited()


@pytest.mark.parametrize("period", [period for _, period in PERIODS])
async def test_each_period_uses_analytics_service_and_plain_text(
    fsm: FSMContext,
    device_service: AsyncMock,
    analytics: AsyncMock,
    period: AnalyticsPeriod,
) -> None:
    callback = query()
    await action(
        callback,
        StatisticsAction(action="report", device_id=7, period=period.value),
        fsm,
        device_service,
        analytics,
    )
    analytics.get_for_telegram.assert_awaited_once_with(2**40, 7, period)
    sent = messages(callback)[-1]
    assert sent.text.startswith("📊 Статистика за 2 жовтня")
    assert "🏠 Дім <назва>" in sent.text and sent.parse_mode is None
    assert "💡 Світло було:\n17 год 32 хв\n73%" in sent.text
    assert "🔴 Світла не було:\n6 год 28 хв\n27%" in sent.text
    assert "⚪ Немає даних:" not in sent.text
    assert isinstance(sent.reply_markup, InlineKeyboardMarkup)
    back = StatisticsAction.unpack(sent.reply_markup.inline_keyboard[0][0].callback_data or "")
    assert back.action == "device" and back.device_id == 7


async def test_unknown_time_is_separate_and_percentages_use_known_time(
    fsm: FSMContext,
    device_service: AsyncMock,
    analytics: AsyncMock,
) -> None:
    end = START + timedelta(hours=3, minutes=42)
    analytics.get_for_telegram.return_value = calculate_statistics(
        [
            PowerInterval(
                device_id=7,
                state=PowerState.ON,
                started_at=START,
                ended_at=START + timedelta(hours=2),
            ),
            PowerInterval(
                device_id=7,
                state=PowerState.OFF,
                started_at=START + timedelta(hours=2),
                ended_at=START + timedelta(hours=3),
            ),
        ],
        ReportingWindow(START, START + timedelta(days=1)),
        now=end,
    )
    callback = query()
    await action(
        callback,
        StatisticsAction(action="report", device_id=7, period="today"),
        fsm,
        device_service,
        analytics,
    )
    text = messages(callback)[-1].text
    assert "Світло було:\n2 год\n67%" in text
    assert "Світла не було:\n1 год\n33%" in text
    assert "⚪ Немає даних:\n42 хв" in text
    assert "Доступність за відомий час" in text


async def test_no_devices_and_back_cancel_navigation(
    fsm: FSMContext,
    device_service: AsyncMock,
    analytics: AsyncMock,
) -> None:
    device_service.list_devices.return_value = []
    callback = query()
    await open_menu(callback, fsm, device_service, analytics)
    assert "У вас ще немає пристроїв" in messages(callback)[-1].text
    await action(callback, StatisticsAction(action="main"), fsm, device_service, analytics)
    assert messages(callback)[-1].text == "Головне меню"
    assert isinstance(messages(callback)[-1].reply_markup, InlineKeyboardMarkup)
    await action(callback, StatisticsAction(action="devices"), fsm, device_service, analytics)
    assert device_service.list_devices.await_count == 2


@pytest.mark.parametrize("chat_type", ["group", "supergroup", "channel"])
async def test_statistics_cannot_expose_private_device_history_in_other_chats(
    fsm: FSMContext,
    device_service: AsyncMock,
    analytics: AsyncMock,
    chat_type: str,
) -> None:
    callback = query(chat_type)
    await open_menu(callback, fsm, device_service, analytics)
    assert not messages(callback)
    device_service.list_devices.assert_not_awaited()
    analytics.get_for_telegram.assert_not_awaited()
    assert callback.bot
    session = callback.bot.session
    assert isinstance(session, AsyncMock)
    answer = session.call_args.args[1]
    assert isinstance(answer, AnswerCallbackQuery)
    assert answer.text == "Відкрийте статистику в особистому чаті з ботом."


@pytest.mark.parametrize("failure", [LookupError("secret"), RuntimeError("secret")])
async def test_deleted_foreign_device_and_service_failure_get_ukrainian_errors(
    fsm: FSMContext,
    device_service: AsyncMock,
    analytics: AsyncMock,
    failure: Exception,
) -> None:
    device_service.get_device.side_effect = failure
    callback = query()
    await action(
        callback,
        StatisticsAction(action="report", device_id=99, period="today"),
        fsm,
        device_service,
        analytics,
    )
    sent = messages(callback)[-1]
    assert sent.text.startswith("❌ ") and "secret" not in sent.text
    analytics.get_for_telegram.assert_not_awaited()


@pytest.mark.parametrize(
    "data",
    [
        StatisticsAction(action="report", device_id=7, period="bad"),
        StatisticsAction(action="report", device_id=7, period="previous_month"),
        StatisticsAction(action="device", device_id=-1),
        StatisticsAction(action="device", device_id=2**63),
        StatisticsAction(action="bad"),
    ],
)
async def test_forged_callbacks_are_rejected(
    fsm: FSMContext,
    device_service: AsyncMock,
    analytics: AsyncMock,
    data: StatisticsAction,
) -> None:
    callback = query()
    await action(callback, data, fsm, device_service, analytics)
    assert "❌ Пристрій або період недоступні" in messages(callback)[-1].text
    analytics.get_for_telegram.assert_not_awaited()


def test_paginated_device_choices_respect_callback_limit(device_service: AsyncMock) -> None:
    device = device_service.get_device.return_value
    devices = [device] * 25
    keyboard = device_list(devices, 1)
    buttons = [button for row in keyboard.inline_keyboard for button in row]
    assert len(buttons) == 13
    assert all(
        button.callback_data and len(button.callback_data.encode()) <= 64 for button in buttons
    )
    assert "⬅️ Попередні" in {button.text for button in buttons}
    assert "Наступні ➡️" in {button.text for button in buttons}


async def test_dispatcher_registers_statistics_dependencies(
    device_service: AsyncMock,
    analytics: AsyncMock,
) -> None:
    dispatcher = create_dispatcher(device_service=device_service, analytics_service=analytics)
    try:
        assert dispatcher["analytics_service"] is analytics
        assert "statistics" in {router.name for router in dispatcher.sub_routers}
    finally:
        await dispatcher.storage.close()


async def test_real_telegram_id_mapping_and_ownership(
    sessions: MagicMock, db_session: Session
) -> None:
    db_session.add(PowerInterval(device_id=1, state=PowerState.ON, started_at=START, ended_at=None))
    db_session.commit()
    service = AnalyticsService(sessions)
    stats = await service.get_for_telegram(
        2**40, 1, AnalyticsPeriod.TODAY, now=START + timedelta(hours=12)
    )
    assert stats.on_duration == timedelta(hours=12)
    with pytest.raises(LookupError):
        await service.get_for_telegram(
            2**40 + 1, 1, AnalyticsPeriod.TODAY, now=START + timedelta(hours=12)
        )


def test_multi_day_title_does_not_include_next_midnight_date() -> None:
    window = ReportingWindow(START, START + timedelta(days=7))
    stats = calculate_statistics([], window, now=window.end)
    assert format_statistics(stats, "Дім").startswith("📊 Статистика за 2 жовтня — 8 жовтня")
    assert "Немає підтверджених даних." in format_statistics(stats, "Дім")
