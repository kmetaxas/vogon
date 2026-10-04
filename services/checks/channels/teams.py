"""Microsoft Teams notification channel for Check alerts."""

import logging

import httpx

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 3

SEVERITY_COLORS = {
    "healthy": "00FF00",  # Green
    "degraded": "FFA500",  # Orange
    "critical": "FF0000",  # Red
    "unknown": "808080",  # Gray
}


class TeamsChannel:
    """Posts Check alerts as MessageCards to Microsoft Teams webhooks."""

    @classmethod
    def send(cls, check, execution, action_log) -> dict:
        recipient = action_log.recipient
        if not recipient:
            return {"status": "failed", "error": "No Teams webhook URL configured"}

        payload = cls._build_card(check, execution, action_log)
        headers = {"Content-Type": "application/json"}

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
                    "Teams webhook attempt %d/%d for check %s returned %d",
                    attempt + 1,
                    MAX_RETRIES,
                    check.id,
                    resp.status_code,
                )
            except Exception as exc:
                logger.exception("Teams webhook attempt %d/%d failed", attempt + 1, MAX_RETRIES)
                if attempt == MAX_RETRIES - 1:
                    return {"status": "failed", "error": str(exc), "attempt": attempt + 1}

        return {"status": "failed", "error": f"Max retries ({MAX_RETRIES}) exceeded"}

    @classmethod
    def _build_card(cls, check, execution, action_log) -> dict:
        health_state = execution.health_state if execution else "unknown"
        findings = (
            execution.evaluation_result.get("findings", [])
            if execution and execution.evaluation_result
            else []
        )
        facts = []
        for finding in findings[:5]:  # Limit to 5 findings
            facts.append(
                {
                    "name": f"[{finding.get('severity', 'unknown').upper()}]",
                    "value": finding.get("message", ""),
                }
            )

        if not facts:
            facts.append({"name": "Status", "value": f"No findings — state is {health_state}"})

        return {
            "@type": "MessageCard",
            "@context": "https://schema.org/extensions",
            "themeColor": SEVERITY_COLORS.get(health_state, "808080"),
            "summary": f"Check Alert: {check.name} is {health_state}",
            "sections": [
                {
                    "activityTitle": f"Check Alert: {check.name}",
                    "activitySubtitle": f"State: {health_state.upper()}",
                    "facts": facts,
                    "markdown": True,
                }
            ],
            "potentialAction": [
                {
                    "@type": "OpenUri",
                    "name": "View Execution Detail",
                    "targets": [
                        {
                            "os": "default",
                            "uri": (
                                f"/checks/{check.id}/executions/{execution.id if execution else ''}"
                            ),
                        }
                    ],
                }
            ],
        }
