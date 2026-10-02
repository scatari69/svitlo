import json
import logging
import re
from datetime import UTC, datetime

CONTEXT_FIELDS = {
    "device_id",
    "user_id",
    "provider",
    "source_id",
    "subscription_id",
    "worker",
    "event_type",
    "event_id",
    "backend",
    "state",
    "health",
    "exception_type",
    "version_id",
    "channel_id",
    "chat_id",
    "detected_at",
}


class JsonFormatter(logging.Formatter):
    def __init__(self, secrets: tuple[str, ...] = ()) -> None:
        super().__init__()
        self.secrets = tuple(sorted(filter(None, secrets), key=len, reverse=True))

    def redact(self, message: str) -> str:
        for secret in self.secrets:
            message = message.replace(secret, "[REDACTED]")
        message = re.sub(
            r"\b(?:postgresql(?:\+asyncpg)?|redis|rediss|https?)://\S+", "[URL]", message
        )
        message = re.sub(r"(/api/v1/homeassistant/webhook/)[^\s\"?]+", r"\1[REDACTED]", message)
        message = re.sub(r"\b\d{5,}:[A-Za-z0-9_-]{20,}\b", "[REDACTED]", message)
        message = re.sub(
            r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b", "[REDACTED]", message
        )
        message = re.sub(r"(?i)\bBearer\s+[^\s,;]+", "Bearer [REDACTED]", message)
        message = re.sub(
            r"(?i)\b(community|access_token|webhook_token|bot_token|password|encryption_key)"
            r"([\"']?\s*[:=]\s*)(\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\s,;}]+)",
            r"\1\2[REDACTED]",
            message,
        )
        return message

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, str | int | bool | None] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": self.redact(record.getMessage()),
        }
        for field in CONTEXT_FIELDS:
            value = getattr(record, field, None)
            if isinstance(value, str):
                payload[field] = self.redact(value)
            elif isinstance(value, (int, bool)):
                payload[field] = value
        if record.exc_info and record.exc_info[0]:
            payload["exception_type"] = record.exc_info[0].__name__
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str, secrets: tuple[str, ...]) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter(secrets))
    logging.basicConfig(level=level, handlers=[handler], force=True)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
    # Transport debug frames may contain application-level secrets unknown at startup.
    for name in ("pysnmp", "aiohttp.client", "aiogram.client"):
        logging.getLogger(name).setLevel(logging.WARNING)
