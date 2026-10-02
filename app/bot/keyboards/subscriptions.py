from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.services.subscriptions import SubscriptionView

PAGE_SIZE = 8


class SubscriptionAction(CallbackData, prefix="sub"):
    action: str
    subscription_id: int = 0
    index: int = 0
    nonce: str = ""


def button(
    label: str, action: str, subscription_id: int = 0, index: int = 0, nonce: str = ""
) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=label[:60],
        callback_data=SubscriptionAction(
            action=action, subscription_id=subscription_id, index=index, nonce=nonce
        ).pack(),
    )


def navigation(subscription_id: int = 0, nonce: str = "") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                button("⬅️ Назад", "back", subscription_id, nonce=nonce),
                button("❌ Скасувати", "cancel", subscription_id, nonce=nonce),
            ]
        ]
    )


def choices(
    labels: list[str],
    action: str,
    page: int,
    nonce: str,
    subscription_id: int = 0,
    selected: set[int] | None = None,
) -> InlineKeyboardMarkup:
    rows = [
        [
            button(
                ("✅ " if selected and i in selected else "") + labels[i],
                ("unselect" if selected and i in selected else "select")
                if action == "channel"
                else action,
                subscription_id,
                i,
                nonce,
            )
        ]
        for i in range(page * PAGE_SIZE, min((page + 1) * PAGE_SIZE, len(labels)))
    ]
    pages = []
    if page > 0:
        pages.append(button("⬅️ Попередні", "page", subscription_id, page - 1, nonce))
    if (page + 1) * PAGE_SIZE < len(labels):
        pages.append(button("Наступні ➡️", "page", subscription_id, page + 1, nonce))
    if pages:
        rows.append(pages)
    if action == "channel":
        rows.append([button("💾 Зберегти", "save", subscription_id, nonce=nonce)])
        rows.append([button("🔄 Оновити канали", "refresh", subscription_id, nonce=nonce)])
    rows.extend(navigation(subscription_id, nonce).inline_keyboard)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def subscription_list(items: list[SubscriptionView], page: int) -> InlineKeyboardMarkup:
    rows = [
        [button(item.name + (" ⏸" if not item.enabled else ""), "details", item.id)]
        for item in items[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
    ]
    pages = []
    if page > 0:
        pages.append(button("⬅️ Попередні", "list", index=page - 1))
    if (page + 1) * PAGE_SIZE < len(items):
        pages.append(button("Наступні ➡️", "list", index=page + 1))
    if pages:
        rows.append(pages)
    rows.extend([[button("➕ Додати групу", "add")], [button("⬅️ Назад", "main")]])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def details(item: SubscriptionView) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                button("✏️ Змінити назву", "rename", item.id),
                button("🔔 Канали", "channels", item.id),
            ],
            [
                button(
                    "⏸ Вимкнути" if item.enabled else "▶️ Увімкнути",
                    "disable" if item.enabled else "enable",
                    item.id,
                )
            ],
            [button("🗑 Видалити", "delete", item.id)],
            [button("⬅️ Назад", "list")],
        ]
    )


def confirmation(subscription_id: int, nonce: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [button("🗑 Так, видалити", "confirm_delete", subscription_id, nonce=nonce)],
            [button("❌ Скасувати", "cancel", subscription_id, nonce=nonce)],
        ]
    )
