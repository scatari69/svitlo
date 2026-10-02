"""Live Telegram access checks shared by registration and every delivery path."""

import asyncio

from aiogram import Bot
from aiogram.types import (
    ChatFullInfo,
    ChatMemberAdministrator,
    ChatMemberMember,
    ChatMemberOwner,
    ChatMemberRestricted,
)


class ChannelAccessDenied(ValueError):
    pass


async def inspect_chat(bot: Bot, chat_id: int | str) -> ChatFullInfo:
    try:
        async with asyncio.timeout(20):
            chat = await bot.get_chat(chat_id, request_timeout=10)
        if not isinstance(chat, ChatFullInfo):
            raise ChannelAccessDenied("Invalid Telegram chat response")
        return chat
    except Exception:
        raise ChannelAccessDenied("Chat unavailable") from None


async def can_deliver(bot: Bot, chat: ChatFullInfo, *, photo: bool = False) -> bool:
    if chat.type == "private":
        return chat.id > 0
    if chat.type not in {"group", "supergroup", "channel"}:
        return False
    try:
        async with asyncio.timeout(15):
            member = await bot.get_chat_member(chat.id, bot.id, request_timeout=10)
        if member.user.id != bot.id:
            return False
        if isinstance(member, ChatMemberOwner):
            return True
        if isinstance(member, ChatMemberAdministrator):
            return chat.type != "channel" or member.can_post_messages is True
        if chat.type == "channel":
            return False
        if isinstance(member, ChatMemberRestricted):
            return (
                member.is_member
                and member.can_send_messages
                and (not photo or member.can_send_photos)
            )
        if isinstance(member, ChatMemberMember):
            permissions = chat.permissions
            return bool(
                permissions
                and permissions.can_send_messages is True
                and (not photo or permissions.can_send_photos is True)
            )
    except Exception:
        return False
    return False


async def verify_registration(bot: Bot, owner_id: int, chat: ChatFullInfo) -> None:
    if chat.type == "private":
        if chat.id != owner_id:
            raise ChannelAccessDenied("Private destination must be the requesting user")
        return
    # Telegram guarantees querying another member only while the bot is an administrator.
    try:
        async with asyncio.timeout(20):
            own = await bot.get_chat_member(chat.id, bot.id, request_timeout=10)
            if (
                not isinstance(own, (ChatMemberAdministrator, ChatMemberOwner))
                or own.user.id != bot.id
            ):
                raise ChannelAccessDenied("Bot administrator required")
            actor = await bot.get_chat_member(chat.id, owner_id, request_timeout=10)
            if (
                not isinstance(actor, (ChatMemberAdministrator, ChatMemberOwner))
                or actor.user.id != owner_id
            ):
                raise ChannelAccessDenied("Chat administrator required")
    except Exception:
        raise ChannelAccessDenied("Administrator verification failed") from None
    if not await can_deliver(bot, chat, photo=True):
        raise ChannelAccessDenied("Posting permissions required")
