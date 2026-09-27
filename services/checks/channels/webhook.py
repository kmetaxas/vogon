"""Webhook notification channel for Check alerts."""

import logging

import httpx

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 3


class WebhookChannel:
    """POSTs Check alert JSON to configured webhook URLs."""

    @classmethod
    def send(cls, check, execution, action_log) -> dict:
        """Send a webhook alert for a Check.

        Returns:
            dict with status, status_code, error (if any)
        """
        recipient = action_log.recipient
        if not recipient:
            return {"status": "failed", "error": "No webhook URL configured"}

        payload = cls._build_payload(check, execution, action_log)
        headers = (
            action_log.payload.get("action_config", {}).get("headers", {})
            if action_log.payload
            else {}
        )
        headers.setdefault("Content-Type", "application/json")

        for attempt in range(MAX_RETRIES):
            try:
                with httpx.Client(timeout=DEFAULT_TIMEOUT) as client:
                    resp = client.post(recipient, json=payload, headers=headers)

                if resp.status_code < 300:
                    return {
                        "status": "sent",
                        "status_code": resp.status_code,
                        "attempt": attempt + 1,
                    }

                logger.warning(
                    "Webhook attempt %d/%d for check %s returned %d",
                    attempt + 1,
                    MAX_RETRIES,
                    check.id,
                    resp.status_code,
                )
            except Exception as exc:
                logger.exception(
                    "Webhook attempt %d/%d failed for check %s",
                    attempt + 1,
                    MAX_RETRIES,
                    check.id,
                )
                if attempt == MAX_RETRIES - 1:
                    return {"status": "failed", "error": str(exc), "attempt": attempt + 1}

        return {"status": "failed", "error": f"Max retries ({MAX_RETRIES}) exceeded"}

    @classmethod
    def _build_payload(cls, check, execution, action_log) -> dict:
        """Build JSON payload for webhook."""
        health_state = execution.health_state if execution else "unknown"
        findings = (
            execution.evaluation_result.get("findings", [])
            if execution and execution.evaluation_result
            else []
        )

        return {
            "check_name": check.name,
            "check_id": str(check.id),
            "health_state": health_state,
            "findings": findings,
            "summary": (
                execution.evaluation_result.get("summary", "")
                if execution and execution.evaluation_result
                else ""
            ),
            "timestamp": (
                execution.triggered_at.isoformat() if execution and execution.triggered_at else None
            ),
            "execution_id": str(execution.id) if execution else None,
        }
