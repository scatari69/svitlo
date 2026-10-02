import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from io import BytesIO
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import SendPhoto
from PIL import Image
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.analytics.charts import render_report_chart
from app.analytics.report_schedule import due_reports
from app.analytics.service import KYIV, ReportingWindow, calculate_statistics
from app.models import (
    Device,
    DeviceNotificationChannel,
    NotificationChannel,
    PowerInterval,
    ReportDelivery,
    ReportSettings,
)
from app.models.enums import ChannelType, PowerState
from app.services.reports import ReportKind, ReportSettingsService, settings_text
from app.workers.reports import ReportWorker


@pytest.fixture(autouse=True)
def chart_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    # Exercise real Pillow output with a deterministic transport, like the SQLite async fixture.
    async def render_in_test[T](function: Callable[..., T], *args: object) -> T:
        return function(*args)

    monkeypatch.setattr("app.workers.reports.asyncio.to_thread", render_in_test)


def local(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=KYIV)


NOW = local(2026, 10, 2, 12)


def configuration(**values: object) -> ReportSettings:
    defaults = dict(
        device_id=1,
        channel_id=1,
        daily_enabled=True,
        weekly_enabled=True,
        monthly_enabled=True,
        daily_time=540,
        weekly_time=540,
        monthly_time=540,
        weekly_weekday=0,
        created_at=local(2020, 1, 1),
    )
    defaults.update(values)
    return ReportSettings(**defaults)


def test_latest_due_reports_cover_completed_periods() -> None:
    reports = {due.kind: due for due in due_reports(configuration(), NOW)}
    assert reports[ReportKind.DAILY].scheduled_at == local(2026, 10, 2, 9)
    assert reports[ReportKind.DAILY].window == ReportingWindow(
        local(2026, 10, 1), local(2026, 10, 2)
    )
    assert reports[ReportKind.WEEKLY].scheduled_at == local(2026, 9, 28, 9)
    assert reports[ReportKind.WEEKLY].window == ReportingWindow(
        local(2026, 9, 21), local(2026, 9, 28)
    )
    assert reports[ReportKind.MONTHLY].scheduled_at == local(2026, 10, 1, 9)
    assert reports[ReportKind.MONTHLY].window == ReportingWindow(
        local(2026, 9, 1), local(2026, 10, 1)
    )
    assert all(due.scheduled_at.tzinfo == UTC and due.window.end <= NOW for due in reports.values())


@pytest.mark.parametrize("kind", list(ReportKind))
def test_activation_prevents_historical_backlog_and_duplicate_enable_cutoffs(
    kind: ReportKind,
) -> None:
    settings = configuration()
    setattr(settings, f"{kind}_enabled_at", NOW)
    assert kind not in {due.kind for due in due_reports(settings, NOW)}
    # Independent flags still produce their due periods.
    assert len(due_reports(settings, NOW)) == 2


def test_before_daily_time_only_previous_occurrence_is_due() -> None:
    settings = configuration(daily_time=10 * 60 + 15)
    due = next(
        d for d in due_reports(settings, local(2026, 10, 2, 10, 14)) if d.kind == ReportKind.DAILY
    )
    assert due.scheduled_at == local(2026, 10, 1, 10, 15)
    assert due.window.end == local(2026, 10, 1)
    due = next(
        d for d in due_reports(settings, local(2026, 10, 2, 10, 15)) if d.kind == ReportKind.DAILY
    )
    assert due.window.end == local(2026, 10, 2)


@pytest.mark.parametrize("weekday", range(7))
def test_configurable_weekday_always_reports_last_completed_week(weekday: int) -> None:
    settings = configuration(weekly_weekday=weekday)
    now = local(2026, 10, 5 + weekday, 12)
    due = next(d for d in due_reports(settings, now) if d.kind == ReportKind.WEEKLY)
    assert due.scheduled_at.astimezone(KYIV).weekday() == weekday
    assert due.window == ReportingWindow(local(2026, 9, 28), local(2026, 10, 5))


@pytest.mark.parametrize(
    ("now", "hours"), [(local(2026, 3, 30, 9), 23), (local(2026, 10, 26, 9), 25)]
)
def test_daily_reporting_dst_lengths(now: datetime, hours: int) -> None:
    due = next(d for d in due_reports(configuration(), now) if d.kind == ReportKind.DAILY)
    assert due.window.end - due.window.start == timedelta(hours=hours)


