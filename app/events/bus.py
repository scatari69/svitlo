from collections.abc import Awaitable, Callable
from typing import cast

from app.events.models import DomainEvent

type EventHandler[E: DomainEvent] = Callable[[E], Awaitable[None]]


class EventBus:
    """Await subscribers in registration order; surface failures after dispatching to all."""

    def __init__(self) -> None:
        self._handlers: list[tuple[type[DomainEvent], EventHandler[DomainEvent]]] = []

    def subscribe[E: DomainEvent](self, event_type: type[E], handler: EventHandler[E]) -> None:
        subscription = (event_type, cast(EventHandler[DomainEvent], handler))
        if subscription not in self._handlers:
            self._handlers.append(subscription)

    async def publish(self, event: DomainEvent) -> None:
        # ponytail: in-process delivery; add a transactional outbox when durable delivery is needed.
        errors: list[Exception] = []
        for event_type, handler in tuple(self._handlers):
            if isinstance(event, event_type):
                try:
                    await handler(event)
                except Exception as error:
                    errors.append(error)
        if errors:
            raise ExceptionGroup("Domain event handlers failed", errors)
