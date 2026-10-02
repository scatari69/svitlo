import asyncio
import base64
import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from secrets import token_urlsafe

from pydantic import SecretStr
from redis.asyncio import Redis

from app.models import HomeAssistantDeviceConfig, PingDeviceConfig, SnmpDeviceConfig
from app.models.enums import HomeAssistantMode, MonitorHealth, MonitoringType, PowerState, SnmpMode
from app.monitoring.base import MonitorResult, PowerMonitor
from app.monitoring.homeassistant_ws import HomeAssistantWebSocketMonitor, validate_states
from app.monitoring.ping import PingMonitor
from app.monitoring.snmp import SnmpMonitor, comparison_value
from app.monitoring.validation import validate_oid
from app.services.secrets import decrypt_secret, encrypt_secret
from app.services.time import aware_utc
from app.services.validation import validate_host, validate_url


@dataclass(frozen=True)
class SetupField:
    key: str
    prompt: str
    secret: bool = False
    choices: tuple[tuple[str, str], ...] = ()


CONFIG_CLASSES: dict[
    MonitoringType,
    type[PingDeviceConfig] | type[SnmpDeviceConfig] | type[HomeAssistantDeviceConfig],
] = {
    MonitoringType.PING: PingDeviceConfig,
    MonitoringType.SNMP: SnmpDeviceConfig,
    MonitoringType.HOME_ASSISTANT: HomeAssistantDeviceConfig,
}


