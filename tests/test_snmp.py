import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.bot.device_text import snmp_connection_text
from app.models.enums import MonitorHealth, PowerState, SnmpMode
from app.monitoring.snmp import SnmpMonitor, SnmpQueryError, comparison_value, snmp_get


@pytest.mark.parametrize(
    ("value", "on", "off", "expected"),
    [
        (1, "01", 2, PowerState.ON),
        ("+2", 1, "002", PowerState.OFF),
        ("active", "active", "inactive", PowerState.ON),
        ("inactive", "active", "inactive", PowerState.OFF),
        ("ACTIVE", "active", "inactive", PowerState.UNKNOWN),
        ("__import__('os')", 1, 2, PowerState.UNKNOWN),
        ("3", 1, 2, PowerState.UNKNOWN),
    ],
)
async def test_custom_comparison(
    value: str | int, on: str | int, off: str | int, expected: PowerState
) -> None:
    monitor = SnmpMonitor(
        "localhost",
        161,
        "private-secret",
        SnmpMode.CUSTOM_OID,
        oid=".1.3.6.1.4.1.123.0",
        on_value=on,
        off_value=off,
        polling_interval_seconds=15,
    )
    with patch("app.monitoring.snmp.snmp_get", return_value=value):
        result = await monitor.check()
    assert result.state == expected
    assert result.detected_at.utcoffset().total_seconds() == 0  # type: ignore[union-attr]
    assert result.health == (
        MonitorHealth.DEGRADED if expected == PowerState.UNKNOWN else MonitorHealth.HEALTHY
    )
    assert monitor.polling_interval_seconds == 15
    assert "private-secret" not in repr(monitor) + repr(result) + snmp_connection_text(result)


@pytest.mark.parametrize("value", ["1", "+1", "01"])
def test_numeric_aliases_cannot_describe_both_states(value: str) -> None:
    with pytest.raises(ValueError, match="must differ"):
        SnmpMonitor(
            "localhost",
            161,
            "secret",
            SnmpMode.CUSTOM_OID,
            oid="1.3.6",
            on_value=1,
            off_value=value,
        )


@pytest.mark.parametrize("value", ["", "a\n", "9" * 256])
def test_invalid_comparison_values(value: str) -> None:
    with pytest.raises(ValueError):
        comparison_value(value)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"port": 0},
        {"community": ""},
        {"polling_interval_seconds": float("nan")},
        {"polling_interval_seconds": 0},
        {"interface_index": 0},
        {"interface_index": 2**32},
        {"mode": "invalid"},
    ],
)
def test_invalid_configuration(kwargs: dict[str, object]) -> None:
    config = {
        "host": "localhost",
        "port": 161,
        "community": "secret",
        "mode": SnmpMode.INTERFACE,
        "interface_index": 1,
    }
    config.update(kwargs)
    with pytest.raises(ValueError):
        SnmpMonitor(**config)  # type: ignore[arg-type]


@pytest.mark.parametrize("reason", ["timeout", "access_denied", "invalid_oid", "protocol_error"])
async def test_failures_are_unknown_and_safe(reason: str) -> None:
    monitor = SnmpMonitor(
        "localhost", 161, "hidden-community", SnmpMode.INTERFACE, interface_index=2
    )
    with patch("app.monitoring.snmp.snmp_get", side_effect=SnmpQueryError(reason)):  # type: ignore[arg-type]
        result = await monitor.check()
    assert result.state == PowerState.UNKNOWN
    assert result.health == MonitorHealth.UNAVAILABLE
    assert result.metadata["reason"] == reason
    assert "hidden-community" not in repr(result) + snmp_connection_text(result)


async def test_raw_exception_is_never_exposed() -> None:
    monitor = SnmpMonitor("localhost", 161, "secret", SnmpMode.INTERFACE, interface_index=1)
    with patch("app.monitoring.snmp.snmp_get", side_effect=RuntimeError("secret")):
        result = await monitor.check()
    assert result.metadata["reason"] == "transport_error"
    assert "secret" not in repr(result)
    with patch("app.monitoring.snmp.snmp_get", side_effect=asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await monitor.check()


@pytest.mark.parametrize(
    ("error_name", "status", "tag", "reason"),
    [
        ("RequestTimedOut", 0, "Integer", "timeout"),
        ("UnknownCommunityName", 0, "Integer", "access_denied"),
        (None, 6, "Integer", "access_denied"),
        (None, 16, "Integer", "access_denied"),
        (None, 2, "Integer", "invalid_oid"),
        (None, 5, "Integer", "protocol_error"),
        (None, 0, "NoSuchObject", "invalid_oid"),
        (None, 0, "NoSuchInstance", "invalid_oid"),
        (None, 0, "EndOfMibView", "invalid_oid"),
        (None, 0, "ObjectIdentifier", "protocol_error"),
    ],
)
async def test_transport_error_classification(
    error_name: str | None, status: int, tag: str, reason: str
) -> None:
    api = MagicMock()
    api.UdpTransportTarget.create = AsyncMock()
    value = MagicMock()
    value.tagSet = getattr(api, tag).tagSet
    error = type(error_name, (), {})() if error_name else None
    api.get_cmd = AsyncMock(return_value=(error, status, 0, [("oid", value)]))
    with patch("app.monitoring.snmp.import_module", return_value=api):
        with pytest.raises(SnmpQueryError, match=reason):
            await snmp_get("localhost", 161, "secret", "1.3.6")
    api.SnmpEngine.return_value.close_dispatcher.assert_called_once()
    api.CommunityData.assert_called_once_with("secret", mpModel=1)
    assert api.get_cmd.call_args.kwargs == {"lookupMib": False}


@pytest.mark.parametrize("tag", ["Integer", "Integer32", "Counter32"])
async def test_transport_preserves_numeric_values_instead_of_enum_labels(tag: str) -> None:
    api = MagicMock()
    api.UdpTransportTarget.create = AsyncMock()
    value = MagicMock()
    value.tagSet = getattr(api, tag).tagSet
    value.__int__.return_value = 1
    value.prettyPrint.return_value = "up"
    api.get_cmd = AsyncMock(return_value=(None, 0, 0, [("oid", value)]))
    with patch("app.monitoring.snmp.import_module", return_value=api):
        assert await snmp_get("localhost", 161, "secret", "1.3.6") == "1"
    value.prettyPrint.assert_not_called()
