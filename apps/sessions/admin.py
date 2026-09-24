from django.contrib import admin

from apps.sessions.models import (
    Message,
    Thread,
    ToolCall,
    TSession,
    ArchitectureRequest,
    AgentEvent,
    Execution,
)


@admin.register(TSession)
class TSessionAdmin(admin.ModelAdmin):
    list_display = ["title", "organization", "status", "created_by", "created_at"]
    list_filter = ["status", "organization"]
    search_fields = ["title"]


@admin.register(Thread)
class ThreadAdmin(admin.ModelAdmin):
    list_display = ["tsession", "user", "status", "visibility", "created_at"]
    list_filter = ["status", "visibility"]


@admin.register(ToolCall)
class ToolCallAdmin(admin.ModelAdmin):
    list_display = ["thread", "capability", "status", "requested_by", "created_at"]
    list_filter = ["status"]


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ["thread", "role", "tool_call", "created_by", "created_at"]
    list_filter = ["role", "created_at"]
    search_fields = ["content"]


admin.register(ArchitectureRequest)
admin.register(Execution)
admin.register(AgentEvent)