def test_nonexistent_clock_rolls_forward_and_repeated_clock_is_first_fold() -> None:
    settings = configuration(daily_time=3 * 60 + 30)
    # Kyiv jumps from 03:00 to 04:00 in spring; 03:30 is scheduled at 04:30.
    before = next(
        d for d in due_reports(settings, local(2026, 3, 29, 4, 29)) if d.kind == ReportKind.DAILY
    )
    after = next(
        d for d in due_reports(settings, local(2026, 3, 29, 4, 30)) if d.kind == ReportKind.DAILY
    )
    assert before.window.end == local(2026, 3, 28)
    assert after.window.end == local(2026, 3, 29)
    first = next(
        d
        for d in due_reports(settings, local(2026, 10, 25, 3, 30).replace(fold=0))
        if d.kind == ReportKind.DAILY
    )
    second = next(
        d
        for d in due_reports(settings, local(2026, 10, 25, 3, 30).replace(fold=1))
        if d.kind == ReportKind.DAILY
    )
    assert first == second


@pytest.mark.parametrize(
    ("now", "start", "end"),
    [
        (local(2027, 1, 1, 9), local(2026, 12, 1), local(2027, 1, 1)),
        (local(2024, 3, 1, 9), local(2024, 2, 1), local(2024, 3, 1)),
        (local(2026, 10, 1, 8), local(2026, 8, 1), local(2026, 9, 1)),
    ],
)
def test_monthly_report_after_completed_month_and_year_boundary(
    now: datetime, start: datetime, end: datetime
) -> None:
    due = next(d for d in due_reports(configuration(), now) if d.kind == ReportKind.MONTHLY)
    assert due.window == ReportingWindow(start, end)


