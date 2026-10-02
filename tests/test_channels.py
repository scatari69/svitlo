from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramForbiddenError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    AcceptedGiftTypes,
    CallbackQuery,
    Chat,
    ChatFullInfo,
    ChatMemberAdministrator,
    ChatMemberMember,
    ChatMemberOwner,
    ChatPermissions,
    ChatShared,
    Message,
    User,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.bot.handlers.channels import ChannelSetup, action, name, target
from app.bot.keyboards.channels import ChannelAction
from app.models import DeviceNotificationChannel, NotificationChannel, ReportSettings
from app.notifications.telegram import send_power_message, send_report_photo
from app.services.channels import ChannelManagementService
from app.services.telegram_access import ChannelAccessDenied, can_deliver

OWNER = 2**40


def user(identity: int) -> User:
    return User(id=identity, is_bot=False, first_name="Test")


def chat(kind: str = "supergroup", identity: int = -OWNER) -> ChatFullInfo:
    return ChatFullInfo(
        id=identity,
        type=kind,
        accent_color_id=0,
        max_reaction_count=0,
        accepted_gift_types=AcceptedGiftTypes(
            unlimited_gifts=False,
            limited_gifts=False,
            unique_gifts=False,
            premium_subscription=False,
            gifts_from_channels=False,
        ),
        permissions=ChatPermissions(can_send_messages=True, can_send_photos=True),
    )


def admin(identity: int, *, posting: bool = True) -> ChatMemberAdministrator:
    return ChatMemberAdministrator(
        user=user(identity),
        can_be_edited=False,
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
        can_post_messages=posting,
    )


@pytest.fixture
def bot() -> AsyncMock:
    result = AsyncMock(spec=Bot)
    result.id = 123456
    result.get_chat.return_value = chat()
    result.get_chat_member.side_effect = lambda chat_id, identity, **kw: (
        admin(result.id)
        if identity == result.id
        else ChatMemberOwner(user=user(identity), is_anonymous=False)
    )
    return result


@pytest.mark.parametrize("kind", ["group", "supergroup", "channel", "private"])
async def test_register_kinds_and_duplicate(sessions: MagicMock, bot: AsyncMock, kind: str) -> None:
    identity = OWNER if kind == "private" else -OWNER
    bot.get_chat.return_value = chat(kind, identity)
    service = ChannelManagementService(sessions, bot)
    first = await service.register(OWNER, identity, kind, "Сім’я")
    second = await service.register(OWNER, identity, kind, "Робота")
    assert first.id == second.id
    assert second.name == "Робота" and second.channel_type.value == kind
    assert len(await service.list_channels(OWNER)) == (2 if kind == "private" else 1)


async def test_registration_requires_both_admins(sessions: MagicMock, bot: AsyncMock) -> None:
    service = ChannelManagementService(sessions, bot)
    for denied in (bot.id, OWNER):
        bot.get_chat_member.side_effect = lambda cid, identity, denied=denied, **kw: (
            ChatMemberMember(user=user(identity)) if identity == denied else admin(identity)
        )
        with pytest.raises(ChannelAccessDenied):
            await service.register(OWNER, -OWNER, "group", "Сім’я")
    assert (await service.list_channels(OWNER))[0].name == "Group"


async def test_private_and_type_validation(sessions: MagicMock, bot: AsyncMock) -> None:
    service = ChannelManagementService(sessions, bot)
    bot.get_chat.return_value = chat("private", OWNER + 1)
    with pytest.raises(ChannelAccessDenied):
        await service.register(OWNER, OWNER + 1, "private", "Чужий")
    bot.get_chat.return_value = chat("supergroup")
    with pytest.raises(ChannelAccessDenied):
        await service.preview(OWNER, -OWNER, "channel")
    for value in ("not-a-chat", "-9223372036854775809", "@", "12345"):
        with pytest.raises(ChannelAccessDenied):
            await service.preview(OWNER, value, "group")


