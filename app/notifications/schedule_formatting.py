from datetime import UTC, datetime, timedelta

from app.formatting import format_duration, format_ukrainian_date
from app.schedules.diff import intervals, schedule_diff
from app.schedules.models import KYIV, DaySchedule, ScheduleSlot, ScheduleState, day_bounds
from app.services.time import aware_utc


def interval_label(slot: ScheduleSlot, schedule: DaySchedule) -> str:
    start, end = slot.start.astimezone(KYIV), slot.end.astimezone(KYIV)
    end_text = "24:00" if slot.end == day_bounds(schedule.date)[1] else end.strftime("%H:%M")
    # Distinguish repeated wall-clock hours when an interval crosses the autumn DST change.
    if start.utcoffset() != end.utcoffset() and end_text != "24:00":
        return f"{start:%H:%M} (UTC{start:%z})–{end:%H:%M} (UTC{end:%z})"
    return f"{start:%H:%M}–{end_text}"


def format_schedule_notification(
    schedule: DaySchedule, region_name: str, *, now: datetime | None = None
) -> str:
    today = aware_utc(now or datetime.now(UTC)).astimezone(KYIV).date()
    label = format_ukrainian_date(schedule.date)
    if schedule.date == today:
        label = f"Сьогодні, {label}"
    elif schedule.date == today + timedelta(days=1):
        label = f"Завтра, {label}"
    else:
        label += f" {schedule.date.year}"
    off = intervals(schedule, ScheduleState.OFF)
    unknown = intervals(schedule, ScheduleState.UNKNOWN)
    lines = [
        "⚠️ Графік відключень змінено",
        "",
        f"📍 {region_name}",
        f"Група {schedule.group}",
        "",
        label,
        "",
    ]
    lines.extend(f"🔻 {interval_label(slot, schedule).replace('–', ' — ')}" for slot in off)
    if not off:
        lines.append("Запланованих відключень немає.")
    total = sum((slot.duration for slot in off), timedelta())
    lines.extend(["", "Загалом без світла:", format_duration(total) if total else "0 хвилин"])
    if unknown:
        lines.extend(["", "⚪ Невідомі періоди:"])
        lines.extend(interval_label(slot, schedule) for slot in unknown)
    if schedule.emergency:
        lines.extend(["", "⚠️ Діють екстрені відключення."])
    text = "\n".join(lines)
    if len(text) > 3900:
        text = text[:3800].rsplit("\n", 1)[0] + "\n\nПовний перелік змін — за кнопкою нижче."
    return text


def format_schedule_diff(before: DaySchedule | None, after: DaySchedule) -> str:
    if before is None:
        return "Попереднього графіка немає. Це перша збережена версія."
    sections = []
    for change in schedule_diff(before, after):
        title = "🔻 Відключення" if change.state == ScheduleState.OFF else "⚪ Невідомі періоди"
        old = "\n".join(interval_label(slot, before) for slot in change.before) or "Немає"
        new = "\n".join(interval_label(slot, after) for slot in change.after) or "Немає"
        sections.append(f"{title}\n\nБуло:\n{old}\n\nСтало:\n{new}")
    if before.emergency != after.emergency:
        sections.append(
            "Екстрені відключення:\n" + ("Увімкнено" if after.emergency else "Скасовано")
        )
    return "\n\n".join(sections) or "Графік не змінився."


def diff_messages(text: str) -> list[str]:
    """Split long interval lists at line boundaries to respect Telegram's message limit."""
    messages: list[str] = []
    chunk = ""
    for line in text.splitlines():
        if len(chunk) + len(line) + 1 > 3900:
            messages.append(chunk.rstrip())
            chunk = ""
        chunk += line + "\n"
    if chunk:
        messages.append(chunk.rstrip())
    return messages
