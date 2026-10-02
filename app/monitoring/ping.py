import asyncio
import math
from contextlib import suppress
from datetime import UTC, datetime

from app.models.enums import MonitorHealth, PowerState
from app.monitoring.base import MonitorResult
from app.services.validation import validate_host


async def ping_once(host: str, timeout_seconds: float) -> bool:
    host = validate_host(host)
    if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 30:
        raise ValueError("Invalid ping timeout")
    process = await asyncio.create_subprocess_exec(
        "ping",
        "-n",
        "-c",
        "1",
        "-W",
        str(timeout_seconds),
        "--",
        host,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async with asyncio.timeout(timeout_seconds + 1):
            code = await process.wait()
    except BaseException:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
        await process.wait()
        raise
    if code not in {0, 1}:
        raise OSError("Ping execution failed")
    return code == 0


class PingMonitor:
    def __init__(
        self,
        host: str,
        timeout_seconds: float = 2,
        failure_threshold: int = 3,
        recovery_threshold: int = 2,
        *,
        interval_seconds: float = 10,
        initial_state: PowerState = PowerState.UNKNOWN,
    ) -> None:
        self.host = validate_host(host)
        if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 30:
            raise ValueError("Invalid ping timeout")
        if not math.isfinite(interval_seconds) or not 0 < interval_seconds <= 86400:
            raise ValueError("Invalid ping interval")
        if not 2 <= failure_threshold <= 20 or not 1 <= recovery_threshold <= 20:
            raise ValueError("Invalid debounce thresholds")
        self.interval_seconds = interval_seconds
        self.timeout_seconds = timeout_seconds
        self.failure_threshold = failure_threshold
        self.recovery_threshold = recovery_threshold
        self.state = initial_state
        # ponytail: counters reset on restart; persist observation windows for exact crash recovery.
        self._last_success: bool | None = None
        self._count = 0
        self._sequence_started = datetime.now(UTC)

    async def check(self) -> MonitorResult:
        now = datetime.now(UTC)
        try:
            success = await ping_once(self.host, self.timeout_seconds)
        except Exception:
            self._last_success = None
            self._count = 0
            self.state = PowerState.UNKNOWN
            return MonitorResult(
                PowerState.UNKNOWN,
                MonitorHealth.UNAVAILABLE,
                now,
                {"reason": "execution_error", "confirmed": False},
                observed_at=now,
            )
        if success != self._last_success:
            self._count = 0
            self._sequence_started = now
        self._last_success = success
        self._count += 1
        threshold = self.recovery_threshold if success else self.failure_threshold
        if self._count >= threshold:
            self.state = PowerState.ON if success else PowerState.OFF
        return MonitorResult(
            self.state,
            MonitorHealth.HEALTHY if success else MonitorHealth.DEGRADED,
            self._sequence_started if self._count >= threshold else now,
            {
                "reason": "reply" if success else "no_reply",
                "responded": success,
                "consecutive_observations": self._count,
                "confirmation_threshold": threshold,
                "confirmed": self._count >= threshold,
            },
            observed_at=now,
        )
