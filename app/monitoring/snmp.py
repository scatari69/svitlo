import asyncio
import math
import re
from datetime import UTC, datetime
from importlib import import_module
from typing import Literal

from app.models.enums import MonitorHealth, PowerState, SnmpMode
from app.monitoring.base import MonitorResult
from app.monitoring.validation import validate_oid
from app.services.validation import validate_host

type SnmpFailure = Literal["timeout", "access_denied", "invalid_oid", "protocol_error"]


class SnmpQueryError(ConnectionError):
    def __init__(self, reason: SnmpFailure) -> None:
        self.reason = reason
        super().__init__(reason)


def comparison_value(value: str | int) -> str | int:
    """Compare decimal integers numerically, other strings literally; never evaluate input."""
    if isinstance(value, int):
        return value
    if len(value) > 255 or not value or any(ord(char) < 32 for char in value):
        raise ValueError("Invalid SNMP comparison value")
    return int(value) if re.fullmatch(r"[+-]?[0-9]+", value) else value


async def snmp_get(host: str, port: int, community: str, oid: str) -> str:
    # API-only startup does not need to load the SNMP transport.
    api = import_module("pysnmp.hlapi.v3arch.asyncio")
    engine = api.SnmpEngine()
    try:
        async with asyncio.timeout(8):
            target = await api.UdpTransportTarget.create((host, port), timeout=2, retries=1)
            error, status, _, values = await api.get_cmd(
                engine,
                api.CommunityData(community, mpModel=1),
                target,
                api.ContextData(),
                api.ObjectType(api.ObjectIdentity(oid)),
                lookupMib=False,
            )
        if error:
            # Upstream exceptions can contain credentials: classify, never return their text.
            name = type(error).__name__
            reason: SnmpFailure = "protocol_error"
            if name == "RequestTimedOut":
                reason = "timeout"
            elif name in {"UnknownCommunityName", "AuthorizationError", "NoAccessEntry"}:
                reason = "access_denied"
            raise SnmpQueryError(reason)
        if status:
            failures: dict[int, SnmpFailure] = {
                2: "invalid_oid",
                6: "access_denied",
                16: "access_denied",
            }
            raise SnmpQueryError(failures.get(int(status), "protocol_error"))
        if not values:
            raise SnmpQueryError("protocol_error")
        value = values[0][1]
        if value.tagSet in (
            api.NoSuchObject.tagSet,
            api.NoSuchInstance.tagSet,
            api.EndOfMibView.tagSet,
        ):
            raise SnmpQueryError("invalid_oid")
        if value.tagSet in (
            api.Integer.tagSet,
            api.Integer32.tagSet,
            api.Counter32.tagSet,
            api.Gauge32.tagSet,
            api.Unsigned32.tagSet,
            api.TimeTicks.tagSet,
            api.Counter64.tagSet,
        ):
            return str(int(value))
        if value.tagSet == api.OctetString.tagSet:
            return bytes(value.asOctets()).decode("utf-8")
        raise SnmpQueryError("protocol_error")
    finally:
        engine.close_dispatcher()


class SnmpMonitor:
    def __init__(
        self,
        host: str,
        port: int,
        community: str,
        mode: SnmpMode,
        *,
        polling_interval_seconds: float = 10,
        interface_index: int | None = None,
        oid: str | None = None,
        on_value: str | int | None = None,
        off_value: str | int | None = None,
    ) -> None:
        self.host = validate_host(host)
        if (
            not 1 <= port <= 65535
            or not community
            or not math.isfinite(polling_interval_seconds)
            or not 0 < polling_interval_seconds <= 86400
        ):
            raise ValueError("Invalid SNMP configuration")
        self.port = port
        self._community = community
        self.polling_interval_seconds = polling_interval_seconds
        self.mode = SnmpMode(mode)
        if self.mode == SnmpMode.INTERFACE:
            if interface_index is None or not 1 <= interface_index <= 2**31 - 1:
                raise ValueError("Invalid interface index")
            self.oid = f"1.3.6.1.2.1.2.2.1.8.{interface_index}"
            self.on_value: str | int = 1
            self.off_value: str | int = 2
        else:
            if oid is None or on_value is None or off_value is None:
                raise ValueError("Invalid custom OID configuration")
            self.oid = validate_oid(oid)
            self.on_value, self.off_value = comparison_value(on_value), comparison_value(off_value)
            if self.on_value == self.off_value:
                raise ValueError("SNMP ON and OFF values must differ")

    @property
    def interval_seconds(self) -> float:
        return self.polling_interval_seconds

    async def check(self) -> MonitorResult:
        try:
            value = comparison_value(
                await snmp_get(self.host, self.port, self._community, self.oid)
            )
        except Exception as error:
            reason = (
                error.reason
                if isinstance(error, SnmpQueryError)
                else "timeout"
                if isinstance(error, TimeoutError)
                else "dependency_unavailable"
                if isinstance(error, ImportError)
                else "transport_error"
            )
            return MonitorResult(
                PowerState.UNKNOWN,
                MonitorHealth.UNAVAILABLE,
                datetime.now(UTC),
                {"reason": reason, "backend": "snmp"},
            )
        state = {self.on_value: PowerState.ON, self.off_value: PowerState.OFF}.get(
            value, PowerState.UNKNOWN
        )
        return MonitorResult(
            state,
            MonitorHealth.DEGRADED if state == PowerState.UNKNOWN else MonitorHealth.HEALTHY,
            datetime.now(UTC),
            {
                "reason": "unexpected_value" if state == PowerState.UNKNOWN else "response",
                "backend": "snmp",
                "responded": True,
                "confirmed": True,
            },
        )
