import json
import logging

from app.logging import JsonFormatter


def test_structured_logging_redacts_secrets_and_exception_details() -> None:
    formatter = JsonFormatter(("private-token", "database-secret", "redis-secret"))
    record = logging.LogRecord(
        name="app.test",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="Failed %s %s %s https://example.com/private-token",
        args=(
            "private-token",
            "database-secret",
            "redis-secret",
        ),
        exc_info=(ValueError, ValueError("private-token"), None),
    )
    output = formatter.format(record)
    payload = json.loads(output)
    assert payload["level"] == "ERROR"
    assert payload["logger"] == "app.test"
    assert payload["timestamp"].endswith("+00:00")
    assert payload["exception_type"] == "ValueError"
    assert "private-token" not in output
    assert "database-secret" not in output
    assert "redis-secret" not in output
    assert "https://" not in output
