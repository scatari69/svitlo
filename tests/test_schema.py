import re
from datetime import UTC, date, datetime, timedelta
from importlib.util import module_from_spec, spec_from_file_location
from io import StringIO
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, delete, inspect, select
from sqlalchemy.engine import Dialect
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateIndex, CreateTable

from app.db.base import Base
from app.models import (
    Device,
    DeviceNotificationChannel,
    HomeAssistantDeviceConfig,
    NotificationChannel,
    PingDeviceConfig,
    PowerInterval,
    ReportSettings,
    ScheduleNotificationChannel,
    ScheduleSubscription,
    ScheduleVersion,
    SnmpDeviceConfig,
    User,
)
from app.models.enums import (
    HomeAssistantMode,
    MonitoringType,
    PowerState,
    SnmpMode,
)

NOW = datetime(2026, 10, 2, 12, tzinfo=UTC)


def test_defaults_64_bit_ids_and_safe_repr(db_session: Session) -> None:
    user = db_session.get(User, 1)
    channel = db_session.get(NotificationChannel, 1)
    device = db_session.get(Device, 1)
    assert user and user.telegram_user_id == 2**40
    assert channel and channel.telegram_chat_id == -(2**40)
    assert device and device.current_power_state == PowerState.UNKNOWN
    assert device.created_at.utcoffset() == timedelta(0)
    configs = [
        SnmpDeviceConfig(community_encrypted=b"secret-community"),
        HomeAssistantDeviceConfig(
            access_token_encrypted=b"secret-token", webhook_token_hash="secret-hash"
        ),
    ]
    assert all("secret" not in repr(config) for config in configs)


@pytest.mark.parametrize("state", [PowerState.ON, PowerState.OFF, PowerState.UNKNOWN])
def test_single_open_interval_constraint(db_session: Session, state: PowerState) -> None:
    db_session.add(PowerInterval(device_id=1, state=state, started_at=NOW))
    db_session.commit()
    db_session.add(PowerInterval(device_id=1, state=state, started_at=NOW + timedelta(seconds=1)))
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()
    current = db_session.scalar(select(PowerInterval))
    assert current
    current.ended_at = NOW + timedelta(minutes=2)
    db_session.flush()
    db_session.add(PowerInterval(device_id=1, state=state, started_at=current.ended_at))
    db_session.commit()


@pytest.mark.parametrize(
    "model",
    [
        User(telegram_user_id=2**40),
        User(telegram_user_id=0),
        Device(user_id=99, name="Missing owner", monitoring_type=MonitoringType.PING),
        Device(user_id=1, name=" ", monitoring_type=MonitoringType.PING),
        PingDeviceConfig(device_id=1, host="localhost", failure_threshold=1),
        PingDeviceConfig(device_id=2, host="localhost"),
        SnmpDeviceConfig(
            device_id=2, host="localhost", community_encrypted=b"x", mode=SnmpMode.INTERFACE
        ),
        SnmpDeviceConfig(
            device_id=2,
            host="localhost",
            community_encrypted=b"x",
            mode=SnmpMode.CUSTOM_OID,
            oid="1.2",
            on_value="1",
            off_value="1",
        ),
        HomeAssistantDeviceConfig(device_id=3, mode=HomeAssistantMode.WEBHOOK),
        HomeAssistantDeviceConfig(
            device_id=3,
            mode=HomeAssistantMode.API,
            url="ftp://localhost",
            access_token_encrypted=b"x",
            entity_id="switch.power",
        ),
        DeviceNotificationChannel(device_id=1, channel_id=2, user_id=1),
        ScheduleNotificationChannel(subscription_id=1, channel_id=2, user_id=1),
        ReportSettings(device_id=1, channel_id=1),
        PowerInterval(
            device_id=1, state=PowerState.OFF, started_at=NOW, ended_at=NOW - timedelta(seconds=1)
        ),
    ],
)
def test_database_constraints(db_session: Session, model: Base) -> None:
    db_session.add(model)
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_invalid_enum_and_naive_datetime_rejected(db_session: Session) -> None:
    for interval in [
        PowerInterval(device_id=1, state="invalid", started_at=NOW),
        PowerInterval(device_id=1, state=PowerState.ON, started_at=NOW.replace(tzinfo=None)),
    ]:
        db_session.add(interval)
        with pytest.raises(StatementError):
            db_session.flush()
        db_session.rollback()


