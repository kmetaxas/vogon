"""Action Dispatcher for Check notifications."""

import hashlib
import logging
from datetime import timedelta
from typing import Any, cast

from django.utils import timezone

from apps.checks.enums import HealthState, Severity
from apps.checks.models import Check, CheckActionLog, CheckExecution

logger = logging.getLogger(__name__)


class ActionDispatcher:
    """Routes Check findings to notification channels with conditions, dedup, cooldown, retry."""

    DEFAULT_COOLDOWN_SECONDS = 300
    MAX_RETRIES = 3

    @classmethod
    def dispatch(
        cls,
        check: Check,
        execution: CheckExecution,
        findings: list[dict],
    ) -> list[CheckActionLog]:
        notification_config = cast(dict[str, Any], check.notification_config or {})
        actions_config = notification_config.get("actions", [])

        if not actions_config:
            return []

        dispatched = []
        previous_health = cls._get_previous_health(check, execution)
        current_health = cls._health_state(execution.health_state if execution else None)

        for action_config in actions_config:
            if not cls._should_dispatch(
                action_config,
                check,
                execution,
                findings,
                previous_health,
                current_health,
            ):
                continue

            if cls._is_duplicate_suppressed(check, action_config):
                logger.info(
                    "Action suppressed by cooldown: %s for check %s",
                    action_config.get("type"),
                    check.id,
                )
                continue

            action_log = cls._create_action_log(check, execution, action_config, findings)
            dispatched.append(action_log)

        return dispatched

    @classmethod
    def _should_dispatch(
        cls,
        action_config: dict,
        check: Check,
        execution: CheckExecution,
        findings: list[dict],
        previous_health: HealthState,
        current_health: HealthState,
    ) -> bool:
        condition = action_config.get("condition", "on_completion")

        if condition == "on_completion":
            return execution.execution_status == CheckExecution.ExecutionStatus.COMPLETED

        if condition == "on_failure":
            return execution.execution_status in (
                CheckExecution.ExecutionStatus.FAILED,
                CheckExecution.ExecutionStatus.CANCELLED,
            )

        if condition == "on_recovery":
            return (
                previous_health in (HealthState.CRITICAL, HealthState.DEGRADED)
                and current_health == HealthState.HEALTHY
            )

        if condition == "on_health_transition":
            from_state = action_config.get("from_state")
            to_state = action_config.get("to_state")
            prev_match = from_state is None or previous_health.value == from_state
            curr_match = to_state is None or current_health.value == to_state
            return prev_match and curr_match and previous_health != current_health

        if condition == "on_severity_threshold":
            threshold = cls._severity(
                action_config.get("severity_threshold", Severity.CRITICAL.value)
            )
            for finding in findings:
                finding_severity = cls._severity(finding.get("severity"))
                if finding_severity == threshold:
                    return True
                if threshold == Severity.WARNING and finding_severity == Severity.CRITICAL:
                    return True
            return False

        if condition == "periodic_reminder":
            return True

        return False

    @classmethod
    def _is_duplicate_suppressed(cls, check: Check, action_config: dict) -> bool:
        cooldown_seconds = action_config.get("cooldown_seconds", cls.DEFAULT_COOLDOWN_SECONDS)
        action_type = action_config.get("type", CheckActionLog.ActionType.WEBHOOK)
        condition = action_config.get("condition", "on_completion")
        dedup_key = cls._dedup_key(check, action_type, condition)
        window_start = timezone.now() - timedelta(seconds=cooldown_seconds)

        recent = (
            CheckActionLog.objects.filter(
                check=check,
                action_type=action_type,
                created_at__gte=window_start,
                payload__dedup_key=dedup_key,
            )
            .exclude(delivery_status=CheckActionLog.DeliveryStatus.FAILED)
            .first()
        )

        return recent is not None

    @classmethod
    def _create_action_log(
        cls,
        check: Check,
        execution: CheckExecution,
        action_config: dict,
        findings: list[dict],
    ) -> CheckActionLog:
        action_type = action_config.get("type", CheckActionLog.ActionType.WEBHOOK)
        recipient = action_config.get("target", "")
        condition = action_config.get("condition", "on_completion")
        dedup_key = cls._dedup_key(check, action_type, condition)

        payload: dict[str, Any] = {
            "check_name": check.name,
            "check_id": str(check.id),
            "execution_id": str(execution.id) if execution else None,
            "health_state": execution.health_state if execution else HealthState.UNKNOWN.value,
            "findings": findings,
            "action_config": action_config,
            "dedup_key": dedup_key,
        }

        return CheckActionLog.objects.create(
            check=check,
            execution=execution,
            action_type=action_type,
            recipient=recipient,
            payload=payload,
            delivery_status=CheckActionLog.DeliveryStatus.PENDING,
        )

    @classmethod
    def _get_previous_health(
        cls,
        check: Check,
        execution: CheckExecution | None = None,
    ) -> HealthState:
        try:
            executions = CheckExecution.objects.filter(check=check).exclude(
                health_state=CheckExecution.HealthState.UNKNOWN
            )
            if execution:
                executions = executions.exclude(id=execution.id)
            previous = executions.order_by("-triggered_at").first()
            if previous:
                return cls._health_state(previous.health_state)
        except Exception:
            logger.exception("Failed to load previous health state for check %s", check.id)
        return HealthState.UNKNOWN

    @classmethod
    def _dedup_key(cls, check: Check, action_type: str, condition: str) -> str:
        return hashlib.sha256(f"{check.id}:{action_type}:{condition}".encode()).hexdigest()

    @classmethod
    def _health_state(cls, value: Any) -> HealthState:
        try:
            return HealthState(value)
        except (TypeError, ValueError):
            return HealthState.UNKNOWN

    @classmethod
    def _severity(cls, value: Any) -> Severity | None:
        try:
            return Severity(value)
        except (TypeError, ValueError):
            return None


class RetryManager:
    """Manages retry logic for failed action deliveries."""

    @classmethod
    def should_retry(cls, action_log: CheckActionLog) -> bool:
        if action_log.delivery_status != CheckActionLog.DeliveryStatus.FAILED:
            return False
        if action_log.retry_count >= ActionDispatcher.MAX_RETRIES:
            return False
        return True

    @classmethod
    def backoff_seconds(cls, retry_count: int) -> int:
        return min(int(2**retry_count * 5), 300)
