import logging
from contextlib import suppress

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app.bot import device_text
from app.bot.homeassistant_text import webhook_setup_text
from app.bot.keyboards import devices as keyboard
from app.bot.keyboards.channels import button
from app.bot.keyboards.main import main_menu
from app.bot.states.devices import DeviceDelete, DeviceEdit, DeviceSetup
from app.models.enums import MonitorHealth, MonitoringType, PowerState
from app.services.devices import DeviceManagementService

logger = logging.getLogger(__name__)
ERROR = "❌ Не вдалося виконати дію. Перевірте введені дані та підключення і спробуйте ще раз."


def create_router() -> Router:
    router = Router(name="devices")
    router.callback_query.register(open_menu, F.data == "menu:devices")
    router.callback_query.register(select_method, keyboard.SetupMethod.filter(), DeviceSetup.method)
    router.callback_query.register(action, keyboard.DeviceAction.filter())
    router.message.register(
        enter_value, StateFilter(DeviceSetup.name, DeviceSetup.field, DeviceEdit.name)
    )
    return router


async def render(callback: CallbackQuery, text: str, markup: InlineKeyboardMarkup) -> None:
    if isinstance(callback.message, Message):
        with suppress(TelegramBadRequest):
            await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(text, reply_markup=markup)


async def show_list(callback: CallbackQuery, service: DeviceManagementService) -> None:
    devices = await service.list_devices(callback.from_user.id)
    await render(callback, device_text.list_text(devices), keyboard.device_list(devices))


async def open_menu(
    callback: CallbackQuery,
    state: FSMContext,
    device_service: DeviceManagementService,
) -> None:
    if not isinstance(callback.message, Message) or callback.message.chat.type != "private":
        await callback.answer(
            "Для керування пристроями відкрийте приватний чат із ботом.", show_alert=True
        )
        return
    await callback.answer()
    await state.clear()
    try:
        await show_list(callback, device_service)
    except Exception:
        logger.exception("Device list failed")
        await render(callback, ERROR, keyboard.navigation("main"))


async def prompt_field(
    message: Message,
    state: FSMContext,
    service: DeviceManagementService,
) -> None:
    data = await state.get_data()
    method = MonitoringType(data["method"])
    values = data.get("values", {})
    fields = service.setup.fields(method, values)
    index = data.get("index", 0)
    if index >= len(fields):
        await state.set_state(DeviceSetup.review)
        if (
            method == MonitoringType.HOME_ASSISTANT
            and values["mode"] == "webhook"
            and "webhook_token_hash" not in values
        ):
            digest, url = await service.setup.prepare_webhook()
            values["webhook_token_hash"] = digest
            await state.update_data(values=values)
            await message.answer(webhook_setup_text(url), parse_mode="HTML", protect_content=False)
        await message.answer(
            f"Пристрій «{data['name']}»\nСпосіб: {device_text.METHOD_NAMES[method]}\n\n"
            "Перевірте підключення перед збереженням.",
            reply_markup=keyboard.review(False),
        )
        return
    await state.set_state(DeviceSetup.field)
    field = fields[index]
    navigation = keyboard.navigation("field_back")
    if field.choices:
        navigation.inline_keyboard[:0] = [
            [keyboard.button(label, f"value_{value}")] for label, value in field.choices
        ]
    await message.answer(field.prompt, reply_markup=navigation)


async def select_method(
    callback: CallbackQuery,
    callback_data: keyboard.SetupMethod,
    state: FSMContext,
    device_service: DeviceManagementService,
) -> None:
    await callback.answer()
    await state.update_data(method=callback_data.method.value, values={}, index=0)
    await state.set_state(DeviceSetup.name)
    await render(callback, "Введіть назву пристрою:", keyboard.navigation("method"))


async def consume_field(
    message: Message,
    value: str,
    state: FSMContext,
    service: DeviceManagementService,
) -> None:
    data = await state.get_data()
    method = MonitoringType(data["method"])
    values = data.get("values", {})
    index = data["index"]
    field = service.setup.fields(method, values)[index]
    # Secrets leave the FSM only as ciphertext, and are never echoed in menus.
    if field.secret:
        with suppress(TelegramAPIError):
            await message.delete()
    values[field.key] = service.setup.parse(field, value)
    await state.update_data(values=values, index=index + 1, tested=False)
    await prompt_field(message, state, service)


