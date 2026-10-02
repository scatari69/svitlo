from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, User

from app.bot.handlers.devices import action, enter_value, select_method
from app.bot.keyboards.devices import DeviceAction, SetupMethod
from app.bot.states.devices import DeviceDelete, DeviceSetup
from app.models.enums import MonitorHealth, MonitoringType, PowerState
from app.monitoring.base import MonitorResult
from app.services.device_setup import DeviceSetupService
from app.services.devices import DeviceManagementService, DeviceView


@pytest.fixture
def fsm() -> FSMContext:
    return FSMContext(MemoryStorage(), StorageKey(bot_id=123456, chat_id=1, user_id=1))


@pytest.fixture
def service() -> AsyncMock:
    result = AsyncMock(spec=DeviceManagementService)
    result.setup = DeviceSetupService(None)
    result.setup.test = AsyncMock(
        return_value=MonitorResult(PowerState.ON, MonitorHealth.HEALTHY, datetime.now(UTC))
    )
    result.validate_name = DeviceManagementService.validate_name
    result.list_devices.return_value = []
    result.get_device.return_value = DeviceView(
        1, "Дім", MonitoringType.PING, PowerState.UNKNOWN, True, None, None
    )
    result.save_setup.return_value = result.get_device.return_value
    return result


def message(text: str = "") -> Message:
    bot = Bot("123456:TEST_TOKEN", session=AsyncMock(spec=BaseSession))
    return Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=1, type="private"),
        from_user=User(id=1, is_bot=False, first_name="Test"),
        text=text,
    ).as_(bot)


def callback() -> CallbackQuery:
    msg = message()
    return CallbackQuery(
        id="callback",
        from_user=msg.from_user or User(id=1, is_bot=False, first_name="Test"),
        chat_instance="chat",
        message=msg,
    ).as_(msg.bot)


async def click(name: str, fsm: FSMContext, service: AsyncMock, device_id: int = 0) -> None:
    await action(callback(), DeviceAction(action=name, device_id=device_id), fsm, service)


async def test_complete_ping_wizard_requires_test_before_save(
    fsm: FSMContext, service: AsyncMock
) -> None:
    await click("add", fsm, service)
    assert await fsm.get_state() == DeviceSetup.method.state
    await select_method(callback(), SetupMethod(method=MonitoringType.PING), fsm, service)
    assert await fsm.get_state() == DeviceSetup.name.state
    await enter_value(message("Дім"), fsm, service)
    assert await fsm.get_state() == DeviceSetup.field.state
    for value in ("localhost", "10", "2", "3", "2"):
        await enter_value(message(value), fsm, service)
    assert await fsm.get_state() == DeviceSetup.review.state
    await click("save", fsm, service)
    service.save_setup.assert_not_awaited()
    await click("test_setup", fsm, service)
    assert (await fsm.get_data())["tested"] is True
    await click("save", fsm, service)
    service.save_setup.assert_awaited_once()
    assert await fsm.get_state() is None
    await click("save", fsm, service)
    assert service.save_setup.await_count == 1


async def test_back_invalidates_test_and_cancel_clears_data(
    fsm: FSMContext, service: AsyncMock
) -> None:
    await fsm.set_state(DeviceSetup.review)
    await fsm.set_data(
        {
            "method": "ping",
            "name": "Дім",
            "index": 5,
            "tested": True,
            "values": {
                "host": "localhost",
                "interval_seconds": "10",
                "timeout_seconds": "2",
                "failure_threshold": "3",
                "recovery_threshold": "2",
            },
        }
    )
    await click("field_back", fsm, service)
    assert await fsm.get_state() == DeviceSetup.field.state
    assert (await fsm.get_data())["tested"] is False
    assert "recovery_threshold" not in (await fsm.get_data())["values"]
    await click("cancel", fsm, service)
    assert await fsm.get_state() is None and await fsm.get_data() == {}


async def test_invalid_input_does_not_advance(fsm: FSMContext, service: AsyncMock) -> None:
    await fsm.set_state(DeviceSetup.field)
    await fsm.set_data({"method": "ping", "index": 0, "values": {}})
    await enter_value(message("--help"), fsm, service)
    assert (await fsm.get_data())["index"] == 0
    assert await fsm.get_state() == DeviceSetup.field.state


async def test_delete_requires_matching_confirmation_and_prevents_replay(
    fsm: FSMContext, service: AsyncMock
) -> None:
    await click("confirm_delete", fsm, service, 1)
    service.delete.assert_not_awaited()
    await click("delete", fsm, service, 1)
    assert await fsm.get_state() == DeviceDelete.confirmation.state
    await click("confirm_delete", fsm, service, 2)
    service.delete.assert_not_awaited()
    await click("confirm_delete", fsm, service, 1)
    service.delete.assert_awaited_once_with(1, 1)
    await click("confirm_delete", fsm, service, 1)
    assert service.delete.await_count == 1


