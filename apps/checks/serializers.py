from croniter import croniter
from django.core.exceptions import ValidationError
from rest_framework import serializers

from apps.checks.models import (
    Check,
    CheckActionLog,
    CheckExecution,
    CheckReceiver,
    CheckVersion,
    ReceiverAdmissionDecision,
    ReceiverEvent,
    normalize_execution_budget,
)
from apps.llm.models import LLMProvider


def build_receiver_webhook_url(receiver, secret, request=None):
    path = f"/api/check-receivers/{receiver.id}/{secret}/"
    if request is not None:
        return request.build_absolute_uri(path)
    return path


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

        expression = "" if schedule_expression is None else str(schedule_expression).strip()

        if schedule_type == Check.ScheduleType.EVENT:
            pass
        elif not expression:
            raise serializers.ValidationError("Schedule expression cannot be empty.")
        elif schedule_type == Check.ScheduleType.CRON:
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


class CheckReceiverSerializer(serializers.ModelSerializer):
    check_name = serializers.CharField(source="check.name", read_only=True)
    secret = serializers.SerializerMethodField()
    webhook_url = serializers.SerializerMethodField()

    class Meta:
        model = CheckReceiver
        fields = [
            "id",
            "organization",
            "check",
            "check_name",
            "name",
            "source_type",
            "enabled",
            "admission_mode",
            "admission_llm_provider",
            "gating_prompt",
            "max_active_executions",
            "dedup_window_seconds",
            "fail_open_on_timeout",
            "secret",
            "webhook_url",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "check_name", "secret", "webhook_url", "created_at", "updated_at"]
        extra_kwargs = {
            "source_type": {"default": "alertmanager"},
            "organization": {"required": False},
        }

    def get_secret(self, obj):
        return getattr(obj, "_plaintext_secret", None)

    def get_webhook_url(self, obj):
        secret = getattr(obj, "_plaintext_secret", None)
        if not secret:
            return None
        return build_receiver_webhook_url(obj, secret, self.context.get("request"))

    def _resolve_organization(self, attrs):
        organization = attrs.get("organization")
        if organization is None and self.instance is not None:
            organization = self.instance.organization
        if organization is None:
            request = self.context.get("request")
            if request is not None and request.user.is_authenticated:
                organization = request.user.get_current_organization()
        return organization

    def validate(self, attrs):
        organization = self._resolve_organization(attrs)
        check = attrs.get("check", getattr(self.instance, "check", None))
        if (
            check is not None
            and organization is not None
            and check.organization_id != organization.id
        ):
            raise serializers.ValidationError(
                {"check": "Check must belong to the same organization as the receiver."}
            )

        provider = attrs.get(
            "admission_llm_provider",
            getattr(self.instance, "admission_llm_provider", None),
        )
        if (
            provider is not None
            and organization is not None
            and provider.organization_id != organization.id
        ):
            raise serializers.ValidationError(
                {
                    "admission_llm_provider": (
                        "LLM provider must belong to the same organization as the receiver."
                    )
                }
            )

        admission_mode = attrs.get(
            "admission_mode",
            getattr(self.instance, "admission_mode", None),
        )
        if admission_mode == "ai_gated":
            if not provider or not getattr(provider, "is_jev", False):
                has_jev_default = LLMProvider.objects.filter(
                    organization=organization,
                    enabled=True,
                    is_jev=True,
                    is_jev_default=True,
                ).exists()
                if not has_jev_default:
                    raise serializers.ValidationError(
                        {
                            "admission_llm_provider": (
                                "AI Gated mode requires a JEV provider or a JEV default "
                                "must be set for the organization."
                            )
                        }
                    )

        return attrs

    def create(self, validated_data):
        instance = super().create(validated_data)
        instance._plaintext_secret = instance.generate_secret()
        return instance


class ReceiverEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = ReceiverEvent
        fields = [
            "id",
            "receiver",
            "check",
            "external_fingerprint",
            "source_type",
            "status",
            "disposition",
            "related_event",
            "check_execution",
            "received_at",
            "decided_at",
            "created_at",
        ]
        read_only_fields = fields


class ReceiverAdmissionDecisionSerializer(serializers.ModelSerializer):
    class Meta:
        model = ReceiverAdmissionDecision
        fields = [
            "id",
            "receiver_event",
            "decision",
            "reason",
            "model",
            "confidence",
            "candidate_investigations",
            "timed_out",
            "reasoning",
            "created_at",
        ]
        read_only_fields = fields
