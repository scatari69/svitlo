from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.keyboards.main import MENU_ITEMS, main_menu

WELCOME_MESSAGE = (
    "Вітаю! Я допоможу стежити за наявністю електроенергії, "
    "отримувати графіки відключень та вести статистику."
)


def create_router() -> Router:
    router = Router(name="start")
    router.message.register(start, CommandStart())
    router.callback_query.register(
        menu_placeholder,
        F.data.in_(
            {
                f"menu:{action}"
                for _, action in MENU_ITEMS
                if action not in {"devices", "queues", "statistics", "settings", "channels"}
            }
        ),
    )
    return router


async def start(message: Message, state: FSMContext | None = None) -> None:
    if state is not None:
        await state.clear()
    await message.answer(WELCOME_MESSAGE, reply_markup=main_menu())


async def menu_placeholder(callback: CallbackQuery) -> None:
    await callback.answer("Ця можливість ще в розробці.", show_alert=True)