async def enter_value(
    message: Message,
    state: FSMContext,
    device_service: DeviceManagementService,
) -> None:
    if message.from_user is None or message.chat.type != "private":
        return
    try:
        if not message.text:
            raise ValueError("Text required")
        current = await state.get_state()
        if current == DeviceEdit.name.state:
            data = await state.get_data()
            device = await device_service.rename(
                message.from_user.id, data["device_id"], message.text
            )
            await state.clear()
            await message.answer(
                device_text.details_text(device), reply_markup=keyboard.details(device.id)
            )
        elif current == DeviceSetup.name.state:
            name = device_service.validate_name(message.text)
            await state.update_data(name=name, values={}, index=0, tested=False)
            await prompt_field(message, state, device_service)
        else:
            await consume_field(message, message.text, state, device_service)
    except (ValueError, KeyError):
        await message.answer("❌ Некоректне значення. Перевірте формат і введіть його ще раз.")
    except Exception:
        logger.exception("Device setup input failed")
        await message.answer(ERROR, reply_markup=keyboard.navigation("list"))


async def action(
    callback: CallbackQuery,
    callback_data: keyboard.DeviceAction,
    state: FSMContext,
    device_service: DeviceManagementService,
) -> None:
    await callback.answer()
    if not isinstance(callback.message, Message):
        return
    if callback.message.chat.type != "private":
        await callback.message.answer("Для керування пристроями відкрийте приватний чат із ботом.")
        return
    try:
        await handle_action(callback, callback_data, state, device_service)
    except LookupError:
        await state.clear()
        await render(callback, "❌ Пристрій не знайдено.", keyboard.navigation("list"))
    except Exception:
        logger.exception("Device action failed")
        await render(callback, ERROR, keyboard.navigation("list"))


