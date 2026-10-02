from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.models.enums import MonitoringType
from app.services.devices import DeviceView


class DeviceAction(CallbackData, prefix="dev"):
    action: str
    device_id: int = 0


class SetupMethod(CallbackData, prefix="dev_method"):
    method: MonitoringType


def button(text: str, action: str, device_id: int = 0) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=text, callback_data=DeviceAction(action=action, device_id=device_id).pack()
    )


def device_list(devices: list[DeviceView]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            *[[button(device.name, "details", device.id)] for device in devices],
            [button("➕ Додати пристрій", "add")],
            [button("⬅️ Назад", "main")],
        ]
    )


def methods() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=SetupMethod(method=method).pack())]
            for label, method in [
                ("🏠 Home Assistant", MonitoringType.HOME_ASSISTANT),
                ("🌐 Ping", MonitoringType.PING),
                ("📡 SNMP", MonitoringType.SNMP),
            ]
        ]
        + [[button("⬅️ Назад", "list"), button("❌ Скасувати", "cancel")]]
    )


def navigation(back: str, device_id: int = 0) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                button("⬅️ Назад", back, device_id),
                button("❌ Скасувати", "cancel", device_id),
            ]
        ]
    )


def review(tested: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [button("🧪 Перевірити підключення", "test_setup")],
            *([[button("💾 Зберегти", "save")]] if tested else []),
            [button("⬅️ Назад", "field_back"), button("❌ Скасувати", "cancel")],
        ]
    )


def details(device_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [button("⚙️ Налаштувати", "configure", device_id)],
            [
                button("🔔 Канали", "channels", device_id),
                button("🧪 Перевірити", "check", device_id),
            ],
            [button("🗑 Видалити", "delete", device_id)],
            [button("⬅️ Назад", "list")],
        ]
    )


def configure(device_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [button("✏️ Змінити назву", "rename", device_id)],
            [button("🔌 Налаштувати підключення", "connection", device_id)],
            [button("⬅️ Назад", "details", device_id)],
        ]
    )


def confirmation(device_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [button("🗑 Так, видалити", "confirm_delete", device_id)],
            [button("⬅️ Назад", "details", device_id), button("❌ Скасувати", "cancel", device_id)],
        ]
    )
