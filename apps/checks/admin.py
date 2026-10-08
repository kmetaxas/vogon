from django.contrib import admin

from apps.checks.models import (
    Check,
    CheckActionLog,
    CheckExecution,
    CheckReceiver,
    CheckVersion,
    ReceiverAdmissionDecision,
    ReceiverEvent,
)


@admin.register(Check)
class CheckAdmin(admin.ModelAdmin):
    list_display = [
        "name",
        "organization",
        "schedule_type",
        "enabled",
        "created_at",
    ]
    list_filter = ["enabled", "schedule_type", "organization"]
    search_fields = ["name", "description"]


@admin.register(CheckExecution)
class CheckExecutionAdmin(admin.ModelAdmin):
    list_display = ["check", "execution_status", "health_state", "triggered_at"]
    list_filter = ["execution_status", "health_state"]


@admin.register(CheckVersion)
class CheckVersionAdmin(admin.ModelAdmin):
    list_display = ["check", "version_number", "created_at"]


@admin.register(CheckActionLog)
class CheckActionLogAdmin(admin.ModelAdmin):
    list_display = ["check", "action_type", "delivery_status", "created_at"]


@admin.register(CheckReceiver)
class CheckReceiverAdmin(admin.ModelAdmin):
    list_display = [
        "name",
        "organization",
        "check",
        "source_type",
        "enabled",
        "admission_mode",
        "created_at",
    ]
    list_filter = ["source_type", "enabled", "admission_mode", "organization"]
    search_fields = ["name", "check__name"]
    readonly_fields = ["id", "created_at", "updated_at", "secret_hash"]
    fieldsets = [
        (
            "Basic",
            {
                "fields": [
                    "name",
                    "organization",
                    "check",
                    "source_type",
                    "enabled",
                ]
            },
        ),
        (
            "Admission",
            {
                "fields": [
                    "admission_mode",
                    "admission_llm_provider",
                    "gating_prompt",
                    "fail_open_on_timeout",
                ]
            },
        ),
        (
            "Limits",
            {
                "fields": [
                    "max_active_executions",
                    "dedup_window_seconds",
                ]
            },
        ),
        (
            "Audit",
            {
                "fields": [
                    "id",
                    "secret_hash",
                    "created_at",
                    "updated_at",
                ]
            },
        ),
    ]
    actions = ["regenerate_secret"]

    @admin.action(description="Regenerate webhook secret for selected receivers")
    def regenerate_secret(self, request, queryset):
        count = 0
        for receiver in queryset:
            receiver.generate_secret()
            count += 1
        self.message_user(request, f"Regenerated secret for {count} receiver(s).")


@admin.register(ReceiverEvent)
class ReceiverEventAdmin(admin.ModelAdmin):
    list_display = [
        "receiver",
        "external_fingerprint",
        "source_type",
        "status",
        "disposition",
        "created_at",
    ]
    list_filter = ["source_type", "status", "disposition", "created_at"]
    search_fields = ["external_fingerprint", "receiver__name"]
    readonly_fields = [
        "id",
        "receiver",
        "check",
        "external_fingerprint",
        "source_type",
        "status",
        "raw_payload",
        "normalized_payload",
        "correlation_identity",
        "disposition",
        "related_event",
        "check_execution",
        "received_at",
        "decided_at",
        "created_at",
        "updated_at",
    ]
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(ReceiverAdmissionDecision)
class ReceiverAdmissionDecisionAdmin(admin.ModelAdmin):
    list_display = [
        "receiver_event",
        "decision",
        "model",
        "confidence",
        "timed_out",
        "created_at",
    ]
    list_filter = ["decision", "timed_out", "created_at"]
    readonly_fields = [
        "id",
        "receiver_event",
        "decision",
        "reason",
        "model",
        "confidence",
        "candidate_investigations",
        "timed_out",
        "created_at",
    ]
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
