from datetime import UTC, datetime, timedelta

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    LargeBinary,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import UTCDateTime
from app.models.common import Timestamps, enum_type, id_type
from app.models.enums import HomeAssistantMode, MonitorHealth, MonitoringType, PowerState, SnmpMode


class Device(Timestamps, Base):
    __tablename__ = "devices"
    __table_args__ = (
        CheckConstraint("length(trim(name)) > 0", name="ck_devices_name"),
        CheckConstraint("monitor_failure_count >= 0", name="ck_devices_monitor_failures"),
        UniqueConstraint("id", "user_id", name="uq_devices_id_owner"),
        UniqueConstraint("id", "monitoring_type", name="uq_devices_id_backend"),
        Index("ix_devices_owner_active", "user_id", "deleted_at", "enabled"),
        Index(
            "ix_devices_monitoring_enabled",
            "monitoring_type",
            "id",
            postgresql_where=text("enabled IS TRUE AND deleted_at IS NULL"),
            sqlite_where=text("enabled IS TRUE AND deleted_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(id_type, Identity(), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    name: Mapped[str] = mapped_column(String(255))
    monitoring_type: Mapped[MonitoringType] = mapped_column(
        enum_type(MonitoringType, "monitoring_type")
    )
    enabled: Mapped[bool] = mapped_column(default=True, server_default=text("true"))
    current_power_state: Mapped[PowerState] = mapped_column(
        enum_type(PowerState, "device_power_state"),
        default=PowerState.UNKNOWN,
        server_default="unknown",
    )
    monitor_health: Mapped[MonitorHealth] = mapped_column(
        enum_type(MonitorHealth, "monitor_health"),
        default=MonitorHealth.UNAVAILABLE,
        server_default="unavailable",
    )
    monitor_failure_count: Mapped[int] = mapped_column(default=0, server_default="0")
    monitor_failure_started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    monitor_health_changed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_monitor_observation_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_successful_check_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_state_changed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class PingDeviceConfig(Timestamps, Base):
    __tablename__ = "ping_device_configs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["device_id", "monitoring_type"],
            ["devices.id", "devices.monitoring_type"],
            ondelete="CASCADE",
        ),
        CheckConstraint("monitoring_type = 'ping'", name="ck_ping_backend"),
        CheckConstraint("length(trim(host)) > 0", name="ck_ping_host"),
        CheckConstraint("interval_seconds > 0 AND timeout_seconds > 0", name="ck_ping_timing"),
        CheckConstraint(
            "failure_threshold > 1 AND recovery_threshold > 0", name="ck_ping_thresholds"
        ),
    )

    device_id: Mapped[int] = mapped_column(id_type, primary_key=True)
    monitoring_type: Mapped[str] = mapped_column(String(14), default="ping", server_default="ping")
    host: Mapped[str] = mapped_column(String(255))
    interval_seconds: Mapped[float] = mapped_column(default=10, server_default="10")
    timeout_seconds: Mapped[float] = mapped_column(default=2, server_default="2")
    failure_threshold: Mapped[int] = mapped_column(default=3, server_default="3")
    recovery_threshold: Mapped[int] = mapped_column(default=2, server_default="2")


class SnmpDeviceConfig(Timestamps, Base):
    __tablename__ = "snmp_device_configs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["device_id", "monitoring_type"],
            ["devices.id", "devices.monitoring_type"],
            ondelete="CASCADE",
        ),
        CheckConstraint("monitoring_type = 'snmp'", name="ck_snmp_backend"),
        CheckConstraint("length(trim(host)) > 0", name="ck_snmp_host"),
        CheckConstraint("port BETWEEN 1 AND 65535", name="ck_snmp_port"),
        CheckConstraint("polling_interval_seconds > 0", name="ck_snmp_interval"),
        CheckConstraint("length(community_encrypted) > 0", name="ck_snmp_community"),
        CheckConstraint(
            "(mode = 'interface' AND interface_index IS NOT NULL "
            "AND interface_index > 0 AND oid IS NULL "
            "AND on_value IS NULL AND off_value IS NULL) OR "
            "(mode = 'custom_oid' AND interface_index IS NULL AND oid IS NOT NULL "
            "AND length(trim(oid)) > 0 "
            "AND on_value IS NOT NULL AND off_value IS NOT NULL AND on_value <> off_value)",
            name="ck_snmp_mode_config",
        ),
    )

    device_id: Mapped[int] = mapped_column(id_type, primary_key=True)
    monitoring_type: Mapped[str] = mapped_column(String(14), default="snmp", server_default="snmp")
    host: Mapped[str] = mapped_column(String(255))
    port: Mapped[int] = mapped_column(default=161, server_default="161")
    community_encrypted: Mapped[bytes] = mapped_column(LargeBinary)
    polling_interval_seconds: Mapped[float] = mapped_column(default=10, server_default="10")
    mode: Mapped[SnmpMode] = mapped_column(enum_type(SnmpMode, "snmp_mode"))
    interface_index: Mapped[int | None]
    oid: Mapped[str | None] = mapped_column(String(255))
    on_value: Mapped[str | None] = mapped_column(String(255))
    off_value: Mapped[str | None] = mapped_column(String(255))


