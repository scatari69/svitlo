from datetime import UTC, datetime, timedelta
from random import Random
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analytics.reports import format_statistics
from app.analytics.service import (
    AnalyticsPeriod,
    AnalyticsService,
    ReportingWindow,
    calculate_statistics,
    period_window,
)
from app.models import Device, PowerInterval, ScheduleVersion
from app.models.enums import MonitorHealth, PowerState

KYIV = ZoneInfo("Europe/Kyiv")


def local(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=KYIV)


def record(
    state: PowerState, start: datetime, end: datetime | None, device_id: int = 1
) -> PowerInterval:
    return PowerInterval(device_id=device_id, state=state, started_at=start, ended_at=end)


@pytest.mark.parametrize(
    ("period", "start", "end"),
    [
        (AnalyticsPeriod.TODAY, local(2026, 10, 2), local(2026, 10, 3)),
        (AnalyticsPeriod.YESTERDAY, local(2026, 10, 1), local(2026, 10, 2)),
        (AnalyticsPeriod.LAST_7_DAYS, local(2026, 9, 26), local(2026, 10, 3)),
        (AnalyticsPeriod.CALENDAR_WEEK, local(2026, 9, 28), local(2026, 10, 5)),
        (AnalyticsPeriod.CURRENT_MONTH, local(2026, 10, 1), local(2026, 11, 1)),
        (AnalyticsPeriod.PREVIOUS_MONTH, local(2026, 9, 1), local(2026, 10, 1)),
    ],
)
def test_all_kyiv_periods(period: AnalyticsPeriod, start: datetime, end: datetime) -> None:
    window = period_window(period, now=local(2026, 10, 2, 15))
    assert (window.start, window.end) == (start, end)
    assert window.start.tzinfo == UTC and window.end.tzinfo == UTC


@pytest.mark.parametrize(
    ("now", "period", "start", "end"),
    [
        (
            local(2027, 1, 1, 12),
            AnalyticsPeriod.PREVIOUS_MONTH,
            local(2026, 12, 1),
            local(2027, 1, 1),
        ),
        (
            local(2026, 12, 31, 12),
            AnalyticsPeriod.CURRENT_MONTH,
            local(2026, 12, 1),
            local(2027, 1, 1),
        ),
        (local(2024, 3, 12), AnalyticsPeriod.PREVIOUS_MONTH, local(2024, 2, 1), local(2024, 3, 1)),
        (local(2025, 3, 12), AnalyticsPeriod.PREVIOUS_MONTH, local(2025, 2, 1), local(2025, 3, 1)),
        (local(2026, 9, 28), AnalyticsPeriod.CALENDAR_WEEK, local(2026, 9, 28), local(2026, 10, 5)),
        (
            local(2026, 10, 4, 23),
            AnalyticsPeriod.CALENDAR_WEEK,
            local(2026, 9, 28),
            local(2026, 10, 5),
        ),
        (
            local(2026, 10, 5),
            AnalyticsPeriod.CALENDAR_WEEK,
            local(2026, 10, 5),
            local(2026, 10, 12),
        ),
        (
            datetime(2026, 10, 1, 21, 30, tzinfo=UTC),
            AnalyticsPeriod.TODAY,
            local(2026, 10, 2),
            local(2026, 10, 3),
        ),
    ],
)
def test_calendar_edges(
    now: datetime, period: AnalyticsPeriod, start: datetime, end: datetime
) -> None:
    result = period_window(period, now=now)
    assert result.start == start and result.end == end


