import base64
import re
from typing import Literal
from urllib.parse import urlsplit

from aiogram.utils.token import validate_token
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

from app.services.validation import validate_url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    database_url: SecretStr = Field(repr=False)
    redis_url: SecretStr = Field(repr=False)
    telegram_bot_token: SecretStr | None = Field(default=None, repr=False)
    encryption_key: SecretStr | None = Field(default=None, repr=False)
    public_base_url: str = "http://localhost:8000"
    bot_enabled: bool = True
    monitoring_enabled: bool = True
    monitor_failure_threshold: int = Field(default=3, ge=2, le=20)
    monitor_health_warnings_enabled: bool = False
    monitor_health_recovery_enabled: bool = False
    monitor_health_warning_cooldown_seconds: int = Field(default=3600, ge=60, le=604800)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    shutdown_timeout: float = Field(default=10, ge=1, le=60)
    health_timeout: float = Field(default=3.0, gt=0, le=30)
    schedule_sources: dict[str, str] = Field(
        default_factory=lambda: {
            "svitlo_live": "https://svitlo-proxy.svitlo-proxy.workers.dev",
            "dtek": "https://dtek-api.svitlo-proxy.workers.dev",
        }
    )
    schedule_cache_seconds: int = Field(default=600, ge=10, le=3600)
    schedule_timeout: float = Field(default=8, gt=0, le=30)
    schedule_retries: int = Field(default=2, ge=0, le=5)

    @field_validator("schedule_sources")
    @classmethod
    def validate_schedule_sources(cls, sources: dict[str, str]) -> dict[str, str]:
        if any(not re.fullmatch(r"[a-z][a-z0-9_-]{0,99}", provider) for provider in sources):
            raise ValueError("Invalid schedule provider ID")
        for url in sources.values():
            validate_url(url)
        return {provider: url.strip() for provider, url in sources.items()}

    @field_validator("encryption_key", mode="before")
    @classmethod
    def empty_key(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("encryption_key")
    @classmethod
    def validate_encryption_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None:
            try:
                raw = value.get_secret_value().encode("ascii")
                if (
                    len(raw) != 44
                    or len(base64.b64decode(raw, altchars=b"-_", validate=True)) != 32
                ):
                    raise ValueError
            except (ValueError, UnicodeError):
                raise ValueError("ENCRYPTION_KEY must be a Fernet key") from None
        return value

    @field_validator("public_base_url")
    @classmethod
    def validate_public_url(cls, value: str) -> str:
        return validate_url(value)

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: SecretStr) -> SecretStr:
        try:
            url = make_url(value.get_secret_value())
            if url.drivername != "postgresql+asyncpg" or not url.host or not url.database:
                raise ValueError
        except Exception:
            raise ValueError("DATABASE_URL must be a PostgreSQL asyncpg URL") from None
        return value

    @field_validator("redis_url")
    @classmethod
    def validate_redis_url(cls, value: SecretStr) -> SecretStr:
        try:
            url = urlsplit(value.get_secret_value())
            if url.scheme not in {"redis", "rediss"} or not url.hostname:
                raise ValueError
            _ = url.port
        except ValueError:
            raise ValueError("REDIS_URL must be a Redis URL") from None
        return value

    @model_validator(mode="after")
    def validate_bot(self) -> "Settings":
        if self.bot_enabled:
            if self.telegram_bot_token is None:
                raise ValueError("TELEGRAM_BOT_TOKEN is required when BOT_ENABLED=true")
            try:
                validate_token(self.telegram_bot_token.get_secret_value())
            except Exception:
                raise ValueError("TELEGRAM_BOT_TOKEN is invalid") from None
        return self
