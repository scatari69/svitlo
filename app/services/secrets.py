from importlib import import_module

from pydantic import SecretStr


def encrypt_secret(key: SecretStr | None, value: str) -> bytes:
    if key is None:
        raise RuntimeError("ENCRYPTION_KEY is required for credential storage")
    cipher = import_module("cryptography.fernet").Fernet(key.get_secret_value())
    return bytes(cipher.encrypt(value.encode()))


def decrypt_secret(key: SecretStr | None, value: bytes) -> str:
    if key is None:
        raise RuntimeError("ENCRYPTION_KEY is required for credential storage")
    cipher = import_module("cryptography.fernet").Fernet(key.get_secret_value())
    return str(cipher.decrypt(value), "utf-8")