def test_utc_normalization_and_duration_across_dst(db_session: Session) -> None:
    kyiv = ZoneInfo("Europe/Kyiv")
    start = datetime(2026, 10, 25, 2, tzinfo=kyiv)
    end = datetime(2026, 10, 25, 4, tzinfo=kyiv)
    interval = PowerInterval(device_id=1, state=PowerState.OFF, started_at=start, ended_at=end)
    assert interval.duration() == timedelta(hours=3)
    db_session.add(interval)
    db_session.commit()
    db_session.expire_all()
    loaded = db_session.scalar(select(PowerInterval))
    assert loaded
    assert loaded.started_at == start.astimezone(UTC)
    assert loaded.started_at.tzinfo == UTC
    assert loaded.duration() == timedelta(hours=3)
    open_interval = PowerInterval(state=PowerState.UNKNOWN, started_at=NOW)
    assert open_interval.duration(until=NOW + timedelta(minutes=3)) == timedelta(minutes=3)
    with pytest.raises(ValueError):
        open_interval.duration()


def test_backend_configs_and_report_flags(db_session: Session) -> None:
    db_session.add_all(
        [
            PingDeviceConfig(device_id=1, host="localhost"),
            SnmpDeviceConfig(
                device_id=2,
                host="localhost",
                community_encrypted=b"ciphertext",
                mode=SnmpMode.INTERFACE,
                interface_index=1,
            ),
            HomeAssistantDeviceConfig(
                device_id=3, mode=HomeAssistantMode.WEBHOOK, webhook_token_hash="a" * 64
            ),
            DeviceNotificationChannel(device_id=1, channel_id=1, user_id=1),
            ScheduleNotificationChannel(subscription_id=1, channel_id=1, user_id=1),
        ]
    )
    db_session.flush()
    reports = ReportSettings(device_id=1, channel_id=1, daily_enabled=True)
    db_session.add(reports)
    db_session.commit()
    assert reports.daily_enabled and not reports.weekly_enabled and not reports.monthly_enabled
    reports.daily_enabled = False
    db_session.commit()
    assert db_session.get(Device, 1) is not None


def test_intentional_deletion_and_history_retention(db_session: Session) -> None:
    db_session.add(PowerInterval(device_id=1, state=PowerState.ON, started_at=NOW))
    db_session.commit()
    with pytest.raises(IntegrityError):
        db_session.execute(delete(Device).where(Device.id == 1))
    db_session.rollback()
    with pytest.raises(IntegrityError):
        db_session.execute(delete(User).where(User.id == 1))
    db_session.rollback()
    db_session.add(PingDeviceConfig(device_id=4, host="localhost"))
    db_session.commit()
    db_session.execute(delete(Device).where(Device.id == 4))
    db_session.commit()
    assert db_session.get(PingDeviceConfig, 4) is None
    db_session.add(DeviceNotificationChannel(device_id=1, channel_id=1, user_id=1))
    db_session.flush()
    db_session.add(ReportSettings(device_id=1, channel_id=1))
    db_session.commit()
    db_session.execute(delete(NotificationChannel).where(NotificationChannel.id == 1))
    db_session.commit()
    assert db_session.get(ReportSettings, (1, 1)) is None
    assert db_session.scalar(select(PowerInterval)) is not None


def test_shared_schedule_snapshots_allow_content_reversions(db_session: Session) -> None:
    for offset, content_hash in enumerate(["a" * 64, "b" * 64, "a" * 64]):
        db_session.add(
            ScheduleVersion(
                provider="test",
                region="kyiv",
                queue="1.2",
                schedule_date=date(2026, 10, 2),
                content_hash=content_hash,
                normalized_content={"slots": []},
                fetched_at=NOW + timedelta(seconds=offset),
            )
        )
    db_session.commit()
    db_session.execute(delete(ScheduleSubscription).where(ScheduleSubscription.id == 1))
    db_session.commit()
    assert len(db_session.scalars(select(ScheduleVersion)).all()) == 3


