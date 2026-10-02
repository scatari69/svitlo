import asyncio
from collections.abc import Awaitable, Callable
from contextvars import ContextVar

from aiogram import Bot
from aiogram.client.session.middlewares.base import BaseRequestMiddleware, NextRequestMiddlewareType
from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import Response, SendMessage, SendPhoto, TelegramMethod
from aiogram.methods.base import TelegramType

_retry_owned = ContextVar("telegram_retry_owned", default=False)


async def retry_telegram[T](
    operation: Callable[[], Awaitable[T]], *, independent: bool = False
) -> T:
    """Retry only explicit flood rejections, never ambiguous network failures."""
    if _retry_owned.get() and not independent:
        return await operation()
    token = _retry_owned.set(True)
    try:
        try:
            return await operation()
        except TelegramRetryAfter as error:
            if not 0 < error.retry_after <= 30:
                raise
            await asyncio.sleep(error.retry_after)
            return await operation()
    finally:
        _retry_owned.reset(token)


class TelegramFloodControl(BaseRequestMiddleware):
    async def __call__(
        self,
        make_request: NextRequestMiddlewareType[TelegramType],
        bot: Bot,
        method: TelegramMethod[TelegramType],
    ) -> Response[TelegramType]:
        return await retry_telegram(
            lambda: make_request(bot, method),
            independent=not isinstance(method, (SendMessage, SendPhoto)),
        )
