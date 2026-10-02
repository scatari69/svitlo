from enum import StrEnum


class MonitoringType(StrEnum):
    PING = "ping"
    SNMP = "snmp"
    HOME_ASSISTANT = "home_assistant"


class PowerState(StrEnum):
    ON = "on"
    OFF = "off"
    UNKNOWN = "unknown"


class MonitorHealth(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class ChannelType(StrEnum):
    PRIVATE = "private"
    GROUP = "group"
    SUPERGROUP = "supergroup"
    CHANNEL = "channel"


class SnmpMode(StrEnum):
    INTERFACE = "interface"
    CUSTOM_OID = "custom_oid"


class HomeAssistantMode(StrEnum):
    WEBHOOK = "webhook"
    API = "api"
