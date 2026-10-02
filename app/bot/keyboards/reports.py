from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.repositories.channels import ChannelView
from app.services.devices import DeviceView
from app.services.reports import WEEKDAYS, ReportKind, ReportSettingsView

PAGE_SIZE = 8


class ReportAction(CallbackData, prefix="rpt"):
    action: str
    device_id: int = 0
    channel_id: int = 0
    kind: str = ""
    value: int = 0


def button(
    text: str, action: str, device_id: int = 0, channel_id: int = 0, kind: str = "", value: int = 0
) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=text[:60],
        callback_data=ReportAction(
            action=action, device_id=device_id, channel_id=channel_id, kind=kind, value=value
        ).pack(),
    )


def devices(items: list[DeviceView], page: int) -> InlineKeyboardMarkup:
    rows = [
        [button(item.name, "channels", item.id)]
        for item in items[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
    ]
    rows.extend(pages(len(items), page, "devices"))
    rows.append([button("⬅️ Назад", "main"), button("❌ Скасувати", "main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def pages(
    count: int, page: int, action: str, device_id: int = 0
) -> list[list[InlineKeyboardButton]]:
    buttons = []
    if page > 0:
        buttons.append(button("⬅️ Попередні", action, device_id, value=page - 1))
    if (page + 1) * PAGE_SIZE < count:
        buttons.append(button("Наступні ➡️", action, device_id, value=page + 1))
    return [buttons] if buttons else []


def channels(items: list[ChannelView], device_id: int, page: int) -> InlineKeyboardMarkup:
    rows = [
        [button(item.name + (" ⏸" if not item.enabled else ""), "details", device_id, item.id)]
        for item in items[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
    ]
    rows.extend(pages(len(items), page, "channels", device_id))
    rows.append([button("⬅️ Назад", "devices"), button("❌ Скасувати", "main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def details(view: ReportSettingsView) -> InlineKeyboardMarkup:
    rows = []
    for kind, label in [
        (ReportKind.DAILY, "Щоденний"),
        (ReportKind.WEEKLY, "Щотижневий"),
        (ReportKind.MONTHLY, "Щомісячний"),
    ]:
        enabled = getattr(view, f"{kind}_enabled")
        rows.append(
            [
                button(
                    f"{'⏸ Вимкнути' if enabled else '▶️ Увімкнути'} {label.lower()}",
                    "enable",
                    view.device_id,
                    view.channel_id,
                    kind.value,
                    int(not enabled),
                )
            ]
        )
        rows.append(
            [button(f"🕒 {label}: час", "time", view.device_id, view.channel_id, kind.value)]
        )
    rows.append([button("🗓 День тижня", "weekday", view.device_id, view.channel_id)])
    rows.extend(navigation(view.device_id, view.channel_id).inline_keyboard)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def navigation(
    device_id: int, channel_id: int = 0, *, editing: bool = False
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                button("⬅️ Назад", "details" if editing else "channels", device_id, channel_id),
                button("❌ Скасувати", "details" if editing else "main", device_id, channel_id),
            ]
        ]
    )


def weekdays(device_id: int, channel_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [button(label, "set_day", device_id, channel_id, "weekly", index)]
            for index, label in enumerate(WEEKDAYS)
        ]
        + navigation(device_id, channel_id, editing=True).inline_keyboard
    )