def test_all_disabled_and_naive_clock_rejected() -> None:
    assert (
        due_reports(
            configuration(daily_enabled=False, weekly_enabled=False, monthly_enabled=False), NOW
        )
        == []
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        due_reports(configuration(), NOW.replace(tzinfo=None))


async def test_settings_independent_flags_pairs_and_history_retention(
    sessions: MagicMock,
    db_session: Session,
) -> None:
    service = ReportSettingsService(sessions)
    channels = await service.channels(2**40, 1)
    private = next(c for c in channels if c.name == "Особисті повідомлення")
    first = await service.settings(
        2**40, 1, 1, kind=ReportKind.DAILY, enabled=True, local_time="22:15"
    )
    assert first.daily_enabled and not first.weekly_enabled and not first.monthly_enabled
    assert first.daily_time == 1335
    row = db_session.get(ReportSettings, (1, 1))
    assert row
    enabled_at = row.daily_enabled_at
    await service.settings(2**40, 1, 1, kind=ReportKind.DAILY, enabled=True)
    db_session.expire_all()
    assert row.daily_enabled_at == enabled_at
    first = await service.settings(
        2**40, 1, 1, kind=ReportKind.WEEKLY, enabled=True, local_time="07:45", weekday=6
    )
    assert first.daily_enabled and first.weekly_enabled and not first.monthly_enabled
    assert first.weekly_weekday == 6 and first.weekly_time == 465
    second = await service.settings(2**40, 1, private.id, kind=ReportKind.MONTHLY, enabled=True)
    assert second.monthly_enabled and not second.daily_enabled
    db_session.add(PowerInterval(device_id=1, state=PowerState.ON, started_at=NOW, ended_at=None))
    db_session.commit()
    disabled = await service.settings(2**40, 1, 1, kind=ReportKind.DAILY, enabled=False)
    assert not disabled.daily_enabled and disabled.weekly_enabled
    assert len(list(db_session.scalars(select(PowerInterval)))) == 1
    text = settings_text(disabled)
    assert "Щоденний: ❌" in text and "Щотижневий: ✅" in text and "Щомісячний: ❌" in text


@pytest.mark.parametrize(("device_id", "channel_id"), [(4, 1), (1, 2), (99, 1), (1, 99)])
async def test_foreign_and_missing_report_targets_rejected(
    sessions: MagicMock,
    db_session: Session,
    device_id: int,
    channel_id: int,
) -> None:
    with pytest.raises(LookupError):
        await ReportSettingsService(sessions).settings(
            2**40, device_id, channel_id, kind=ReportKind.DAILY, enabled=True
        )
    assert list(db_session.scalars(select(ReportSettings))) == []


@pytest.mark.parametrize("clock", ["24:00", "9:00", "12:60", "09:00\n", "secret", ""])
async def test_time_validation_before_database(sessions: MagicMock, clock: str) -> None:
    with pytest.raises(ValueError):
        await ReportSettingsService(sessions).settings(
            2**40, 1, 1, kind=ReportKind.DAILY, local_time=clock
        )
    sessions.begin.assert_not_called()


@pytest.mark.parametrize("weekday", [-1, 7])
async def test_weekday_validation(sessions: MagicMock, weekday: int) -> None:
    with pytest.raises(ValueError):
        await ReportSettingsService(sessions).settings(
            2**40, 1, 1, kind=ReportKind.WEEKLY, weekday=weekday
        )


@pytest.fixture
def reports(db_session: Session) -> None:
    db_session.add(DeviceNotificationChannel(device_id=1, channel_id=1, user_id=1))
    db_session.flush()
    db_session.add(configuration())
    db_session.add(
        PowerInterval(device_id=1, state=PowerState.ON, started_at=local(2026, 9, 1), ended_at=None)
    )
    db_session.commit()


async def test_worker_sends_chart_all_periods_and_restart_is_deduplicated(
    sessions: MagicMock,
    db_session: Session,
    reports: None,
) -> None:
    bot = AsyncMock(spec=Bot)
    worker = ReportWorker(sessions, bot)
    await worker.refresh(now=NOW)
    await worker.refresh(now=NOW)
    await ReportWorker(sessions, bot).refresh(now=NOW)
    assert bot.send_photo.await_count == 3
    for call in bot.send_photo.call_args_list:
        assert call.args[0] == -(2**40)
        assert call.args[1].data.startswith(b"\x89PNG")
        assert call.kwargs["parse_mode"] is None and len(call.kwargs["caption"]) <= 1024
    rows = list(db_session.scalars(select(ReportDelivery)))
    assert len(rows) == 3 and all(row.status == "sent" and row.sent_at for row in rows)
    assert {row.kind for row in rows} == {"daily", "weekly", "monthly"}


async def test_worker_failure_isolation_and_ambiguous_send_not_retried(
    sessions: MagicMock,
    db_session: Session,
    reports: None,
) -> None:
    bot = AsyncMock(spec=Bot)
    bot.send_photo.side_effect = [
        TelegramNetworkError(method=SendPhoto(chat_id=1, photo="file"), message="secret"),
        None,
        None,
    ]
    worker = ReportWorker(sessions, bot)
    await worker.refresh(now=NOW)
    await worker.refresh(now=NOW)
    assert bot.send_photo.await_count == 3
    assert {row.status for row in db_session.scalars(select(ReportDelivery))} == {
        "sent",
        "uncertain",
    }


async def test_known_rejection_and_bounded_rate_limit_retry(
    sessions: MagicMock,
    db_session: Session,
    reports: None,
) -> None:
    bot = AsyncMock(spec=Bot)
    bot.send_photo.side_effect = [
        TelegramRetryAfter(
            method=SendPhoto(chat_id=1, photo="file"), message="slow", retry_after=2
        ),
        None,
        TelegramForbiddenError(method=SendPhoto(chat_id=1, photo="file"), message="secret"),
        None,
    ]
    with patch("app.workers.reports.asyncio.sleep") as sleep:
        await ReportWorker(sessions, bot).refresh(now=NOW)
    sleep.assert_awaited_once_with(2)
    assert bot.send_photo.await_count == 4
    assert {row.status for row in db_session.scalars(select(ReportDelivery))} == {"sent", "failed"}


@pytest.mark.parametrize("condition", ["disabled_channel", "deleted_device", "disabled_reports"])
async def test_worker_respects_current_configuration(
    sessions: MagicMock,
    db_session: Session,
    reports: None,
    condition: str,
) -> None:
    if condition == "disabled_channel":
        row = db_session.get(NotificationChannel, 1)
        assert row
        row.enabled = False
    elif condition == "deleted_device":
        device = db_session.get(Device, 1)
        assert device
        device.deleted_at = NOW
    else:
        settings = db_session.get(ReportSettings, (1, 1))
        assert settings
        settings.daily_enabled = settings.weekly_enabled = settings.monthly_enabled = False
    db_session.commit()
    bot = AsyncMock(spec=Bot)
    await ReportWorker(sessions, bot).refresh(now=NOW)
    bot.send_photo.assert_not_awaited()


async def test_concurrent_workers_claim_each_period_once(
    sessions: MagicMock, reports: None
) -> None:
    bot = AsyncMock(spec=Bot)
    await asyncio.gather(
        ReportWorker(sessions, bot).refresh(now=NOW), ReportWorker(sessions, bot).refresh(now=NOW)
    )
    assert bot.send_photo.await_count == 3


async def test_render_failure_is_retryable_and_other_reports_continue(
    sessions: MagicMock,
    reports: None,
) -> None:
    bot = AsyncMock(spec=Bot)
    with patch(
        "app.workers.reports.render_report_chart",
        side_effect=[ValueError("bad chart"), b"png", b"png"],
    ):
        await ReportWorker(sessions, bot).refresh(now=NOW)
    assert bot.send_photo.await_count == 2
    with patch("app.workers.reports.render_report_chart", return_value=b"png"):
        await ReportWorker(sessions, bot).refresh(now=NOW)
    assert bot.send_photo.await_count == 3


def test_chart_png_dimensions_colors_unknown_and_dst() -> None:
    start, end = local(2026, 10, 25), local(2026, 10, 26)
    history = [
        PowerInterval(
            device_id=1, state=PowerState.ON, started_at=start, ended_at=start + timedelta(hours=12)
        ),
        PowerInterval(
            device_id=1,
            state=PowerState.OFF,
            started_at=start + timedelta(hours=12),
            ended_at=start + timedelta(hours=18),
        ),
    ]
    stats = calculate_statistics(history, ReportingWindow(start, end), now=end)
    assert stats.unknown_duration == timedelta(hours=6)  # 25 actual hours, 13 ON hours across DST.
    data = render_report_chart(stats, history, "Дім")
    image = Image.open(BytesIO(data))
    assert image.format == "PNG" and image.size == (1080, 404)
    counted = image.convert("RGB").getcolors(image.width * image.height)
    assert counted is not None
    colors = {color for _, color in counted}
    assert {(22, 163, 74), (220, 38, 38), (148, 163, 184)} <= colors


@pytest.mark.parametrize(
    "bad", [dict(daily_time=-1), dict(monthly_time=1440), dict(weekly_weekday=7)]
)
def test_database_report_constraints(db_session: Session, bad: dict[str, int]) -> None:
    db_session.add(DeviceNotificationChannel(device_id=1, channel_id=1, user_id=1))
    db_session.flush()
    db_session.add(configuration(**bad))
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


async def test_channel_deletion_preserves_history_and_delivery_deduplication(
    sessions: MagicMock,
    db_session: Session,
    reports: None,
) -> None:
    bot = AsyncMock(spec=Bot)
    await ReportWorker(sessions, bot).refresh(now=NOW)
    db_session.execute(delete(NotificationChannel).where(NotificationChannel.id == 1))
    db_session.commit()
    assert list(db_session.scalars(select(ReportSettings))) == []
    assert len(list(db_session.scalars(select(ReportDelivery)))) == 3
    assert len(list(db_session.scalars(select(PowerInterval)))) == 1


async def test_disable_during_render_prevents_send(
    sessions: MagicMock,
    db_session: Session,
    reports: None,
) -> None:
    bot = AsyncMock(spec=Bot)

    def disable_reports(*args: object) -> bytes:
        row = db_session.get(ReportSettings, (1, 1))
        assert row
        row.daily_enabled = row.weekly_enabled = row.monthly_enabled = False
        db_session.commit()
        return b"png"

    with patch("app.workers.reports.render_report_chart", side_effect=disable_reports):
        await ReportWorker(sessions, bot).refresh(now=NOW)
    bot.send_photo.assert_not_awaited()
    assert list(db_session.scalars(select(ReportDelivery))) == []


async def test_reports_deliver_to_independent_channels_and_disabled_devices(
    sessions: MagicMock,
    db_session: Session,
    reports: None,
) -> None:
    db_session.add(
        NotificationChannel(
            id=3,
            user_id=1,
            telegram_chat_id=-1234567890123,
            name="Family",
            channel_type=ChannelType.GROUP,
        )
    )
    db_session.flush()
    db_session.add(DeviceNotificationChannel(device_id=1, channel_id=3, user_id=1))
    db_session.flush()
    db_session.add(configuration(channel_id=3, weekly_enabled=False, monthly_enabled=False))
    device = db_session.get(Device, 1)
    assert device
    device.enabled = False
    db_session.commit()
    bot = AsyncMock(spec=Bot)
    await ReportWorker(sessions, bot).refresh(now=NOW)
    assert bot.send_photo.await_count == 4
    chats = [call.args[0] for call in bot.send_photo.call_args_list]
    assert chats.count(-(2**40)) == 3 and chats.count(-1234567890123) == 1


async def test_chart_failure_for_one_device_does_not_stop_other_devices(
    sessions: MagicMock,
    db_session: Session,
    reports: None,
) -> None:
    db_session.add(DeviceNotificationChannel(device_id=4, channel_id=2, user_id=2))
    db_session.flush()
    db_session.add(
        configuration(device_id=4, channel_id=2, weekly_enabled=False, monthly_enabled=False)
    )
    db_session.commit()

    def chart(statistics: object, history: object, name: str) -> bytes:
        if name == "Home":
            raise ValueError("invalid device history")
        return b"png"

    bot = AsyncMock(spec=Bot)
    with patch("app.workers.reports.render_report_chart", side_effect=chart):
        await ReportWorker(sessions, bot).refresh(now=NOW)
    bot.send_photo.assert_awaited_once()
    assert bot.send_photo.call_args.args[0] == 2**40


async def test_cancelled_send_is_reserved_and_not_repeated(
    sessions: MagicMock,
    db_session: Session,
    reports: None,
) -> None:
    row = db_session.get(ReportSettings, (1, 1))
    assert row
    row.weekly_enabled = row.monthly_enabled = False
    db_session.commit()
    bot = AsyncMock(spec=Bot)
    bot.send_photo.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await ReportWorker(sessions, bot).refresh(now=NOW)
    bot.send_photo.side_effect = None
    await ReportWorker(sessions, bot).refresh(now=NOW)
    assert bot.send_photo.await_count == 1
    stored = db_session.scalar(select(ReportDelivery))
    assert stored and stored.status == "claimed"


@pytest.fixture(autouse=True)
def verified_destination(monkeypatch: pytest.MonkeyPatch) -> None:
    # Access checks have dedicated transport tests; these tests isolate delivery semantics.
    monkeypatch.setattr("app.notifications.telegram.require_destination", AsyncMock())


async def test_report_refresh_bounds_queued_delivery_tasks(sessions: MagicMock) -> None:
    from app.repositories.reports import ReportRepository

    worker = ReportWorker(sessions, AsyncMock(spec=Bot))
    targets = [
        (configuration(channel_id=index), Device(id=1), NotificationChannel(id=index))
        for index in range(40)
    ]
    active, peak, completed = 0, 0, 0

    async def deliver(*args: object) -> None:
        nonlocal active, peak, completed
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        active -= 1
        completed += 1

    expected = sum(len(due_reports(settings, NOW)) for settings, _, _ in targets)
    with (
        patch.object(ReportRepository, "active", return_value=targets),
        patch.object(worker, "_deliver", side_effect=deliver),
    ):
        await worker.refresh(now=NOW)
    assert completed == expected > 8
    assert peak == 8


async def test_report_writers_lock_device_before_report_settings(
    sessions: MagicMock, db_session: Session
) -> None:
    from app.repositories.devices import DeviceRepository
    from app.repositories.reports import ReportRepository

    get_device = DeviceRepository.get
    get_settings = ReportRepository.settings
    locks: list[str] = []

    async def device_lock(
        repository: DeviceRepository, user_id: int, device_id: int, *, for_update: bool = False
    ) -> Device | None:
        assert for_update, "Report operations must serialize with channel assignment edits"
        locks.append("device")
        return await get_device(repository, user_id, device_id, for_update=for_update)

    async def settings_lock(
        repository: ReportRepository, device_id: int, channel_id: int, *, lock: bool = False
    ) -> ReportSettings | None:
        assert lock and locks[-1] == "device", "Inverted locks can deadlock assignment deletion"
        locks.append("settings")
        return await get_settings(repository, device_id, channel_id, lock=lock)

    with (
        patch.object(DeviceRepository, "get", device_lock),
        patch.object(ReportRepository, "settings", settings_lock),
    ):
        await ReportSettingsService(sessions).settings(2**40, 1, 1)
        device = db_session.get(Device, 1)
        assert device
        async with sessions.begin() as session:
            await ReportWorker(sessions, AsyncMock(spec=Bot))._current(
                session, device, 1, due_reports(configuration(), NOW)[0], NOW
            )
    assert locks == ["device", "settings", "device", "settings"]
