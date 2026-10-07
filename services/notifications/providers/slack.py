"""Slack notification provider for delivering notifications via webhook."""

import logging

import httpx

from services.notifications.registry import BaseNotificationProvider
from services.notifications.renderers import NotificationRenderer

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 3


class SlackProvider(BaseNotificationProvider):
    """Sends notifications to Slack via incoming webhook URL."""

    provider_type = "slack"
    display_name = "Slack"
    config_schema = {
        "type": "object",
        "title": "Slack",
        "properties": {
            "webhook_url": {
                "type": "string",
                "format": "uri",
                "title": "Webhook URL",
                "description": "Incoming webhook URL. Treated as a secret.",
                "writeOnly": True,
            },
            "channel_label": {
                "type": "string",
                "title": "Channel label",
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
            return {
                "status": "failed",
                "error": "No webhook URL configured.",
            }

        rendered = NotificationRenderer.render(notification, "slack")
        payload = {
            "text": rendered["title"],
            "blocks": [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": rendered["title"],
                        "emoji": True,
                    },
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"*Severity:* {rendered['severity'].upper()}\n"
                            f"*Attention:* {rendered['attention'].upper()}\n"
                            f"*Source:* {rendered['source_type']}:{rendered['source_id']}"
                        ),
                    },
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": rendered["summary"][:3000],
                    },
                },
            ],
        }

        for attempt in range(MAX_RETRIES):
            try:
                with httpx.Client(timeout=DEFAULT_TIMEOUT) as client:
                    resp = client.post(webhook_url, json=payload)

                if resp.status_code < 300:
                    return {
                        "status": "sent",
                        "status_code": resp.status_code,
                        "attempt": attempt + 1,
                    }

                logger.warning(
                    "Slack attempt %d/%d returned %d",
                    attempt + 1,
                    MAX_RETRIES,
                    resp.status_code,
                )
            except Exception as exc:
                logger.exception(
                    "Slack attempt %d/%d failed",
                    attempt + 1,
                    MAX_RETRIES,
                )
                if attempt == MAX_RETRIES - 1:
                    return {"status": "failed", "error": str(exc), "attempt": attempt + 1}

        return {"status": "failed", "error": f"Max retries ({MAX_RETRIES}) exceeded"}
