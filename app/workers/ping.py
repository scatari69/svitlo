import asyncio
import logging
from datetime import UTC, datetime

from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.models.enums import MonitorHealth, PowerState
from app.monitoring.base import MonitorResult
from app.monitoring.service import MonitoringService, PingTarget, SnmpTarget
from app.workers.homeassistant import HomeAssistantWorker

logger = logging.getLogger(__name__)
LEADER_LOCK = 0x535649544C4F


class PingWorker:
    def __init__(
        self,
        engine: AsyncEngine,
        service: MonitoringService,
        *,
        refresh_seconds: float = 5,
        homeassistant: HomeAssistantWorker | None = None,
        encryption_key: SecretStr | None = None,
    ) -> None:
        self.engine, self.service = engine, service
        self.refresh_seconds = refresh_seconds
        self.homeassistant = homeassistant
        self.encryption_key = encryption_key
        self._tasks: dict[int, tuple[PingTarget | SnmpTarget, asyncio.Task[None]]] = {}
        self._checks = asyncio.Semaphore(32)

    async def run(self) -> None:
        while True:
            try:
                async with self.engine.connect() as connection:
                    acquired = False
                    try:
                        acquired = bool(
                            await connection.scalar(
                                text("SELECT pg_try_advisory_lock(:key)"),
                                {"key": LEADER_LOCK},
                            )
                        )
                        await connection.commit()
                        if acquired:
                            logger.info("Monitoring scheduler started worker=monitoring")
                            await self._lead(connection)
                    finally:
                        if acquired:
                            await self._stop_devices()
                            try:
                                await connection.execute(
                                    text("SELECT pg_advisory_unlock(:key)"),
                                    {"key": LEADER_LOCK},
                                )
                                await connection.commit()
                            except Exception:
                                # Never return a connection retaining a session lock to the pool.
                                await connection.invalidate()
            except Exception:
                logger.exception("Ping scheduler unavailable worker=ping")
            await asyncio.sleep(self.refresh_seconds)

    async def _lead(self, connection: AsyncConnection) -> None:
        while True:
            # Connection loss stops child checks before leadership is reacquired.
            await connection.execute(text("SELECT 1"))
            await connection.commit()
            try:
                await self.refresh()
            except Exception:
                # A discovery failure must not tear down otherwise healthy device tasks.
                logger.warning("Monitor discovery unavailable", extra={"worker": "monitoring"})
            if self.homeassistant is not None:
                try:
                    await self.homeassistant.refresh()
                except Exception:
                    logger.warning("Home Assistant discovery unavailable worker=home_assistant")
            await asyncio.sleep(self.refresh_seconds)

    async def refresh(self) -> None:
        discovered: list[PingTarget | SnmpTarget] = [*await self.service.ping_targets()]
        discovered.extend(await self.service.snmp_targets())
        targets = {target.device_id: target for target in discovered}
        for device_id, (previous, task) in list(self._tasks.items()):
            if task.done() or targets.get(device_id) != previous:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                del self._tasks[device_id]
        for device_id, target in targets.items():
            if device_id not in self._tasks:
                task = asyncio.create_task(
                    self._monitor(target), name=f"monitor-device-{device_id}"
                )
                self._tasks[device_id] = target, task

    async def _monitor(self, target: PingTarget | SnmpTarget) -> None:
        checking = True
        try:
            monitor = (
                target.monitor(self.encryption_key)
                if isinstance(target, SnmpTarget)
                else target.monitor()
            )
            checking = False
            while True:
                started = asyncio.get_running_loop().time()
                async with self._checks:
                    checking = True
                    result = await monitor.check()
                    checking = False
                # Database/event consumers (including Telegram) do not occupy network-check slots.
                applied = (
                    await self.service.apply_snmp_result(target, result)
                    if isinstance(target, SnmpTarget)
                    else await self.service.apply_ping_result(target, result)
                )
                if not applied:
                    return
                logger.debug(
                    "Monitor checked device_id=%s state=%s health=%s diagnostics=%s",
                    target.device_id,
                    result.state.value,
                    result.health.value,
                    dict(result.metadata),
                )
                # Skip missed ticks rather than accumulating overlapping checks.
                elapsed = asyncio.get_running_loop().time() - started
                await asyncio.sleep(max(0, monitor.interval_seconds - elapsed))
        except Exception:
            logger.warning("Monitor device failed device_id=%s", target.device_id)
            # Database/event-consumer failures do not imply monitor communication failure.
            if not checking:
                return
            # Initialization failures (including decryption/dependency errors) also affect health.
            try:
                result = MonitorResult(
                    PowerState.UNKNOWN,
                    MonitorHealth.UNAVAILABLE,
                    datetime.now(UTC),
                    {"reason": "worker_error"},
                )
                if isinstance(target, SnmpTarget):
                    await self.service.apply_snmp_result(target, result)
                else:
                    await self.service.apply_ping_result(target, result)
            except Exception:
                logger.warning(
                    "Monitor failure persistence unavailable device_id=%s", target.device_id
                )
            # Discovery retries with fresh persisted state; other device tasks keep running.

    async def _stop_devices(self) -> None:
        tasks = [task for _, task in self._tasks.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        if self.homeassistant is not None:
            await self.homeassistant.stop()
