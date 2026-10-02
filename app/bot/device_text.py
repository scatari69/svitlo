from datetime import datetime
from zoneinfo import ZoneInfo

from app.models.enums import MonitorHealth, MonitoringType, PowerState
from app.monitoring.base import MonitorResult
from app.services.devices import DeviceView
from app.services.time import aware_utc

METHOD_NAMES = {
    MonitoringType.HOME_ASSISTANT: "Home Assistant",
    MonitoringType.PING: "Ping",
    MonitoringType.SNMP: "SNMP",
}
STATE_NAMES = {
    PowerState.ON: "🟢 Є світло",
    PowerState.OFF: "🔴 Немає світла",
    PowerState.UNKNOWN: "⚪ Невідомо",
}

HEALTH_NAMES = {
    MonitorHealth.HEALTHY: "✅ Працює",
    MonitorHealth.DEGRADED: "⚠️ Є проблеми з перевіркою",
    MonitorHealth.UNAVAILABLE: "❌ Недоступний",
}


def device_line(device: DeviceView) -> str:
    icon = {"дім": "🏠", "офіс": "🏢", "дача": "🏡"}.get(device.name.casefold(), "🏠")
    return f"{icon} {device.name} — {STATE_NAMES[device.current_power_state]}"


def list_text(devices: list[DeviceView]) -> str:
    return "⚙️ Пристрої\n\n" + ("\n".join(map(device_line, devices)) or "У вас ще немає пристроїв.")


def format_time(value: datetime | None) -> str:
    return (
        aware_utc(value).astimezone(ZoneInfo("Europe/Kyiv")).strftime("%d.%m.%Y %H:%M")
        if value
        else "Ще немає даних"
    )


def details_text(device: DeviceView) -> str:
    return (
        f"{device_line(device)}\n\n"
        f"Спосіб моніторингу: {METHOD_NAMES[device.monitoring_type]}\n"
        f"Моніторинг: {'увімкнений' if device.enabled else 'вимкнений'}\n"
        f"Стан моніторингу: {HEALTH_NAMES[device.monitor_health]}\n"
        f"Остання зміна стану: {format_time(device.last_state_changed_at)}\n"
        f"Остання успішна перевірка: {format_time(device.last_successful_check_at)}"
    )


def snmp_connection_text(result: MonitorResult) -> str:
    if result.state != PowerState.UNKNOWN:
        return f"✅ Підключення перевірено.\n{STATE_NAMES[result.state]}"
    messages = {
        "timeout": "Пристрій не відповідає. Перевірте адресу, мережу та спільноту SNMP.",
        "access_denied": "Доступ заборонено. Перевірте спільноту SNMP та дозволи пристрою.",
        "invalid_oid": "OID або індекс інтерфейсу не знайдено. Перевірте налаштування.",
        "unexpected_value": "Отримано невідоме значення. Перевірте OID та значення станів.",
    }
    return "❌ " + messages.get(
        str(result.metadata.get("reason")),
        "Не вдалося підключитися до пристрою. Перевірте налаштування та мережеве з’єднання.",
    )
