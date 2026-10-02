import asyncio
from collections.abc import MutableMapping
from datetime import UTC, datetime, timedelta
from typing import cast
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from app.models.enums import MonitorHealth, PowerState
from app.monitoring.base import DiagnosticValue, MonitorResult
from app.monitoring.ping import PingMonitor, ping_once

NOW = datetime(2026, 10, 2, 12, tzinfo=UTC)


@pytest.mark.parametrize(
    ("initial", "observations", "expected"),
    [
        (
            PowerState.ON,
            [False, False, False, False],
            [PowerState.ON, PowerState.ON, PowerState.OFF, PowerState.OFF],
        ),
        (PowerState.OFF, [True, True, True], [PowerState.OFF, PowerState.ON, PowerState.ON]),
        (
            PowerState.ON,
            [False, False, True, False, False, False],
            [PowerState.ON] * 5 + [PowerState.OFF],
        ),
        (PowerState.OFF, [True, False, True, True], [PowerState.OFF] * 3 + [PowerState.ON]),
    ],
)
async def test_ping_confirmation_requires_consecutive_observations(
    initial: PowerState,
    observations: list[bool],
    expected: list[PowerState],
) -> None:
    monitor = PingMonitor("localhost", initial_state=initial)
    with patch("app.monitoring.ping.ping_once", side_effect=observations):
        results = [await monitor.check() for _ in observations]
    assert [result.state for result in results] == expected
    assert results[-1].metadata["confirmed"] is True
    assert monitor.interval_seconds == 10 and monitor.timeout_seconds == 2
    assert monitor.failure_threshold == 3 and monitor.recovery_threshold == 2


async def test_ping_uses_first_observation_timestamp_and_error_resets_sequence() -> None:
    monitor = PingMonitor("localhost", initial_state=PowerState.ON)
    with (
        patch(
            "app.monitoring.ping.ping_once",
            side_effect=[False, False, False, OSError("sensitive text"), True, True],
        ),
        patch("app.monitoring.ping.datetime") as clock,
    ):
        clock.now.side_effect = [NOW + timedelta(seconds=10 * i) for i in range(6)]
        results = [await monitor.check() for _ in range(6)]
    assert results[2].state == PowerState.OFF and results[2].detected_at == NOW
    assert results[3].state == PowerState.UNKNOWN and results[3].health == MonitorHealth.UNAVAILABLE
    assert "sensitive text" not in str(results[3].metadata)
    assert results[4].state == PowerState.UNKNOWN
    assert results[5].state == PowerState.ON and results[5].detected_at == NOW + timedelta(
        seconds=40
    )


async def test_subprocess_cancellation_kills_and_reaps_child() -> None:
    waiting = asyncio.Event()
    process = MagicMock()
    process.returncode = None
    calls = 0

    async def wait() -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            waiting.set()
            await asyncio.Event().wait()
        return -9

    from unittest.mock import AsyncMock

    process.wait = AsyncMock(side_effect=wait)
    with patch("app.monitoring.ping.asyncio.create_subprocess_exec", return_value=process):
        task = asyncio.create_task(ping_once("localhost", 2))
        async with asyncio.timeout(1):
            await waiting.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    process.kill.assert_called_once()
    assert calls == 2


@pytest.mark.parametrize(
    "options",
    [
        {"timeout_seconds": 0},
        {"timeout_seconds": float("nan")},
        {"interval_seconds": float("inf")},
        {"interval_seconds": -1},
        {"failure_threshold": 1},
        {"recovery_threshold": 0},
    ],
)
def test_ping_rejects_invalid_configuration(options: dict[str, float]) -> None:
    # Runtime validation also protects configurations originating outside Telegram.
    from typing import Any

    PingOptions = cast(dict[str, Any], options)
    with pytest.raises(ValueError):
        PingMonitor("localhost", **PingOptions)


def test_monitor_result_normalizes_utc_and_freezes_diagnostics() -> None:
    metadata: dict[str, DiagnosticValue] = {"responded": True}
    result = MonitorResult(
        PowerState.ON, MonitorHealth.HEALTHY, NOW.astimezone(ZoneInfo("Europe/Kyiv")), metadata
    )
    assert result.detected_at.tzinfo is UTC
    metadata["responded"] = False
    assert result.metadata["responded"] is True
    with pytest.raises(TypeError):
        cast(MutableMapping[str, DiagnosticValue], result.metadata)["responded"] = False
    with pytest.raises(ValueError):
        MonitorResult(PowerState.UNKNOWN, MonitorHealth.UNAVAILABLE, datetime(2026, 1, 1))