@pytest.mark.parametrize(
    ("now", "period", "hours"),
    [
        (local(2026, 3, 30, 12), AnalyticsPeriod.YESTERDAY, 23),
        (local(2026, 10, 26, 12), AnalyticsPeriod.YESTERDAY, 25),
        (local(2026, 3, 29, 12), AnalyticsPeriod.CALENDAR_WEEK, 167),
        (local(2026, 10, 25, 12), AnalyticsPeriod.CALENDAR_WEEK, 169),
        (local(2026, 3, 29, 12), AnalyticsPeriod.LAST_7_DAYS, 167),
        (local(2026, 10, 25, 12), AnalyticsPeriod.LAST_7_DAYS, 169),
        (local(2026, 4, 15), AnalyticsPeriod.PREVIOUS_MONTH, 31 * 24 - 1),
        (local(2026, 11, 15), AnalyticsPeriod.PREVIOUS_MONTH, 31 * 24 + 1),
        (local(2024, 3, 12), AnalyticsPeriod.PREVIOUS_MONTH, 29 * 24),
        (local(2025, 3, 12), AnalyticsPeriod.PREVIOUS_MONTH, 28 * 24),
    ],
)
def test_calendar_period_lengths_follow_real_elapsed_time(
    now: datetime, period: AnalyticsPeriod, hours: int
) -> None:
    window = period_window(period, now=now)
    assert window.end - window.start == timedelta(hours=hours)


def test_all_metrics_include_unknown_and_current_outage() -> None:
    start, end = local(2026, 10, 2), local(2026, 10, 3)
    now = local(2026, 10, 2, 12)
    history = [
        record(PowerState.ON, start, local(2026, 10, 2, 4)),
        record(PowerState.OFF, local(2026, 10, 2, 4), local(2026, 10, 2, 6)),
        record(PowerState.UNKNOWN, local(2026, 10, 2, 6), local(2026, 10, 2, 7)),
        record(PowerState.ON, local(2026, 10, 2, 7), local(2026, 10, 2, 9)),
        record(PowerState.OFF, local(2026, 10, 2, 9), None),
    ]
    stats = calculate_statistics(history, ReportingWindow(start, end), now=now)
    assert stats.on_duration == timedelta(hours=6)
    assert stats.off_duration == timedelta(hours=5)
    assert stats.unknown_duration == timedelta(hours=1)
    assert stats.known_duration == timedelta(hours=11)
    assert stats.availability_percentage == pytest.approx(100 * 6 / 11)
    assert stats.outage_count == 2
    assert stats.longest_outage == timedelta(hours=3)
    assert stats.average_outage_duration == timedelta(hours=2, minutes=30)
    assert stats.measured_until == now and stats.measured_until.tzinfo == UTC
    assert history[-1].ended_at is None  # Calculation never closes an interval.


@pytest.mark.parametrize("state", list(PowerState))
def test_open_interval_crosses_both_window_boundaries(state: PowerState) -> None:
    window = ReportingWindow(local(2026, 10, 2), local(2026, 10, 3))
    stats = calculate_statistics(
        [record(state, local(2026, 10, 1, 16), None)], window, now=local(2026, 10, 4)
    )
    assert getattr(stats, f"{state.value}_duration") == timedelta(days=1)
    assert stats.outage_count == (1 if state == PowerState.OFF else 0)
    assert stats.availability_percentage == (
        None if state == PowerState.UNKNOWN else 100 if state == PowerState.ON else 0
    )


@pytest.mark.parametrize("state", list(PowerState))
def test_closed_interval_never_counts_future_time(state: PowerState) -> None:
    window = ReportingWindow(local(2026, 10, 2), local(2026, 10, 3))
    stats = calculate_statistics(
        [record(state, window.start, window.end)], window, now=local(2026, 10, 2, 10)
    )
    assert getattr(stats, f"{state.value}_duration") == timedelta(hours=10)


