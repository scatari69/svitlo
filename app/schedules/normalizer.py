import re
from datetime import date, timedelta

from app.schedules.models import KYIV, DaySchedule, ScheduleSlot, ScheduleState, day_bounds


def normalize_half_hours(
    provider: str,
    region: str,
    group: str,
    day: date,
    values: dict[str, ScheduleState],
    *,
    emergency: bool = False,
) -> DaySchedule:
    """Interpret civil half-hour labels on the actual UTC timeline of a Kyiv day.

    Nonexistent spring labels have no interval. Repeated autumn labels apply to
    both folds because a wall-clock feed cannot distinguish them.
    """
    if any(not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[03]0", label) for label in values):
        raise ValueError("Invalid half-hour label")
    states = {label: ScheduleState(value) for label, value in values.items()}
    cursor, end = day_bounds(day)
    slots: list[ScheduleSlot] = []
    while cursor < end:
        state = states.get(cursor.astimezone(KYIV).strftime("%H:%M"), ScheduleState.UNKNOWN)
        following = min(cursor + timedelta(minutes=30), end)
        if slots and slots[-1].state == state:
            slots[-1] = ScheduleSlot(start=slots[-1].start, end=following, state=state)
        else:
            slots.append(ScheduleSlot(start=cursor, end=following, state=state))
        cursor = following
    return DaySchedule(
        provider=provider,
        region=region,
        group=group,
        date=day,
        slots=tuple(slots),
        emergency=emergency,
    )
