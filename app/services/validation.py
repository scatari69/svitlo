import ipaddress
import re
from urllib.parse import urlsplit, urlunsplit


def validate_host(value: str) -> str:
    value = value.strip()
    try:
        ipaddress.ip_address(value)
        return value
    except ValueError:
        pass
    if (
        len(value) > 253
        or not value
        or any(
            not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
            for label in value.rstrip(".").split(".")
        )
    ):
        raise ValueError("Invalid hostname or IP address")
    return value


def validate_url(value: str) -> str:
    url = urlsplit(value.strip())
    if (
        url.scheme not in {"http", "https"}
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
    ):
        raise ValueError("Invalid HTTP URL")
    validate_host(url.hostname)
    if url.port is not None and not 1 <= url.port <= 65535:
        raise ValueError("Invalid port")
    return urlunsplit(url).rstrip("/")


def validate_name(value: str) -> str:
    value = value.strip()
    if not 1 <= len(value) <= 100 or any(ord(character) < 32 for character in value):
        raise ValueError("Names must contain 1 to 100 printable characters")
    return value
