from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

MENU_ITEMS = (
    ("💡 Стан", "status"),
    ("📅 Графік відключень", "schedules"),
    ("📊 Статистика", "statistics"),
    ("⚙️ Пристрої", "devices"),
    ("🔔 Канали", "channels"),
    ("📍 Групи відключень", "queues"),
    ("⚙️ Налаштування", "settings"),
)


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=f"menu:{action}")]
            for label, action in MENU_ITEMS
        ]
    )
