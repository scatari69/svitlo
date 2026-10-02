import pytest
from pydantic import SecretStr, ValidationError

from app.config import Settings


def test_settings_load_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://user:secret@localhost/svitlo")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("BOT_ENABLED", "false")
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    settings = Settings(_env_file=None)
    assert settings.bot_enabled is False
    assert settings.log_level == "WARNING"
    assert "secret" not in repr(settings)
    assert "postgresql" not in repr(settings)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("database_url", "sqlite:///database-secret"),
        ("redis_url", "https://redis-secret"),
        ("redis_url", "redis://localhost:invalid"),
        ("health_timeout", 0),
        ("log_level", "INVALID"),
        ("bot_enabled", True),
    ],
)
def test_invalid_settings_rejected(settings: Settings, field: str, value: object) -> None:
    data = settings.model_dump()
    data[field] = value
    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None, **data)
    assert "database-secret" not in str(error.value)
    assert "redis-secret" not in str(error.value)


def test_invalid_bot_token_is_not_exposed(settings: Settings) -> None:
    data = settings.model_dump()
    data.update(bot_enabled=True, telegram_bot_token=SecretStr("private-invalid-token"))
    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None, **data)
    assert "private-invalid-token" not in str(error.value)


def test_encryption_key_is_optional_but_validated(settings: Settings) -> None:
    import base64

    from pydantic import ValidationError

    values = settings.model_dump()
    assert Settings(_env_file=None, **{**values, "encryption_key": ""}).encryption_key is None
    good = base64.urlsafe_b64encode(bytes(range(32))).decode()
    result = Settings(_env_file=None, **{**values, "encryption_key": good})
    assert result.encryption_key and result.encryption_key.get_secret_value() == good
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{**values, "encryption_key": "bad-key"})


def test_schedule_source_validation_preserves_endpoint_path(settings: Settings) -> None:
    assert Settings.validate_schedule_sources({"source": "https://provider.example/feed/"}) == {
        "source": "https://provider.example/feed/"
    }
    with pytest.raises(ValueError):
        Settings.validate_schedule_sources({"bad\nname": "https://provider.example"})
    with pytest.raises(ValueError):
        Settings.validate_schedule_sources({"source": "https://user:secret@provider.example"})
