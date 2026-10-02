import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app.analytics.reports import format_statistics
from app.analytics.service import AnalyticsPeriod, AnalyticsService
from app.bot.keyboards import statistics as keyboard
from app.bot.keyboards.main import main_menu
from app.services.devices import DeviceManagementService

logger = logging.getLogger(__name__)


def create_router() -> Router:
    router = Router(name="statistics")
    router.callback_query.register(open_menu, F.data == "menu:statistics")
    router.callback_query.register(action, keyboard.StatisticsAction.filter())
    return router


async def render(query: CallbackQuery, text: str, markup: InlineKeyboardMarkup) -> None:
    if isinstance(query.message, Message):
        await query.message.answer(text, reply_markup=markup, parse_mode=None)


async def open_menu(
    query: CallbackQuery,
    state: FSMContext,
    device_service: DeviceManagementService,
    analytics_service: AnalyticsService,
) -> None:
    await action(
        query, keyboard.StatisticsAction(action="devices"), state, device_service, analytics_service
    )


async def action(
    query: CallbackQuery,
    callback_data: keyboard.StatisticsAction,
    state: FSMContext,
    device_service: DeviceManagementService,
    analytics_service: AnalyticsService,
) -> None:
    if not isinstance(query.message, Message) or query.message.chat.type != "private":
        await query.answer("Відкрийте статистику в особистому чаті з ботом.", show_alert=True)
        return
    await query.answer()
    await state.clear()
    data = callback_data
    try:
        if data.action == "main":
            await render(query, "Головне меню", main_menu())
        elif data.action == "devices":
            devices = await device_service.list_devices(query.from_user.id)
            page = max(0, min(data.page, max(0, (len(devices) - 1) // keyboard.PAGE_SIZE)))
            text = "📊 Статистика\n\n" + (
                "Оберіть пристрій:"
                if devices
                else "У вас ще немає пристроїв. Додайте пристрій у меню «⚙️ Пристрої»."
            )
            await render(query, text, keyboard.device_list(devices, page))
        elif data.action in {"device", "report"}:
            if not 0 < data.device_id < 2**63:
                raise ValueError("Invalid device ID")
            device = await device_service.get_device(query.from_user.id, data.device_id)
            if data.action == "device":
                await render(
                    query,
                    f"📊 Статистика\n\n🏠 {device.name}\n\nОберіть період:",
                    keyboard.periods(device.id),
                )
            else:
                period = AnalyticsPeriod(data.period)
                if period not in {period for _, period in keyboard.PERIODS}:
                    raise ValueError("Unsupported menu period")
                statistics = await analytics_service.get_for_telegram(
                    query.from_user.id, device.id, period
                )
                await render(
                    query,
                    format_statistics(statistics, device.name),
                    keyboard.report_navigation(device.id),
                )
        else:
            raise ValueError("Unknown statistics action")
    except (LookupError, ValueError):
        await render(
            query,
            "❌ Пристрій або період недоступні. Оберіть пристрій знову.",
            keyboard.report_navigation(0),
        )
    except Exception:
        logger.warning("Statistics interaction unavailable")
        await render(
            query,
            "❌ Не вдалося завантажити статистику. Спробуйте пізніше.",
            keyboard.report_navigation(data.device_id)
            if data.device_id > 0
            else keyboard.report_navigation(0),
        )
