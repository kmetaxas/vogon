"""Rate limiting for check receiver webhooks using Django cache."""

from __future__ import annotations

from django.core.cache import cache

_RATE_LIMIT_KEY_PREFIX = "receiver_rate_limit"
_RATE_LIMIT_MAX = 60
_RATE_LIMIT_TTL = 60


class RateLimiter:
    """Simple per-receiver rate limiter backed by Django cache.

    Each *receiver_id* gets its own counter. The window is fixed at
    :data:`_RATE_LIMIT_TTL` seconds and allows up to
    :data:`_RATE_LIMIT_MAX` requests.
    """

    @staticmethod
    def is_allowed(receiver_id: str) -> bool:
        """Increment the counter for *receiver_id* and return True if within limit."""
        key = f"{_RATE_LIMIT_KEY_PREFIX}:{receiver_id}"
        try:
            count = cache.incr(key)
        except ValueError:
            # Key does not exist yet — initialise at 1 with TTL.
            cache.add(key, 1, timeout=_RATE_LIMIT_TTL)
            count = 1
        return count <= _RATE_LIMIT_MAX

    @staticmethod
    def get_remaining(receiver_id: str) -> int:
        """Return the number of remaining requests in the current window."""
        key = f"{_RATE_LIMIT_KEY_PREFIX}:{receiver_id}"
        count: int = cache.get(key, 0)
        return max(_RATE_LIMIT_MAX - count, 0)

    @staticmethod
    def reset(receiver_id: str) -> None:
        """Reset the counter for *receiver_id*."""
        key = f"{_RATE_LIMIT_KEY_PREFIX}:{receiver_id}"
        cache.delete(key)
