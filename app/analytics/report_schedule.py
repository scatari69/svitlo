"""Latest due report occurrence and its completed Kyiv calendar period."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from app.analytics.service import KYIV, ReportingWindow
from app.models import ReportSettings
from app.services.reports import ReportKind
from app.services.time import aware_utc


@dataclass(frozen=True)
class DueReport:
    kind: ReportKind
    scheduled_at: datetime
    window: ReportingWindow


def previous_month(first: date) -> date:
    return (first.replace(day=1) - timedelta(days=1)).replace(day=1)


def due_reports(settings: ReportSettings, now: datetime) -> list[DueReport]:
    now = aware_utc(now)
    today = now.astimezone(KYIV).date()
    # ponytail: latest period only; add a backlog queue if every missed report is required.
    result = []
    for kind in ReportKind:
        if not getattr(settings, f"{kind}_enabled"):
            continue
        minutes = getattr(settings, f"{kind}_time")
        if kind == ReportKind.DAILY:
            occurrence = today
        elif kind == ReportKind.WEEKLY:
            occurrence = today - timedelta(days=(today.weekday() - settings.weekly_weekday) % 7)
        else:
            occurrence = today.replace(day=1)

        def scheduled(day: date, local_minutes: int = minutes) -> datetime:
            # fold=0 selects the first repeated hour; nonexistent times roll forward by the DST gap.
            return aware_utc(
                datetime.combine(day, time(local_minutes // 60, local_minutes % 60), KYIV)
            )

        if scheduled(occurrence) > now:
            occurrence = (
                previous_month(occurrence)
                if kind == ReportKind.MONTHLY
                else occurrence - timedelta(days=7 if kind == ReportKind.WEEKLY else 1)
            )
        scheduled_at = scheduled(occurrence)
        enabled_at = getattr(settings, f"{kind}_enabled_at") or settings.created_at
        if scheduled_at < aware_utc(enabled_at):
            continue
        if kind == ReportKind.DAILY:
            start, end = occurrence - timedelta(days=1), occurrence
        elif kind == ReportKind.WEEKLY:
            end = occurrence - timedelta(days=occurrence.weekday())
            start = end - timedelta(days=7)
        else:
            start, end = previous_month(occurrence), occurrence
        result.append(
            DueReport(
                kind,
                scheduled_at,
                ReportingWindow(
                    datetime.combine(start, time(), KYIV), datetime.combine(end, time(), KYIV)
                ),
            )
        )
    return result
