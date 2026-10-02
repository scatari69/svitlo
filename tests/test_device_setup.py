import base64
import json
import logging
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import SecretStr

from app.logging import JsonFormatter
from app.models.enums import MonitorHealth, MonitoringType, PowerState
from app.monitoring.base import MonitorResult
from app.services.device_setup import DeviceSetupService, SetupField
from app.services.secrets import decrypt_secret, encrypt_secret


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("failure_threshold", "1"),
        ("recovery_threshold", "0"),
        ("port", "65536"),
        ("timeout_seconds", "nan"),
        ("interval_seconds", "inf"),
        ("interval_seconds", "0"),
        ("entity_id", "binary_sensor/a"),
        ("oid", "1.3."),
    ],
)
def test_setup_fields_reject_invalid_values(key: str, value: str) -> None:
    with pytest.raises(ValueError):
        DeviceSetupService(None).parse(SetupField(key, ""), value)


def test_secrets_are_encrypted_before_entering_fsm() -> None:
    service = DeviceSetupService(SecretStr("key"))
    cipher = MagicMock()
    cipher.encrypt.return_value = b"ciphertext"
    cipher.decrypt.return_value = b"secret"
    module = MagicMock()
    module.Fernet.return_value = cipher
    with patch("app.services.secrets.import_module", return_value=module):
        stored = service.parse(SetupField("community_encrypted", "", secret=True), "secret")
        assert stored == base64.urlsafe_b64encode(b"ciphertext").decode()
        assert "secret" not in stored
        assert service.secret(stored) == "secret"
        config = service.config_values(
            MonitoringType.SNMP,
            {
                "host": "localhost",
                "port": "161",
                "polling_interval_seconds": "10",
                "community_encrypted": stored,
                "mode": "interface",
                "interface_index": "1",
            },
        )
        assert config["community_encrypted"] == b"ciphertext"
    cipher.encrypt.assert_called_once_with(b"secret")


def test_fernet_roundtrip_and_wrong_key_rejection() -> None:
    fernet = pytest.importorskip(
        "cryptography.fernet", reason="Package installation unavailable in sandbox"
    )
    key = SecretStr(fernet.Fernet.generate_key().decode())
    ciphertext = encrypt_secret(key, "secret")
    assert b"secret" not in ciphertext
    assert decrypt_secret(key, ciphertext) == "secret"
    with pytest.raises(fernet.InvalidToken):
        decrypt_secret(SecretStr(fernet.Fernet.generate_key().decode()), ciphertext)


def test_credential_storage_requires_encryption_key() -> None:
    with pytest.raises(RuntimeError, match="ENCRYPTION_KEY"):
        encrypt_secret(None, "secret")


async def test_webhook_setup_is_random_and_test_requires_received_request() -> None:
    redis = MagicMock()
    redis.set = AsyncMock()
    redis.get = AsyncMock(return_value="pending")
    service = DeviceSetupService(None, redis, "https://bot.example")
    first, url = await service.prepare_webhook()
    second, _ = await service.prepare_webhook()
    assert first != second and len(first) == 64 and len(url.rsplit("/", 1)[1]) >= 43
    assert url.startswith("https://bot.example/api/v1/homeassistant/webhook/")
    values = {"mode": "webhook", "webhook_token_hash": first}
    result = await service.test(MonitoringType.HOME_ASSISTANT, values)
    assert result.state == PowerState.UNKNOWN and result.health == MonitorHealth.UNAVAILABLE
    received_at = datetime.now(UTC)
    redis.get.return_value = json.dumps({"state": "off", "received_at": received_at.isoformat()})
    result = await service.test(MonitoringType.HOME_ASSISTANT, values)
    assert result.state == PowerState.OFF and result.health == MonitorHealth.HEALTHY
    assert result.detected_at == received_at
    assert url.rsplit("/", 1)[1] not in str(redis.mock_calls)


def test_webhook_access_log_redacts_token() -> None:
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        1,
        "POST /api/v1/homeassistant/webhook/secret-token HTTP/1.1",
        (),
        None,
    )
    text = JsonFormatter().format(record)
    assert "secret-token" not in text and "[REDACTED]" in text


def test_snmp_custom_values_cannot_be_equal() -> None:
    service = DeviceSetupService(None)
    values = {
        "host": "localhost",
        "port": "161",
        "community_encrypted": "Y2lwaGVy",
        "polling_interval_seconds": "10",
        "mode": "custom_oid",
        "oid": "1.3.6.1",
        "on_value": "1",
        "off_value": "1",
    }
    with patch.object(service, "secret", return_value="community"):
        with pytest.raises(ValueError, match="differ"):
            service.config_values(MonitoringType.SNMP, values)


