import re


def validate_oid(value: str) -> str:
    value = value.strip().lstrip(".")
    if not re.fullmatch(r"[0-2](?:\.\d+)+", value):
        raise ValueError("Invalid numeric OID")
    parts = list(map(int, value.split(".")))
    if parts[0] < 2 and parts[1] > 39 or any(part > 2**32 - 1 for part in parts):
        raise ValueError("Invalid numeric OID")
    return value
