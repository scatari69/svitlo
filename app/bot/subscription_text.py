from app.services.subscriptions import SubscriptionView


def subscription_line(item: SubscriptionView) -> str:
    icon = {"дім": "🏠", "батьки": "👪", "дача": "🏡", "офіс": "🏢"}.get(item.name.casefold(), "🏠")
    return f"{icon} {item.name} — група {item.queue}" + (" ⏸ Вимкнено" if not item.enabled else "")


def list_text(items: list[SubscriptionView]) -> str:
    lines = ["📍 Групи відключень"]
    if not items:
        lines.append("У вас ще немає підписок на графіки відключень.")
    for provider, region in dict.fromkeys((item.provider, item.region) for item in items):
        selected = [item for item in items if (item.provider, item.region) == (provider, region)]
        lines.extend(["", selected[0].region_name, *(subscription_line(item) for item in selected)])
    return "\n".join(lines)


def details_text(item: SubscriptionView, channel_names: list[str]) -> str:
    channels = "\n".join(channel_names[:8]) or "Канали не вибрано. Сповіщення не надсилатимуться."
    if len(channel_names) > 8:
        channels += f"\nТа ще {len(channel_names) - 8}."
    return (
        f"{item.region_name}\n\n{subscription_line(item)}\n"
        f"Підписка: {'увімкнена' if item.enabled else 'вимкнена'}\n\n🔔 Канали:\n{channels}"
    )
