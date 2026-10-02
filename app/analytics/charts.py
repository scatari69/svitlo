"""Pillow report charts, independent of Telegram and planned schedules."""

from datetime import datetime, time, timedelta
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from app.analytics.service import KYIV, PowerStatistics, ReportingWindow, calculate_statistics
from app.formatting import format_duration
from app.models import PowerInterval


def render_report_chart(
    statistics: PowerStatistics, history: list[PowerInterval], name: str
) -> bytes:
    font_paths = (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/source-foundry-hack-fonts/Hack-Regular.ttf",
    )
    font_path = next((path for path in font_paths if Path(path).exists()), None)
    if font_path is None:
        raise FileNotFoundError("Install DejaVu Sans fonts for Ukrainian report charts")
    font = ImageFont.truetype(font_path, 18)
    title_font = ImageFont.truetype(font_path, 28)
    day = statistics.window.start.astimezone(KYIV).date()
    rows = []
    while datetime.combine(day, time(), KYIV) < statistics.measured_until:
        start = datetime.combine(day, time(), KYIV)
        end = datetime.combine(day + timedelta(days=1), time(), KYIV)
        window = ReportingWindow(
            max(statistics.window.start, start), min(statistics.window.end, end)
        )
        rows.append((day, calculate_statistics(history, window, now=statistics.measured_until)))
        day += timedelta(days=1)
    image = Image.new("RGB", (1080, 370 + max(1, len(rows)) * 34), "#f8fafc")
    draw = ImageDraw.Draw(image)
    draw.text((28, 20), "Фактична наявність електроенергії", font=title_font, fill="#0f172a")
    draw.text((28, 64), name[:65], font=font, fill="#0f172a")
    start, end = statistics.window.start.astimezone(KYIV), statistics.window.end.astimezone(KYIV)
    draw.text(
        (28, 96),
        f"{start:%d.%m.%Y} — {(end - timedelta(days=1)):%d.%m.%Y} • Київ",
        font=font,
        fill="#334155",
    )
    values = (statistics.on_duration, statistics.off_duration, statistics.unknown_duration)
    colors = ("#16a34a", "#dc2626", "#94a3b8")
    labels = ("Світло було", "Світла не було", "Немає даних")
    for i, (label, value, color) in enumerate(zip(labels, values, colors, strict=True)):
        x = 28 + i * 350
        draw.rectangle((x, 140, x + 18, 158), fill=color)
        draw.text((x + 26, 136), label, font=font, fill="#0f172a")
        draw.text(
            (x, 172),
            format_duration(value, short=True) if value else "0 хв",
            font=title_font,
            fill="#0f172a",
        )
    known = statistics.availability_percentage
    draw.text(
        (28, 219),
        "Доступність за відомий час: "
        + (f"{known:.1f}%" if known is not None else "немає підтверджених даних"),
        font=font,
        fill="#0f172a",
    )
    longest = (
        format_duration(statistics.longest_outage, short=True)
        if statistics.outage_count
        else "0 хв"
    )
    draw.text(
        (28, 249),
        f"Відключень: {statistics.outage_count} • Найдовше: {longest}",
        font=font,
        fill="#0f172a",
    )
    draw.text(
        (28, 280),
        "За кожен день: світло було / світла не було / немає даних (год)",
        font=font,
        fill="#334155",
    )
    for i, (day, stats) in enumerate(rows):
        y, bar_x = 320 + i * 34, 115.0
        draw.text((28, y), f"{day:%d.%m}", font=font, fill="#0f172a")
        durations = (stats.on_duration, stats.off_duration, stats.unknown_duration)
        total = sum(durations, timedelta()).total_seconds()
        for index, (value, color) in enumerate(zip(durations, colors, strict=True)):
            width = 555 * value.total_seconds() / total if total else 0
            if width:
                draw.rectangle((bar_x, y, bar_x + width, y + 22), fill=color)
                if index == 2:
                    for hatch in range(int(bar_x), int(bar_x + width), 9):
                        draw.line((hatch, y, min(hatch + 8, bar_x + width), y + 22), fill="#475569")
            bar_x += width
        draw.text(
            (690, y),
            " / ".join(f"{value.total_seconds() / 3600:.1f}" for value in durations),
            font=font,
            fill="#0f172a",
        )
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()
