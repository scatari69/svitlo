from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Device, DeviceNotificationChannel, NotificationChannel
from app.models.enums import ChannelType


@dataclass(frozen=True)
class ChannelView:
    id: int
    name: str
    enabled: bool


class NotificationChannelRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def ensure_private(self, user_id: int, telegram_chat_id: int) -> None:
        await self.session.execute(
            insert(NotificationChannel)
            .values(
                user_id=user_id,
                telegram_chat_id=telegram_chat_id,
                name="Особисті повідомлення",
                channel_type=ChannelType.PRIVATE,
            )
            .on_conflict_do_nothing(
                index_elements=[NotificationChannel.user_id, NotificationChannel.telegram_chat_id]
            )
        )

    async def list_owned(self, user_id: int) -> list[NotificationChannel]:
        query = select(NotificationChannel).where(NotificationChannel.user_id == user_id)
        return list((await self.session.scalars(query.order_by(NotificationChannel.id))).all())

    async def for_device(self, user_id: int, device_id: int) -> list[NotificationChannel]:
        query = (
            select(NotificationChannel)
            .join(
                DeviceNotificationChannel,
                DeviceNotificationChannel.channel_id == NotificationChannel.id,
            )
            .join(Device, Device.id == DeviceNotificationChannel.device_id)
            .where(
                Device.user_id == user_id,
                Device.id == device_id,
                Device.deleted_at.is_(None),
                DeviceNotificationChannel.user_id == user_id,
                NotificationChannel.user_id == user_id,
                NotificationChannel.enabled.is_(True),
            )
            .order_by(NotificationChannel.id)
        )
        return list((await self.session.scalars(query)).all())
