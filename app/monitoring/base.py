from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Protocol

from app.models.enums import MonitorHealth, PowerState
from app.services.time import aware_utc

type DiagnosticValue = str | int | float | bool | None


@dataclass(frozen=True)
class MonitorResult:
    state: PowerState
    health: MonitorHealth
    detected_at: datetime
    # Adapters supply safe codes/counters, never credentials or raw exception messages.
    metadata: Mapping[str, DiagnosticValue] = field(default_factory=dict)

    observed_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "detected_at", aware_utc(self.detected_at))
        observed_at = aware_utc(self.observed_at or self.detected_at)
        if observed_at < self.detected_at:
            raise ValueError("Detection cannot follow the observation")
        object.__setattr__(self, "observed_at", observed_at)
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


class PowerMonitor(Protocol):
    async def check(self) -> MonitorResult: ...
