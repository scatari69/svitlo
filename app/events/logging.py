import logging

from app.events.models import DomainEvent, MonitorHealthChanged, PowerStateChanged, ScheduleChanged

logger = logging.getLogger(__name__)


async def log_event(event: DomainEvent) -> None:
    """Log explicit safe fields rather than entire objects or provider payloads."""
    if isinstance(event, PowerStateChanged):
        details = (
            f"device_id={event.device_id} previous_state={event.previous_state.value} "
            f"new_state={event.new_state.value}"
        )
    elif isinstance(event, MonitorHealthChanged):
        details = (
            f"device_id={event.device_id} previous_health={event.previous_health.value} "
            f"new_health={event.new_health.value}"
        )
    elif isinstance(event, ScheduleChanged):
        details = (
            f"version_id={event.new_version_id} schedule_date={event.schedule_date.isoformat()}"
        )
    else:
        details = ""
    context: dict[str, str | int] = {
        "event_type": type(event).__name__,
        "event_id": str(event.event_id),
        "detected_at": event.detected_at.isoformat(),
    }
    if isinstance(event, (PowerStateChanged, MonitorHealthChanged)):
        context["device_id"] = event.device_id
        context["user_id"] = event.user_id
    elif isinstance(event, ScheduleChanged):
        context["provider"] = event.provider
        context["version_id"] = event.new_version_id
    logger.info(
        "Domain event received event_type=%s event_id=%s detected_at=%s %s",
        type(event).__name__,
        event.event_id,
        event.detected_at.isoformat(),
        details,
        extra=context,
    )
