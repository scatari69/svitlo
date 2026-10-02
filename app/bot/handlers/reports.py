import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app.bot.keyboards import reports as keyboard
from app.bot.keyboards.main import main_menu
from app.services.devices import DeviceManagementService
from app.services.reports import ReportKind, ReportSettingsService, settings_text

logger = logging.getLogger(__name__)


class ReportSetup(StatesGroup):
    time = State()


def create_router() -> Router:
    router = Router(name="reports")
    router.callback_query.register(open_settings, F.data == "menu:settings")
    router.callback_query.register(action, keyboard.ReportAction.filter())
    router.message.register(enter_time, ReportSetup.time)
    return router


async def render(query: CallbackQuery, text: str, markup: InlineKeyboardMarkup) -> None:
    if isinstance(query.message, Message):
        await query.message.answer(text, reply_markup=markup, parse_mode=None)


async def open_settings(query: CallbackQuery, state: FSMContext) -> None:
    if not isinstance(query.message, Message) or query.message.chat.type != "private":
        await query.answer("Відкрийте налаштування в особистому чаті з ботом.", show_alert=True)
        return
    await query.answer()
    await state.clear()
    await render(
        query,
        "⚙️ Налаштування",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [keyboard.button("📊 Автоматичні звіти", "devices")],
                [keyboard.button("⬅️ Назад", "main")],
            ]
        ),
    )


async def action(
    query: CallbackQuery,
    callback_data: keyboard.ReportAction,
    state: FSMContext,
    device_service: DeviceManagementService,
    report_service: ReportSettingsService,
) -> None:
    if not isinstance(query.message, Message) or query.message.chat.type != "private":
        await query.answer("Відкрийте звіти в особистому чаті з ботом.", show_alert=True)
        return
    await query.answer()
    await state.clear()
    data = callback_data
    try:
        if data.action == "main":
            await render(query, "Головне меню", main_menu())
        elif data.action == "devices":
            items = await device_service.list_devices(query.from_user.id)
            page = max(0, min(data.value, max(0, (len(items) - 1) // keyboard.PAGE_SIZE)))
            await render(
                query,
                "📊 Автоматичні звіти\n\n"
                + ("Оберіть пристрій:" if items else "У вас ще немає пристроїв."),
                keyboard.devices(items, page),
            )
        elif data.action == "channels":
            items_channels = await report_service.channels(query.from_user.id, data.device_id)
            page = max(0, min(data.value, max(0, (len(items_channels) - 1) // keyboard.PAGE_SIZE)))
            await render(
                query,
                "Оберіть канал для звітів.\n\n"
                "Канал буде підключено до сповіщень пристрою та ввімкнених звітів.",
                keyboard.channels(items_channels, data.device_id, page),
            )
        else:
            enabled = None
            weekday = None
            kind = ReportKind(data.kind) if data.kind else None
            if data.action == "enable":
                if data.value not in (0, 1):
                    raise ValueError("Invalid enabled state")
                enabled = bool(data.value)
            elif data.action == "set_day":
                weekday = data.value
                kind = ReportKind.WEEKLY
            elif data.action not in {"details", "time", "weekday"}:
                raise ValueError("Invalid report action")
            view = await report_service.settings(
                query.from_user.id,
                data.device_id,
                data.channel_id,
                kind=kind,
                enabled=enabled,
                weekday=weekday,
            )
            if data.action == "time":
                if kind is None:
                    raise ValueError("Missing report kind")
                await state.set_data(
                    {"device_id": data.device_id, "channel_id": data.channel_id, "kind": kind.value}
                )
                await state.set_state(ReportSetup.time)
                await render(
                    query,
                    "Введіть час надсилання за Києвом у форматі ГГ:ХХ (наприклад, 09:00):",
                    keyboard.navigation(data.device_id, data.channel_id, editing=True),
                )
            elif data.action == "weekday":
                await render(
                    query,
                    "Оберіть день надсилання щотижневого звіту:",
                    keyboard.weekdays(data.device_id, data.channel_id),
                )
            else:
                await render(query, settings_text(view), keyboard.details(view))
    except (LookupError, ValueError):
        await render(
            query, "❌ Пристрій, канал або налаштування недоступні.", keyboard.devices([], 0)
        )
    except Exception:
        logger.warning("Report settings interaction unavailable")
        await render(
            query, "❌ Не вдалося змінити налаштування. Спробуйте пізніше.", keyboard.devices([], 0)
        )


async def enter_time(
    message: Message, state: FSMContext, report_service: ReportSettingsService
) -> None:
    if message.from_user is None or message.chat.type != "private":
        return
    saved = await state.get_data()
    try:
        view = await report_service.settings(
            message.from_user.id,
            saved["device_id"],
            saved["channel_id"],
            kind=ReportKind(saved["kind"]),
            local_time=message.text or "",
        )
        await state.clear()
        await message.answer(
            "✅ Час збережено.\n\n" + settings_text(view),
            reply_markup=keyboard.details(view),
            parse_mode=None,
        )
    except ValueError:
        await message.answer(
            "❌ Введіть коректний час у форматі ГГ:ХХ, від 00:00 до 23:59.",
            reply_markup=keyboard.navigation(saved["device_id"], saved["channel_id"], editing=True),
        )
    except Exception:
        logger.warning("Report time interaction unavailable")
        await state.clear()
        await message.answer(
            "❌ Не вдалося зберегти час. Відкрийте налаштування звітів знову.",
            reply_markup=keyboard.devices([], 0),
        )
