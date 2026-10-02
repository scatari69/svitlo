from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import SendMessage
from aiogram.types import CallbackQuery, Chat, InlineKeyboardMarkup, Message, User

from app.bot.app import create_dispatcher
from app.bot.handlers.reports import ReportSetup, action, enter_time, open_settings
from app.bot.keyboards.reports import ReportAction, channels, details
from app.models.enums import MonitoringType, PowerState
from app.repositories.channels import ChannelView
from app.services.devices import DeviceManagementService, DeviceView
from app.services.reports import ReportKind, ReportSettingsService, ReportSettingsView


@pytest.fixture
def fsm() -> FSMContext:
    return FSMContext(MemoryStorage(), StorageKey(bot_id=123456, chat_id=1, user_id=1))


@pytest.fixture
def devices() -> AsyncMock:
    service = AsyncMock(spec=DeviceManagementService)
    service.list_devices.return_value = [
        DeviceView(1, "Дім", MonitoringType.PING, PowerState.ON, True, None, None)
    ]
    return service


@pytest.fixture
def service() -> AsyncMock:
    service = AsyncMock(spec=ReportSettingsService)
    service.channels.return_value = [
        ChannelView(10, "Родина", True),
        ChannelView(11, "Група", False),
    ]
    service.settings.return_value = ReportSettingsView(
        1, 10, "Дім", "Родина", True, True, True, False, 540, 600, 540, 0
    )
    return service


def message(text: str = "") -> Message:
    bot = Bot("123456:TEST_TOKEN", session=AsyncMock(spec=BaseSession))
    return Message(
        message_id=1,
        date=datetime.now(UTC),
        text=text,
        chat=Chat(id=1, type="private"),
        from_user=User(id=1, is_bot=False, first_name="User"),
    ).as_(bot)


def query() -> CallbackQuery:
    incoming = message()
    return CallbackQuery(
        id="query",
        from_user=User(id=1, is_bot=False, first_name="User"),
        chat_instance="chat",
        message=incoming,
    ).as_(incoming.bot)


def messages(incoming: CallbackQuery | Message) -> list[SendMessage]:
    assert incoming.bot and isinstance(incoming.bot.session, AsyncMock)
    return [
        call.args[1]
        for call in incoming.bot.session.call_args_list
        if isinstance(call.args[1], SendMessage)
    ]


async def click(
    action_name: str,
    fsm: FSMContext,
    devices: AsyncMock,
    service: AsyncMock,
    *,
    channel_id: int = 10,
    kind: str = "",
    value: int = 0,
) -> CallbackQuery:
    callback = query()
    await action(
        callback,
        ReportAction(
            action=action_name, device_id=1, channel_id=channel_id, kind=kind, value=value
        ),
        fsm,
        devices,
        service,
    )
    return callback


async def test_settings_entry_device_channel_selection_and_pair_details(
    fsm: FSMContext,
    devices: AsyncMock,
    service: AsyncMock,
) -> None:
    await fsm.set_state("other_setup")
    callback = query()
    await open_settings(callback, fsm)
    assert await fsm.get_state() is None
    entry_keyboard = messages(callback)[-1].reply_markup
    assert isinstance(entry_keyboard, InlineKeyboardMarkup)
    assert entry_keyboard.inline_keyboard[0][0].text == "📊 Автоматичні звіти"
    await click("devices", fsm, devices, service)
    devices.list_devices.assert_awaited_once_with(1)
    callback = await click("channels", fsm, devices, service)
    service.channels.assert_awaited_once_with(1, 1)
    assert "підключено до сповіщень пристрою" in messages(callback)[-1].text
    callback = await click("details", fsm, devices, service)
    service.settings.assert_awaited_with(1, 1, 10, kind=None, enabled=None, weekday=None)
    text = messages(callback)[-1].text
    assert "Щоденний: ✅" in text and "Щотижневий: ✅" in text and "Щомісячний: ❌" in text
    assert "Час: Київ" in text


@pytest.mark.parametrize("kind", list(ReportKind))
async def test_enable_callbacks_encode_explicit_state_for_each_kind(
    fsm: FSMContext,
    devices: AsyncMock,
    service: AsyncMock,
    kind: ReportKind,
) -> None:
    await click("enable", fsm, devices, service, kind=kind.value, value=1)
    await click("enable", fsm, devices, service, kind=kind.value, value=1)
    service.settings.assert_awaited_with(1, 1, 10, kind=kind, enabled=True, weekday=None)
    await click("enable", fsm, devices, service, kind=kind.value, value=0)
    service.settings.assert_awaited_with(1, 1, 10, kind=kind, enabled=False, weekday=None)


