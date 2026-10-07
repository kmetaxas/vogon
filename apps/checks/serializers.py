from croniter import croniter
from django.core.exceptions import ValidationError
from rest_framework import serializers

from apps.checks.models import (
    Check,
    CheckActionLog,
    CheckExecution,
    CheckVersion,
    normalize_execution_budget,
)


class CheckSerializer(serializers.ModelSerializer):
    llm_provider_name = serializers.CharField(source="llm_provider.name", read_only=True)
    target_scope_name = serializers.CharField(source="target_scope.name", read_only=True)
    organization_name = serializers.CharField(source="organization.name", read_only=True)
    created_by_username = serializers.CharField(source="created_by.username", read_only=True)

    class Meta:
        model = Check
        fields = [
            "id",
            "organization",
            "organization_name",
            "name",
            "description",
            "instructions",
            "enabled",
            "schedule_type",
            "schedule_expression",
            "timezone",
            "notification_config",
            "notification_policy_name",
            "execution_budget",
            "llm_provider",
            "llm_provider_name",
            "target_scope",
            "target_scope_name",
            "created_by",
            "created_by_username",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "created_at",
            "updated_at",
            "organization_name",
            "llm_provider_name",
            "target_scope_name",
            "created_by_username",
        ]
        extra_kwargs = {
            "schedule_expression": {"allow_blank": True},
        }

    def validate(self, attrs):
        schedule_expression = attrs.get(
            "schedule_expression",
            getattr(self.instance, "schedule_expression", None),
        )
        schedule_type = attrs.get(
            "schedule_type",
            getattr(self.instance, "schedule_type", None),
        )

        if schedule_expression is None or not str(schedule_expression).strip():
            raise serializers.ValidationError("Schedule expression cannot be empty.")

        expression = str(schedule_expression).strip()

        if schedule_type == Check.ScheduleType.CRON:
            if not croniter.is_valid(expression):
                raise serializers.ValidationError(f"Invalid cron expression: {expression}")
        elif schedule_type == Check.ScheduleType.INTERVAL:
            try:
                seconds = int(expression)
            except (TypeError, ValueError):
                raise serializers.ValidationError("Interval must be a positive integer (seconds).")
            if seconds <= 0:
                raise serializers.ValidationError("Interval must be a positive integer (seconds).")

        execution_budget = attrs.get(
            "execution_budget",
            getattr(self.instance, "execution_budget", None),
        )
        if execution_budget is not None:
            try:
                attrs["execution_budget"] = normalize_execution_budget(execution_budget)
            except ValidationError as exc:
                raise serializers.ValidationError(exc.messages) from exc

        return attrs


class CheckVersionSerializer(serializers.ModelSerializer):
    check_name = serializers.CharField(source="check.name", read_only=True)

    class Meta:
        model = CheckVersion
        fields = [
            "id",
            "check",
            "check_name",
            "version_number",
            "definition_snapshot",
            "created_at",
        ]
        read_only_fields = ["id", "created_at", "check_name"]


class CheckExecutionSerializer(serializers.ModelSerializer):
    check_name = serializers.CharField(source="check.name", read_only=True)

    class Meta:
        model = CheckExecution
        fields = [
            "id",
            "check",
            "check_name",
            "version",
            "triggered_at",
            "started_at",
            "completed_at",
            "execution_status",
            "health_state",
            "evaluation_result",
            "evidence",
            "resolved_targets",
            "error_message",
            "input_tokens",
            "output_tokens",
            "cost",
            "actions_triggered",
            "dry_run",
        ]
        read_only_fields = ["id", "triggered_at"]


class CheckActionLogSerializer(serializers.ModelSerializer):
    check_name = serializers.CharField(source="check.name", read_only=True)

    class Meta:
        model = CheckActionLog
        fields = [
            "id",
            "check",
            "check_name",
            "execution",
            "action_type",
            "recipient",
            "payload",
            "delivery_status",
            "sent_at",
            "error_message",
            "retry_count",
            "created_at",
        ]
        read_only_fields = ["id", "created_at", "check_name"]
