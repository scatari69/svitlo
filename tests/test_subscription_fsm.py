from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import SendMessage
from aiogram.types import CallbackQuery, Chat, Message, User

from app.bot.app import create_dispatcher
from app.bot.handlers.subscriptions import action, enter_name, open_menu
from app.bot.keyboards.subscriptions import SubscriptionAction, choices
from app.bot.states.subscriptions import SubscriptionSetup
from app.bot.subscription_text import list_text
from app.schedules.models import OutageGroup, Region
from app.services.subscriptions import (
    Catalog,
    CatalogUnavailable,
    ChannelView,
    SubscriptionManagementService,
    SubscriptionView,
)


@pytest.fixture
def fsm() -> FSMContext:
    return FSMContext(MemoryStorage(), StorageKey(bot_id=123456, chat_id=1, user_id=1))


@pytest.fixture
def service() -> AsyncMock:
    service = AsyncMock(spec=SubscriptionManagementService)
    service.regions.return_value = Catalog(
        (Region(provider="dynamic", id="region-from-api", name="Нова область"),)
    )
    service.groups.return_value = Catalog(
        (OutageGroup(provider="dynamic", region="region-from-api", id="7.3", name="Група 7.3"),)
    )
    item = SubscriptionView(1, "dynamic", "region-from-api", "Нова область", "7.3", "Дім", True)
    service.get_subscription.return_value = service.create_subscription.return_value = item
    service.list_subscriptions.return_value = [item]
    service.channels.return_value = [
        ChannelView(10, "Особисті повідомлення", True),
        ChannelView(11, "Родина", False),
    ]
    service.channel_ids.return_value = []
    return service


def message(text: str = "") -> Message:
    return Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=1, type="private"),
        from_user=User(id=1, is_bot=False, first_name="Test"),
        text=text,
    ).as_(Bot("123456:TEST_TOKEN", session=AsyncMock(spec=BaseSession)))


def query() -> CallbackQuery:
    incoming = message()
    return CallbackQuery(
        id="callback",
        from_user=User(id=1, is_bot=False, first_name="Test"),
        chat_instance="private",
        message=incoming,
    ).as_(incoming.bot)


async def click(
    action_name: str,
    fsm: FSMContext,
    service: AsyncMock,
    *,
    subscription_id: int = 0,
    index: int = 0,
    nonce: str | None = None,
) -> CallbackQuery:
    if nonce is None:
        nonce = (await fsm.get_data()).get("nonce", "")
    callback = query()
    await action(
        callback,
        SubscriptionAction(
            action=action_name, subscription_id=subscription_id, index=index, nonce=nonce
        ),
        fsm,
        service,
    )
    return callback


def sent(callback: CallbackQuery) -> list[SendMessage]:
    assert callback.bot
    session = callback.bot.session
    assert isinstance(session, AsyncMock)
    return [
        call.args[1] for call in session.call_args_list if isinstance(call.args[1], SendMessage)
    ]


async def test_complete_dynamic_subscription_wizard_and_duplicate_callbacks(
    fsm: FSMContext, service: AsyncMock
) -> None:
    await click("add", fsm, service)
    assert await fsm.get_state() == SubscriptionSetup.region.state
    await click("region", fsm, service)
    service.groups.assert_awaited_once_with("dynamic", "region-from-api")
    assert await fsm.get_state() == SubscriptionSetup.group.state
    await click("group", fsm, service)
    assert await fsm.get_state() == SubscriptionSetup.name.state
    await enter_name(message("Батьки"), fsm, service)
    assert await fsm.get_state() == SubscriptionSetup.channels.state
    await click("select", fsm, service, index=1)
    await click("select", fsm, service, index=1)
    assert (await fsm.get_data())["selected"] == [11]
    data = await fsm.get_data()
    callback = await click("save", fsm, service)
    service.create_subscription.assert_awaited_once_with(
        1, "dynamic", "region-from-api", "7.3", "Батьки", {11}, data["creation_key"]
    )
    assert sent(callback)[-1].text.startswith("✅ Підписку збережено.")
    assert await fsm.get_state() is None
    await click("save", fsm, service, nonce=data["nonce"])
    assert service.create_subscription.await_count == 1


async def test_back_cancel_and_invalid_name(fsm: FSMContext, service: AsyncMock) -> None:
    await click("add", fsm, service)
    await click("region", fsm, service)
    await click("back", fsm, service)
    assert await fsm.get_state() == SubscriptionSetup.region.state
    await click("region", fsm, service)
    await click("group", fsm, service)
    await enter_name(message("x" * 101), fsm, service)
    assert await fsm.get_state() == SubscriptionSetup.name.state
    service.channels.assert_not_awaited()
    await enter_name(message("Дім"), fsm, service)
    await click("back", fsm, service)
    assert await fsm.get_state() == SubscriptionSetup.name.state
    await click("back", fsm, service)
    assert await fsm.get_state() == SubscriptionSetup.group.state
    await click("cancel", fsm, service)
    assert await fsm.get_state() is None and await fsm.get_data() == {}


