from django.contrib import admin

from apps.checks.models import (
    Check,
    CheckActionLog,
    CheckCapabilityExecution,
    CheckExecution,
    CheckHealthState,
    CheckVersion,
)


@admin.register(Check)
class CheckAdmin(admin.ModelAdmin):
    list_display = [
        "name",
        "organization",
        "schedule_type",
        "execution_mode",
        "enabled",
        "created_at",
    ]
    list_filter = ["enabled", "execution_mode", "schedule_type", "organization"]
    search_fields = ["name", "description"]


@admin.register(CheckExecution)
class CheckExecutionAdmin(admin.ModelAdmin):
    list_display = ["check", "execution_status", "health_state", "triggered_at"]
    list_filter = ["execution_status", "health_state"]


class CheckCapabilityExecutionInline(admin.TabularInline):
    model = CheckCapabilityExecution
    extra = 0
    fields = [
        "capability_name",
        "marvin",
        "status",
        "input_tokens",
        "output_tokens",
        "cost",
        "started_at",
        "completed_at",
    ]
    readonly_fields = [
        "capability_name",
        "marvin",
        "status",
        "input_tokens",
        "output_tokens",
        "cost",
        "started_at",
        "completed_at",
    ]


@admin.register(CheckCapabilityExecution)
class CheckCapabilityExecutionAdmin(admin.ModelAdmin):
    list_display = ["capability_name", "check_execution", "marvin", "status", "created_at"]
    list_filter = ["status"]
    search_fields = ["capability_name"]


@admin.register(CheckHealthState)
class CheckHealthStateAdmin(admin.ModelAdmin):
    list_display = ["check", "current_state", "consecutive_failures", "updated_at"]


@admin.register(CheckVersion)
class CheckVersionAdmin(admin.ModelAdmin):
    list_display = ["check", "version_number", "created_at"]


@admin.register(CheckActionLog)
class CheckActionLogAdmin(admin.ModelAdmin):
    list_display = ["check", "action_type", "delivery_status", "created_at"]
