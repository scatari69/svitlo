"""ORM schema; importing this package registers every table with Base.metadata."""

from app.models.channel import DeviceNotificationChannel, NotificationChannel
from app.models.device import (
    Device,
    HomeAssistantDeviceConfig,
    PingDeviceConfig,
    PowerInterval,
    SnmpDeviceConfig,
)
from app.models.notification import (
    MonitorHealthNotificationState,
    PowerNotificationDelivery,
    ScheduleNotificationDelivery,
)
from app.models.report import ReportDelivery, ReportSettings
from app.models.schedule import ScheduleNotificationChannel, ScheduleSubscription, ScheduleVersion
from app.models.user import User

__all__ = [
    "Device",
    "DeviceNotificationChannel",
    "HomeAssistantDeviceConfig",
    "NotificationChannel",
    "MonitorHealthNotificationState",
    "PingDeviceConfig",
    "PowerInterval",
    "PowerNotificationDelivery",
    "ReportSettings",
    "ReportDelivery",
    "ScheduleNotificationChannel",
    "ScheduleNotificationDelivery",
    "ScheduleSubscription",
    "ScheduleVersion",
    "SnmpDeviceConfig",
    "User",
]
