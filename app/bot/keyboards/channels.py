from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


class ChannelAction(CallbackData, prefix="chn"):
    action: str
    channel_id: int = 0
    device_id: int = 0
    nonce: str = ""
    page: int = 0


def button(text: str, action: str, **values: int | str) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=text, callback_data=ChannelAction(action=action, **values).pack()
    )


def markup(*rows: list[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=list(rows))


def navigation() -> list[InlineKeyboardButton]:
    return [button("⬅️ Назад", "list"), button("❌ Скасувати", "list")]