async def test_time_fsm_valid_invalid_input_and_cancel(
    fsm: FSMContext,
    devices: AsyncMock,
    service: AsyncMock,
) -> None:
    callback = await click("time", fsm, devices, service, kind="daily")
    assert await fsm.get_state() == ReportSetup.time.state
    assert "ГГ:ХХ" in messages(callback)[-1].text
    service.settings.side_effect = ValueError("secret")
    incoming = message("25:00")
    await enter_time(incoming, fsm, service)
    assert "❌ Введіть коректний час" in messages(incoming)[-1].text
    assert "secret" not in messages(incoming)[-1].text
    assert await fsm.get_state() == ReportSetup.time.state
    service.settings.side_effect = None
    await enter_time(message("07:45"), fsm, service)
    service.settings.assert_awaited_with(1, 1, 10, kind=ReportKind.DAILY, local_time="07:45")
    assert await fsm.get_state() is None and await fsm.get_data() == {}
    await click("time", fsm, devices, service, kind="weekly")
    await click("details", fsm, devices, service)
    assert await fsm.get_state() is None


async def test_weekday_selection_and_database_errors_are_ukrainian(
    fsm: FSMContext,
    devices: AsyncMock,
    service: AsyncMock,
) -> None:
    callback = await click("weekday", fsm, devices, service)
    sent = messages(callback)[-1]
    assert isinstance(sent.reply_markup, InlineKeyboardMarkup)
    assert [row[0].text for row in sent.reply_markup.inline_keyboard[:7]] == [
        "Понеділок",
        "Вівторок",
        "Середа",
        "Четвер",
        "П’ятниця",
        "Субота",
        "Неділя",
    ]
    await click("set_day", fsm, devices, service, value=6)
    service.settings.assert_awaited_with(1, 1, 10, kind=ReportKind.WEEKLY, enabled=None, weekday=6)
    service.settings.side_effect = RuntimeError("secret database error")
    callback = await click("details", fsm, devices, service)
    assert messages(callback)[-1].text.startswith("❌ Не вдалося")
    assert "secret" not in messages(callback)[-1].text


async def test_foreign_target_and_group_chat_cannot_change_settings(
    fsm: FSMContext,
    devices: AsyncMock,
    service: AsyncMock,
) -> None:
    service.settings.side_effect = LookupError("secret")
    callback = await click("details", fsm, devices, service)
    assert messages(callback)[-1].text == "❌ Пристрій, канал або налаштування недоступні."
    service.settings.reset_mock()
    callback = query()
    assert isinstance(callback.message, Message)
    callback = callback.model_copy(
        update={"message": callback.message.model_copy(update={"chat": Chat(id=-1, type="group")})}
    ).as_(callback.bot)
    await action(
        callback,
        ReportAction(action="enable", device_id=1, channel_id=10, kind="daily", value=1),
        fsm,
        devices,
        service,
    )
    service.settings.assert_not_awaited()


def test_channel_pagination_and_buttons_stay_within_callback_limit(service: AsyncMock) -> None:
    markup = channels(service.channels.return_value * 12, 1, 1)
    buttons = [button for row in markup.inline_keyboard for button in row]
    assert len(buttons) == 12
    assert all(
        button.callback_data and len(button.callback_data.encode()) <= 64 for button in buttons
    )
    assert "Група ⏸" in {button.text for button in buttons}
    data = ReportAction.unpack(buttons[0].callback_data or "")
    assert data.device_id == 1 and data.channel_id == 10
    controls = details(service.settings.return_value)
    flags = [
        ReportAction.unpack(row[0].callback_data or "")
        for row in controls.inline_keyboard
        if ReportAction.unpack(row[0].callback_data or "").action == "enable"
    ]
    assert [(flag.kind, flag.value) for flag in flags] == [
        ("daily", 0),
        ("weekly", 0),
        ("monthly", 1),
    ]


async def test_dispatcher_registers_report_services(devices: AsyncMock, service: AsyncMock) -> None:
    dispatcher = create_dispatcher(device_service=devices, report_service=service)
    try:
        assert dispatcher["report_service"] is service
        assert "reports" in {router.name for router in dispatcher.sub_routers}
    finally:
        await dispatcher.storage.close()
