import asyncio
import json
import logging
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from itertools import batched

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.events.bus import EventBus
from app.events.models import ScheduleChanged
from app.models import ScheduleSubscription, ScheduleVersion
from app.repositories.schedules import ScheduleRepository
from app.schedules.diff import intervals
from app.schedules.models import KYIV, DaySchedule, Freshness, ScheduleState
from app.schedules.service import ScheduleService

logger = logging.getLogger(__name__)


def schedule_hash(schedule: DaySchedule) -> str:
    slots = sorted(
        (slot for state in ScheduleState for slot in intervals(schedule, state)),
        key=lambda slot: slot.start,
    )
    canonical = schedule.model_copy(update={"slots": tuple(slots)}).model_dump(mode="json")
    return sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class ScheduleWorker:
    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], schedules: ScheduleService, bus: EventBus
    ) -> None:
        self.sessions, self.schedules, self.bus = sessions, schedules, bus
        self._limit = asyncio.Semaphore(8)

    async def run(self) -> None:
        while True:
            try:
                await self.refresh()
            except Exception:
                logger.warning("Schedule discovery unavailable", extra={"worker": "schedules"})
            await asyncio.sleep(60)

    async def refresh(self) -> None:
        async with self.sessions.begin() as session:
            sources = (
                await session.execute(
                    select(
                        ScheduleSubscription.provider,
                        ScheduleSubscription.region,
                        ScheduleSubscription.queue,
                    )
                    .where(ScheduleSubscription.enabled.is_(True))
                    .distinct()
                )
            ).all()
        today = datetime.now(KYIV).date()
        jobs = (
            (provider, region, queue, day)
            for provider, region, queue in sources
            for day in (today, today + timedelta(days=1))
        )
        for batch in batched(jobs, 8, strict=False):
            await asyncio.gather(*(self._refresh_source(*source) for source in batch))

    async def _refresh_source(self, provider: str, region: str, queue: str, day: date) -> None:
        async with self._limit:
            try:
                result = await self.schedules.get_schedule(provider, region, queue, day)
                if (
                    result.data is None
                    or result.freshness in {Freshness.STALE, Freshness.UNAVAILABLE}
                    or result.checked_at is None
                ):
                    return
                schedule = result.data
                if (schedule.provider, schedule.region, schedule.group, schedule.date) != (
                    provider,
                    region,
                    queue,
                    day,
                ):
                    raise ValueError("Schedule source mismatch")
                content_hash = schedule_hash(schedule)
                source_key = json.dumps([provider, region, queue, day.isoformat()])
                lock_id = int.from_bytes(sha256(source_key.encode()).digest()[:8], signed=True)
                event = None
                async with self.sessions.begin() as session:
                    # Cross-process serialization covers only version persistence, never HTTP.
                    await session.execute(
                        text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_id}
                    )
                    previous = await ScheduleRepository(session).latest_version(
                        provider, region, queue, day
                    )
                    if previous is not None and (
                        previous.content_hash == content_hash
                        or previous.fetched_at >= result.checked_at
                    ):
                        return
                    version = ScheduleVersion(
                        provider=provider,
                        region=region,
                        queue=queue,
                        schedule_date=day,
                        content_hash=content_hash,
                        normalized_content=schedule.model_dump(mode="json"),
                        fetched_at=result.checked_at,
                        source_metadata={"freshness": result.freshness.value},
                    )
                    session.add(version)
                    await session.flush()
                    if previous is not None:
                        event = ScheduleChanged(
                            provider=provider,
                            region=region,
                            queue=queue,
                            schedule_date=day,
                            previous_version_id=previous.id,
                            new_version_id=version.id,
                            detected_at=datetime.now(UTC),
                        )
                if event is not None:
                    await self.bus.publish(event)
            except Exception:
                logger.warning(
                    "Schedule refresh unavailable",
                    extra={"worker": "schedules", "provider": provider},
                )
