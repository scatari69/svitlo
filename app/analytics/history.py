from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.events.models import PowerStateChanged
from app.repositories.power import PowerIntervalRepository


class HistoryHandler:
    """Persist actual state intervals atomically; analytics uses this history later."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def handle(self, event: PowerStateChanged) -> None:
        async with self.session_factory.begin() as session:
            await PowerIntervalRepository(session).record_transition(
                event.user_id,
                event.device_id,
                event.new_state,
                event.detected_at,
                previous_state=event.previous_state,
            )
