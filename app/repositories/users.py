from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_or_create(self, telegram_user_id: int) -> User:
        if not 0 < telegram_user_id < 2**63:
            raise ValueError("Telegram user ID must be a positive signed 64-bit integer")
        await self.session.execute(
            insert(User)
            .values(telegram_user_id=telegram_user_id)
            .on_conflict_do_nothing(index_elements=[User.telegram_user_id])
        )
        user = await self.session.scalar(
            select(User).where(User.telegram_user_id == telegram_user_id)
        )
        if user is None:
            raise RuntimeError("User was not found after upsert")
        return user
