from django.contrib import admin

from apps.infradesigns.models import InfrastructureDesign


@admin.register(InfrastructureDesign)
class InfrastructureDesignAdmin(admin.ModelAdmin):
    list_display = ["name", "organization", "environment", "created_at", "needs_re_embedding"]
    list_filter = ["environment", "needs_re_embedding"]
    search_fields = ["name", "description"]
