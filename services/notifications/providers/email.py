"""Email notification provider for delivering notifications via Django email."""

from django.conf import settings
from django.core.mail import send_mail
from django.template.loader import render_to_string

from services.notifications.registry import BaseNotificationProvider
from services.notifications.renderers import NotificationRenderer


class EmailProvider(BaseNotificationProvider):
    """Sends notifications via Django's email backend."""

    provider_type = "email"
    display_name = "Email"
    config_schema = {
        "type": "object",
        "title": "Email",
        "properties": {
            "recipient_list": {
                "type": "array",
                "items": {"type": "string", "format": "email"},
                "title": "Recipients",
                "description": "Email addresses that receive the notification.",
            },
            "from_email": {
                "type": "string",
                "format": "email",
                "title": "From address",
                "description": "Optional sender. Falls back to DEFAULT_FROM_EMAIL.",
            },
        },
        "required": ["recipient_list"],
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
        from_email = channel_config.get("from_email") or getattr(settings, "DEFAULT_FROM_EMAIL", "")
        recipient_list = channel_config.get("recipient_list", [])
        if not recipient_list:
            return {"status": "failed", "error": "No recipients configured in channel config."}

        rendered = NotificationRenderer.render(notification, "email")
        subject = rendered["title"]
        context = {
            "notification": notification,
            "rendered": rendered,
            "channel": delivery.channel,
        }

        try:
            text_body = render_to_string("notifications/emails/notification.txt", context)
            html_body = render_to_string("notifications/emails/notification.html", context)
        except Exception as exc:
            return {"status": "failed", "error": f"Template rendering failed: {exc}"}

        try:
            send_mail(
                subject=subject,
                message=text_body,
                from_email=from_email,
                recipient_list=recipient_list,
                html_message=html_body,
                fail_silently=False,
            )
            return {"status": "sent", "recipient": recipient_list[0]}
        except Exception as exc:
            return {"status": "failed", "error": str(exc)}
