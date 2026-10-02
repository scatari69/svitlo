from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from app.notifications.schedule_formatting import diff_messages
from app.notifications.schedules import ScheduleNotificationHandler


def create_router() -> Router:
    router = Router(name="schedule_notifications")
    router.callback_query.register(show_diff, F.data.startswith("schedule_diff:"))
    return router


async def show_diff(
    callback: CallbackQuery, schedule_notifications: ScheduleNotificationHandler
) -> None:
    if not isinstance(callback.message, Message):
        await callback.answer("Повідомлення недоступне.", show_alert=True)
        return
    try:
        version_id = int((callback.data or "").removeprefix("schedule_diff:"))
        if not 0 < version_id < 2**63:
            raise ValueError("Invalid version ID")
    except ValueError:
        await callback.answer("Некоректний запит.", show_alert=True)
        return
    text = await schedule_notifications.diff_for_chat(version_id, callback.message.chat.id)
    if text is None:
        await callback.answer("Зміни цього графіка недоступні.", show_alert=True)
        return
    await callback.answer()
    for message in diff_messages(text):
        await callback.message.answer(message, parse_mode=None)
