import json
import re
from collections.abc import Awaitable
from datetime import UTC, datetime
from hashlib import sha256
from typing import cast

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.events.bus import EventBus
from app.events.models import MonitorHealthChanged, PowerStateChanged
from app.models import Device, HomeAssistantDeviceConfig
from app.models.enums import HomeAssistantMode, MonitorHealth, PowerState
from app.services.power import record_observation


class WebhookNotFound(Exception):
    pass


class WebhookRateLimited(Exception):
    pass


class HomeAssistantWebhookService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        redis: Redis,
        bus: EventBus,
    ) -> None:
        self.sessions, self.redis, self.bus = sessions, redis, bus

    async def receive(self, token: str, state: PowerState) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token) or state == PowerState.UNKNOWN:
            raise WebhookNotFound
        digest = sha256(token.encode()).hexdigest()
        # Atomic TTL and counter avoid immortal rate-limit keys if a worker is interrupted.
        count = await cast(
            Awaitable[str],
            self.redis.eval(
                "local n=redis.call('INCR',KEYS[1]); "
                "if n==1 then redis.call('EXPIRE',KEYS[1],60) end; return n",
                1,
                f"ha:rate:{digest}",
            ),
        )
        if int(count) > 30:
            raise WebhookRateLimited
        events: list[PowerStateChanged | MonitorHealthChanged] = []
        async with self.sessions.begin() as session:
            device = await session.scalar(
                select(Device)
                .join(HomeAssistantDeviceConfig, HomeAssistantDeviceConfig.device_id == Device.id)
                .where(
                    HomeAssistantDeviceConfig.mode == HomeAssistantMode.WEBHOOK,
                    HomeAssistantDeviceConfig.webhook_token_hash == digest,
                )
                .with_for_update(of=Device)
                .execution_options(populate_existing=True)
            )
            if device is not None and (not device.enabled or device.deleted_at is not None):
                raise WebhookNotFound
            draft = device is None
            if device is not None:
                now = datetime.now(UTC)
                device.monitor_failure_count = 0
                device.monitor_failure_started_at = None
                events = await record_observation(
                    session,
                    device,
                    state=state,
                    health=MonitorHealth.HEALTHY,
                    detected_at=now,
                    observed_at=now,
                    checked_at=now,
                    responded=True,
                    confirmed=True,
                )
        if draft:
            # No database transaction/connection is retained while checking the Redis draft.
            updated = await self.redis.set(
                f"ha:setup:{digest}",
                json.dumps({"state": state.value, "received_at": datetime.now(UTC).isoformat()}),
                xx=True,
                keepttl=True,
            )
            if not updated:
                raise WebhookNotFound
            return
        for event in events:
            await self.bus.publish(event)