@pytest.mark.parametrize(
    ("period", "now", "start", "end", "hours"),
    [
        (
            AnalyticsPeriod.YESTERDAY,
            local(2026, 10, 3, 12),
            local(2026, 10, 1, 23),
            local(2026, 10, 2, 2),
            2,
        ),
        (
            AnalyticsPeriod.CALENDAR_WEEK,
            local(2026, 10, 4, 12),
            local(2026, 9, 27, 23),
            local(2026, 9, 28, 2),
            2,
        ),
        (
            AnalyticsPeriod.PREVIOUS_MONTH,
            local(2026, 10, 3, 12),
            local(2026, 8, 31, 23),
            local(2026, 9, 1, 2),
            2,
        ),
        (
            AnalyticsPeriod.PREVIOUS_MONTH,
            local(2026, 10, 3, 12),
            local(2026, 9, 30, 22),
            local(2026, 10, 1, 3),
            2,
        ),
        (
            AnalyticsPeriod.CALENDAR_WEEK,
            local(2026, 10, 4, 12),
            local(2026, 10, 4, 10),
            local(2026, 10, 5, 2),
            2,
        ),
    ],
)
def test_outages_crossing_midnight_week_and_month_are_clipped(
    period: AnalyticsPeriod, now: datetime, start: datetime, end: datetime, hours: int
) -> None:
    window = period_window(period, now=now)
    stats = calculate_statistics([record(PowerState.OFF, start, end)], window, now=now)
    assert stats.off_duration == timedelta(hours=hours)
    assert stats.longest_outage == stats.average_outage_duration == stats.off_duration
    assert stats.outage_count == 1
    assert stats.unknown_duration == stats.measured_until - window.start - stats.off_duration


def test_half_open_boundaries_and_zero_length_intervals_do_not_count() -> None:
    window = ReportingWindow(local(2026, 10, 2), local(2026, 10, 3))
    history = [
        record(PowerState.OFF, window.start - timedelta(hours=1), window.start),
        record(PowerState.OFF, window.end, window.end + timedelta(hours=1)),
        record(
            PowerState.OFF, window.start + timedelta(hours=2), window.start + timedelta(hours=2)
        ),
    ]
    stats = calculate_statistics(history, window, now=window.end)
    assert stats.outage_count == 0 and stats.off_duration == timedelta()
    assert stats.unknown_duration == timedelta(days=1)
    assert stats.availability_percentage is None
    assert stats.longest_outage == stats.average_outage_duration == timedelta()


def test_missing_history_and_internal_gaps_remain_unknown() -> None:
    start = local(2026, 10, 2)
    window = ReportingWindow(start, start + timedelta(days=1))
    empty = calculate_statistics([], window, now=window.end)
    assert empty.unknown_duration == timedelta(days=1) and empty.availability_percentage is None
    stats = calculate_statistics(
        [
            record(PowerState.ON, start + timedelta(hours=2), start + timedelta(hours=3)),
            record(PowerState.OFF, start + timedelta(hours=4), start + timedelta(hours=6)),
            record(PowerState.UNKNOWN, start + timedelta(hours=6), start + timedelta(hours=7)),
        ],
        window,
        now=window.end,
    )
    assert stats.unknown_duration == timedelta(hours=21)
    assert stats.on_duration == timedelta(hours=1) and stats.off_duration == timedelta(hours=2)
    assert stats.availability_percentage == pytest.approx(100 / 3)


@pytest.mark.parametrize("middle", [PowerState.ON, PowerState.UNKNOWN, None])
def test_unknown_gaps_and_confirmed_on_break_outage_episodes(middle: PowerState | None) -> None:
    start = local(2026, 10, 2)
    window = ReportingWindow(start, start + timedelta(hours=3))
    history = [
        record(PowerState.OFF, start, start + timedelta(hours=1)),
        record(PowerState.OFF, start + timedelta(hours=2), window.end),
    ]
    if middle is not None:
        history.append(record(middle, start + timedelta(hours=1), start + timedelta(hours=2)))
    stats = calculate_statistics(history, window, now=window.end)
    assert stats.outage_count == 2
    assert stats.longest_outage == stats.average_outage_duration == timedelta(hours=1)


def test_adjacent_off_records_are_one_episode_and_input_order_is_irrelevant() -> None:
    start = local(2026, 10, 2)
    window = ReportingWindow(start, start + timedelta(hours=3))
    history = [
        record(PowerState.OFF, start + timedelta(hours=n), start + timedelta(hours=n + 1))
        for n in reversed(range(3))
    ]
    stats = calculate_statistics(history, window, now=window.end)
    assert stats.outage_count == 1
    assert (
        stats.off_duration
        == stats.longest_outage
        == stats.average_outage_duration
        == timedelta(hours=3)
    )


