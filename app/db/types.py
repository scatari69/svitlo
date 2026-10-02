from datetime import UTC, datetime

from sqlalchemy import DateTime
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator

from app.services.time import aware_utc


class UTCDateTime(TypeDecorator[datetime]):
    """Reject naive input and normalize both bound and loaded timestamps to UTC."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return aware_utc(value)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            # SQLite returns naive values even for timezone-aware columns.
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
