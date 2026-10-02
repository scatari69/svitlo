from datetime import UTC, datetime


def aware_utc(value: datetime) -> datetime:
    if value.utcoffset() is None:
        raise ValueError("Timestamps must be timezone-aware")
    return value.astimezone(UTC)
