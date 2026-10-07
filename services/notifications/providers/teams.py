"""Microsoft Teams notification provider for delivering notifications via webhook."""

import logging

import httpx

from services.notifications.registry import BaseNotificationProvider
from services.notifications.renderers import NotificationRenderer

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 3

SEVERITY_COLORS = {
    "critical": "FF0000",
    "warning": "FFA500",
    "info": "808080",
}


class TeamsProvider(BaseNotificationProvider):
    """Posts notifications as MessageCards to Microsoft Teams webhooks."""

    provider_type = "teams"
    display_name = "Microsoft Teams"
    config_schema = {
        "type": "object",
        "title": "Microsoft Teams",
        "properties": {
            "webhook_url": {
                "type": "string",
                "format": "uri",
                "title": "Webhook URL",
                "description": "Incoming Teams webhook URL. Treated as a secret.",
                "writeOnly": True,
            },
            "display_name": {
                "type": "string",
                "title": "Display name",
                "description": "Optional display label for this channel.",
            },
        },
        "required": ["webhook_url"],
    }

    def send(self, notification, delivery) -> dict:
        """Deliver ``notification`` using ``delivery`` configuration.

        Args:
            notification: The notification to deliver (``Notification`` model).
            delivery: The delivery record describing the target and config
                (``NotificationDelivery`` model).

        Returns:
            A result dict describing the outcome.
        """
        channel_config = delivery.channel.merged_config or {}
        webhook_url = channel_config.get("webhook_url")
        if not webhook_url:
            return {"status": "failed", "error": "No Teams webhook URL configured."}

        rendered = NotificationRenderer.render(notification, "teams")
        payload = self._build_card(rendered)
        headers = {"Content-Type": "application/json"}

        for attempt in range(MAX_RETRIES):
            try:
                with httpx.Client(timeout=DEFAULT_TIMEOUT) as client:
                    resp = client.post(webhook_url, json=payload, headers=headers)

                if resp.status_code < 300:
                    return {
                        "status": "sent",
                        "status_code": resp.status_code,
                        "attempt": attempt + 1,
                    }

                logger.warning(
                    "Teams webhook attempt %d/%d returned %d",
                    attempt + 1,
                    MAX_RETRIES,
                    resp.status_code,
                )
            except Exception as exc:
                logger.exception("Teams webhook attempt %d/%d failed", attempt + 1, MAX_RETRIES)
                if attempt == MAX_RETRIES - 1:
                    return {"status": "failed", "error": str(exc), "attempt": attempt + 1}

        return {"status": "failed", "error": f"Max retries ({MAX_RETRIES}) exceeded"}

    def _build_card(self, rendered: dict) -> dict:
        """Build a MessageCard payload from a rendered notification dict."""
        theme_color = SEVERITY_COLORS.get(rendered["severity"], "808080")
        return {
            "@type": "MessageCard",
            "@context": "https://schema.org/extensions",
            "themeColor": theme_color,
            "summary": rendered["title"],
            "sections": [
                {
                    "activityTitle": rendered["title"],
                    "activitySubtitle": (
                        f"Severity: {rendered['severity'].upper()} | "
                        f"Attention: {rendered['attention'].upper()}"
                    ),
                    "facts": [
                        {
                            "name": "Source",
                            "value": f"{rendered['source_type']}:{rendered['source_id']}",
                        },
                        {
                            "name": "Summary",
                            "value": rendered["summary"][:200],
                        },
                    ],
                    "markdown": True,
                }
            ],
        }
