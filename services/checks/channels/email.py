"""Email notification channel for Check alerts."""

import logging

from django.conf import settings
from django.core.mail import send_mail
from django.template.loader import render_to_string

logger = logging.getLogger(__name__)


class EmailChannel:
    """Sends Check alert emails via Django's send_mail."""

    @classmethod
    def send(cls, check, execution, action_log) -> dict:
        """Send an email alert for a Check.

        Args:
            check: Check model instance
            execution: CheckExecution model instance
            action_log: CheckActionLog model instance

        Returns:
            dict with status, error (if any)
        """
        recipient = action_log.recipient or getattr(
            settings, "DEFAULT_FROM_EMAIL", "alerts@example.com"
        )

        health_state = execution.health_state if execution else "unknown"
        findings = (
            execution.evaluation_result.get("findings", [])
            if execution and execution.evaluation_result
            else []
        )

        subject = f"[{health_state.upper()}] Check Alert: {check.name}"

        context = {
            "check_name": check.name,
            "check_id": str(check.id),
            "health_state": health_state,
            "findings": findings,
            "summary": (
                execution.evaluation_result.get("summary", "")
                if execution and execution.evaluation_result
                else ""
            ),
            "execution_id": str(execution.id) if execution else None,
        }

        try:
            text_body = render_to_string("checks/emails/check_alert.txt", context)
            html_body = render_to_string("checks/emails/check_alert.html", context)
        except Exception:
            # Fallback if templates don't exist
            text_body = f"Check: {check.name}\nState: {health_state}\nFindings: {len(findings)}\n"
            html_body = f"<p>Check <b>{check.name}</b> is <b>{health_state}</b>.</p>"

        try:
            send_mail(
                subject=subject,
                message=text_body,
                from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "alerts@example.com"),
                recipient_list=[recipient],
                html_message=html_body,
                fail_silently=False,
            )
            return {"status": "sent", "recipient": recipient}
        except Exception as exc:
            logger.exception("Failed to send email for check %s", check.id)
            return {"status": "failed", "error": str(exc), "recipient": recipient}