async def test_delete_requires_matching_confirmation_and_cannot_replay(
    fsm: FSMContext, service: AsyncMock
) -> None:
    await click("confirm_delete", fsm, service, subscription_id=1, nonce="forged")
    service.delete.assert_not_awaited()
    await click("delete", fsm, service, subscription_id=1)
    nonce = (await fsm.get_data())["nonce"]
    assert await fsm.get_state() == SubscriptionSetup.delete.state
    await click("confirm_delete", fsm, service, subscription_id=2, nonce=nonce)
    service.delete.assert_not_awaited()
    await click("confirm_delete", fsm, service, subscription_id=1, nonce=nonce)
    service.delete.assert_awaited_once_with(1, 1)
    await click("confirm_delete", fsm, service, subscription_id=1, nonce=nonce)
    assert service.delete.await_count == 1


async def test_existing_channels_rename_and_explicit_enabled_state(
    fsm: FSMContext, service: AsyncMock
) -> None:
    service.channel_ids.return_value = [10]
    await click("channels", fsm, service, subscription_id=1)
    assert (await fsm.get_data())["selected"] == [10]
    await click("unselect", fsm, service, subscription_id=1)
    await click("unselect", fsm, service, subscription_id=1)
    assert (await fsm.get_data())["selected"] == []
    await click("save", fsm, service, subscription_id=1)
    service.update_channels.assert_awaited_once_with(1, 1, set())
    await click("disable", fsm, service, subscription_id=1)
    service.set_enabled.assert_awaited_with(1, 1, False)
    await click("enable", fsm, service, subscription_id=1)
    service.set_enabled.assert_awaited_with(1, 1, True)
    await click("rename", fsm, service, subscription_id=1)
    await enter_name(message("Дача"), fsm, service)
    service.rename.assert_awaited_once_with(1, 1, "Дача")
    assert await fsm.get_state() is None


async def test_old_catalog_callback_cannot_select_new_wizard(
    fsm: FSMContext, service: AsyncMock
) -> None:
    await click("add", fsm, service)
    old_nonce = (await fsm.get_data())["nonce"]
    await click("add", fsm, service, nonce="")
    callback = await click("region", fsm, service, nonce=old_nonce)
    assert await fsm.get_state() == SubscriptionSetup.region.state
    service.groups.assert_not_awaited()
    assert sent(callback)[-1].text.startswith("Ця дія вже неактуальна.")


async def test_catalog_failure_and_private_chat_guard(fsm: FSMContext, service: AsyncMock) -> None:
    service.regions.side_effect = CatalogUnavailable
    callback = await click("add", fsm, service)
    assert sent(callback)[-1].text == "❌ Не вдалося завантажити каталог. Спробуйте пізніше."
    await click("back", fsm, service)
    assert await fsm.get_state() is None
    callback = query()
    assert isinstance(callback.message, Message)
    callback = callback.model_copy(
        update={"message": callback.message.model_copy(update={"chat": Chat(id=-1, type="group")})}
    ).as_(callback.bot)
    await action(callback, SubscriptionAction(action="delete", subscription_id=1), fsm, service)
    service.get_subscription.assert_not_awaited()


def test_paginated_callbacks_do_not_embed_long_provider_ids() -> None:
    keyboard = choices(["Область " + "я" * 100] * 30, "region", 1, "a" * 12)
    buttons = [button for row in keyboard.inline_keyboard for button in row]
    assert len(buttons) == 12
    assert all(
        button.callback_data and len(button.callback_data.encode()) <= 64 for button in buttons
    )
    assert SubscriptionAction.unpack(buttons[0].callback_data or "").index == 8


def test_grouped_ukrainian_display_includes_disabled_subscriptions() -> None:
    items = [
        SubscriptionView(1, "test", "kyiv", "Київська область", "1.2", "Дім", True),
        SubscriptionView(2, "test", "kyiv", "Київська область", "3.1", "Батьки", False),
    ]
    text = list_text(items)
    assert text.count("Київська область") == 1
    assert "🏠 Дім — група 1.2" in text
    assert "👪 Батьки — група 3.1 ⏸ Вимкнено" in text


async def test_main_menu_handler_uses_subscription_service(
    service: AsyncMock, fsm: FSMContext
) -> None:
    dispatcher: Dispatcher = create_dispatcher(subscription_service=service)
    callback = query().model_copy(update={"data": "menu:queues"})
    assert callback.bot
    try:
        assert dispatcher["subscription_service"] is service
        assert "subscriptions" in {router.name for router in dispatcher.sub_routers}
        await open_menu(callback, fsm, service)
        service.list_subscriptions.assert_awaited_once_with(1)
    finally:
        await dispatcher.storage.close()
        await callback.bot.session.close()