@pytest.mark.parametrize(("day", "hours"), [(29, 1), (25, 3)])
def test_local_two_to_four_am_outage_uses_utc_duration_across_dst(day: int, hours: int) -> None:
    month = 3 if day == 29 else 10
    start, end = local(2026, month, day, 2), local(2026, month, day, 4)
    window = ReportingWindow(local(2026, month, day), local(2026, month, day + 1))
    stats = calculate_statistics([record(PowerState.OFF, start, end)], window, now=window.end)
    assert stats.off_duration == timedelta(hours=hours)


def test_repeated_autumn_hour_is_not_a_zero_duration_outage() -> None:
    start = local(2026, 10, 25, 3, 30).replace(fold=0)
    end = local(2026, 10, 25, 3, 30).replace(fold=1)
    stats = calculate_statistics(
        [record(PowerState.OFF, start, end)],
        ReportingWindow(local(2026, 10, 25), local(2026, 10, 26)),
        now=local(2026, 10, 26),
    )
    assert stats.off_duration == timedelta(hours=1)


def test_subsecond_precision_is_retained() -> None:
    start = local(2026, 10, 2)
    window = ReportingWindow(start, start + timedelta(seconds=3))
    stats = calculate_statistics(
        [
            record(PowerState.ON, start, start + timedelta(seconds=1, microseconds=500000)),
            record(PowerState.OFF, start + timedelta(seconds=1, microseconds=500000), window.end),
        ],
        window,
        now=window.end,
    )
    assert stats.off_duration == stats.average_outage_duration == timedelta(seconds=1.5)
    assert stats.availability_percentage == 50


@pytest.mark.parametrize(
    "case", ["overlap", "duplicate", "open_overlap", "mixed_device", "reversed", "state", "naive"]
)
def test_invalid_history_is_rejected(case: str) -> None:
    start = local(2026, 10, 2)
    window = ReportingWindow(start, start + timedelta(hours=3))
    first = record(PowerState.ON, start, start + timedelta(hours=2))
    second = record(PowerState.OFF, start + timedelta(hours=2), window.end)
    if case == "overlap":
        second.started_at = start + timedelta(hours=1)
    elif case == "duplicate":
        second = first
    elif case == "open_overlap":
        first.ended_at = None
    elif case == "mixed_device":
        second.device_id = 4
    elif case == "reversed":
        first.ended_at = start - timedelta(seconds=1)
    elif case == "state":
        first.state = "bad"  # type: ignore[assignment]
    else:
        first.started_at = start.replace(tzinfo=None)
    with pytest.raises(ValueError):
        calculate_statistics([first, second], window, now=window.end)


def test_naive_clocks_invalid_bounds_and_future_window_are_rejected() -> None:
    start = local(2026, 10, 2)
    with pytest.raises(ValueError, match="timezone-aware"):
        period_window(AnalyticsPeriod.TODAY, now=start.replace(tzinfo=None))
    with pytest.raises(ValueError, match="timezone-aware"):
        ReportingWindow(start.replace(tzinfo=None), start + timedelta(hours=1))
    for end in [start, start - timedelta(seconds=1)]:
        with pytest.raises(ValueError, match="follow"):
            ReportingWindow(start, end)
    window = ReportingWindow(start, start + timedelta(days=1))
    with pytest.raises(ValueError, match="timezone-aware"):
        calculate_statistics([], window, now=start.replace(tzinfo=None))
    with pytest.raises(ValueError, match="future"):
        calculate_statistics([], window, now=start - timedelta(seconds=1))
    with pytest.raises(ValueError):
        period_window("bad", now=start)  # type: ignore[arg-type]
    stats = calculate_statistics([], window, now=start)
    assert stats.unknown_duration == timedelta() and stats.availability_percentage is None


