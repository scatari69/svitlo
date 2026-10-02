from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.models.enums import MonitorHealth, PowerState, SnmpMode
from app.monitoring.homeassistant import HomeAssistantMonitor
from app.monitoring.ping import PingMonitor, ping_once
from app.monitoring.snmp import SnmpMonitor, snmp_get
from app.monitoring.validation import validate_oid
from app.services.validation import validate_host, validate_url


async def test_ping_debounce_recovery_and_failure_timestamp() -> None:
    monitor = PingMonitor("localhost")
    with patch(
        "app.monitoring.ping.ping_once", side_effect=[True, True, False, False, False, True, True]
    ):
        results = [await monitor.check() for _ in range(7)]
    assert [r.state for r in results] == [
        PowerState.UNKNOWN,
        PowerState.ON,
        PowerState.ON,
        PowerState.ON,
        PowerState.OFF,
        PowerState.OFF,
        PowerState.ON,
    ]
    assert results[4].detected_at == results[2].detected_at
    assert results[6].detected_at == results[5].detected_at
    with patch("app.monitoring.ping.ping_once", side_effect=OSError):
        result = await monitor.check()
    assert result.state == PowerState.UNKNOWN and result.health == MonitorHealth.UNAVAILABLE


async def test_ping_subprocess_is_safe_and_timeout_reaps_child() -> None:
    process = MagicMock()
    process.wait = AsyncMock(return_value=0)
    process.returncode = None
    with patch("app.monitoring.ping.asyncio.create_subprocess_exec", return_value=process) as spawn:
        assert await ping_once("localhost", 2)
        assert spawn.call_args.args[-2:] == ("--", "localhost")
        process.wait.side_effect = [TimeoutError, 0]
        with pytest.raises(TimeoutError):
            await ping_once("localhost", 2)
    process.kill.assert_called_once()
    assert process.wait.await_count == 3


@pytest.mark.parametrize(
    ("value", "state", "health"),
    [
        ("1", PowerState.ON, MonitorHealth.HEALTHY),
        ("2", PowerState.OFF, MonitorHealth.HEALTHY),
        ("3", PowerState.UNKNOWN, MonitorHealth.DEGRADED),
    ],
)
async def test_snmp_interface_mapping(value: str, state: PowerState, health: MonitorHealth) -> None:
    monitor = SnmpMonitor(
        "localhost", 161, "private-community", SnmpMode.INTERFACE, interface_index=4
    )
    with patch("app.monitoring.snmp.snmp_get", return_value=value) as query:
        result = await monitor.check()
    assert result.state == state and result.health == health
    assert query.call_args.args[-1] == "1.3.6.1.2.1.2.2.1.8.4"


async def test_snmp_custom_values_and_transport_failure() -> None:
    monitor = SnmpMonitor(
        "localhost",
        161,
        "secret",
        SnmpMode.CUSTOM_OID,
        oid="1.3.6.1.4.1.123",
        on_value="yes",
        off_value="no",
    )
    with patch("app.monitoring.snmp.snmp_get", return_value="no"):
        assert (await monitor.check()).state == PowerState.OFF
    with patch("app.monitoring.snmp.snmp_get", side_effect=TimeoutError):
        result = await monitor.check()
    assert result.state == PowerState.UNKNOWN and result.health == MonitorHealth.UNAVAILABLE


async def test_snmp_api_closes_engine_on_success_and_failure() -> None:
    api = MagicMock()
    api.UdpTransportTarget.create = AsyncMock()
    value = MagicMock()
    value.asOctets.return_value = b"1"
    api.OctetString.tagSet = value.tagSet
    api.get_cmd = AsyncMock(return_value=(None, 0, 0, [("oid", value)]))
    with patch("app.monitoring.snmp.import_module", return_value=api):
        assert await snmp_get("localhost", 161, "secret", "1.3.6") == "1"
        api.get_cmd.side_effect = TimeoutError
        with pytest.raises(TimeoutError):
            await snmp_get("localhost", 161, "secret", "1.3.6")
    assert api.SnmpEngine.return_value.close_dispatcher.call_count == 2
    api.CommunityData.assert_called_with("secret", mpModel=1)


@pytest.mark.parametrize(
    ("payload", "status", "state", "health"),
    [
        ({"state": "on"}, 200, PowerState.ON, MonitorHealth.HEALTHY),
        ({"state": "off"}, 200, PowerState.OFF, MonitorHealth.HEALTHY),
        ({"state": "unavailable"}, 200, PowerState.UNKNOWN, MonitorHealth.DEGRADED),
        ({"state": []}, 200, PowerState.UNKNOWN, MonitorHealth.DEGRADED),
        ([], 200, PowerState.UNKNOWN, MonitorHealth.UNAVAILABLE),
        ({"state": "off"}, 401, PowerState.UNKNOWN, MonitorHealth.UNAVAILABLE),
    ],
)
async def test_homeassistant_mapping(
    payload: object, status: int, state: PowerState, health: MonitorHealth
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/states/binary_sensor.power"
        assert request.headers["Authorization"] == "Bearer secret"
        return httpx.Response(status, json=payload)

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    with patch("app.monitoring.homeassistant.httpx.AsyncClient", return_value=client):
        result = await HomeAssistantMonitor(
            "http://ha.local", "secret", "binary_sensor.power"
        ).check()
    assert result.state == state and result.health == health


@pytest.mark.parametrize(
    "host", ["--help", "a;cat", "a/b", "user@host", "a b", "", "-host", "a\ncommand"]
)
def test_unsafe_hosts_are_rejected(host: str) -> None:
    with pytest.raises(ValueError):
        validate_host(host)


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp",
        "http://user:secret@host",
        "http://host?token=secret",
        "http://host/#secret",
        "http://host:99999",
    ],
)
def test_unsafe_urls_are_rejected(url: str) -> None:
    with pytest.raises(ValueError):
        validate_url(url)


def test_numeric_oid_validation() -> None:
    assert validate_oid(".1.3.6.1") == "1.3.6.1"
    for value in ("IF-MIB::ifOperStatus", "1.50.1", "3.1", "1.3.-1"):
        with pytest.raises(ValueError):
            validate_oid(value)
