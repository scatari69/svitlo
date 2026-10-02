import asyncio
import re
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any

import aiohttp

from app.models.enums import MonitorHealth, PowerState
from app.monitoring.base import MonitorResult
from app.services.validation import validate_url


def validate_states(on_state: str, off_state: str) -> None:
    if on_state == off_state or any(
        not value
        or len(value) > 255
        or value in {"unknown", "unavailable"}
        or any(ord(char) < 32 for char in value)
        for value in (on_state, off_state)
    ):
        raise ValueError("Invalid Home Assistant state mapping")


async def reject_redirect(*_: object) -> None:
    raise ConnectionError("Home Assistant redirects are not allowed")


class HomeAssistantWebSocketMonitor:
    def __init__(
        self,
        url: str,
        access_token: str,
        entity_id: str,
        on_state: str = "on",
        off_state: str = "off",
    ) -> None:
        self.url = validate_url(url)
        if not re.fullmatch(r"[a-z_][a-z0-9_]*\.[a-z0-9_]+", entity_id) or not access_token:
            raise ValueError("Invalid Home Assistant configuration")
        validate_states(on_state, off_state)
        self._access_token = access_token
        self.entity_id, self.on_state, self.off_state = entity_id, on_state, off_state

    def result(self, value: object) -> MonitorResult:
        state = {self.on_state: PowerState.ON, self.off_state: PowerState.OFF}.get(
            value if isinstance(value, str) else "", PowerState.UNKNOWN
        )
        return MonitorResult(
            state,
            MonitorHealth.DEGRADED if state == PowerState.UNKNOWN else MonitorHealth.HEALTHY,
            datetime.now(UTC),
            {"backend": "home_assistant", "responded": True, "confirmed": True},
        )

    @staticmethod
    async def receive(socket: aiohttp.ClientWebSocketResponse) -> dict[str, Any]:
        data = await socket.receive_json()
        if not isinstance(data, dict):
            raise ValueError("Invalid Home Assistant message")
        return data

    async def stream(self) -> AsyncGenerator[MonitorResult]:
        trace = aiohttp.TraceConfig()
        trace.on_request_redirect.append(reject_redirect)
        async with aiohttp.ClientSession(
            trace_configs=[trace], trust_env=False, timeout=aiohttp.ClientTimeout(total=10)
        ) as session:
            async with session.ws_connect(
                f"{self.url}/api/websocket",
                heartbeat=30,
                max_msg_size=4 * 1024 * 1024,
                timeout=aiohttp.ClientWSTimeout(ws_close=5),
            ) as socket:
                async with asyncio.timeout(8):
                    if (await self.receive(socket)).get("type") != "auth_required":
                        raise ConnectionError("Invalid Home Assistant handshake")
                    await socket.send_json({"type": "auth", "access_token": self._access_token})
                    if (await self.receive(socket)).get("type") != "auth_ok":
                        raise ConnectionError("Home Assistant authentication failed")
                    await socket.send_json(
                        {"id": 1, "type": "subscribe_events", "event_type": "state_changed"}
                    )
                    response = await self.receive(socket)
                    if (
                        response.get("id") != 1
                        or response.get("type") != "result"
                        or response.get("success") is not True
                    ):
                        raise ConnectionError("Home Assistant subscription failed")
                    await socket.send_json({"id": 2, "type": "get_states"})
                    while True:
                        response = await self.receive(socket)
                        if response.get("id") == 2 and response.get("type") == "result":
                            break
                    if response.get("success") is not True or not isinstance(
                        response.get("result"), list
                    ):
                        raise ConnectionError("Home Assistant snapshot failed")
                    states = [
                        item
                        for item in response["result"]
                        if isinstance(item, dict) and item.get("entity_id") == self.entity_id
                    ]
                    if len(states) != 1:
                        raise ConnectionError("Home Assistant entity not found")
                # Snapshot is observed now, not backdated to HA's possibly old last_changed.
                yield self.result(states[0].get("state"))
                while True:
                    data = await self.receive(socket)
                    event = data.get("event")
                    if (
                        data.get("type") != "event"
                        or data.get("id") != 1
                        or not isinstance(event, dict)
                        or event.get("event_type") != "state_changed"
                    ):
                        continue
                    payload = event.get("data")
                    if not isinstance(payload, dict) or payload.get("entity_id") != self.entity_id:
                        continue
                    new_state = payload.get("new_state")
                    yield self.result(
                        new_state.get("state") if isinstance(new_state, dict) else None
                    )

    async def check(self) -> MonitorResult:
        stream = self.stream()
        try:
            async with asyncio.timeout(10):
                return await anext(stream)
        except Exception:
            return self.unavailable()
        finally:
            await stream.aclose()

    @staticmethod
    def unavailable() -> MonitorResult:
        return MonitorResult(
            PowerState.UNKNOWN,
            MonitorHealth.UNAVAILABLE,
            datetime.now(UTC),
            {"backend": "home_assistant", "reason": "connection_unavailable"},
        )
