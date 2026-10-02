from datetime import date, timedelta


def ukrainian_plural(number: int, singular: str, few: str, many: str) -> str:
    if number < 0:
        raise ValueError("Plural counts must be nonnegative")
    if 11 <= number % 100 <= 14:
        return many
    if number % 10 == 1:
        return singular
    if 2 <= number % 10 <= 4:
        return few
    return many


def format_duration(duration: timedelta, *, short: bool = False) -> str:
    if duration < timedelta():
        raise ValueError("Durations must be nonnegative")
    total_minutes = duration // timedelta(minutes=1)
    if total_minutes == 0:
        return "менше хвилини"
    hours, minutes = divmod(total_minutes, 60)
    parts = []
    if hours:
        unit = "год" if short else ukrainian_plural(hours, "година", "години", "годин")
        parts.append(f"{hours} {unit}")
    if minutes:
        unit = "хв" if short else ukrainian_plural(minutes, "хвилина", "хвилини", "хвилин")
        parts.append(f"{minutes} {unit}")
    return " ".join(parts)


MONTHS = (
    "січня",
    "лютого",
    "березня",
    "квітня",
    "травня",
    "червня",
    "липня",
    "серпня",
    "вересня",
    "жовтня",
    "листопада",
    "грудня",
)


def format_ukrainian_date(value: date) -> str:
    return f"{value.day} {MONTHS[value.month - 1]}"
