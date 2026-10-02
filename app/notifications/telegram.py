from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import SendMessage
from aiogram.types import BufferedInputFile, InlineKeyboardMarkup

from app.bot.transport import retry_telegram
from app.services.telegram_access import ChannelAccessDenied, can_deliver, inspect_chat


async def send_power_message(
    bot: Bot, chat_id: int, text: str, reply_markup: InlineKeyboardMarkup | None = None
) -> None:
    async def send() -> None:
        await require_destination(bot, chat_id)
        await bot.send_message(
            chat_id, text, parse_mode=None, request_timeout=10, reply_markup=reply_markup
        )

    await retry_telegram(send)


async def require_destination(bot: Bot, chat_id: int, *, photo: bool = False) -> None:
    try:
        chat = await inspect_chat(bot, chat_id)
        if chat.id == chat_id and await can_deliver(bot, chat, photo=photo):
            return
    except ChannelAccessDenied:
        pass
    raise TelegramForbiddenError(
        method=SendMessage(chat_id=chat_id, text=""), message="Destination unavailable"
    )


async def send_report_photo(bot: Bot, chat_id: int, image: bytes, caption: str) -> None:
    photo = BufferedInputFile(image, filename="power-report.png")

    async def send() -> None:
        await require_destination(bot, chat_id, photo=True)
        await bot.send_photo(
            chat_id, photo, caption=caption[:1024], parse_mode=None, request_timeout=10
        )

    await retry_telegram(send)
