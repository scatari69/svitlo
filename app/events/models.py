from dataclasses import dataclass, field
from datetime import date, datetime
from uuid import UUID, uuid4

from app.models.enums import MonitorHealth, PowerState
from app.services.time import aware_utc


@dataclass(frozen=True, kw_only=True)
class DomainEvent:
    detected_at: datetime
    event_id: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        object.__setattr__(self, "detected_at", aware_utc(self.detected_at))


@dataclass(frozen=True, kw_only=True)
class PowerStateChanged(DomainEvent):
    user_id: int
    device_id: int
    previous_state: PowerState
    new_state: PowerState


@dataclass(frozen=True, kw_only=True)
class MonitorHealthChanged(DomainEvent):
    user_id: int
    device_id: int
    previous_health: MonitorHealth
    new_health: MonitorHealth


@dataclass(frozen=True, kw_only=True)
class ScheduleChanged(DomainEvent):
    provider: str
    region: str
    queue: str
    schedule_date: date
    previous_version_id: int | None
    new_version_id: int
