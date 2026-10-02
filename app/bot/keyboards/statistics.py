from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.analytics.service import AnalyticsPeriod
from app.services.devices import DeviceView

PAGE_SIZE = 8
PERIODS = (
    ("📅 Сьогодні", AnalyticsPeriod.TODAY),
    ("↩️ Вчора", AnalyticsPeriod.YESTERDAY),
    ("7️⃣ 7 днів", AnalyticsPeriod.LAST_7_DAYS),
    ("🗓 Тиждень", AnalyticsPeriod.CALENDAR_WEEK),
    ("📆 Місяць", AnalyticsPeriod.CURRENT_MONTH),
)


class StatisticsAction(CallbackData, prefix="stats"):
    action: str
    device_id: int = 0
    period: str = ""
    page: int = 0


def button(
    text: str, action: str, device_id: int = 0, period: str = "", page: int = 0
) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=text[:60],
        callback_data=StatisticsAction(
            action=action, device_id=device_id, period=period, page=page
        ).pack(),
    )


def device_list(devices: list[DeviceView], page: int) -> InlineKeyboardMarkup:
    rows = [
        [button(device.name, "device", device.id)]
        for device in devices[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
    ]
    navigation = []
    if page > 0:
        navigation.append(button("⬅️ Попередні", "devices", page=page - 1))
    if (page + 1) * PAGE_SIZE < len(devices):
        navigation.append(button("Наступні ➡️", "devices", page=page + 1))
    if navigation:
        rows.append(navigation)
    rows.append(
        [InlineKeyboardButton(text="📊 Автоматичні звіти", callback_data="rpt:devices:0:0::0")]
    )
    rows.append([button("⬅️ Назад", "main"), button("❌ Скасувати", "main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def periods(device_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [button(label, "report", device_id, period.value)] for label, period in PERIODS
        ]
        + [[button("⬅️ Назад", "devices"), button("❌ Скасувати", "main")]]
    )


def report_navigation(device_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                button("⬅️ Назад", "device" if device_id else "devices", device_id),
                button("❌ Скасувати", "main"),
            ]
        ]
    )
