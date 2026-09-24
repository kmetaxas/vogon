"""Symmetric encryption utilities for sensitive values at rest.

Uses Fernet (AES-128-CBC + HMAC-SHA256) from the cryptography library.
Requires ``ENCRYPTION_KEY`` environment variable (base64-encoded 32-byte key).
If not set, a derived key from ``SECRET_KEY`` is used as fallback.
"""

import base64
import hashlib
import os

from cryptography.fernet import Fernet
from django.conf import settings


def _get_fernet():
    raw = os.environ.get("ENCRYPTION_KEY", "")
    if raw:
        key = base64.urlsafe_b64encode(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    else:
        # Derive a stable 32-byte key from SECRET_KEY for local/dev use.
        digest = hashlib.sha256(settings.SECRET_KEY.encode()).digest()
        key = base64.urlsafe_b64encode(digest)
    return Fernet(key)


_f = None


def _fernet():
    global _f
    if _f is None:
        _f = _get_fernet()
    return _f


def encrypt(value: str) -> str:
    """Encrypt a plaintext string, return base64 ciphertext."""
    if not value:
        return value
    result: str = _fernet().encrypt(value.encode()).decode()
    return result


def decrypt(value: str) -> str:
    """Decrypt a base64 ciphertext string, return plaintext."""
    if not value:
        return value
    result: str = _fernet().decrypt(value.encode()).decode()
    return result


def maybe_decrypt(value: str) -> str:
    """Decrypt if the value looks like a Fernet token; otherwise return as-is.

    Useful for backwards compatibility with existing unencrypted rows.
    """
    if not value:
        return value
    try:
        result: str = _fernet().decrypt(value.encode()).decode()
        return result
    except Exception:
        return value