def test_postgresql_ddl_and_migration_round_trip(postgres_dialect: Dialect) -> None:
    tables = [
        str(CreateTable(table).compile(dialect=postgres_dialect))
        for table in Base.metadata.sorted_tables
    ]
    ddl = "\n".join(tables)
    assert "telegram_user_id BIGINT" in ddl
    assert "telegram_chat_id BIGINT" in ddl
    assert "TIMESTAMP WITH TIME ZONE" in ddl
    assert "JSONB" in ddl
    indexes = Base.metadata.tables["power_intervals"].indexes
    index = next(index for index in indexes if index.name == "uq_power_intervals_open_device")
    assert "UNIQUE INDEX" in str(CreateIndex(index).compile(dialect=postgres_dialect))
    assert "WHERE ended_at IS NULL" in str(CreateIndex(index).compile(dialect=postgres_dialect))

    path = Path(__file__).parents[1] / "migrations/versions/0001_initial_schema.py"
    spec = spec_from_file_location("initial_schema", path)
    assert spec and spec.loader
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    next_path = path.with_name("0002_power_notification_deliveries.py")
    next_spec = spec_from_file_location("notification_migration", next_path)
    assert next_spec and next_spec.loader
    notification_migration = module_from_spec(next_spec)
    next_spec.loader.exec_module(notification_migration)
    ha_spec = spec_from_file_location(
        "ha_migration", path.with_name("0003_homeassistant_state_mapping.py")
    )
    assert ha_spec and ha_spec.loader
    ha_migration = module_from_spec(ha_spec)
    ha_spec.loader.exec_module(ha_migration)
    sub_spec = spec_from_file_location(
        "subscription_migration", path.with_name("0004_subscription_creation_key.py")
    )
    assert sub_spec and sub_spec.loader
    subscription_migration = module_from_spec(sub_spec)
    sub_spec.loader.exec_module(subscription_migration)
    delivery_spec = spec_from_file_location(
        "schedule_delivery_migration", path.with_name("0005_schedule_notification_deliveries.py")
    )
    assert delivery_spec and delivery_spec.loader
    delivery_migration = module_from_spec(delivery_spec)
    delivery_spec.loader.exec_module(delivery_migration)
    report_spec = spec_from_file_location(
        "report_migration", path.with_name("0006_report_scheduling.py")
    )
    assert report_spec and report_spec.loader
    report_migration = module_from_spec(report_spec)
    report_spec.loader.exec_module(report_migration)
    health_spec = spec_from_file_location(
        "health_migration", path.with_name("0007_monitor_reliability.py")
    )
    assert health_spec and health_spec.loader
    health_migration = module_from_spec(health_spec)
    health_spec.loader.exec_module(health_migration)
    index_spec = spec_from_file_location(
        "index_migration", path.with_name("0008_worker_indexes.py")
    )
    assert index_spec and index_spec.loader
    index_migration = module_from_spec(index_spec)
    index_spec.loader.exec_module(index_migration)
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()
            notification_migration.upgrade()
            ha_migration.upgrade()
            subscription_migration.upgrade()
            delivery_migration.upgrade()
            report_migration.upgrade()
            health_migration.upgrade()
            index_migration.upgrade()
            assert set(inspect(connection).get_table_names()) == set(Base.metadata.tables)
            assert compare_metadata(context, Base.metadata) == []
            index_migration.downgrade()
            health_migration.downgrade()
            report_migration.downgrade()
            delivery_migration.downgrade()
            subscription_migration.downgrade()
            ha_migration.downgrade()
            notification_migration.downgrade()
            migration.downgrade()
            assert inspect(connection).get_table_names() == []
    engine.dispose()

    output = StringIO()
    context = MigrationContext.configure(
        dialect=postgres_dialect, opts={"as_sql": True, "output_buffer": output}
    )
    with Operations.context(context):
        migration.upgrade()
        notification_migration.upgrade()
        ha_migration.upgrade()
        subscription_migration.upgrade()
        delivery_migration.upgrade()
        report_migration.upgrade()
        health_migration.upgrade()
        index_migration.upgrade()
    sql = output.getvalue()
    assert sql.count("CREATE TABLE") == 16
    assert "CREATE UNIQUE INDEX uq_power_intervals_open_device" in sql
    for statement in sql.split(";"):
        if "CREATE TABLE" in statement:
            names = re.findall(r"CONSTRAINT (\w+)", statement)
            assert len(names) == len(set(names)), statement
