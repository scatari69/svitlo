from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import Settings


def create_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        settings.database_url.get_secret_value(),
        pool_pre_ping=True,
        hide_parameters=True,
        pool_timeout=settings.health_timeout,
        connect_args={
            "timeout": settings.health_timeout,
            "command_timeout": 30,
            "server_settings": {
                "timezone": "UTC",
                "statement_timeout": "30000",
                "lock_timeout": "10000",
            },
        },
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
