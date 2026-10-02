import asyncio
import logging

from pydantic import SecretStr

from app.monitoring.homeassistant_ws import HomeAssistantWebSocketMonitor
from app.monitoring.service import HomeAssistantTarget, MonitoringService
from app.services.secrets import decrypt_secret

logger = logging.getLogger(__name__)


class HomeAssistantWorker:
    """Device tasks supervised by the existing monitoring leader's lifecycle."""

    def __init__(self, service: MonitoringService, encryption_key: SecretStr | None) -> None:
        self.service, self.encryption_key = service, encryption_key
        self._tasks: dict[int, tuple[HomeAssistantTarget, asyncio.Task[None]]] = {}

    async def refresh(self) -> None:
        targets = {
            target.device_id: target for target in await self.service.homeassistant_targets()
        }
        for device_id, (previous, task) in list(self._tasks.items()):
            if task.done() or targets.get(device_id) != previous:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                del self._tasks[device_id]
        for device_id, target in targets.items():
            if device_id not in self._tasks:
                self._tasks[device_id] = (
                    target,
                    asyncio.create_task(
                        self._monitor(target), name=f"homeassistant-device-{device_id}"
                    ),
                )

    async def _monitor(self, target: HomeAssistantTarget) -> None:
        delay = 1
        while True:
            stream = None
            communicating = True
            try:
                monitor = HomeAssistantWebSocketMonitor(
                    target.url,
                    decrypt_secret(self.encryption_key, target.access_token_encrypted),
                    target.entity_id,
                    target.on_state,
                    target.off_state,
                )
                stream = monitor.stream()
                async for result in stream:
                    communicating = False
                    if not await self.service.apply_homeassistant_result(target, result):
                        return
                    communicating = True
                    delay = 1
            except Exception:
                # Neither exception text nor transport frames are safe to log.
                logger.warning(
                    "Home Assistant attempt failed backend=home_assistant device_id=%s",
                    target.device_id,
                )
            finally:
                if stream is not None:
                    await stream.aclose()
            if communicating:
                try:
                    if not await self.service.apply_homeassistant_result(
                        target, HomeAssistantWebSocketMonitor.unavailable()
                    ):
                        return
                except Exception:
                    logger.warning(
                        "Home Assistant result persistence failed device_id=%s", target.device_id
                    )
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)

    async def stop(self) -> None:
        tasks = [task for _, task in self._tasks.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
