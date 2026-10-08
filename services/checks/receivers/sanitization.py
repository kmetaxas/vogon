"""Prompt injection detection and payload sanitization for check receivers."""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# Control characters to strip: 0x00-0x1F except \t (0x09), \n (0x0A), \r (0x0D).
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

# Maximum length for a single sanitized string value.
_MAX_STRING_LENGTH = 4096

# Known prompt injection patterns (case-insensitive).
_SUSPICIOUS_PATTERNS = [
    "ignore previous instructions",
    "ignore all previous",
    "disregard",
    "system prompt",
    "you are now",
    "DAN",
    "jailbreak",
]

_SUSPICIOUS_RE = re.compile(
    "|".join(re.escape(p) for p in _SUSPICIOUS_PATTERNS),
    re.IGNORECASE,
)


def _sanitize_string(value: str) -> str:
    """Sanitize a single string value.

    Strips control characters (except \t, \n, \r), truncates to 4096 chars,
    and escapes backticks. Replaces suspicious content with [SANITIZED].
    """
    text = value.encode("utf-8", errors="replace").decode("utf-8", errors="replace")
    if _SUSPICIOUS_RE.search(text):
        return "[SANITIZED]"
    text = _CONTROL_CHARS_RE.sub("", text)
    if len(text) > _MAX_STRING_LENGTH:
        text = text[: _MAX_STRING_LENGTH - 3] + "..."
    text = text.replace("`", "\\`")
    return text


class PromptInjectionSanitizer:
    """Sanitize inbound payloads and detect prompt injection attempts."""

    @classmethod
    def sanitize(cls, payload: dict) -> dict:
        """Recursively walk *payload* and sanitize all string values.

        Returns a new dict; the input is not mutated.
        """
        result = cls._sanitize_value(payload)
        return result if isinstance(result, dict) else {}

    @classmethod
    def _sanitize_value(cls, value: object) -> object:
        if isinstance(value, str):
            return _sanitize_string(value)
        if isinstance(value, dict):
            return {k: cls._sanitize_value(v) for k, v in value.items()}
        if isinstance(value, list):
            return [cls._sanitize_value(item) for item in value]
        return value

    @classmethod
    def sanitize_labels(cls, labels: dict) -> dict:
        """Sanitize label values only.

        Returns a new dict; the input is not mutated.
        """
        return {
            k: _sanitize_string(v) if isinstance(v, str) else cls._sanitize_value(v)
            for k, v in labels.items()
        }

    @classmethod
    def is_suspicious(cls, payload: dict) -> bool:
        """Return True if any string value in *payload* matches a known injection pattern."""
        return cls._check_suspicious(payload)

    @classmethod
    def _check_suspicious(cls, value: object) -> bool:
        if isinstance(value, str):
            return bool(_SUSPICIOUS_RE.search(value))
        if isinstance(value, dict):
            return any(cls._check_suspicious(v) for v in value.values())
        if isinstance(value, list):
            return any(cls._check_suspicious(item) for item in value)
        return False
