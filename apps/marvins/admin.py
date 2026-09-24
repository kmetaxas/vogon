# pyright: reportAttributeAccessIssue=false

from django.contrib import admin

from apps.marvins.models import (
    Capability,
    Marvin,
    MarvinConfig,
    MarvinRegistrationKey,
    MarvinResourceAttachment,
    Resource,
    ResourceType,
)


@admin.register(ResourceType)
class ResourceTypeAdmin(admin.ModelAdmin):
    list_display = ["name", "display_name", "created_at"]
    search_fields = ["name", "display_name"]


@admin.register(Resource)
class ResourceAdmin(admin.ModelAdmin):
    list_display = ["name", "resource_type", "organization", "enabled", "created_at"]
    list_filter = ["enabled", "resource_type", "organization"]
    search_fields = ["name", "description"]


class MarvinResourceAttachmentInline(admin.TabularInline):
    model = MarvinResourceAttachment
    extra = 1
    autocomplete_fields = ["resource"]


class MarvinConfigInline(admin.StackedInline):
    model = MarvinConfig
    extra = 0


@admin.register(Capability)
class CapabilityAdmin(admin.ModelAdmin):
    list_display = ["name", "organization", "enabled", "created_at"]
    list_filter = ["enabled", "organization"]
    search_fields = ["name", "description"]


@admin.register(Marvin)
class MarvinAdmin(admin.ModelAdmin):
    list_display = ["name", "client_id", "organization", "status", "last_seen", "created_at"]
    list_filter = ["status", "organization"]
    search_fields = ["name", "client_id"]
    filter_horizontal = ["capabilities"]
    inlines = [MarvinConfigInline, MarvinResourceAttachmentInline]


@admin.register(MarvinRegistrationKey)
class MarvinRegistrationKeyAdmin(admin.ModelAdmin):
    list_display = ["name", "key_preview", "organization", "active", "created_at"]
    list_filter = ["active", "organization"]
    search_fields = ["name", "key"]
    readonly_fields = ["key"]

    @admin.display(description="Key")
    def key_preview(self, obj: MarvinRegistrationKey) -> str:  # type: ignore[name-defined]
        key_prefix = obj.key[:16]  # type: ignore[index]
        return f"{key_prefix}..."

    def get_readonly_fields(self, request, obj=None):
        if obj:
            return ["key"]
        return []