def test_random_histories_conserve_time_and_known_time_percentage() -> None:
    random = Random(42)
    start = local(2026, 10, 2).astimezone(UTC)
    for _ in range(100):
        history = []
        known = {
            PowerState.ON: timedelta(),
            PowerState.OFF: timedelta(),
            PowerState.UNKNOWN: timedelta(),
        }
        cursor = start
        for _ in range(50):
            state = random.choice(list(PowerState))
            duration = timedelta(seconds=random.randint(1, 3600))
            history.append(record(state, cursor, cursor + duration))
            known[state] += duration
            cursor += duration
        random.shuffle(history)
        stats = calculate_statistics(history, ReportingWindow(start, cursor), now=cursor)
        assert stats.on_duration == known[PowerState.ON]
        assert stats.off_duration == known[PowerState.OFF]
        assert stats.unknown_duration == known[PowerState.UNKNOWN]
        assert stats.known_duration + stats.unknown_duration == cursor - start
        assert stats.availability_percentage == pytest.approx(
            100 * stats.on_duration / stats.known_duration
        )
        assert stats.longest_outage >= stats.average_outage_duration >= timedelta()


@pytest.mark.parametrize("period", list(AnalyticsPeriod))
async def test_service_all_periods_reuses_scoped_history_query(
    sessions: MagicMock, db_session: Session, period: AnalyticsPeriod
) -> None:
    now = local(2026, 10, 2, 12)
    window = period_window(period, now=now)
    db_session.add(record(PowerState.ON, local(2026, 8, 1), None))
    db_session.add(record(PowerState.OFF, local(2026, 8, 1), None, device_id=4))
    db_session.commit()
    stats = await AnalyticsService(sessions).get_statistics(1, 1, period, now=now)
    assert stats.on_duration == min(window.end, now) - window.start
    assert stats.off_duration == stats.unknown_duration == timedelta()
    assert stats.availability_percentage == 100
    assert stats.outage_count == 0


async def test_service_does_not_read_plans_device_state_or_health_for_statistics(
    sessions: MagicMock, db_session: Session
) -> None:
    start = local(2026, 10, 2)
    device = db_session.get(Device, 1)
    assert device
    device.current_power_state = PowerState.OFF
    device.monitor_health = MonitorHealth.UNAVAILABLE
    device.enabled = False
    db_session.add(record(PowerState.ON, start, None))
    db_session.add(
        ScheduleVersion(
            provider="test",
            region="kyiv",
            queue="1.2",
            schedule_date=start.date(),
            content_hash="a" * 64,
            normalized_content={"slots": [{"state": "off"}]},
        )
    )
    db_session.commit()
    stats = await AnalyticsService(sessions).get_statistics(
        1, 1, AnalyticsPeriod.TODAY, now=local(2026, 10, 2, 12)
    )
    assert stats.on_duration == timedelta(hours=12) and stats.off_duration == timedelta()
    assert stats.availability_percentage == 100
    assert len(list(db_session.scalars(select(PowerInterval)))) == 1
    saved = db_session.scalar(select(PowerInterval))
    assert saved is not None and saved.ended_at is None


@pytest.mark.parametrize(("owner", "device_id"), [(2, 1), (1, 4), (1, 99)])
async def test_service_rejects_foreign_and_missing_devices(
    sessions: MagicMock, owner: int, device_id: int
) -> None:
    with pytest.raises(LookupError, match="Device not found"):
        await AnalyticsService(sessions).get_statistics(
            owner, device_id, AnalyticsPeriod.TODAY, now=local(2026, 10, 2, 12)
        )


async def test_service_empty_and_midnight_windows(sessions: MagicMock) -> None:
    service = AnalyticsService(sessions)
    stats = await service.get_statistics(1, 1, AnalyticsPeriod.TODAY, now=local(2026, 10, 2, 12))
    assert stats.unknown_duration == timedelta(hours=12) and stats.availability_percentage is None
    midnight = await service.get_statistics(1, 1, AnalyticsPeriod.TODAY, now=local(2026, 10, 2))
    assert midnight.measured_until == midnight.window.start
    assert midnight.unknown_duration == timedelta() and midnight.outage_count == 0


