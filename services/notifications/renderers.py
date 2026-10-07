"""Notification renderers transform semantic notification fields into normalized dicts.

Providers consume the normalized dict to build transport-specific payloads without
directly accessing model fields.
"""

from __future__ import annotations

from typing import Any


class NotificationRenderer:
    """Transforms a ``Notification`` into a provider-agnostic normalized dict."""

    @staticmethod
    def render(notification: Any, provider_type: str) -> dict:
        """Return a normalized dict of notification fields.

        Args:
            notification: The ``Notification`` model instance to render.
            provider_type: The target provider type (e.g. ``"email"``,
                ``"slack"``, ``"teams"``). Included for future provider-specific
                customization; V1 ignores it.

        Returns:
            A dict with keys: ``title``, ``summary``, ``details``, ``severity``,
            ``attention``, ``source_type``, ``source_id``, ``url``.
        """
        return {
            "title": notification.title,
            "summary": notification.summary,
            "details": notification.details,
            "severity": notification.severity,
            "attention": notification.attention,
            "source_type": notification.source_type,
            "source_id": notification.source_id,
            "url": f"/notifications/{notification.id}/",
        }