class HomeAssistantDeviceConfig(Timestamps, Base):
    __tablename__ = "homeassistant_device_configs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["device_id", "monitoring_type"],
            ["devices.id", "devices.monitoring_type"],
            ondelete="CASCADE",
        ),
        CheckConstraint("monitoring_type = 'home_assistant'", name="ck_ha_backend"),
        CheckConstraint(
            "length(trim(on_state)) > 0 AND length(trim(off_state)) > 0 "
            "AND on_state <> off_state AND on_state NOT IN ('unknown', 'unavailable') "
            "AND off_state NOT IN ('unknown', 'unavailable')",
            name="ck_ha_state_mapping",
        ),
        CheckConstraint(
            "(mode = 'webhook' AND webhook_token_hash IS NOT NULL "
            "AND length(webhook_token_hash) = 64 AND url IS NULL "
            "AND access_token_encrypted IS NULL AND entity_id IS NULL) OR "
            "(mode = 'api' AND webhook_token_hash IS NULL AND url IS NOT NULL "
            "AND (url LIKE 'http://%' OR url LIKE 'https://%') "
            "AND access_token_encrypted IS NOT NULL AND length(access_token_encrypted) > 0 "
            "AND entity_id IS NOT NULL AND length(trim(entity_id)) > 0)",
            name="ck_ha_mode_config",
        ),
    )

    device_id: Mapped[int] = mapped_column(id_type, primary_key=True)
    monitoring_type: Mapped[str] = mapped_column(
        String(14), default="home_assistant", server_default="home_assistant"
    )
    mode: Mapped[HomeAssistantMode] = mapped_column(enum_type(HomeAssistantMode, "ha_mode"))
    webhook_token_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    url: Mapped[str | None] = mapped_column(String(2048))
    access_token_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary)
    entity_id: Mapped[str | None] = mapped_column(String(255))
    on_state: Mapped[str] = mapped_column(String(255), default="on", server_default="on")
    off_state: Mapped[str] = mapped_column(String(255), default="off", server_default="off")


class PowerInterval(Base):
    __tablename__ = "power_intervals"
    __table_args__ = (
        CheckConstraint("ended_at IS NULL OR ended_at >= started_at", name="ck_interval_order"),
        Index("ix_power_intervals_device_start", "device_id", "started_at"),
        Index(
            "uq_power_intervals_open_device",
            "device_id",
            unique=True,
            postgresql_where=text("ended_at IS NULL"),
            sqlite_where=text("ended_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(id_type, Identity(), primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="RESTRICT"))
    state: Mapped[PowerState] = mapped_column(enum_type(PowerState, "interval_power_state"))
    started_at: Mapped[datetime] = mapped_column(UTCDateTime())
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    def duration(self, *, until: datetime | None = None) -> timedelta:
        end = self.ended_at if self.ended_at is not None else until
        if end is None:
            raise ValueError("Open intervals require an explicit end time")
        if self.started_at.utcoffset() is None or end.utcoffset() is None or end < self.started_at:
            raise ValueError("Interval timestamps must be aware and ordered")
        return end.astimezone(UTC) - self.started_at.astimezone(UTC)