async def test_ping_probe_deadline_is_a_failed_connection_result() -> None:
    service = DeviceSetupService(None)
    monitor = MagicMock()
    monitor.check = AsyncMock(side_effect=TimeoutError)
    values = {
        "host": "localhost",
        "interval_seconds": "10",
        "timeout_seconds": "2",
        "failure_threshold": "3",
        "recovery_threshold": "2",
    }
    with patch.object(service, "monitor", return_value=monitor):
        result = await service.test(MonitoringType.PING, values)
    assert result.state == PowerState.UNKNOWN and result.health == MonitorHealth.UNAVAILABLE
    assert result.metadata["reason"] == "probe_deadline"


def test_snmp_numeric_aliases_rejected_before_persistence() -> None:
    service = DeviceSetupService(None)
    values = {
        "host": "localhost",
        "port": "161",
        "polling_interval_seconds": "10",
        "community_encrypted": base64.urlsafe_b64encode(b"ciphertext").decode(),
        "mode": "custom_oid",
        "oid": "1.3.6.1",
        "on_value": "1",
        "off_value": "+01",
    }
    with patch.object(service, "secret", return_value="secret"):
        with pytest.raises(ValueError, match="differ"):
            service.config_values(MonitoringType.SNMP, values)


async def test_snmp_setup_service_builds_monitor_and_tests_response() -> None:
    service = DeviceSetupService(None)
    values = {
        "host": "localhost",
        "port": "1161",
        "polling_interval_seconds": "25",
        "community_encrypted": base64.urlsafe_b64encode(b"ciphertext").decode(),
        "mode": "custom_oid",
        "oid": "1.3.6.1",
        "on_value": "running",
        "off_value": "stopped",
    }
    with (
        patch.object(service, "secret", return_value="secret"),
        patch("app.monitoring.snmp.snmp_get", return_value="stopped") as query,
    ):
        monitor = service.monitor(MonitoringType.SNMP, values)
        result = await service.test(MonitoringType.SNMP, values)
    from app.monitoring.snmp import SnmpMonitor

    assert isinstance(monitor, SnmpMonitor)
    assert monitor.polling_interval_seconds == 25
    assert result.state == PowerState.OFF and result.health == MonitorHealth.HEALTHY
    query.assert_awaited_once_with("localhost", 1161, "secret", "1.3.6.1")


async def test_webhook_token_reservation_cannot_overwrite_existing_setup() -> None:
    redis = MagicMock()
    redis.set = AsyncMock(return_value=False)
    service = DeviceSetupService(None, redis, "https://bot.example")
    with pytest.raises(RuntimeError, match="reserve"):
        await service.prepare_webhook()
    assert redis.set.call_args.kwargs == {"ex": 1800, "nx": True}


@pytest.mark.parametrize(
    "message",
    [
        "POST /api/v1/homeassistant/webhook/private-token HTTP/1.1",
        'request="/api/v1/homeassistant/webhook/private-token?source=ha"',
        "request https://bot.example/api/v1/homeassistant/webhook/private-token",
    ],
)
def test_webhook_tokens_are_redacted_from_log_variants(message: str) -> None:
    record = logging.LogRecord("uvicorn.access", logging.INFO, "", 1, message, (), None)
    assert "private-token" not in JsonFormatter().format(record)


async def test_advanced_ha_setup_tests_websocket_with_custom_mapping() -> None:
    from app.monitoring.homeassistant_ws import HomeAssistantWebSocketMonitor

    service = DeviceSetupService(None)
    values = {
        "mode": "api",
        "url": "https://ha.local",
        "entity_id": "sensor.power",
        "access_token_encrypted": base64.urlsafe_b64encode(b"ciphertext").decode(),
        "on_state": "online",
        "off_state": "offline",
    }
    with (
        patch.object(service, "secret", return_value="secret"),
        patch.object(
            HomeAssistantWebSocketMonitor,
            "check",
            return_value=MonitorResult(PowerState.ON, MonitorHealth.HEALTHY, datetime.now(UTC)),
        ) as probe,
    ):
        monitor = service.monitor(MonitoringType.HOME_ASSISTANT, values)
        result = await service.test(MonitoringType.HOME_ASSISTANT, values)
    assert result.state == PowerState.ON
    assert isinstance(monitor, HomeAssistantWebSocketMonitor)
    assert monitor.result("online").state == PowerState.ON
    assert monitor.result("offline").state == PowerState.OFF
    probe.assert_awaited_once()
