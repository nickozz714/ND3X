# app/utils/crypto.py

from functools import lru_cache

from cryptography.fernet import Fernet

from component.config import settings


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    # Built on first use, not at import, so a missing key never blocks startup —
    # only the features that actually encrypt/decrypt (mail + provider registry
    # secrets) fail, and with a clear message.
    key = settings.MAIL_SECRET_KEY
    if not key:
        raise RuntimeError(
            "MAIL_SECRET_KEY is not configured; set it to read or write encrypted "
            "mail settings and provider secrets."
        )
    return Fernet(key.encode())


def encrypt_value(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt_value(value: str) -> str:
    return _fernet().decrypt(value.encode()).decode()