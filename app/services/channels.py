import re
from dataclasses import dataclass

from aiogram import Bot
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import DeviceNotificationChannel, NotificationChannel
from app.models.enums import ChannelType
from app.repositories.channels import NotificationChannelRepository
from app.repositories.devices import DeviceRepository
from app.repositories.users import UserRepository
from app.services.telegram_access import ChannelAccessDenied, inspect_chat, verify_registration
from app.services.validation import validate_name


@dataclass(frozen=True)
class ManagedChannel:
    id: int
    name: str
    channel_type: ChannelType
    enabled: bool

    @classmethod
    def from_model(cls, row: NotificationChannel) -> "ManagedChannel":
        return cls(row.id, row.name, row.channel_type, row.enabled)


class ChannelManagementService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], bot: Bot) -> None:
        self.sessions, self.bot = sessions, bot

    async def list_channels(self, telegram_user_id: int) -> list[ManagedChannel]:
        async with self.sessions.begin() as session:
            user = await UserRepository(session).get_or_create(telegram_user_id)
            rows = await NotificationChannelRepository(session).list_owned(user.id)
            return [ManagedChannel.from_model(row) for row in rows]

    async def _owned(
        self, session: AsyncSession, telegram_user_id: int, channel_id: int, *, lock: bool = False
    ) -> NotificationChannel:
        user = await UserRepository(session).get_or_create(telegram_user_id)
        query = select(NotificationChannel).where(
            NotificationChannel.id == channel_id, NotificationChannel.user_id == user.id
        )
        if lock:
            query = query.with_for_update().execution_options(populate_existing=True)
        row = await session.scalar(query)
        if row is None:
            raise LookupError("Channel not found")
        return row

    async def get(self, telegram_user_id: int, channel_id: int) -> ManagedChannel:
        async with self.sessions.begin() as session:
            return ManagedChannel.from_model(
                await self._owned(session, telegram_user_id, channel_id)
            )

    async def preview(self, telegram_user_id: int, target: int | str, expected_type: str) -> int:
        if expected_type not in {"private", "group", "supergroup", "channel"}:
            raise ChannelAccessDenied("Invalid destination type")
        if isinstance(target, str):
            if re.fullmatch(r"@[A-Za-z][A-Za-z0-9_]{4,31}", target):
                pass
            elif re.fullmatch(r"-[1-9][0-9]{0,18}", target) and -(2**63) <= int(target) < 0:
                target = int(target)
            else:
                raise ChannelAccessDenied("Invalid chat identifier")
        chat = await inspect_chat(self.bot, target)
        valid_types = (
            {"private"}
            if expected_type == "private"
            else ({"channel"} if expected_type == "channel" else {"group", "supergroup"})
        )
        if chat.type not in valid_types or (isinstance(target, int) and chat.id != target):
            raise ChannelAccessDenied("Unexpected destination")
        await verify_registration(self.bot, telegram_user_id, chat)
        return chat.id

    async def register(
        self, telegram_user_id: int, chat_id: int, channel_type: str, name: str
    ) -> ManagedChannel:
        name = validate_name(name)
        await self.preview(telegram_user_id, chat_id, channel_type)
        chat = await inspect_chat(self.bot, chat_id)
        async with self.sessions.begin() as session:
            user = await UserRepository(session).get_or_create(telegram_user_id)
            row = await session.scalar(
                insert(NotificationChannel)
                .values(
                    user_id=user.id,
                    telegram_chat_id=chat.id,
                    name=name,
                    channel_type=ChannelType(chat.type),
                    enabled=True,
                )
                .on_conflict_do_update(
                    index_elements=[
                        NotificationChannel.user_id,
                        NotificationChannel.telegram_chat_id,
                    ],
                    set_={"name": name, "channel_type": ChannelType(chat.type), "enabled": True},
                )
                .returning(NotificationChannel)
            )
            if row is None:
                raise RuntimeError("Channel missing after registration")
            return ManagedChannel.from_model(row)

    async def rename(self, telegram_user_id: int, channel_id: int, name: str) -> ManagedChannel:
        name = validate_name(name)
        async with self.sessions.begin() as session:
            row = await self._owned(session, telegram_user_id, channel_id, lock=True)
            row.name = name
            return ManagedChannel.from_model(row)

    async def check(self, telegram_user_id: int, channel_id: int) -> None:
        async with self.sessions.begin() as session:
            row = await self._owned(session, telegram_user_id, channel_id)
            chat_id, kind = row.telegram_chat_id, row.channel_type.value
        await self.preview(telegram_user_id, chat_id, kind)

    async def set_enabled(
        self, telegram_user_id: int, channel_id: int, enabled: bool
    ) -> ManagedChannel:
        if enabled:
            await self.check(telegram_user_id, channel_id)
        async with self.sessions.begin() as session:
            row = await self._owned(session, telegram_user_id, channel_id, lock=True)
            row.enabled = enabled
            return ManagedChannel.from_model(row)

    async def delete(self, telegram_user_id: int, channel_id: int) -> None:
        async with self.sessions.begin() as session:
            row = await self._owned(session, telegram_user_id, channel_id, lock=True)
            await session.delete(row)

    async def device_channels(
        self, telegram_user_id: int, device_id: int
    ) -> tuple[list[ManagedChannel], set[int]]:
        async with self.sessions.begin() as session:
            user = await UserRepository(session).get_or_create(telegram_user_id)
            if await DeviceRepository(session).get(user.id, device_id) is None:
                raise LookupError("Device not found")
            rows = await NotificationChannelRepository(session).list_owned(user.id)
            selected = set(
                (
                    await session.scalars(
                        select(DeviceNotificationChannel.channel_id).where(
                            DeviceNotificationChannel.user_id == user.id,
                            DeviceNotificationChannel.device_id == device_id,
                        )
                    )
                ).all()
            )
            return [ManagedChannel.from_model(row) for row in rows], selected

    async def assign_device(self, telegram_user_id: int, device_id: int, ids: set[int]) -> None:
        async with self.sessions.begin() as session:
            user = await UserRepository(session).get_or_create(telegram_user_id)
            if await DeviceRepository(session).get(user.id, device_id, for_update=True) is None:
                raise LookupError("Device not found")
            owned = {
                row.id for row in await NotificationChannelRepository(session).list_owned(user.id)
            }
            if not ids <= owned:
                raise LookupError("Channel not found")
            await session.execute(
                delete(DeviceNotificationChannel).where(
                    DeviceNotificationChannel.device_id == device_id,
                    DeviceNotificationChannel.user_id == user.id,
                    DeviceNotificationChannel.channel_id.not_in(ids),
                )
            )
            for channel_id in sorted(ids):
                await session.execute(
                    insert(DeviceNotificationChannel)
                    .values(device_id=device_id, channel_id=channel_id, user_id=user.id)
                    .on_conflict_do_nothing()
                )