async def test_owner_scoping_and_assignments(
    sessions: MagicMock, db_session: Session, bot: AsyncMock
) -> None:
    service = ChannelManagementService(sessions, bot)
    for operation in (
        service.get(OWNER, 2),
        service.rename(OWNER, 2, "Робота"),
        service.delete(OWNER, 2),
        service.assign_device(OWNER, 4, {1}),
        service.assign_device(OWNER, 1, {2}),
    ):
        with pytest.raises(LookupError):
            await operation
    await service.assign_device(OWNER, 1, {1})
    await service.assign_device(OWNER, 2, {1})
    db_session.add(ReportSettings(device_id=1, channel_id=1, daily_enabled=True))
    db_session.commit()
    await service.assign_device(OWNER, 1, {1})
    db_session.expire_all()
    assert db_session.get(ReportSettings, (1, 1)) is not None
    assert (await service.device_channels(OWNER, 1))[1] == {1}
    await service.assign_device(OWNER, 1, set())
    db_session.expire_all()
    assert db_session.get(ReportSettings, (1, 1)) is None
    assert (await service.device_channels(OWNER, 2))[1] == {1}
    await service.delete(OWNER, 1)
    assert not db_session.scalars(select(DeviceNotificationChannel)).all()
    assert db_session.get(NotificationChannel, 2) is not None


async def test_disable_and_reenable_requires_access(sessions: MagicMock, bot: AsyncMock) -> None:
    service = ChannelManagementService(sessions, bot)
    await service.set_enabled(OWNER, 1, False)
    bot.get_chat.side_effect = TimeoutError()
    with pytest.raises(ChannelAccessDenied):
        await service.set_enabled(OWNER, 1, True)
    assert not (await service.get(OWNER, 1)).enabled


@pytest.mark.parametrize("kind", ["group", "supergroup", "channel"])
async def test_delivery_checks_permissions(bot: AsyncMock, kind: str) -> None:
    bot.get_chat.return_value = chat(kind)
    await send_power_message(bot, -OWNER, "Світло з’явилося")
    assert bot.send_message.await_count == 1
    bot.get_chat_member.side_effect = None
    bot.get_chat_member.return_value = (
        admin(bot.id, posting=False) if kind == "channel" else (ChatMemberMember(user=user(bot.id)))
    )
    bot.get_chat.return_value = chat(kind).model_copy(
        update={"permissions": ChatPermissions(can_send_messages=False, can_send_photos=False)}
    )
    with pytest.raises(TelegramForbiddenError):
        await send_power_message(bot, -OWNER, "Зникло світло")
    with pytest.raises(TelegramForbiddenError):
        await send_report_photo(bot, -OWNER, b"image", "Звіт")
    assert bot.send_message.await_count == 1
    bot.send_photo.assert_not_awaited()


async def test_delivery_fails_closed_on_timeout(bot: AsyncMock) -> None:
    bot.get_chat.side_effect = TimeoutError()
    with pytest.raises(TelegramForbiddenError):
        await send_power_message(bot, -OWNER, "Сповіщення")
    bot.send_message.assert_not_awaited()


async def test_photo_restriction(bot: AsyncMock) -> None:
    bot.get_chat_member.side_effect = None
    bot.get_chat_member.return_value = ChatMemberMember(user=user(bot.id))
    destination = chat().model_copy(
        update={"permissions": ChatPermissions(can_send_messages=True, can_send_photos=False)}
    )
    assert await can_deliver(bot, destination)
    assert not await can_deliver(bot, destination, photo=True)


@pytest.fixture
def fsm() -> FSMContext:
    return FSMContext(MemoryStorage(), StorageKey(bot_id=123456, chat_id=OWNER, user_id=OWNER))


def message(text: str = "", shared: ChatShared | None = None) -> Message:
    transport = Bot("123456:TEST_TOKEN", session=AsyncMock(spec=BaseSession))
    return Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=OWNER, type="private"),
        from_user=user(OWNER),
        text=text or None,
        chat_shared=shared,
    ).as_(transport)


def query() -> CallbackQuery:
    msg = message()
    return CallbackQuery(id="test", from_user=user(OWNER), chat_instance="test", message=msg).as_(
        msg.bot
    )


async def test_group_picker_fsm(fsm: FSMContext) -> None:
    service = AsyncMock(spec=ChannelManagementService)
    service.list_channels.return_value = []
    service.preview.return_value = -OWNER
    await action(query(), ChannelAction(action="group"), fsm, service)
    stored = await fsm.get_data()
    assert await fsm.get_state() == ChannelSetup.target.state
    await target(
        message(shared=ChatShared(request_id=stored["request_id"] + 1, chat_id=-OWNER)),
        fsm,
        service,
    )
    service.preview.assert_not_awaited()
    await target(
        message(shared=ChatShared(request_id=stored["request_id"], chat_id=-OWNER)), fsm, service
    )
    assert await fsm.get_state() == ChannelSetup.name.state
    await name(message("Сім’я"), fsm, service)
    service.register.assert_awaited_once_with(OWNER, -OWNER, "group", "Сім’я")
    assert await fsm.get_state() is None


