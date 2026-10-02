from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import SecretStr
from sqlalchemy import create_engine
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Dialect
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.base import Base
from app.models import Device, NotificationChannel, ScheduleSubscription, User
from app.models.enums import ChannelType, MonitoringType


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        database_url=SecretStr("postgresql+asyncpg://user:database-secret@localhost/svitlo"),
        redis_url=SecretStr("redis://:redis-secret@localhost:6379/0"),
        telegram_bot_token=None,
        bot_enabled=False,
        monitoring_enabled=False,
    )


@pytest.fixture
def db_session() -> Iterator[Session]:
    engine = create_engine("sqlite://")
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        connection.commit()
        Base.metadata.create_all(connection)
        connection.commit()
        with Session(connection, expire_on_commit=False) as session:
            session.add_all(
                [User(id=1, telegram_user_id=2**40), User(id=2, telegram_user_id=2**40 + 1)]
            )
            session.flush()
            session.add_all(
                [
                    Device(id=1, user_id=1, name="Home", monitoring_type=MonitoringType.PING),
                    Device(id=2, user_id=1, name="Office", monitoring_type=MonitoringType.SNMP),
                    Device(
                        id=3,
                        user_id=1,
                        name="Parents",
                        monitoring_type=MonitoringType.HOME_ASSISTANT,
                    ),
                    Device(id=4, user_id=2, name="Other", monitoring_type=MonitoringType.PING),
                    NotificationChannel(
                        id=1,
                        user_id=1,
                        telegram_chat_id=-(2**40),
                        name="Group",
                        channel_type=ChannelType.SUPERGROUP,
                    ),
                    NotificationChannel(
                        id=2,
                        user_id=2,
                        telegram_chat_id=2**40,
                        name="Private",
                        channel_type=ChannelType.PRIVATE,
                    ),
                    ScheduleSubscription(
                        id=1, user_id=1, provider="test", region="kyiv", queue="1.2", name="Home"
                    ),
                ]
            )
            session.commit()
            yield session
    engine.dispose()


@pytest.fixture
def postgres_dialect() -> Dialect:
    return cast(Callable[[], Dialect], postgresql.dialect)()


@pytest.fixture
def sessions(db_session: Session) -> MagicMock:
    @asynccontextmanager
    async def transaction() -> AsyncIterator[AsyncMock]:
        with Session(db_session.get_bind(), expire_on_commit=False) as persisted:
            with persisted.begin():
                session = AsyncMock(spec=AsyncSession)
                for name in ("scalar", "scalars", "execute", "flush", "get", "delete"):
                    getattr(session, name).side_effect = getattr(persisted, name)
                session.add.side_effect = persisted.add
                yield session

    factory = MagicMock(spec=async_sessionmaker)
    factory.begin.side_effect = transaction
    return factory
