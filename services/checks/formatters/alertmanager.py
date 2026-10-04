# pyright: reportAttributeAccessIssue=false, reportCallIssue=false, reportArgumentType=false
"""Alertmanager-compatible alert formatter for Check results."""

from datetime import UTC, datetime

from apps.checks.models import Check, CheckExecution


class AlertmanagerFormatter:
    """Converts CheckExecution results to Prometheus Alertmanager alert format."""

    SEVERITY_MAP = {
        "healthy": "info",
        "degraded": "warning",
        "critical": "critical",
        "unknown": "none",
    }

    @classmethod
    def format(
        cls,
        check: Check,
        execution: CheckExecution | None = None,
        status: str = "firing",
    ) -> dict:
        """Format a Check execution as an Alertmanager-compatible alert.

        Args:
            check: Check model instance
            execution: CheckExecution model instance (optional)
            status: "firing" or "resolved"

        Returns:
            dict matching Alertmanager webhook receiver payload schema
        """
        health_state = execution.health_state if execution else "unknown"
        findings = (
            execution.evaluation_result.get("findings", [])
            if execution and execution.evaluation_result
            else []
        )
        summary = (
            execution.evaluation_result.get("summary", "")
            if execution and execution.evaluation_result
            else ""
        )

        severity = cls.SEVERITY_MAP.get(health_state, "none")

        # Build annotations
        description_parts = []
        for finding in findings[:10]:  # Limit to 10 findings
            description_parts.append(
                f"[{finding.get('severity', 'unknown')}] {finding.get('message', '')}"
            )
        description = (
            "\n".join(description_parts)
            if description_parts
            else f"Check {check.name} is {health_state}"
        )

        annotations = {
            "summary": summary or f"Check {check.name} is {health_state}",
            "description": description,
        }

        # Build labels
        labels = {
            "alertname": f"Check_{check.name.replace(' ', '_')}",
            "check_name": check.name,
            "check_id": str(check.id),
            "organization": str(check.organization_id),
            "severity": severity,
        }

        # Timestamps
        if execution and execution.triggered_at:
            starts_at = execution.triggered_at.isoformat()
        else:
            starts_at = datetime.now(UTC).isoformat()

        alert = {
            "status": status,
            "labels": labels,
            "annotations": annotations,
            "startsAt": starts_at,
            "endsAt": starts_at if status == "resolved" else None,
            "generatorURL": (f"/checks/{check.id}/executions/{execution.id if execution else ''}"),
        }

        return {
            "version": "4",
            "groupKey": f'{{alertname="{labels["alertname"]}"}}',
            "status": status,
            "receiver": "check-alerts",
            "alerts": [alert],
        }

    @classmethod
    def format_batch(
        cls,
        checks_executions: list[tuple[Check, CheckExecution | None]],
        status: str = "firing",
    ) -> dict:
        """Format multiple Check executions as a batch Alertmanager payload."""
        alerts = []
        for check, execution in checks_executions:
            alert_payload = cls.format(check, execution, status)
            alerts.extend(alert_payload["alerts"])

        return {
            "version": "4",
            "groupKey": '{alertname="CheckAlerts"}',
            "status": status,
            "receiver": "check-alerts",
            "alerts": alerts,
        }