async def test_confirm_delete_and_stale_assignments(fsm: FSMContext) -> None:
    service = AsyncMock(spec=ChannelManagementService)
    service.get.return_value = MagicMock(id=1, name="Сім’я")
    service.list_channels.return_value = []
    await action(query(), ChannelAction(action="confirm", channel_id=1, nonce="old"), fsm, service)
    service.delete.assert_not_awaited()
    await action(query(), ChannelAction(action="delete", channel_id=1), fsm, service)
    stored = await fsm.get_data()
    await action(
        query(), ChannelAction(action="confirm", channel_id=1, nonce=stored["nonce"]), fsm, service
    )
    service.delete.assert_awaited_once_with(OWNER, 1)
    await action(query(), ChannelAction(action="save", device_id=1, nonce="old"), fsm, service)
    service.assign_device.assert_not_awaited()


def test_callback_fits_telegram_limit() -> None:
    assert (
        len(
            ChannelAction(
                action="unselect",
                channel_id=2**63 - 1,
                device_id=2**63 - 1,
                nonce="12345678",
                page=99,
            )
            .pack()
            .encode()
        )
        <= 64
    )


async def test_channels_shared_with_schedule_subscriptions(
    sessions: MagicMock, bot: AsyncMock
) -> None:
    from app.services.subscriptions import SubscriptionManagementService

    channels = ChannelManagementService(sessions, bot)
    bot.get_chat.return_value = chat("private", OWNER)
    personal = await channels.register(OWNER, OWNER, "private", "Особисті")
    subscriptions = SubscriptionManagementService(sessions, MagicMock())
    assert {row.id for row in await subscriptions.channels(OWNER)} == {1, personal.id}
    await channels.assign_device(OWNER, 1, {1, personal.id})
    await subscriptions.update_channels(OWNER, 1, {personal.id})
    assert (await channels.device_channels(OWNER, 1))[1] == {1, personal.id}
    assert await subscriptions.channel_ids(OWNER, 1) == [personal.id]
    await channels.delete(OWNER, personal.id)
    assert await subscriptions.channel_ids(OWNER, 1) == []
    assert (await channels.device_channels(OWNER, 1))[1] == {1}


async def test_assignment_selection_is_idempotent(fsm: FSMContext) -> None:
    from app.models.enums import ChannelType
    from app.services.channels import ManagedChannel

    service = AsyncMock(spec=ChannelManagementService)
    service.device_channels.return_value = (
        [ManagedChannel(1, "Сім’я", ChannelType.GROUP, True)],
        set(),
    )
    await action(query(), ChannelAction(action="device", device_id=1), fsm, service)
    nonce = (await fsm.get_data())["nonce"]
    selection = ChannelAction(action="select", channel_id=1, device_id=1, nonce=nonce)
    await action(query(), selection, fsm, service)
    await action(query(), selection, fsm, service)
    assert (await fsm.get_data())["selected"] == [1]
    await action(query(), ChannelAction(action="save", device_id=1, nonce=nonce), fsm, service)
    service.assign_device.assert_awaited_once_with(OWNER, 1, {1})


async def test_delivery_photo_success_and_retry_rechecks(bot: AsyncMock) -> None:
    from aiogram.exceptions import TelegramRetryAfter
    from aiogram.methods import SendMessage

    await send_report_photo(bot, -OWNER, b"image", "Звіт")
    bot.send_photo.assert_awaited_once()
    bot.send_message.side_effect = TelegramRetryAfter(
        method=SendMessage(chat_id=-OWNER, text="test"), message="limited", retry_after=1
    )
    # Membership disappears between a rejected attempt and its retry.
    bot.get_chat.side_effect = [chat(), TimeoutError()]
    from unittest.mock import patch

    with patch("app.bot.transport.asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(TelegramForbiddenError):
            await send_power_message(bot, -OWNER, "Сповіщення")
    assert bot.send_message.await_count == 1


async def test_database_failure_keeps_pending_name(fsm: FSMContext) -> None:
    service = AsyncMock(spec=ChannelManagementService)
    service.register.side_effect = RuntimeError("internal details")
    await fsm.set_state(ChannelSetup.name)
    await fsm.update_data(chat_id=OWNER, kind="private")
    msg = message("Особисті")
    await name(msg, fsm, service)
    assert await fsm.get_state() == ChannelSetup.name.state
    assert msg.bot is not None
    assert isinstance(msg.bot.session, AsyncMock)
    request = msg.bot.session.call_args.args[1]
    assert request.text == "❌ Не вдалося зберегти канал. Спробуйте пізніше."
