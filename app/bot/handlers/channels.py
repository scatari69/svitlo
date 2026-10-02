import logging
import secrets

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    ChatAdministratorRights,
    KeyboardButton,
    KeyboardButtonRequestChat,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from app.bot.keyboards.channels import ChannelAction, button, markup, navigation
from app.bot.keyboards.main import main_menu
from app.services.channels import ChannelManagementService

logger = logging.getLogger(__name__)


class ChannelSetup(StatesGroup):
    target = State()
    name = State()
    rename = State()
    delete = State()
    assignments = State()


def create_router() -> Router:
    router = Router(name="channels")
    router.callback_query.register(open_menu, F.data == "menu:channels")
    router.callback_query.register(action, ChannelAction.filter())
    router.message.register(
        cancel,
        F.text.in_({"❌ Скасувати", "⬅️ Назад"}),
        F.chat.type == "private",
        ChannelSetup.target,
    )
    router.message.register(target, ChannelSetup.target, F.chat.type == "private")
    router.message.register(name, ChannelSetup.name, F.chat.type == "private")
    router.message.register(name, ChannelSetup.rename, F.chat.type == "private")
    return router


async def show_list(
    message: Message, owner: int, service: ChannelManagementService, page: int = 0
) -> None:
    channels = await service.list_channels(owner)
    page = max(0, min(page, max(0, (len(channels) - 1) // 8)))
    rows = [
        [button(("✅ " if row.enabled else "⏸ ") + row.name, "details", channel_id=row.id)]
        for row in channels[page * 8 : page * 8 + 8]
    ]
    if page > 0:
        rows.append([button("⬅️ Попередні", "list", page=page - 1)])
    if (page + 1) * 8 < len(channels):
        rows.append([button("➡️ Наступні", "list", page=page + 1)])
    rows += [[button("➕ Додати канал", "add")], [button("⬅️ Головне меню", "main")]]
    await message.answer(
        "🔔 Канали\n\n" + ("Оберіть канал:" if channels else "Каналів ще немає."),
        reply_markup=markup(*rows),
        parse_mode=None,
    )


async def open_menu(
    query: CallbackQuery, state: FSMContext, channel_service: ChannelManagementService
) -> None:
    await action(query, ChannelAction(action="list"), state, channel_service)


async def cancel(
    message: Message, state: FSMContext, channel_service: ChannelManagementService
) -> None:
    if message.from_user is None:
        return
    await state.clear()
    await message.answer("Додавання скасовано.", reply_markup=ReplyKeyboardRemove())
    await show_list(message, message.from_user.id, channel_service)


async def action(
    query: CallbackQuery,
    callback_data: ChannelAction,
    state: FSMContext,
    channel_service: ChannelManagementService,
) -> None:
    if not isinstance(query.message, Message) or query.message.chat.type != "private":
        await query.answer("Керуйте каналами в особистому чаті з ботом.", show_alert=True)
        return
    await query.answer()
    message, owner, data = query.message, query.from_user.id, callback_data
    if await state.get_state() == ChannelSetup.target.state:
        await message.answer("Оберіть подальшу дію.", reply_markup=ReplyKeyboardRemove())
    try:
        if data.action in {"list", "main", "add", "private", "group", "channel"}:
            await state.clear()
            if data.action == "list":
                await show_list(message, owner, channel_service, data.page)
            elif data.action == "main":
                await message.answer("Головне меню", reply_markup=main_menu())
            elif data.action == "add":
                await message.answer(
                    "Оберіть тип каналу сповіщень:",
                    reply_markup=markup(
                        [button("👤 Особистий чат", "private")],
                        [button("👥 Група", "group")],
                        [button("📢 Telegram-канал", "channel")],
                        navigation(),
                    ),
                )
            elif data.action == "private":
                await channel_service.preview(owner, owner, "private")
                await state.set_state(ChannelSetup.name)
                await state.update_data(chat_id=owner, kind="private")
                await message.answer(
                    "Введіть назву каналу, наприклад «Особисті»:", reply_markup=markup(navigation())
                )
            else:
                request_id = secrets.randbelow(2**31)
                rights = ChatAdministratorRights(
                    is_anonymous=False,
                    can_manage_chat=True,
                    can_delete_messages=False,
                    can_manage_video_chats=False,
                    can_restrict_members=False,
                    can_promote_members=False,
                    can_change_info=False,
                    can_invite_users=False,
                    can_post_stories=False,
                    can_edit_stories=False,
                    can_delete_stories=False,
                    can_send_welcome_messages=False,
                    can_post_messages=True if data.action == "channel" else None,
                )
                await state.set_state(ChannelSetup.target)
                await state.update_data(kind=data.action, request_id=request_id)
                picker = KeyboardButtonRequestChat(
                    request_id=request_id,
                    chat_is_channel=data.action == "channel",
                    bot_is_member=True,
                    user_administrator_rights=rights,
                    bot_administrator_rights=rights,
                )
                await message.answer(
                    "Додайте бота до групи або каналу як адміністратора. Для каналу дозвольте "
                    "публікацію повідомлень. Ви також маєте бути адміністратором.\n\n"
                    "Оберіть чат кнопкою нижче або введіть його @назву чи числовий ідентифікатор.",
                    reply_markup=ReplyKeyboardMarkup(
                        keyboard=[
                            [KeyboardButton(text="📍 Обрати чат", request_chat=picker)],
                            [KeyboardButton(text="⬅️ Назад"), KeyboardButton(text="❌ Скасувати")],
                        ],
                        resize_keyboard=True,
                    ),
                )
        elif data.action in {"device", "select", "unselect", "save", "page"}:
            stored = await state.get_data()
            if data.action == "device":
                channels, selected = await channel_service.device_channels(owner, data.device_id)
                await state.clear()
                await state.set_state(ChannelSetup.assignments)
                stored = {
                    "device_id": data.device_id,
                    "selected": list(selected),
                    "nonce": secrets.token_hex(4),
                }
                await state.update_data(**stored)
            else:
                if (
                    await state.get_state() != ChannelSetup.assignments.state
                    or stored.get("nonce") != data.nonce
                    or stored.get("device_id") != data.device_id
                ):
                    raise ValueError("Expired selection")
                channels, _ = await channel_service.device_channels(owner, data.device_id)
                selected = set(stored["selected"])
                if data.action == "save":
                    await channel_service.assign_device(owner, data.device_id, selected)
                    await state.clear()
                    await message.answer(
                        "✅ Канали пристрою збережено.", reply_markup=markup(navigation())
                    )
                    return
                if data.action != "page" and data.channel_id not in {row.id for row in channels}:
                    raise LookupError("Channel not found")
                if data.action == "select":
                    selected.add(data.channel_id)
                elif data.action == "unselect":
                    selected.discard(data.channel_id)
                stored["selected"] = list(selected)
                await state.update_data(selected=list(selected))
            page = max(0, min(data.page, max(0, (len(channels) - 1) // 8)))
            rows = [
                [
                    button(
                        ("✅ " if row.id in stored["selected"] else "☐ ")
                        + row.name
                        + (" (вимкнено)" if not row.enabled else ""),
                        "unselect" if row.id in stored["selected"] else "select",
                        channel_id=row.id,
                        device_id=data.device_id,
                        nonce=stored["nonce"],
                        page=page,
                    )
                ]
                for row in channels[page * 8 : page * 8 + 8]
            ]
            for label, destination in (("⬅️ Попередні", page - 1), ("➡️ Наступні", page + 1)):
                if 0 <= destination <= (len(channels) - 1) // 8:
                    rows.append(
                        [
                            button(
                                label,
                                "page",
                                device_id=data.device_id,
                                nonce=stored["nonce"],
                                page=destination,
                            )
                        ]
                    )
            rows += [
                [button("💾 Зберегти", "save", device_id=data.device_id, nonce=stored["nonce"])],
                navigation(),
            ]
            await message.answer(
                "🔔 Оберіть канали пристрою.\n\n"
                "Від’єднання каналу видалить його налаштування автоматичних "
                "звітів для цього пристрою. Історія збережеться.",
                reply_markup=markup(*rows),
                parse_mode=None,
            )
        else:
            row = await channel_service.get(owner, data.channel_id)
            if data.action == "rename":
                await state.clear()
                await state.set_state(ChannelSetup.rename)
                await state.update_data(channel_id=row.id)
                await message.answer(
                    "Введіть нову назву каналу:", reply_markup=markup(navigation())
                )
                return
            if data.action == "delete":
                nonce = secrets.token_hex(4)
                await state.clear()
                await state.set_state(ChannelSetup.delete)
                await state.update_data(channel_id=row.id, nonce=nonce)
                await message.answer(
                    f"Видалити канал «{row.name}»?\n\n"
                    "⚠️ Прив’язки та налаштування звітів буде видалено. "
                    "Історія моніторингу збережеться.",
                    parse_mode=None,
                    reply_markup=markup(
                        [button("🗑 Так, видалити", "confirm", channel_id=row.id, nonce=nonce)],
                        navigation(),
                    ),
                )
                return
            if data.action == "confirm":
                stored = await state.get_data()
                if (
                    await state.get_state() != ChannelSetup.delete.state
                    or stored.get("channel_id") != row.id
                    or stored.get("nonce") != data.nonce
                ):
                    raise ValueError("Expired confirmation")
                await channel_service.delete(owner, row.id)
                await state.clear()
                await show_list(message, owner, channel_service)
                return
            if data.action == "check":
                await channel_service.check(owner, row.id)
                await message.answer("✅ Бот має доступ до каналу.")
            elif data.action in {"enable", "disable"}:
                row = await channel_service.set_enabled(owner, row.id, data.action == "enable")
            elif data.action != "details":
                raise ValueError("Unknown action")
            await state.clear()
            await message.answer(
                f"🔔 {row.name}\nСповіщення: " + ("✅ Увімкнено" if row.enabled else "⏸ Вимкнено"),
                parse_mode=None,
                reply_markup=markup(
                    [button("✏️ Назва", "rename", channel_id=row.id)],
                    [button("🧪 Перевірити доступ", "check", channel_id=row.id)],
                    [
                        button(
                            "⏸ Вимкнути" if row.enabled else "✅ Увімкнути",
                            "disable" if row.enabled else "enable",
                            channel_id=row.id,
                        )
                    ],
                    [button("🗑 Видалити", "delete", channel_id=row.id)],
                    navigation(),
                ),
            )
    except (ValueError, LookupError):
        await message.answer(
            "❌ Не вдалося виконати дію. Перевірте права адміністратора "
            "та доступ бота до чату, а потім спробуйте ще раз.",
            reply_markup=markup(navigation()),
        )
    except Exception:
        logger.warning("Channel interaction unavailable")
        await message.answer(
            "❌ Не вдалося виконати дію. Спробуйте пізніше.", reply_markup=markup(navigation())
        )


async def target(
    message: Message, state: FSMContext, channel_service: ChannelManagementService
) -> None:
    if message.from_user is None:
        return
    stored = await state.get_data()
    try:
        if message.chat_shared is not None:
            if message.chat_shared.request_id != stored.get("request_id"):
                raise ValueError("Expired picker")
            destination: int | str = message.chat_shared.chat_id
        elif message.text:
            destination = message.text.strip()
        else:
            raise ValueError("Missing destination")
        chat_id = await channel_service.preview(message.from_user.id, destination, stored["kind"])
    except ValueError:
        await message.answer(
            "❌ Чат недоступний. Перевірте адресу та права адміністратора для себе й бота."
        )
        return
    await state.update_data(chat_id=chat_id)
    await state.set_state(ChannelSetup.name)
    await message.answer("✅ Чат обрано.", reply_markup=ReplyKeyboardRemove())
    await message.answer(
        "Введіть назву каналу, наприклад «Сім’я»:", reply_markup=markup(navigation())
    )


async def name(
    message: Message, state: FSMContext, channel_service: ChannelManagementService
) -> None:
    if message.from_user is None:
        return
    stored = await state.get_data()
    try:
        if await state.get_state() == ChannelSetup.rename.state:
            await channel_service.rename(
                message.from_user.id, stored["channel_id"], message.text or ""
            )
        else:
            await channel_service.register(
                message.from_user.id, stored["chat_id"], stored["kind"], message.text or ""
            )
    except (ValueError, LookupError):
        await message.answer(
            "❌ Назва має містити від 1 до 100 символів. Також перевірте "
            "права бота й адміністратора чату.",
            reply_markup=markup(navigation()),
        )
        return
    except Exception:
        logger.warning("Channel registration or rename unavailable")
        await message.answer(
            "❌ Не вдалося зберегти канал. Спробуйте пізніше.", reply_markup=markup(navigation())
        )
        return
    await state.clear()
    await message.answer("✅ Канал збережено.")
    await show_list(message, message.from_user.id, channel_service)
