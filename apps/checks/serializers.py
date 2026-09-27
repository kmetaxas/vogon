from rest_framework import serializers

from apps.checks.models import (
    Check,
    CheckActionLog,
    CheckExecution,
    CheckHealthState,
    CheckVersion,
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
            "enabled",
            "schedule_type",
            "schedule_expression",
            "timezone",
            "execution_mode",
            "evaluation_config",
            "notification_config",
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


class CheckHealthStateSerializer(serializers.ModelSerializer):
    check_name = serializers.CharField(source="check.name", read_only=True)

    class Meta:
        model = CheckHealthState
        fields = [
            "id",
            "check",
            "check_name",
            "current_state",
            "previous_state",
            "state_changed_at",
            "consecutive_failures",
            "consecutive_successes",
            "updated_at",
        ]
        read_only_fields = ["id", "updated_at", "check_name"]


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
