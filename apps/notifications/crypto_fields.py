"""Encrypted credential field helpers.

Provides a descriptor and a mixin that transparently encrypt values on
assignment and decrypt them on access using :mod:`services.crypto` (Fernet).

Both helpers store ciphertext in a backing model field (conventionally named
``_<name>_encrypted``) and never log or expose the plaintext.
"""

from __future__ import annotations

import json
from typing import Any

from services.crypto import encrypt, maybe_decrypt


class EncryptedCharField:
    """Descriptor that stores an encrypted value in a backing model field.

    Usage::

        class MyModel(models.Model):
            _api_key_encrypted = models.CharField(max_length=500, blank=True)
            api_key = EncryptedCharField("_api_key_encrypted")

    Reading ``instance.api_key`` returns the decrypted plaintext (or the raw
    value if it is not a valid Fernet token, for backwards compatibility).
    Assigning ``instance.api_key = "secret"`` encrypts before storing.
    ``None`` and empty strings are passed through unchanged.
    """

    def __init__(self, encrypted_field_name: str) -> None:
        self.encrypted_field_name = encrypted_field_name

    def __get__(self, instance: Any, owner: type | None = None) -> Any:
        if instance is None:
            return self
        raw = getattr(instance, self.encrypted_field_name, "")
        return maybe_decrypt(raw)

    def __set__(self, instance: Any, value: Any) -> None:
        setattr(instance, self.encrypted_field_name, encrypt(value))

    def __delete__(self, instance: Any) -> None:
        setattr(instance, self.encrypted_field_name, "")


class EncryptedJSONField:
    def __init__(self, encrypted_field_name: str) -> None:
        self.encrypted_field_name = encrypted_field_name

    def __get__(self, instance: Any, owner: type | None = None) -> Any:
        if instance is None:
            return self
        raw = getattr(instance, self.encrypted_field_name, "")
        if not raw:
            return {}
        try:
            value = json.loads(maybe_decrypt(raw))
        except Exception:
            return {}
        if not isinstance(value, dict):
            return {}
        return value

    def __set__(self, instance: Any, value: Any) -> None:
        if not isinstance(value, dict):
            raise TypeError("EncryptedJSONField value must be a dict.")
        setattr(instance, self.encrypted_field_name, encrypt(json.dumps(value)))

    def __delete__(self, instance: Any) -> None:
        setattr(instance, self.encrypted_field_name, "")


class EncryptedFieldMixin:
    """Mixin offering explicit encrypt/decrypt helpers for model properties.

    Usage::

        class MyModel(EncryptedFieldMixin, models.Model):
            _api_key_encrypted = models.CharField(max_length=500, blank=True)

            @property
            def api_key(self) -> str:
                return self.decrypt_field("_api_key_encrypted")

            @api_key.setter
            def api_key(self, value: str) -> None:
                self.encrypt_field("_api_key_encrypted", value)
    """

    def encrypt_field(self, field_name: str, value: Any) -> None:
        """Encrypt ``value`` and store it in ``field_name``."""
        setattr(self, field_name, encrypt(value))

    def decrypt_field(self, field_name: str) -> Any:
        """Return the decrypted value stored in ``field_name``."""
        return maybe_decrypt(getattr(self, field_name, ""))
