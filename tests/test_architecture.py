"""Guard the transport/domain boundaries reviewed for the production service."""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "app"


@pytest.mark.parametrize(
    ("directory", "forbidden"),
    [
        ("bot/handlers", ("sqlalchemy", "app.repositories", "app.monitoring")),
        ("monitoring", ("aiogram", "app.bot", "app.notifications", "app.schedules")),
        ("analytics", ("aiogram", "app.bot", "app.schedules")),
        ("schedules", ("aiogram", "app.bot", "app.monitoring")),
    ],
)
def test_domain_boundaries(directory: str, forbidden: tuple[str, ...]) -> None:
    for path in (ROOT / directory).rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            imports = (
                [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else []
            )
            for module in imports:
                assert not any(
                    module == prefix or module.startswith(prefix + ".") for prefix in forbidden
                ), f"{path.relative_to(ROOT)} imports {module} across its domain boundary"


@pytest.mark.parametrize("formatter", ["device", "health", "schedule"])
def test_presentation_rejects_naive_timestamps(formatter: str) -> None:
    from datetime import date, datetime

    from app.bot.device_text import format_time
    from app.notifications.health import format_health_notification
    from app.notifications.schedule_formatting import format_schedule_notification
    from app.schedules.normalizer import normalize_half_hours

    naive = datetime(2026, 10, 2, 12)
    with pytest.raises(ValueError, match="timezone-aware"):
        if formatter == "device":
            format_time(naive)
        elif formatter == "health":
            format_health_notification("Дім", naive, recovery=False)
        else:
            schedule = normalize_half_hours("test", "kyiv", "1.2", date(2026, 10, 2), {})
            format_schedule_notification(schedule, "Київська область", now=naive)
