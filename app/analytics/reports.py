"""Ukrainian presentation of actual statistics; no calculations or delivery here."""

from datetime import timedelta

from app.analytics.service import KYIV, PowerStatistics
from app.formatting import format_duration, format_ukrainian_date


def format_statistics(statistics: PowerStatistics, device_name: str) -> str:
    start = statistics.window.start.astimezone(KYIV)
    end = statistics.measured_until.astimezone(KYIV)
    last_day = (statistics.measured_until - timedelta(microseconds=1)).astimezone(KYIV).date()
    last_day = max(start.date(), last_day)
    label = format_ukrainian_date(start.date())
    if last_day != start.date():
        label += f" — {format_ukrainian_date(last_day)}"
    availability = statistics.availability_percentage

    def duration(value: timedelta) -> str:
        return format_duration(value, short=True) if value else "0 хв"

    lines = [
        f"📊 Статистика за {label}",
        "",
        f"🏠 {device_name}",
        f"🕒 {start:%d.%m.%Y %H:%M} — {end:%d.%m.%Y %H:%M} (Київ)",
        "",
        "💡 Світло було:",
        duration(statistics.on_duration),
    ]
    if availability is not None:
        lines.append(f"{availability:.0f}%")
    lines.extend(["", "🔴 Світла не було:", duration(statistics.off_duration)])
    if availability is not None:
        lines.append(f"{100 - round(availability)}%")
    if statistics.unknown_duration:
        lines.extend(["", "⚪ Немає даних:", duration(statistics.unknown_duration)])
    lines.extend(
        [
            "",
            "Доступність за відомий час (світло було + світла не було)."
            if availability is not None
            else "Немає підтверджених даних.",
            "",
            "Кількість відключень:",
            str(statistics.outage_count),
            "",
            "Найдовше відключення:",
            duration(statistics.longest_outage),
            "",
            "Середня тривалість відключення:",
            duration(statistics.average_outage_duration),
            "",
            "Відключення враховано лише в межах вибраного періоду, включно з поточними.",
        ]
    )
    return "\n".join(lines)
