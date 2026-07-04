from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

ENCRYPTION_PREFIX = "enc:v1:"


class EncryptionError(RuntimeError):
    pass


def _fernet(secret: str) -> Fernet:
    key = hashlib.sha256(secret.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_value(value: str, secret: str | None) -> str:
    if not secret:
        return value
    if value.startswith(ENCRYPTION_PREFIX):
        return value

    token = _fernet(secret).encrypt(value.encode("utf-8")).decode("utf-8")
    return f"{ENCRYPTION_PREFIX}{token}"


def decrypt_value(value: str, secret: str | None) -> str:
    if not value.startswith(ENCRYPTION_PREFIX):
        return value
    if not secret:
        raise EncryptionError(
            "Encrypted configuration found but PSF_ENCRYPTION_SECRET is not set.",
        )

    token = value[len(ENCRYPTION_PREFIX) :]
    try:
        return _fernet(secret).decrypt(token.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise EncryptionError("Unable to decrypt configuration with current secret.") from exc