async def test_router_matches_any_wizard_state(fsm: FSMContext, service: AsyncMock) -> None:
    from app.bot.handlers.devices import create_router

    router = create_router()
    await fsm.set_state(DeviceSetup.name)
    await fsm.set_data({"method": "ping"})
    await router.propagate_event(
        "message",
        message("Дім"),
        state=fsm,
        device_service=service,
        raw_state=DeviceSetup.name.state,
    )
    assert await fsm.get_state() == DeviceSetup.field.state
    await router.propagate_event(
        "message",
        message("localhost"),
        state=fsm,
        device_service=service,
        raw_state=DeviceSetup.field.state,
    )
    assert (await fsm.get_data())["values"]["host"] == "localhost"


@pytest.mark.parametrize(
    ("method", "inputs"),
    [
        (MonitoringType.SNMP, ("localhost", "161", "secret-community", "10", "interface", "1")),
        (
            MonitoringType.SNMP,
            (
                "localhost",
                "161",
                "secret-community",
                "10",
                "custom_oid",
                "1.3.6.1.4.1.1",
                "yes",
                "no",
            ),
        ),
        (
            MonitoringType.HOME_ASSISTANT,
            ("api", "http://ha.local", "secret-token", "binary_sensor.power", "on", "off"),
        ),
    ],
)
async def test_snmp_and_ha_config_wizards_encrypt_secret_inputs(
    fsm: FSMContext,
    service: AsyncMock,
    method: MonitoringType,
    inputs: tuple[str, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.services.device_setup.encrypt_secret", lambda key, value: b"ciphertext"
    )
    await click("add", fsm, service)
    await select_method(callback(), SetupMethod(method=method), fsm, service)
    await enter_value(message("Дім"), fsm, service)
    for value in inputs:
        await enter_value(message(value), fsm, service)
    assert await fsm.get_state() == DeviceSetup.review.state
    assert "secret-community" not in str(await fsm.get_data())
    assert "secret-token" not in str(await fsm.get_data())
    await click("test_setup", fsm, service)
    await click("save", fsm, service)
    assert service.save_setup.call_args.args[2] == method
    assert await fsm.get_state() is None


async def test_failed_setup_test_keeps_save_disabled(fsm: FSMContext, service: AsyncMock) -> None:
    from unittest.mock import patch

    await fsm.set_state(DeviceSetup.review)
    await fsm.set_data({"method": "ping", "name": "Дім", "values": {}, "tested": True})
    with patch.object(
        service.setup,
        "test",
        return_value=MonitorResult(
            PowerState.UNKNOWN,
            MonitorHealth.UNAVAILABLE,
            datetime.now(UTC),
        ),
    ):
        await click("test_setup", fsm, service)
    assert (await fsm.get_data())["tested"] is False
    await click("save", fsm, service)
    service.save_setup.assert_not_awaited()


@pytest.mark.parametrize(
    ("healthy", "expected"),
    [
        (True, "✅ Пристрій відповідає."),
        (False, "❌ Не вдалося отримати відповідь від пристрою."),
    ],
)
async def test_ping_setup_connection_feedback_is_ukrainian(
    fsm: FSMContext,
    service: AsyncMock,
    healthy: bool,
    expected: str,
) -> None:
    from unittest.mock import patch

    from aiogram.methods import SendMessage

    await fsm.set_state(DeviceSetup.review)
    await fsm.set_data({"method": "ping", "name": "Дім", "values": {}})
    query = callback()
    with patch.object(
        service.setup,
        "test",
        return_value=MonitorResult(
            PowerState.ON if healthy else PowerState.UNKNOWN,
            MonitorHealth.HEALTHY if healthy else MonitorHealth.UNAVAILABLE,
            datetime.now(UTC),
        ),
    ):
        await action(query, DeviceAction(action="test_setup"), fsm, service)
    assert query.bot
    session = query.bot.session
    assert isinstance(session, AsyncMock)
    sent = [
        call.args[1] for call in session.call_args_list if isinstance(call.args[1], SendMessage)
    ]
    assert sent[-1].text == expected
    assert (await fsm.get_data())["tested"] is healthy


async def test_ping_validation_message_stays_ukrainian(fsm: FSMContext, service: AsyncMock) -> None:
    from aiogram.methods import SendMessage

    await fsm.set_state(DeviceSetup.field)
    await fsm.set_data({"method": "ping", "index": 0, "values": {}})
    incoming = message("host; rm -rf /")
    await enter_value(incoming, fsm, service)
    assert incoming.bot
    session = incoming.bot.session
    assert isinstance(session, AsyncMock)
    sent = session.call_args.args[1]
    assert isinstance(sent, SendMessage)
    assert sent.text == "❌ Некоректне значення. Перевірте формат і введіть його ще раз."
    assert (await fsm.get_data())["index"] == 0


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("timeout", "Перевірте адресу, мережу та спільноту SNMP."),
        ("access_denied", "Перевірте спільноту SNMP та дозволи пристрою."),
        ("invalid_oid", "OID або індекс інтерфейсу не знайдено."),
        ("unexpected_value", "Отримано невідоме значення."),
    ],
)
async def test_snmp_wizard_failure_feedback_and_save_guard(
    fsm: FSMContext, service: AsyncMock, reason: str, expected: str
) -> None:
    from unittest.mock import patch

    from aiogram.methods import SendMessage

    await fsm.set_state(DeviceSetup.review)
    await fsm.set_data({"method": "snmp", "name": "Дім", "values": {}, "tested": True})
    query = callback()
    with patch.object(
        service.setup,
        "test",
        return_value=MonitorResult(
            PowerState.UNKNOWN, MonitorHealth.UNAVAILABLE, datetime.now(UTC), {"reason": reason}
        ),
    ):
        await action(query, DeviceAction(action="test_setup"), fsm, service)
    assert query.bot
    session = query.bot.session
    assert isinstance(session, AsyncMock)
    sent = [
        call.args[1] for call in session.call_args_list if isinstance(call.args[1], SendMessage)
    ]
    assert expected in sent[-1].text
    assert sent[-1].reply_markup
    assert "🧪 Перевірити підключення" in str(sent[-1].reply_markup)
    assert (await fsm.get_data())["tested"] is False
    await click("save", fsm, service)
    service.save_setup.assert_not_awaited()