class DeviceSetupService:
    def __init__(
        self,
        encryption_key: SecretStr | None,
        redis: Redis | None = None,
        public_base_url: str = "http://localhost:8000",
    ) -> None:
        self.encryption_key = encryption_key
        self.redis = redis
        self.public_base_url = public_base_url

    def fields(self, method: MonitoringType, values: dict[str, str]) -> list[SetupField]:
        if method == MonitoringType.PING:
            return [
                SetupField("host", "Введіть IP-адресу або ім’я хоста:"),
                SetupField("interval_seconds", "Інтервал перевірки в секундах (типово 10):"),
                SetupField("timeout_seconds", "Час очікування в секундах (типово 2):"),
                SetupField(
                    "failure_threshold",
                    "Кількість невдалих спроб для підтвердження відключення (типово 3):",
                ),
                SetupField(
                    "recovery_threshold",
                    "Кількість успішних спроб для підтвердження відновлення (типово 2):",
                ),
            ]
        if method == MonitoringType.HOME_ASSISTANT:
            mode = SetupField(
                "mode",
                "Оберіть режим Home Assistant:",
                choices=(
                    ("🔗 Вебхук", "webhook"),
                    ("🌐 WebSocket (розширений)", "api"),
                ),
            )
            if values.get("mode") == "webhook":
                return [mode]
            return [
                mode,
                SetupField("url", "Введіть адресу Home Assistant (http:// або https://):"),
                SetupField("access_token_encrypted", "Введіть довгостроковий токен доступу:", True),
                SetupField(
                    "entity_id", "Введіть ідентифікатор сутності (наприклад, binary_sensor.power):"
                ),
                SetupField("on_state", "Введіть стан Home Assistant, коли світло є (типово on):"),
                SetupField(
                    "off_state", "Введіть стан Home Assistant, коли світла немає (типово off):"
                ),
            ]
        fields = [
            SetupField("host", "Введіть IP-адресу або ім’я хоста SNMP:"),
            SetupField("port", "Введіть порт SNMP (типово 161):"),
            SetupField("community_encrypted", "Введіть спільноту SNMP:", True),
            SetupField("polling_interval_seconds", "Інтервал перевірки в секундах (типово 10):"),
            SetupField(
                "mode",
                "Оберіть режим SNMP:",
                choices=(
                    ("📡 Стан інтерфейсу", "interface"),
                    ("🔢 Власний OID", "custom_oid"),
                ),
            ),
        ]
        if values.get("mode") == "custom_oid":
            return fields + [
                SetupField("oid", "Введіть числовий OID:"),
                SetupField("on_value", "Значення, коли світло є:"),
                SetupField("off_value", "Значення, коли світла немає:"),
            ]
        return fields + [SetupField("interface_index", "Введіть індекс інтерфейсу:")]

    def parse(self, field: SetupField, value: str) -> str:
        value = value.strip()
        if not value or len(value) > 4096 or any(ord(c) < 32 for c in value):
            raise ValueError("Invalid field value")
        if field.choices and value not in {choice for _, choice in field.choices}:
            raise ValueError("Invalid choice")
        if field.key == "host":
            return validate_host(value)
        if field.key == "url":
            if len(value) > 2048:
                raise ValueError("URL too long")
            return validate_url(value)
        if field.key == "oid":
            if len(value) > 255:
                raise ValueError("OID too long")
            return validate_oid(value)
        if field.key == "entity_id" and not re.fullmatch(r"[a-z_][a-z0-9_]*\.[a-z0-9_]+", value):
            raise ValueError("Invalid entity ID")
        if field.key in {"port", "interface_index", "failure_threshold", "recovery_threshold"}:
            number = int(value)
            limits = {
                "port": (1, 65535),
                "interface_index": (1, 2**31 - 1),
                "failure_threshold": (2, 20),
                "recovery_threshold": (1, 20),
            }
            low, high = limits[field.key]
            if not low <= number <= high:
                raise ValueError("Invalid integer range")
            return str(number)
        if field.key in {"interval_seconds", "polling_interval_seconds", "timeout_seconds"}:
            number_float = float(value)
            high_float = 30 if field.key == "timeout_seconds" else 86400
            if not math.isfinite(number_float) or not 0 < number_float <= high_float:
                raise ValueError("Invalid timing")
            return str(number_float)
        if (
            field.key in {"oid", "entity_id", "on_value", "off_value", "on_state", "off_state"}
            and len(value) > 255
        ):
            raise ValueError("Value too long")
        if field.secret:
            return base64.urlsafe_b64encode(encrypt_secret(self.encryption_key, value)).decode()
        return value

    def secret(self, value: str) -> str:
        return decrypt_secret(self.encryption_key, base64.urlsafe_b64decode(value))

    async def prepare_webhook(self) -> tuple[str, str]:
        if self.redis is None:
            raise RuntimeError("Redis is required for webhook setup")
        token = token_urlsafe(32)
        digest = sha256(token.encode()).hexdigest()
        if not await self.redis.set(f"ha:setup:{digest}", "pending", ex=1800, nx=True):
            raise RuntimeError("Could not reserve webhook token")
        return digest, f"{self.public_base_url}/api/v1/homeassistant/webhook/{token}"

    def config_values(self, method: MonitoringType, values: dict[str, str]) -> dict[str, object]:
        # Revalidate persisted FSM input; credentials already contain Fernet ciphertext.
        values = dict(values)
        if method == MonitoringType.HOME_ASSISTANT and values.get("mode") == "api":
            values.setdefault("on_state", "on")
            values.setdefault("off_state", "off")
            validate_states(values["on_state"], values["off_state"])
        expected = self.fields(method, values)
        result: dict[str, object] = {}
        for field in expected:
            value = values[field.key]
            if field.secret:
                self.secret(value)  # Reject tampered ciphertext before persistence.
                result[field.key] = base64.urlsafe_b64decode(value)
            else:
                value = self.parse(field, value)
                if field.key in {
                    "port",
                    "interface_index",
                    "failure_threshold",
                    "recovery_threshold",
                }:
                    result[field.key] = int(value)
                elif field.key.endswith("seconds"):
                    result[field.key] = float(value)
                else:
                    result[field.key] = value
        if method == MonitoringType.SNMP:
            result["mode"] = SnmpMode(values["mode"])
            if result["mode"] == SnmpMode.CUSTOM_OID and comparison_value(
                values["on_value"]
            ) == comparison_value(values["off_value"]):
                raise ValueError("SNMP ON and OFF values must differ")
        if method == MonitoringType.HOME_ASSISTANT:
            result["mode"] = HomeAssistantMode(values["mode"])
            if result["mode"] == HomeAssistantMode.WEBHOOK:
                digest = values["webhook_token_hash"]
                if not re.fullmatch(r"[0-9a-f]{64}", digest):
                    raise ValueError("Invalid webhook hash")
                result["webhook_token_hash"] = digest
        return result

    def monitor(self, method: MonitoringType, values: dict[str, str]) -> PowerMonitor:
        self.config_values(method, values)
        if method == MonitoringType.PING:
            return PingMonitor(
                values["host"],
                float(values["timeout_seconds"]),
                int(values["failure_threshold"]),
                int(values["recovery_threshold"]),
                interval_seconds=float(values["interval_seconds"]),
            )
        if method == MonitoringType.SNMP:
            return SnmpMonitor(
                values["host"],
                int(values["port"]),
                self.secret(values["community_encrypted"]),
                SnmpMode(values["mode"]),
                interface_index=int(values["interface_index"])
                if "interface_index" in values
                else None,
                polling_interval_seconds=float(values["polling_interval_seconds"]),
                oid=values.get("oid"),
                on_value=values.get("on_value"),
                off_value=values.get("off_value"),
            )
        return HomeAssistantWebSocketMonitor(
            values["url"],
            self.secret(values["access_token_encrypted"]),
            values["entity_id"],
            values.get("on_state", "on"),
            values.get("off_state", "off"),
        )

    async def test(self, method: MonitoringType, values: dict[str, str]) -> MonitorResult:
        self.config_values(method, values)
        if method == MonitoringType.HOME_ASSISTANT and values["mode"] == "webhook":
            if self.redis is None:
                raise RuntimeError("Redis is required")
            raw = await self.redis.get(f"ha:setup:{values['webhook_token_hash']}")
            now = datetime.now(UTC)
            try:
                data = json.loads(raw or "null")
                state = PowerState(data["state"])
                received_at = aware_utc(datetime.fromisoformat(data["received_at"]))
                if state == PowerState.UNKNOWN or not timedelta() <= now - received_at <= timedelta(
                    minutes=30
                ):
                    raise ValueError("Invalid setup observation")
            except (ValueError, TypeError, KeyError):
                return MonitorResult(PowerState.UNKNOWN, MonitorHealth.UNAVAILABLE, now)
            return MonitorResult(state, MonitorHealth.HEALTHY, received_at)
        monitor = self.monitor(method, values)
        # Probe confirmation uses the same adapter, never a single failed Ping packet.
        count = int(values["recovery_threshold"]) if method == MonitoringType.PING else 1
        try:
            async with asyncio.timeout(90):
                result = await monitor.check()
                for _ in range(count - 1):
                    if result.health != MonitorHealth.HEALTHY:
                        break
                    await asyncio.sleep(min(float(values["interval_seconds"]), 1))
                    result = await monitor.check()
            return result
        except TimeoutError:
            return MonitorResult(
                PowerState.UNKNOWN,
                MonitorHealth.UNAVAILABLE,
                datetime.now(UTC),
                {"reason": "probe_deadline", "confirmed": False},
            )
