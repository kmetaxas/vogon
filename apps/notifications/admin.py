from django.contrib import admin

from apps.notifications.models import (
    Notification,
    NotificationChannel,
    NotificationDelivery,
    NotificationPolicy,
    NotificationProvider,
    NotificationRoute,
    NotificationSuppression,
)


@admin.register(NotificationProvider)
class NotificationProviderAdmin(admin.ModelAdmin):
    list_display = ["provider_type", "display_name", "updated_at"]
    list_filter = ["provider_type"]
    search_fields = ["display_name", "provider_type"]


@admin.register(NotificationChannel)
class NotificationChannelAdmin(admin.ModelAdmin):
    list_display = ["name", "provider_type", "enabled", "organization"]
    list_filter = ["provider_type", "enabled"]
    exclude = ["_credentials_encrypted"]
    readonly_fields = ["credentials"]

    @admin.display(description="Credentials")
    def credentials(self, obj: NotificationChannel) -> str:
        """Masked indicator; never render decrypted credentials in admin."""
        return "••••••••" if obj._credentials_encrypted else "(not set)"


@admin.register(NotificationPolicy)
class NotificationPolicyAdmin(admin.ModelAdmin):
    list_display = ["name", "is_default", "organization"]
    list_filter = ["is_default"]


@admin.register(NotificationRoute)
class NotificationRouteAdmin(admin.ModelAdmin):
    list_display = ["policy", "severity", "attention", "channel", "enabled"]
    list_filter = ["severity", "attention", "enabled"]


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = [
        "title",
        "severity",
        "attention",
        "status",
        "source_type",
        "created_at",
    ]
    list_filter = ["severity", "attention", "status", "source_type"]
    search_fields = ["title", "summary", "source_id"]


@admin.register(NotificationDelivery)
class NotificationDeliveryAdmin(admin.ModelAdmin):
    list_display = [
        "notification",
        "channel",
        "status",
        "attempt_count",
        "last_attempt_at",
    ]
    list_filter = ["status"]


@admin.register(NotificationSuppression)
class NotificationSuppressionAdmin(admin.ModelAdmin):
    list_display = ["dedup_key", "created_at"]
