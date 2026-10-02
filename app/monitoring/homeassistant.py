import re
from datetime import UTC, datetime

import httpx

from app.models.enums import MonitorHealth, PowerState
from app.monitoring.base import MonitorResult
from app.services.validation import validate_url


class HomeAssistantMonitor:
    def __init__(self, url: str, access_token: str, entity_id: str) -> None:
        self.url = validate_url(url)
        if not re.fullmatch(r"[a-z_][a-z0-9_]*\.[a-z0-9_]+", entity_id) or not access_token:
            raise ValueError("Invalid Home Assistant configuration")
        self.entity_id = entity_id
        self.access_token = access_token

    async def check(self) -> MonitorResult:
        now = datetime.now(UTC)
        try:
            async with httpx.AsyncClient(
                timeout=8, follow_redirects=False, trust_env=False
            ) as client:
                response = await client.get(
                    f"{self.url}/api/states/{self.entity_id}",
                    headers={"Authorization": f"Bearer {self.access_token}"},
                )
                response.raise_for_status()
                data = response.json()
            if not isinstance(data, dict):
                raise ValueError("Invalid Home Assistant response")
            value = data.get("state")
            state = {"on": PowerState.ON, "off": PowerState.OFF}.get(
                value if isinstance(value, str) else "", PowerState.UNKNOWN
            )
        except Exception:
            return MonitorResult(PowerState.UNKNOWN, MonitorHealth.UNAVAILABLE, now)
        return MonitorResult(
            state,
            MonitorHealth.DEGRADED if state == PowerState.UNKNOWN else MonitorHealth.HEALTHY,
            now,
        )