async def handle_action(
    callback: CallbackQuery,
    data: keyboard.DeviceAction,
    state: FSMContext,
    service: DeviceManagementService,
) -> None:
    message = callback.message
    if not isinstance(message, Message):
        return
    user_id, device_id = callback.from_user.id, data.device_id
    current = await state.get_state()
    saved = await state.get_data()
    if data.action in {"list", "cancel", "main"}:
        await state.clear()
        if data.action == "main":
            await render(callback, "Головне меню", main_menu())
        else:
            await show_list(callback, service)
    elif data.action in {"add", "method"}:
        await state.clear()
        await state.set_state(DeviceSetup.method)
        await render(callback, "Оберіть спосіб визначення наявності світла:", keyboard.methods())
    elif data.action == "connection":
        device = await service.get_device(user_id, device_id)
        await state.clear()
        await state.update_data(
            device_id=device_id,
            method=device.monitoring_type.value,
            name=device.name,
            values={},
            index=0,
        )
        await prompt_field(message, state, service)
    elif data.action == "field_back" and current in {
        DeviceSetup.field.state,
        DeviceSetup.review.state,
    }:
        index = max(0, saved["index"] - 1)
        if saved["index"] == 0:
            await state.set_state(DeviceSetup.name)
            await render(callback, "Введіть назву пристрою:", keyboard.navigation("list"))
        else:
            fields = service.setup.fields(MonitoringType(saved["method"]), saved.get("values", {}))
            values = {field.key: saved["values"][field.key] for field in fields[:index]}
            await state.update_data(index=index, values=values, tested=False)
            await prompt_field(message, state, service)
    elif data.action.startswith("value_") and current == DeviceSetup.field.state:
        field = service.setup.fields(MonitoringType(saved["method"]), saved["values"])[
            saved["index"]
        ]
        if not field.choices:
            raise ValueError("Field requires text input")
        await consume_field(message, data.action.removeprefix("value_"), state, service)
    elif data.action == "test_setup" and current == DeviceSetup.review.state:
        await render(callback, "⏳ Перевіряю підключення…", keyboard.navigation("field_back"))
        result = await service.setup.test(MonitoringType(saved["method"]), saved["values"])
        passed = result.health == MonitorHealth.HEALTHY and result.state != PowerState.UNKNOWN
        await state.update_data(tested=passed)
        if MonitoringType(saved["method"]) == MonitoringType.PING:
            await render(
                callback,
                "✅ Пристрій відповідає."
                if passed
                else "❌ Не вдалося отримати відповідь від пристрою.",
                keyboard.review(passed),
            )
            return
        if MonitoringType(saved["method"]) == MonitoringType.SNMP:
            await render(
                callback, device_text.snmp_connection_text(result), keyboard.review(passed)
            )
            return
        await render(
            callback,
            "✅ Підключення перевірено."
            if passed
            else (
                "❌ Не вдалося підтвердити стан. "
                "Перевірте адресу, налаштування та мережеве з’єднання."
            ),
            keyboard.review(passed),
        )
    elif data.action == "save" and current == DeviceSetup.review.state and saved.get("tested"):
        device = await service.save_setup(
            user_id,
            saved["name"],
            MonitoringType(saved["method"]),
            saved["values"],
            saved.get("device_id", 0),
        )
        await state.clear()
        await render(
            callback,
            (
                "✅ Інтеграцію створено."
                if saved["method"] == MonitoringType.HOME_ASSISTANT.value
                and saved["values"].get("mode") == "webhook"
                else "✅ Пристрій збережено."
            )
            + "\n\n"
            + device_text.details_text(device),
            keyboard.details(device.id),
        )
    elif (
        data.action == "confirm_delete"
        and current == DeviceDelete.confirmation.state
        and saved.get("device_id") == device_id
    ):
        await service.delete(user_id, device_id)
        await state.clear()
        await show_list(callback, service)
    elif data.action in {"details", "configure", "channels", "check", "delete", "rename"}:
        device = await service.get_device(user_id, device_id)
        await state.clear()
        if data.action == "details":
            await render(callback, device_text.details_text(device), keyboard.details(device_id))
        elif data.action == "configure":
            await render(callback, "⚙️ Налаштування пристрою", keyboard.configure(device_id))
        elif data.action == "rename":
            await state.set_state(DeviceEdit.name)
            await state.update_data(device_id=device_id)
            await render(
                callback, "Введіть нову назву пристрою:", keyboard.navigation("details", device_id)
            )
        elif data.action == "channels":
            names = await service.channel_names(user_id, device_id)
            await render(
                callback,
                "🔔 Канали сповіщень\n\n" + ("\n".join(names) or "Канали ще не призначено."),
                keyboard.navigation("details", device_id).model_copy(
                    update={
                        "inline_keyboard": [
                            [button("⚙️ Обрати канали", "device", device_id=device_id)],
                            *keyboard.navigation("details", device_id).inline_keyboard,
                        ]
                    }
                ),
            )
        elif data.action == "check":
            await render(
                callback, "⏳ Перевіряю підключення…", keyboard.navigation("details", device_id)
            )
            result = await service.check(user_id, device_id)
            text = (
                (
                    "✅ Стан за результатом перевірки:\n"
                    + device_text.STATE_NAMES[result.state]
                    + "\nЧас: "
                    + device_text.format_time(result.detected_at)
                )
                if result.health == MonitorHealth.HEALTHY
                else (
                    "❌ Не вдалося підтвердити підключення. "
                    "Перевірте налаштування та мережеве з’єднання."
                )
            )
            if device.monitoring_type == MonitoringType.PING:
                text = (
                    "✅ Пристрій відповідає."
                    if result.health == MonitorHealth.HEALTHY
                    else "❌ Не вдалося отримати відповідь від пристрою."
                )
            if device.monitoring_type == MonitoringType.SNMP:
                text = device_text.snmp_connection_text(result)
            await render(callback, text, keyboard.navigation("details", device_id))
        else:
            await state.set_state(DeviceDelete.confirmation)
            await state.update_data(device_id=device_id)
            await render(
                callback,
                f"Видалити пристрій «{device.name}»?\n\n"
                "Моніторинг буде зупинено. Історія залишиться збереженою.",
                keyboard.confirmation(device_id),
            )
    else:
        await render(
            callback,
            "Ця дія вже неактуальна. Відкрийте меню пристроїв знову.",
            keyboard.navigation("list"),
        )
