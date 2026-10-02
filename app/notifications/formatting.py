from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.formatting import format_duration
from app.models.enums import PowerState
from app.services.time import aware_utc


def format_power_notification(
    name: str, new_state: PowerState, detected_at: datetime, duration: timedelta
) -> str:
    if new_state not in {PowerState.ON, PowerState.OFF}:
        raise ValueError("Power notifications require a confirmed ON or OFF state")
    title, previous = (
        ("🔴 Зникло світло", "Світло було:")
        if new_state == PowerState.OFF
        else ("🟢 Світло з’явилося", "Світла не було:")
    )
    time = aware_utc(detected_at).astimezone(ZoneInfo("Europe/Kyiv")).strftime("%H:%M")
    return f"{title}\n\n🏠 {name}\n🕒 {time}\n\n{previous}\n{format_duration(duration)}"
