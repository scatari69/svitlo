from datetime import UTC, datetime
from unittest.mock import AsyncMock

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, SendMessage
from aiogram.types import CallbackQuery, Chat, InlineKeyboardMarkup, Message, User

from app.bot.app import create_dispatcher
from app.bot.handlers.start import WELCOME_MESSAGE, menu_placeholder, start


async def test_start_sends_ukrainian_welcome_and_menu() -> None:
    session = AsyncMock(spec=BaseSession)
    bot = Bot(token="123456:TEST_TOKEN", session=session)
    dispatcher = create_dispatcher()
    message = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=1, type="private"),
        text="/start",
        from_user=User(id=1, is_bot=False, first_name="Test"),
    ).as_(bot)
    try:
        await start(message)
        method = session.call_args.args[1]
        assert isinstance(method, SendMessage)
        assert method.text == WELCOME_MESSAGE
        keyboard = method.reply_markup
        assert isinstance(keyboard, InlineKeyboardMarkup)
        assert [button.text for row in keyboard.inline_keyboard for button in row] == [
            "💡 Стан",
            "📅 Графік відключень",
            "📊 Статистика",
            "⚙️ Пристрої",
            "🔔 Канали",
            "📍 Групи відключень",
            "⚙️ Налаштування",
        ]
        assert [button.callback_data for row in keyboard.inline_keyboard for button in row] == [
            "menu:status",
            "menu:schedules",
            "menu:statistics",
            "menu:devices",
            "menu:channels",
            "menu:queues",
            "menu:settings",
        ]
    finally:
        await dispatcher.storage.close()
        await bot.session.close()


async def test_menu_button_acknowledges_in_ukrainian() -> None:
    session = AsyncMock(spec=BaseSession)
    bot = Bot(token="123456:TEST_TOKEN", session=session)
    dispatcher = create_dispatcher()
    callback = CallbackQuery(
        id="query",
        from_user=User(id=1, is_bot=False, first_name="Test"),
        chat_instance="chat",
        data="menu:status",
    ).as_(bot)
    try:
        await menu_placeholder(callback)
        method = session.call_args.args[1]
        assert isinstance(method, AnswerCallbackQuery)
        assert method.text == "Ця можливість ще в розробці."
        assert method.show_alert is True
    finally:
        await dispatcher.storage.close()
        await bot.session.close()