def test_ukrainian_report_shows_kyiv_times_unknown_and_known_denominator() -> None:
    start = local(2026, 10, 2)
    window = ReportingWindow(start, start + timedelta(days=1))
    stats = calculate_statistics(
        [
            record(PowerState.ON, start, start + timedelta(hours=6)),
            record(PowerState.OFF, start + timedelta(hours=6), start + timedelta(hours=8)),
        ],
        window,
        now=start + timedelta(hours=12),
    )
    text = format_statistics(stats, "Дім")
    assert "02.10.2026 00:00 — 02.10.2026 12:00 (Київ)" in text
    assert "💡 Світло було:\n6 год" in text
    assert "⚪ Немає даних:\n4 год" in text
    assert "Доступність за відомий час" in text and "75%" in text
    assert "Кількість відключень:\n1" in text
    assert "Середня тривалість відключення:\n2 год" in text


def test_empty_report_uses_unknown_and_no_fabricated_percentage() -> None:
    window = ReportingWindow(local(2026, 10, 2), local(2026, 10, 3))
    text = format_statistics(calculate_statistics([], window, now=window.end), "Дім")
    assert "Немає підтверджених даних." in text
    assert "Світло було:\n0 хв" in text
    assert "Немає даних:\n24 год" in text


def test_future_records_do_not_change_an_as_of_report() -> None:
    start = local(2026, 10, 2)
    now = start + timedelta(hours=12)
    window = ReportingWindow(start, start + timedelta(days=1))
    stats = calculate_statistics(
        [
            record(PowerState.ON, start, now),
            record(PowerState.OFF, now, now + timedelta(hours=2)),
            record(PowerState.UNKNOWN, now + timedelta(hours=2), None),
        ],
        window,
        now=now,
    )
    assert stats.on_duration == timedelta(hours=12)
    assert stats.off_duration == stats.unknown_duration == timedelta()
    assert stats.outage_count == 0


def test_adjacent_day_reports_partition_cross_midnight_interval() -> None:
    start, boundary, end = local(2026, 10, 1), local(2026, 10, 2), local(2026, 10, 3)
    history = [
        record(PowerState.ON, start, local(2026, 10, 1, 22)),
        record(PowerState.OFF, local(2026, 10, 1, 22), local(2026, 10, 2, 2)),
        record(PowerState.ON, local(2026, 10, 2, 2), None),
    ]
    first = calculate_statistics(history, ReportingWindow(start, boundary), now=end)
    second = calculate_statistics(history, ReportingWindow(boundary, end), now=end)
    combined = calculate_statistics(history, ReportingWindow(start, end), now=end)
    assert first.off_duration == second.off_duration == timedelta(hours=2)
    assert combined.off_duration == first.off_duration + second.off_duration
    assert combined.on_duration == first.on_duration + second.on_duration
    assert combined.outage_count == 1 and first.outage_count == second.outage_count == 1
    assert combined.longest_outage == timedelta(hours=4)


def test_equivalent_input_timezones_produce_identical_statistics() -> None:
    start = local(2026, 10, 2)
    end = local(2026, 10, 3)
    now = local(2026, 10, 2, 12)
    reference = calculate_statistics(
        [record(PowerState.OFF, start, None)], ReportingWindow(start, end), now=now
    )
    new_york = ZoneInfo("America/New_York")
    converted = calculate_statistics(
        [record(PowerState.OFF, start.astimezone(new_york), None)],
        ReportingWindow(start.astimezone(new_york), end.astimezone(new_york)),
        now=now.astimezone(new_york),
    )
    assert converted == reference


async def test_service_rejects_deleted_devices_without_erasing_history(
    sessions: MagicMock, db_session: Session
) -> None:
    start = local(2026, 10, 2)
    device = db_session.get(Device, 1)
    assert device
    device.deleted_at = start
    db_session.add(record(PowerState.ON, start, None))
    db_session.commit()
    with pytest.raises(LookupError):
        await AnalyticsService(sessions).get_statistics(
            1, 1, AnalyticsPeriod.TODAY, now=start + timedelta(hours=12)
        )
    assert len(list(db_session.scalars(select(PowerInterval)))) == 1
