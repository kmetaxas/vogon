from django.contrib import admin

from apps.checks.models import (
    Check,
    CheckActionLog,
    CheckExecution,
    CheckVersion,
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
