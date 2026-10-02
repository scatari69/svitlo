import logging
from contextlib import suppress
from uuid import uuid4

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app.bot.keyboards import subscriptions as keyboard
from app.bot.keyboards.main import main_menu
from app.bot.states.subscriptions import SubscriptionSetup
from app.bot.subscription_text import details_text, list_text
from app.services.subscriptions import CatalogUnavailable, SubscriptionManagementService
from app.services.validation import validate_name

logger = logging.getLogger(__name__)
EXPIRED = "Ця дія вже неактуальна. Відкрийте меню груп відключень знову."


def create_router() -> Router:
    router = Router(name="subscriptions")
    router.callback_query.register(open_menu, F.data == "menu:queues")
    router.callback_query.register(action, keyboard.SubscriptionAction.filter())
    router.message.register(enter_name, SubscriptionSetup.name)
    return router


async def render(query: CallbackQuery, text: str, markup: InlineKeyboardMarkup) -> None:
    if isinstance(query.message, Message):
        with suppress(TelegramBadRequest):
            await query.message.edit_reply_markup(reply_markup=None)
        await query.message.answer(text, reply_markup=markup, parse_mode=None)


async def show_list(
    query: CallbackQuery, service: SubscriptionManagementService, page: int = 0
) -> None:
    items = await service.list_subscriptions(query.from_user.id)
    page = max(0, min(page, max(0, (len(items) - 1) // keyboard.PAGE_SIZE)))
    await render(
        query,
        list_text(items[page * keyboard.PAGE_SIZE : (page + 1) * keyboard.PAGE_SIZE]),
        keyboard.subscription_list(items, page),
    )


async def show_details(
    query: CallbackQuery,
    service: SubscriptionManagementService,
    subscription_id: int,
    prefix: str = "",
) -> None:
    item = await service.get_subscription(query.from_user.id, subscription_id)
    selected = set(await service.channel_ids(query.from_user.id, subscription_id))
    channels = await service.channels(query.from_user.id)
    names = [
        channel.name + (" ⏸ Вимкнено" if not channel.enabled else "")
        for channel in channels
        if channel.id in selected
    ]
    await render(query, prefix + details_text(item, names), keyboard.details(item))


async def show_choices(query: CallbackQuery, state: FSMContext, page: int = 0) -> None:
    saved = await state.get_data()
    current = await state.get_state()
    selected = None
    if current == SubscriptionSetup.region.state:
        options = saved["regions"]
        labels = [option["name"] for option in options]
        labels = [
            label + (f" (джерело {i + 1})" if labels.count(label) > 1 else "")
            for i, label in enumerate(labels)
        ]
        kind, text = "region", "Оберіть область або місто:"
    elif current == SubscriptionSetup.group.state:
        options = saved["groups"]
        labels = [option["name"] for option in options]
        kind, text = "group", f"{saved['region_name']}\n\nОберіть групу відключень:"
    else:
        options = saved["channels"]
        labels = [
            option["name"] + (" ⏸ Вимкнено" if not option["enabled"] else "") for option in options
        ]
        ids = set(saved.get("selected", []))
        selected = {i for i, option in enumerate(options) if option["id"] in ids}
        kind, text = (
            "channel",
            f"Оберіть канали для сповіщень (вибрано: {len(selected)}).\n"
            "Можна зберегти без каналів.",
        )
    if saved.get("catalog_stale") and kind != "channel":
        text += "\n\n⚠️ Використано останній доступний каталог. Джерело тимчасово недоступне."
    if saved.get("catalog_partial") and kind == "region":
        text += "\n\n⚠️ Частина джерел тимчасово недоступна."
    page = max(0, min(page, max(0, (len(labels) - 1) // keyboard.PAGE_SIZE)))
    await state.update_data(page=page)
    await render(
        query,
        text,
        keyboard.choices(
            labels, kind, page, saved["nonce"], saved.get("subscription_id", 0), selected
        ),
    )


async def load_channels(
    query: CallbackQuery, state: FSMContext, service: SubscriptionManagementService
) -> None:
    saved = await state.get_data()
    channels = await service.channels(query.from_user.id)
    ids = {channel.id for channel in channels}
    await state.update_data(
        channels=[{"id": c.id, "name": c.name, "enabled": c.enabled} for c in channels],
        selected=[i for i in saved.get("selected", []) if i in ids],
    )
    await state.set_state(SubscriptionSetup.channels)
    await show_choices(query, state)


async def open_menu(
    query: CallbackQuery, state: FSMContext, subscription_service: SubscriptionManagementService
) -> None:
    await action(query, keyboard.SubscriptionAction(action="list"), state, subscription_service)


async def action(
    query: CallbackQuery,
    callback_data: keyboard.SubscriptionAction,
    state: FSMContext,
    subscription_service: SubscriptionManagementService,
) -> None:
    await query.answer()
    if not isinstance(query.message, Message) or query.message.chat.type != "private":
        return
    service, data = subscription_service, callback_data
    try:
        await dispatch(query, data, state, service)
    except CatalogUnavailable:
        await render(
            query,
            "❌ Не вдалося завантажити каталог. Спробуйте пізніше.",
            keyboard.navigation(data.subscription_id, (await state.get_data()).get("nonce", "")),
        )
    except (LookupError, ValueError):
        await render(
            query,
            "❌ Підписку або канал не знайдено чи дані вже змінилися. "
            "Відкрийте меню груп відключень знову.",
            keyboard.navigation(data.subscription_id, (await state.get_data()).get("nonce", "")),
        )
    except Exception:
        logger.warning("Subscription interaction unavailable")
        await render(
            query,
            "❌ Не вдалося виконати дію. Спробуйте пізніше.",
            keyboard.navigation(data.subscription_id, (await state.get_data()).get("nonce", "")),
        )


async def dispatch(
    query: CallbackQuery,
    data: keyboard.SubscriptionAction,
    state: FSMContext,
    service: SubscriptionManagementService,
) -> None:
    saved, current = await state.get_data(), await state.get_state()
    user_id, subscription_id = query.from_user.id, data.subscription_id
    if data.nonce and (
        data.nonce != saved.get("nonce") or subscription_id != saved.get("subscription_id", 0)
    ):
        await render(query, EXPIRED, main_menu())
        return
    if data.action in {"main", "list", "cancel"} or (data.action == "back" and not data.nonce):
        await state.clear()
        if data.action == "main":
            await render(query, "Головне меню", main_menu())
        elif data.action == "cancel" and subscription_id:
            await show_details(query, service, subscription_id)
        else:
            await show_list(query, service, data.index)
    elif data.action == "add":
        catalog = await service.regions()
        creation_key = uuid4().hex
        await state.set_data(
            {
                "regions": [region.model_dump() for region in catalog.items],
                "creation_key": creation_key,
                "nonce": creation_key[:12],
                "subscription_id": 0,
                "catalog_stale": catalog.stale,
                "catalog_partial": catalog.partial,
            }
        )
        await state.set_state(SubscriptionSetup.region)
        await show_choices(query, state)
    elif data.action in {"details", "channels", "enable", "disable", "delete", "rename"}:
        item = await service.get_subscription(user_id, subscription_id)
        await state.clear()
        if data.action in {"enable", "disable"}:
            await service.set_enabled(user_id, subscription_id, data.action == "enable")
            await show_details(query, service, subscription_id)
        elif data.action == "details":
            await show_details(query, service, subscription_id)
        else:
            nonce = uuid4().hex[:12]
            await state.set_data({"subscription_id": subscription_id, "nonce": nonce})
            if data.action == "channels":
                await state.update_data(
                    selected=await service.channel_ids(user_id, subscription_id)
                )
                await load_channels(query, state, service)
            elif data.action == "rename":
                await state.set_state(SubscriptionSetup.name)
                await render(
                    query,
                    "Введіть нову назву підписки:",
                    keyboard.navigation(subscription_id, nonce),
                )
            else:
                await state.set_state(SubscriptionSetup.delete)
                await render(
                    query,
                    f"Видалити підписку «{item.name}»?\n\n"
                    "Прив’язки до каналів буде видалено. Збережені графіки залишаться.",
                    keyboard.confirmation(subscription_id, nonce),
                )
    elif not data.nonce:
        await render(query, EXPIRED, main_menu())
    elif data.action == "confirm_delete" and current == SubscriptionSetup.delete.state:
        await service.delete(user_id, subscription_id)
        await state.clear()
        await show_list(query, service)
    elif data.action == "region" and current == SubscriptionSetup.region.state:
        if not 0 <= data.index < len(saved["regions"]):
            raise ValueError("Invalid region selection")
        region = saved["regions"][data.index]
        groups = await service.groups(region["provider"], region["id"])
        await state.update_data(
            provider=region["provider"],
            region=region["id"],
            region_name=region["name"],
            groups=[group.model_dump() for group in groups.items],
            catalog_stale=groups.stale,
        )
        await state.set_state(SubscriptionSetup.group)
        await show_choices(query, state)
    elif data.action == "group" and current == SubscriptionSetup.group.state:
        if not 0 <= data.index < len(saved["groups"]):
            raise ValueError("Invalid group selection")
        await state.update_data(queue=saved["groups"][data.index]["id"])
        await state.set_state(SubscriptionSetup.name)
        await render(
            query,
            "Введіть назву підписки (наприклад, Дім або Батьки):",
            keyboard.navigation(nonce=data.nonce),
        )
    elif data.action in {"select", "unselect"} and current == SubscriptionSetup.channels.state:
        if not 0 <= data.index < len(saved["channels"]):
            raise ValueError("Invalid channel selection")
        selected = set(saved.get("selected", []))
        channel_id = saved["channels"][data.index]["id"]
        if data.action == "select":
            selected.add(channel_id)
        else:
            selected.discard(channel_id)
        await state.update_data(selected=sorted(selected))
        await show_choices(query, state, saved.get("page", 0))
    elif data.action == "save" and current == SubscriptionSetup.channels.state:
        ids = set(saved.get("selected", []))
        if subscription_id:
            await service.update_channels(user_id, subscription_id, ids)
        else:
            item = await service.create_subscription(
                user_id,
                saved["provider"],
                saved["region"],
                saved["queue"],
                saved["name"],
                ids,
                saved["creation_key"],
            )
            subscription_id = item.id
        await state.clear()
        await show_details(query, service, subscription_id, "✅ Підписку збережено.\n\n")
    elif data.action == "refresh" and current == SubscriptionSetup.channels.state:
        await load_channels(query, state, service)
    elif data.action == "page" and current in {
        SubscriptionSetup.region.state,
        SubscriptionSetup.group.state,
        SubscriptionSetup.channels.state,
    }:
        await show_choices(query, state, data.index)
    elif data.action == "back":
        if subscription_id:
            await state.clear()
            await show_details(query, service, subscription_id)
        elif current == SubscriptionSetup.region.state:
            await state.clear()
            await show_list(query, service)
        elif current == SubscriptionSetup.group.state:
            await state.set_state(SubscriptionSetup.region)
            await show_choices(query, state)
        elif current == SubscriptionSetup.name.state:
            await state.set_state(SubscriptionSetup.group)
            await show_choices(query, state)
        elif current == SubscriptionSetup.channels.state:
            await state.set_state(SubscriptionSetup.name)
            await render(query, "Введіть назву підписки:", keyboard.navigation(nonce=data.nonce))
        else:
            await state.clear()
            await show_list(query, service)
    else:
        await render(query, EXPIRED, main_menu())


async def enter_name(
    message: Message, state: FSMContext, subscription_service: SubscriptionManagementService
) -> None:
    if message.from_user is None or message.chat.type != "private":
        return
    saved = await state.get_data()
    try:
        name = validate_name(message.text or "")
    except ValueError:
        await message.answer(
            "❌ Введіть назву від 1 до 100 символів без переносів рядка.",
            reply_markup=keyboard.navigation(saved.get("subscription_id", 0), saved["nonce"]),
        )
        return
    query = CallbackQuery(
        id="name", from_user=message.from_user, chat_instance="private", message=message
    ).as_(message.bot)
    try:
        subscription_id = saved.get("subscription_id", 0)
        if subscription_id:
            await subscription_service.rename(message.from_user.id, subscription_id, name)
            await state.clear()
            await show_details(
                query, subscription_service, subscription_id, "✅ Назву змінено.\n\n"
            )
        else:
            await state.update_data(name=name)
            await load_channels(query, state, subscription_service)
    except Exception:
        logger.warning("Subscription name interaction unavailable")
        await message.answer(
            "❌ Не вдалося зберегти дані. Спробуйте пізніше.",
            reply_markup=keyboard.navigation(subscription_id, saved["nonce"]),
        )