async def test_invalid_snmp_oid_does_not_advance(fsm: FSMContext, service: AsyncMock) -> None:
    from aiogram.methods import SendMessage

    await fsm.set_state(DeviceSetup.field)
    await fsm.set_data({"method": "snmp", "index": 5, "values": {"mode": "custom_oid"}})
    incoming = message("1.3.6; cat /etc/passwd")
    await enter_value(incoming, fsm, service)
    assert incoming.bot
    session = incoming.bot.session
    assert isinstance(session, AsyncMock)
    sent = session.call_args.args[1]
    assert isinstance(sent, SendMessage)
    assert sent.text == "❌ Некоректне значення. Перевірте формат і введіть його ще раз."
    assert (await fsm.get_data())["index"] == 5


async def test_webhook_wizard_shows_copyable_url_once_and_confirms_integration(
    fsm: FSMContext, service: AsyncMock
) -> None:
    from html import unescape

    from aiogram.methods import SendMessage

    from app.bot.handlers.devices import prompt_field

    url = "https://bot.example/api/v1/homeassistant/webhook/" + "a" * 43
    service.setup.prepare_webhook = AsyncMock(return_value=("b" * 64, url))
    await fsm.set_data(
        {"method": "home_assistant", "name": "Дім", "index": 1, "values": {"mode": "webhook"}}
    )
    incoming = message()
    await prompt_field(incoming, fsm, service)
    await prompt_field(incoming, fsm, service)
    assert incoming.bot
    session = incoming.bot.session
    assert isinstance(session, AsyncMock)
    sent = [
        call.args[1] for call in session.call_args_list if isinstance(call.args[1], SendMessage)
    ]
    text = unescape("\n".join(item.text for item in sent))
    assert text.count(url) == 1
    assert (
        "Не публікуйте це посилання — воно використовується для передачі стану вашого пристрою."
        in text
    )
    assert "rest_command:" in text and "automation:" in text
    assert "states('binary_sensor.power') in ['on', 'off']" in text
    assert 'content_type: "application/json"' in text
    assert sent[0].parse_mode == "HTML"
    assert not sent[0].protect_content
    assert url not in str(await fsm.get_data())
    service.setup.prepare_webhook.assert_awaited_once()
    await click("test_setup", fsm, service)
    query = callback()
    await action(query, DeviceAction(action="save"), fsm, service)
    assert query.bot
    session = query.bot.session
    assert isinstance(session, AsyncMock)
    sent = [
        call.args[1] for call in session.call_args_list if isinstance(call.args[1], SendMessage)
    ]
    assert sent[-1].text.startswith("✅ Інтеграцію створено.")
    assert url not in sent[-1].text
    assert await fsm.get_state() is None


async def test_device_menu_does_not_disclose_owned_devices_in_a_group(
    fsm: FSMContext, service: AsyncMock
) -> None:
    from app.bot.handlers.devices import open_menu

    query = callback()
    assert isinstance(query.message, Message)
    bot = query.bot
    assert bot is not None and isinstance(bot.session, AsyncMock)
    query = query.model_copy(
        update={
            "message": query.message.model_copy(update={"chat": Chat(id=-1, type="supergroup")})
        }
    ).as_(bot)
    await open_menu(query, fsm, service)
    service.list_devices.assert_not_awaited()
    assert bot.session.call_args.args[1].show_alert is True
