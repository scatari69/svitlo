"""Compare normalized intervals, independently of provider metadata and slot boundaries."""

from dataclasses import dataclass

from app.schedules.models import DaySchedule, ScheduleSlot, ScheduleState


def intervals(schedule: DaySchedule, state: ScheduleState) -> tuple[ScheduleSlot, ...]:
    result: list[ScheduleSlot] = []
    for slot in schedule.slots:
        if slot.state != state:
            continue
        if result and result[-1].end == slot.start:
            result[-1] = result[-1].model_copy(update={"end": slot.end})
        else:
            result.append(slot)
    return tuple(result)


@dataclass(frozen=True)
class IntervalDiff:
    state: ScheduleState
    before: tuple[ScheduleSlot, ...]
    after: tuple[ScheduleSlot, ...]


def schedule_diff(before: DaySchedule, after: DaySchedule) -> tuple[IntervalDiff, ...]:
    if (before.provider, before.region, before.group, before.date) != (
        after.provider,
        after.region,
        after.group,
        after.date,
    ):
        raise ValueError("Cannot compare unrelated schedules")
    changes = []
    for state in (ScheduleState.OFF, ScheduleState.UNKNOWN):
        old, new = intervals(before, state), intervals(after, state)
        removed = tuple(slot for slot in old if slot not in new)
        added = tuple(slot for slot in new if slot not in old)
        if removed or added:
            changes.append(IntervalDiff(state, removed, added))
    return tuple(changes)
